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
    with pytest.raises(ValueError, match="CC_REMOTE_CODEX_DAEMON"):
        validate_wrapper_config(_wrapper_cfg(codex_daemon_mode="always"))
    validate_wrapper_config(_wrapper_cfg(codex_daemon_mode="auto"))
    validate_wrapper_config(_wrapper_cfg(codex_daemon_mode="off"))
    # The hidden broker experiment must not make a stale legacy variable break
    # the supported native-CLI mirror path. Its socket is validated only after
    # an explicit opt-in.
    validate_wrapper_config(_wrapper_cfg(claude_broker_socket="relative.sock"))
    with pytest.raises(ValueError, match="CC_REMOTE_CLAUDE_BROKER_SOCKET"):
        validate_wrapper_config(_wrapper_cfg(
            claude_broker_socket="relative.sock",
            experimental_claude_broker=True,
        ))


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


@pytest.mark.parametrize(("url", "environment", "expected_proxy"), [
    ("ws://127.0.0.1:8765/ws", {"HTTPS_PROXY": "http://proxy.example:7890"}, None),
    ("ws://[::1]:8765/ws", {"HTTPS_PROXY": "http://proxy.example:7890"}, None),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890"},
     "http://proxy.example:7890"),
    ("wss://relay.example/ws", {"http_proxy": "http://proxy.example:7890"},
     "http://proxy.example:7890"),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                               "NO_PROXY": "relay.example"}, None),
    ("wss://relay.internal.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                                        "no_proxy": ".internal.example"}, None),
    ("ws://10.0.0.2:8765/ws", {"HTTP_PROXY": "http://proxy.example:7890",
                              "NO_PROXY": "10.0.0.2:8765"}, None),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                               "NO_PROXY": "*"}, None),
    ("wss://other.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                               "NO_PROXY": "relay.example"},
     "http://proxy.example:7890"),
    ("wss://relay.example/ws", {"ALL_PROXY": "socks5://proxy.example:9999"}, None),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://old.example:7890",
                               "https_proxy": "http://new.example:8080"},
     "http://new.example:8080"),
    ("wss://relay.example/ws", {"https_proxy": "http://new.example:8080",
                               "HTTPS_PROXY": "http://old.example:7890"},
     "http://new.example:8080"),
    ("wss://relay.example/ws", {"HTTP_PROXY": "http://old.example:7890",
                               "http_proxy": "http://new.example:8080"},
     "http://new.example:8080"),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://old.example:7890",
                               "https_proxy": ""}, None),
    ("wss://relay.example/ws", {"HTTP_PROXY": "http://old.example:7890",
                               "http_proxy": ""}, None),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://old.example:7890",
                               "https_proxy": "",
                               "HTTP_PROXY": "http://fallback.example:8080"},
     "http://fallback.example:8080"),
    ("wss://relay.example/ws", {"https_proxy": "http://secure.example:8080",
                               "http_proxy": "http://fallback.example:8080"},
     "http://secure.example:8080"),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                               "NO_PROXY": "*", "no_proxy": ""},
     "http://proxy.example:7890"),
    ("wss://relay.example/ws", {"HTTPS_PROXY": "http://proxy.example:7890",
                               "NO_PROXY": "other.example", "no_proxy": "relay.example"},
     None),
    ("wss://relay.example/ws", {"HTTP_PROXY": "http://inherited.example:7890",
                               "REQUEST_METHOD": "GET"}, None),
    ("wss://relay.example/ws", {"HTTP_PROXY": "http://inherited.example:7890",
                               "http_proxy": "http://explicit.example:8080",
                               "REQUEST_METHOD": "GET"},
     "http://explicit.example:8080"),
])
def test_connect_honors_explicit_http_proxy_and_bypass(
    monkeypatch, url, environment, expected_proxy,
):
    import os
    import urllib.request

    import cc_remote.wrapper.transport as transport_module

    for key in list(os.environ):
        if key.lower().endswith("_proxy"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def system_proxy_lookup():
        pytest.fail("transport must not consult system proxy settings")

    monkeypatch.setattr(urllib.request, "getproxies", system_proxy_lookup)

    async def run():
        transport = WrapperTransport(url, "secret")
        called = {}

        def fake_connect(url, **kwargs):
            called.update({"url": url, "kwargs": kwargs})
            transport._stop = True
            raise RuntimeError("stop after capture")

        monkeypatch.setattr(transport_module, "connect", fake_connect)
        await transport._run()
        assert called["kwargs"]["max_queue"] == 4
        assert called["kwargs"]["proxy"] == expected_proxy

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
