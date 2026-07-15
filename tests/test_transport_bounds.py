"""Zero-network checks for wrapper transport resource bounds."""
from __future__ import annotations

import asyncio

import pytest

from cc_remote.config import WrapperConfig, validate_wrapper_config
from cc_remote.protocol import Ping
from cc_remote.wrapper.transport import WrapperTransport


def _wrapper_cfg(**overrides):
    values = {
        "relay_url": "ws://127.0.0.1:8765/ws",
        "wrapper_token": "w" * 48,
    }
    values.update(overrides)
    return WrapperConfig(**values)


def test_wrapper_startup_config_fails_closed():
    validate_wrapper_config(_wrapper_cfg())
    validate_wrapper_config(_wrapper_cfg(relay_url="wss://relay.example/ws"))
    with pytest.raises(ValueError, match="WRAPPER_TOKEN"):
        validate_wrapper_config(WrapperConfig(wrapper_token="change-me-wrapper"))
    with pytest.raises(ValueError, match="must use wss"):
        validate_wrapper_config(_wrapper_cfg(relay_url="ws://relay.example/ws"))
    # ALLOW_INSECURE_HTTP is an explicit opt-in escape hatch for a bare public
    # IP without TLS in front; it must not affect the default (off) path above.
    validate_wrapper_config(
        _wrapper_cfg(relay_url="ws://relay.example/ws", allow_insecure_http=True)
    )
    with pytest.raises(ValueError, match="path must be /ws"):
        validate_wrapper_config(_wrapper_cfg(relay_url="wss://relay.example/other"))
    with pytest.raises(ValueError, match="WRAPPER_INBOX_BYTES"):
        validate_wrapper_config(_wrapper_cfg(transport_inbox_bytes=1024))
    with pytest.raises(ValueError, match="MAX_CONCURRENT_SESSIONS"):
        validate_wrapper_config(_wrapper_cfg(max_concurrent_sessions=0))
    with pytest.raises(ValueError, match="RING_MAX_EVENTS"):
        validate_wrapper_config(_wrapper_cfg(ring_max_events=1))
    with pytest.raises(ValueError, match="RING_MAX_BYTES"):
        validate_wrapper_config(_wrapper_cfg(ring_max_bytes=1024))
    with pytest.raises(ValueError, match="TOOL_RESULT_MAX"):
        validate_wrapper_config(_wrapper_cfg(tool_result_max=16 * 1024 * 1024))
    with pytest.raises(ValueError, match="CC_CWD"):
        validate_wrapper_config(_wrapper_cfg(cc_cwd="x" * 5000))
    with pytest.raises(ValueError, match="CC_RESUME_SESSION_ID"):
        validate_wrapper_config(_wrapper_cfg(resume_session_id="../bad id"))
    with pytest.raises(ValueError, match="CODEX_TURN_IDLE_WARN_SECONDS"):
        validate_wrapper_config(_wrapper_cfg(codex_turn_idle_warn_seconds=1))
    validate_wrapper_config(_wrapper_cfg(codex_turn_idle_warn_seconds=0))


def test_wrapper_transport_queues_and_frame_size_are_bounded():
    transport = WrapperTransport(
        "ws://127.0.0.1:8765/ws",
        "secret",
        inbox_cap=7,
        send_cap=11,
        max_size=12345,
        inbox_bytes=23456,
        send_bytes=34567,
    )
    assert transport._inbox.maxsize == 7
    assert transport._send_q.maxsize == 11
    assert transport._inbox.max_bytes == 23456
    assert transport._send_q.max_bytes == 34567
    assert transport.max_size == 12345


def test_transport_byte_backpressure_and_stop_wake_waiters():
    async def run():
        transport = WrapperTransport(
            "ws://127.0.0.1:8765/ws", "secret", inbox_cap=2,
            max_size=1024, inbox_bytes=2048,
        )
        first = object()
        await transport._inbox.put(first, 1500)
        blocked = asyncio.create_task(transport._inbox.put(object(), 1000))
        await asyncio.sleep(0)
        assert not blocked.done()
        assert await transport._inbox.get() is first
        await asyncio.wait_for(blocked, timeout=0.1)

        await asyncio.wait_for(transport.stop(), timeout=0.1)
        assert await transport._inbox.get() is None

    asyncio.run(run())


def test_outbound_queue_stores_serialized_bytes_with_generation():
    async def run():
        transport = WrapperTransport(
            "ws://127.0.0.1:8765/ws", "secret", max_size=1024,
            send_bytes=2048,
        )
        transport._connected = True
        transport._generation = 7
        await transport.send(Ping(n=1))
        generation, raw = await transport._send_q.get()
        assert generation == 7
        assert '"type":"ping"' in raw
        await transport.stop()

    asyncio.run(run())


def test_connect_relies_on_websockets_proxy_bypass(monkeypatch):
    import cc_remote.wrapper.transport as transport_module

    async def run():
        transport = WrapperTransport("ws://127.0.0.1:8765/ws", "secret")
        called = {}

        def fake_connect(url, **kwargs):
            called.update({"url": url, "kwargs": kwargs})
            transport._stop = True
            raise RuntimeError("stop after capture")

        monkeypatch.setattr(transport_module, "connect", fake_connect)
        await transport._run()
        assert called["kwargs"]["max_queue"] == 4
        assert called["kwargs"]["proxy"] is None

    asyncio.run(run())


def test_transport_stop_reaps_socket_sender_and_receiver(monkeypatch):
    import cc_remote.wrapper.transport as transport_module

    async def run():
        receiver_started = asyncio.Event()
        receiver_stopped = asyncio.Event()
        context_exited = asyncio.Event()

        class FakeSocket:
            def __aiter__(self):
                return self

            async def __anext__(self):
                receiver_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    receiver_stopped.set()
                raise StopAsyncIteration

            async def send(self, _raw):
                return None

        class FakeConnection:
            async def __aenter__(self):
                return FakeSocket()

            async def __aexit__(self, *_args):
                context_exited.set()

        monkeypatch.setattr(
            transport_module, "connect", lambda *_args, **_kwargs: FakeConnection())
        transport = WrapperTransport("ws://127.0.0.1:8765/ws", "secret")
        await transport.start()
        await asyncio.wait_for(receiver_started.wait(), timeout=0.2)
        await asyncio.wait_for(transport.stop(), timeout=0.2)

        assert receiver_stopped.is_set()
        assert context_exited.is_set()
        assert transport._connected is False

    asyncio.run(run())
