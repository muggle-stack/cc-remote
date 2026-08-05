"""Codex shared app-server daemon/proxy regressions (no model calls)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import signal
import sys

import pytest

from cc_remote.wrapper import codex_daemon as daemon_module
from cc_remote.wrapper import codex_handle as handle_module
from cc_remote.wrapper.codex_daemon import (
    CodexDaemonManager,
    CodexDaemonUpgradeRequired,
)
from cc_remote.wrapper.codex_handle import (
    CodexHandle,
    CodexProxyProtocolError,
    _websocket_client_frame,
)
from cc_remote.wrapper.process_scan import ProcessIdentity


class _Cfg:
    cc_cwd = "/tmp"
    tool_result_max = 8000


class _Reader:
    def __init__(self, data: bytes = b"", *, block_at_eof: bool = False):
        self.data = bytearray(data)
        self.block_at_eof = block_at_eof

    async def read(self, size: int) -> bytes:
        if not self.data:
            if self.block_at_eof:
                await asyncio.Event().wait()
            return b""
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk

    async def readline(self) -> bytes:
        if not self.data:
            if self.block_at_eof:
                await asyncio.Event().wait()
            return b""
        try:
            boundary = self.data.index(0x0A) + 1
        except ValueError:
            boundary = len(self.data)
        return await self.read(boundary)


class _Writer:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return None


class _Process:
    def __init__(self, stdout: _Reader | None = None, pid: int = 43210):
        self.pid = pid
        self.returncode = None
        self.stdin = _Writer()
        self.stdout = stdout or _Reader(block_at_eof=True)
        self.stderr = _Reader(block_at_eof=True)

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = 0


def _result(returncode: int, payload: dict | None = None):
    data = b"" if payload is None else daemon_module.json.dumps(payload).encode()
    return daemon_module._CommandResult(returncode, data, b"")


def test_daemon_manager_starts_enables_versions_and_reconnects(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        manager = CodexDaemonManager("auto")
        calls: list[tuple[str, ...]] = []
        version_calls = 0

        async def command(_bin, _env, *args):
            nonlocal version_calls
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                version_calls += 1
                if version_calls == 1:
                    return _result(1)
                return _result(0, {
                    "status": "running", "backend": "pid",
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.144.1",
                    "appServerVersion": "0.144.1",
                })
            if args[-1] == "start":
                return _result(0, {"status": "started"})
            assert args[-1] == "enable-remote-control"
            return _result(0, {
                "status": "enabled", "remoteControlEnabled": True,
                "socketPath": "/tmp/codex.sock",
                "cliVersion": "0.144.1",
                "appServerVersion": "0.144.1",
            })

        manager._run = command  # type: ignore[method-assign]
        argv = await manager.proxy_args("/bin/codex", {})
        assert argv == [
            "/bin/codex", "app-server", "proxy",
            "--sock", "/tmp/codex.sock",
        ]
        assert manager.info is not None
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "start"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
            ("app-server", "daemon", "version"),
        ]
        assert await manager.proxy_args("/bin/codex", {}) == argv
        assert len(calls) == 7

        # Unexpected proxy EOF invalidates only liveness.  Help capability stays
        # cached while reconnect performs version -> enable -> version again.
        manager.invalidate()
        assert await manager.proxy_args("/bin/codex", {}) == argv
        assert calls[-3:] == [
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
            ("app-server", "daemon", "version"),
        ]

    asyncio.run(run())


def test_stale_darwin_updater_is_recovered_before_generic_restart(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        monkeypatch.setattr(
            daemon_module,
            "_terminate_stale_darwin_daemon_updater",
            lambda _bin, _env: True,
        )
        manager = CodexDaemonManager("auto")
        calls: list[tuple[str, ...]] = []
        recovered = False

        async def command(_bin, _env, *args):
            nonlocal recovered
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                if not recovered:
                    return _result(1)
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": "0.145.0",
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.145.0",
                    "appServerVersion": "0.145.0",
                })
            if args[-1] == "start":
                if len([call for call in calls if call[-1] == "start"]) > 1:
                    recovered = True
                return _result(1)
            assert args[-1] == "enable-remote-control"
            return _result(0, {
                "status": "enabled",
                "remoteControlEnabled": True,
                "socketPath": "/tmp/codex.sock",
            })

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) == [
            "/bin/codex", "app-server", "proxy",
            "--sock", "/tmp/codex.sock",
        ]
        assert manager.strict_shared_affinity is True
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "start"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "start"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
            ("app-server", "daemon", "version"),
        ]

    asyncio.run(run())


def test_stale_managed_daemon_uses_official_restart_when_not_exact(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        monkeypatch.setattr(
            daemon_module,
            "_terminate_stale_darwin_daemon_updater",
            lambda _bin, _env: False,
        )
        manager = CodexDaemonManager("auto")
        calls: list[tuple[str, ...]] = []
        restarted = False

        async def command(_bin, _env, *args):
            nonlocal restarted
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                if not restarted:
                    return _result(1)
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": "0.145.0",
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.145.0",
                    "appServerVersion": "0.145.0",
                })
            if args[-1] == "start":
                return _result(1)
            if args[-1] == "restart":
                restarted = True
                return _result(0, {"status": "restarted"})
            assert args[-1] == "enable-remote-control"
            return _result(0, {
                "status": "enabled",
                "remoteControlEnabled": True,
                "socketPath": "/tmp/codex.sock",
            })

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) == [
            "/bin/codex", "app-server", "proxy",
            "--sock", "/tmp/codex.sock",
        ]
        assert calls[2:7] == [
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "start"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "restart"),
            ("app-server", "daemon", "version"),
        ]

    asyncio.run(run())


def test_unrecoverable_stale_daemon_still_falls_back_to_stdio(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        monkeypatch.setattr(
            daemon_module,
            "_terminate_stale_darwin_daemon_updater",
            lambda _bin, _env: False,
        )
        manager = CodexDaemonManager("auto")
        calls: list[tuple[str, ...]] = []

        async def command(_bin, _env, *args):
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            return _result(1)

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) is None
        assert manager.info is None
        assert manager.strict_shared_affinity is False
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "start"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "restart"),
        ]

    asyncio.run(run())


def test_exact_zombie_daemon_updater_gets_sigterm_only(monkeypatch, tmp_path):
    daemon_root = tmp_path / "app-server-daemon"
    daemon_root.mkdir()
    (daemon_root / "app-server.pid").write_text('{"pid": 41002}')
    (daemon_root / "app-server-updater.pid").write_text('{"pid": 41001}')
    updater_identity = ProcessIdentity(41001, 101)
    server_identity = ProcessIdentity(41002, 102)
    updater = (
        updater_identity,
        1,
        0,
        (b"/opt/codex", b"app-server", b"daemon", b"pid-update-loop"),
    )
    server = (server_identity, 41001, 0, (b"codex-app-server",))
    processes = {41001: updater, 41002: server}
    signals: list[tuple[int, int]] = []

    running_on_windows = sys.platform == "win32"
    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        daemon_module, "_darwin_process_info", lambda pid: processes.get(pid))
    monkeypatch.setattr(
        daemon_module, "process_owner_uid",
        lambda _pid: getattr(os, "getuid", lambda: 0)())
    monkeypatch.setattr(
        daemon_module, "_darwin_process_state", lambda _pid: "Z+")
    if running_on_windows:
        # ``_managed_pid`` requires ``os.O_NOFOLLOW`` (POSIX-only) and always
        # returns ``None`` here, so the real PID-file reader is bypassed to
        # exercise the darwin-only signaling logic under test.
        monkeypatch.setattr(
            daemon_module, "_managed_pid",
            lambda path: 41002 if path.name == "app-server.pid" else 41001)

    def terminate(pid, sig):
        signals.append((pid, sig))
        processes.pop(pid)

    monkeypatch.setattr(daemon_module.os, "kill", terminate)
    assert daemon_module._terminate_stale_darwin_daemon_updater(
        "/opt/codex", {"CODEX_HOME": str(tmp_path)},
    ) is True
    assert signals == [(41001, signal.SIGTERM)]


@pytest.mark.parametrize("mismatch", [
    "server_not_zombie",
    "different_binary",
    "different_parent",
    "different_uid",
    "updater_has_tty",
    "pid_reused",
])
def test_ambiguous_darwin_daemon_state_never_signals(
    monkeypatch, tmp_path, mismatch,
):
    daemon_root = tmp_path / "app-server-daemon"
    daemon_root.mkdir()
    (daemon_root / "app-server.pid").write_text('{"pid": 42002}')
    (daemon_root / "app-server-updater.pid").write_text('{"pid": 42001}')
    updater_identity = ProcessIdentity(42001, 201)
    server_identity = ProcessIdentity(42002, 202)
    updater = (
        updater_identity,
        1,
        1 if mismatch == "updater_has_tty" else 0,
        (b"/wrong/codex" if mismatch == "different_binary" else b"/opt/codex",
         b"app-server", b"daemon", b"pid-update-loop"),
    )
    server = (
        server_identity,
        999 if mismatch == "different_parent" else 42001,
        0,
        (b"codex-app-server",),
    )
    calls = {42001: 0, 42002: 0}

    def process_info(pid):
        calls[pid] += 1
        if mismatch == "pid_reused" and pid == 42001 and calls[pid] > 1:
            return (ProcessIdentity(pid, 999), *updater[1:])
        return updater if pid == 42001 else server

    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")
    monkeypatch.setattr(daemon_module, "_darwin_process_info", process_info)
    monkeypatch.setattr(
        daemon_module,
        "process_owner_uid",
        lambda pid: os.getuid() + (1 if mismatch == "different_uid" else 0),
    )
    monkeypatch.setattr(
        daemon_module,
        "_darwin_process_state",
        lambda _pid: "S" if mismatch == "server_not_zombie" else "Z",
    )
    monkeypatch.setattr(
        daemon_module.os,
        "kill",
        lambda _pid, _sig: pytest.fail("ambiguous process was signalled"),
    )
    assert daemon_module._terminate_stale_darwin_daemon_updater(
        "/opt/codex", {"CODEX_HOME": str(tmp_path)},
    ) is False


def test_lagging_managed_daemon_restarts_before_shared_proxy(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v2",))
        manager = CodexDaemonManager("auto")
        calls: list[tuple[str, ...]] = []
        restarted = False

        async def command(_bin, _env, *args):
            nonlocal restarted
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                version = "0.145.0" if restarted else "0.144.6"
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": version,
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.145.0-alpha.18",
                    "appServerVersion": version,
                })
            if args[-1] == "restart":
                restarted = True
                return _result(0, {"status": "restarted"})
            assert args[-1] == "enable-remote-control"
            return _result(0, {
                "status": "enabled",
                "remoteControlEnabled": True,
                "socketPath": "/tmp/codex.sock",
            })

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) == [
            "/bin/codex", "app-server", "proxy",
            "--sock", "/tmp/codex.sock",
        ]
        assert manager.strict_shared_affinity is True
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "restart"),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
        ]

    asyncio.run(run())


def test_lagging_managed_daemon_restart_failure_never_uses_stdio(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v2",))
        manager = CodexDaemonManager("auto")

        async def command(_bin, _env, *args):
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": "0.144.6",
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.145.0-alpha.18",
                    "appServerVersion": "0.144.6",
                })
            if args[-1] == "enable-remote-control":
                return _result(0, {
                    "status": "enabled",
                    "remoteControlEnabled": True,
                    "socketPath": "/tmp/codex.sock",
                })
            assert args[-1] == "restart"
            return _result(1)

        manager._run = command  # type: ignore[method-assign]
        with pytest.raises(CodexDaemonUpgradeRequired, match="could not"):
            await manager.proxy_args("/bin/codex", {})
        assert manager.info is None
        assert manager.strict_shared_affinity is False

    asyncio.run(run())


def test_daemon_enable_failure_is_not_reported_ready(monkeypatch):
    async def run():
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        manager = CodexDaemonManager("auto")

        async def command(_bin, _env, *args):
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                return _result(0, {
                    "status": "running", "socketPath": "/tmp/codex.sock",
                })
            assert args[-1] == "enable-remote-control"
            return _result(1)

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) is None
        assert manager.info is None

    asyncio.run(run())


def test_existing_official_app_server_is_exposed_for_proxy_validation(monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        manager = CodexDaemonManager("auto")

        async def command(_bin, _env, *args):
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": "0.144.4",
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.144.4",
                    "appServerVersion": "0.144.1",
                })
            assert args[-1] == "enable-remote-control"
            return _result(1)

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args("/bin/codex", {}) == [
            "/bin/codex", "app-server", "proxy", "--sock", "/tmp/codex.sock",
        ]
        assert manager.info is not None
        assert manager.info.socket_path == "/tmp/codex.sock"

    asyncio.run(run())


def test_daemon_capability_cache_invalidates_on_binary_change(
        tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr(daemon_module.os, "name", "posix")
        binary = tmp_path / "codex"
        binary.write_text("old")
        manager = CodexDaemonManager("auto")
        calls = 0

        async def command(_bin, _env, *_args):
            nonlocal calls
            calls += 1
            return _result(0)

        manager._run = command  # type: ignore[method-assign]
        assert await manager.capability(str(binary), {}) is True
        assert await manager.capability(str(binary), {}) is True
        assert calls == 2
        binary.write_text("new executable identity")
        assert await manager.capability(str(binary), {}) is True
        assert calls == 4

    asyncio.run(run())


def _handshake_response(nonce: bytes, *, status: int = 101,
                        accept: str | None = None) -> bytes:
    key = base64.b64encode(nonce)
    expected = base64.b64encode(hashlib.sha1(
        key + handle_module._WEBSOCKET_GUID).digest()).decode()
    selected = expected if accept is None else accept
    return (
        f"HTTP/1.1 {status} Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: keep-alive, Upgrade\r\n"
        f"Sec-WebSocket-Accept: {selected}\r\n\r\n"
    ).encode()


def test_proxy_handshake_validates_101_accept_and_preserves_frame(monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda size: nonce)
        trailing = b"\x81\x02{}"
        process = _Process(_Reader(_handshake_response(nonce) + trailing))
        handle = CodexHandle(_Cfg(), daemon_mode="off")
        await handle._proxy_handshake(process)
        assert process.stdin.writes[0].startswith(b"GET / HTTP/1.1\r\n")
        assert b"Sec-WebSocket-Version: 13\r\n" in process.stdin.writes[0]
        assert bytes(handle._proxy_read_buffer) == trailing

    asyncio.run(run())


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
    _handshake_response(b"0123456789abcdef", accept="wrong"),
])
def test_proxy_handshake_rejects_http_body_and_bad_accept(monkeypatch, response):
    async def run():
        monkeypatch.setattr(
            handle_module.os, "urandom", lambda _size: b"0123456789abcdef")
        process = _Process(_Reader(response))
        handle = CodexHandle(_Cfg(), daemon_mode="off")
        with pytest.raises(CodexProxyProtocolError):
            await handle._proxy_handshake(process)
        # No response body is ever promoted into the WebSocket/JSON buffer.
        assert handle._proxy_read_buffer == bytearray()

    asyncio.run(run())


@pytest.mark.parametrize("size, marker, extended", [
    (5, 5, b""),
    (126, 126, (126).to_bytes(2, "big")),
    (65536, 127, (65536).to_bytes(8, "big")),
])
def test_proxy_client_frames_are_masked_with_canonical_lengths(
        monkeypatch, size, marker, extended):
    mask = b"\x01\x02\x03\x04"
    monkeypatch.setattr(handle_module.os, "urandom", lambda _size: mask)
    payload = bytes(index & 0xFF for index in range(size))
    frame = _websocket_client_frame(payload)
    assert frame[0] == 0x81
    assert frame[1] & 0x80
    assert frame[1] & 0x7F == marker
    offset = 2 + len(extended)
    assert frame[2:offset] == extended
    assert frame[offset:offset + 4] == mask
    encoded = frame[offset + 4:]
    assert bytes(value ^ mask[index & 3]
                 for index, value in enumerate(encoded)) == payload


def _server_frame(payload: bytes, opcode: int = 0x1, *, fin: bool = True,
                  masked: bool = False) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length <= 125:
        header = bytes((first, (0x80 if masked else 0) | length))
    elif length <= 0xFFFF:
        header = bytes((first, (0x80 if masked else 0) | 126))
        header += length.to_bytes(2, "big")
    else:
        header = bytes((first, (0x80 if masked else 0) | 127))
        header += length.to_bytes(8, "big")
    if not masked:
        return header + payload
    mask = b"mask"
    return header + mask + bytes(
        value ^ mask[index & 3] for index, value in enumerate(payload))


def _decode_client_frame(frame: bytes) -> tuple[int, bytes]:
    opcode = frame[0] & 0x0F
    assert frame[1] & 0x80
    marker = frame[1] & 0x7F
    offset = 2
    if marker == 126:
        length = int.from_bytes(frame[offset:offset + 2], "big")
        offset += 2
    elif marker == 127:
        length = int.from_bytes(frame[offset:offset + 8], "big")
        offset += 8
    else:
        length = marker
    mask = frame[offset:offset + 4]
    encoded = frame[offset + 4:offset + 4 + length]
    return opcode, bytes(value ^ mask[index & 3]
                         for index, value in enumerate(encoded))


def test_proxy_reassembles_fragments_and_handles_ping_and_close(monkeypatch):
    async def run():
        monkeypatch.setattr(
            handle_module.os, "urandom", lambda _size: b"mask")
        wire = b"".join([
            _server_frame(b'{"id":', fin=False),
            _server_frame(b"ping", opcode=0x9),
            _server_frame(b"1}", opcode=0x0),
            _server_frame((1000).to_bytes(2, "big"), opcode=0x8),
        ])
        process = _Process(_Reader(wire))
        handle = CodexHandle(_Cfg(), daemon_mode="off")
        handle.proc = process
        handle._using_daemon_proxy = True

        assert await handle._proxy_read_message(process) == b'{"id":1}'
        assert _decode_client_frame(process.stdin.writes[0]) == (0xA, b"ping")
        assert await handle._proxy_read_message(process) is None
        assert _decode_client_frame(process.stdin.writes[1]) == (
            0x8, (1000).to_bytes(2, "big"))

    asyncio.run(run())


def test_proxy_rejects_masked_server_and_oversized_frames():
    async def run():
        handle = CodexHandle(_Cfg(), daemon_mode="off")
        masked = _Process(_Reader(_server_frame(b"{}", masked=True)))
        with pytest.raises(CodexProxyProtocolError, match="masked"):
            await handle._proxy_read_frame(masked)

        too_large = bytes((0x81, 127)) + (
            handle_module._PROXY_MESSAGE_MAX + 1).to_bytes(8, "big")
        oversized = _Process(_Reader(too_large))
        with pytest.raises(CodexProxyProtocolError, match="exceeds"):
            await handle._proxy_read_frame(oversized)

    asyncio.run(run())


class _Manager:
    mode = "auto"

    def __init__(self, argv=None, *, strict_shared=False):
        self.argv = argv
        self.strict_shared_affinity = strict_shared
        self.proxy_calls = 0
        self.invalidations = 0

    async def proxy_args(self, _bin, _env):
        self.proxy_calls += 1
        return self.argv

    def invalidate(self):
        self.invalidations += 1


def test_proxy_protocol_error_invalidates_daemon_and_clears_live_state():
    async def run():
        manager = _Manager()
        process = _Process(_Reader(_server_frame(b"not-json")))
        handle = CodexHandle(
            _Cfg(), daemon_mode="auto", daemon_manager=manager)
        handle.proc = process
        handle._using_daemon_proxy = True
        handle._dead = False

        await handle._read_loop(process, handle._generation)

        assert manager.invalidations == 1
        assert handle.using_daemon_proxy is False
        assert handle._dead is True

    asyncio.run(run())


def test_code_daemon_unavailable_falls_back_and_work_never_probes(monkeypatch):
    async def run(work_mode: bool):
        manager = _Manager(None)
        captured = []

        async def spawn(*argv, **_kwargs):
            captured.append(list(argv))
            raise RuntimeError("captured")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(RuntimeError, match="captured"):
            await CodexHandle(
                _Cfg(), work_mode=work_mode, daemon_manager=manager,
            ).connect()
        assert captured[0][:3] == [
            "/usr/bin/codex", "app-server", "--stdio"]
        assert manager.proxy_calls == (0 if work_mode else 1)

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_oversized_resume_newer_core_bypasses_shared_daemon(monkeypatch):
    async def run():
        manager = _Manager(["/managed/codex", "app-server", "proxy"])
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            raise RuntimeError("captured private core")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/managed/codex")
        monkeypatch.setattr(
            handle_module, "_newer_private_core_for_oversized_resume",
            lambda _bin, _sid: "/Applications/Codex.app/Resources/codex",
        )
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)

        with pytest.raises(RuntimeError, match="captured private core"):
            await CodexHandle(
                _Cfg(), daemon_mode="auto", daemon_manager=manager,
            ).connect(resume_id="oversized-thread", cwd="/tmp")

        assert spawned[0][:3] == [
            "/Applications/Codex.app/Resources/codex",
            "app-server", "--stdio",
        ]
        assert manager.proxy_calls == 0

    asyncio.run(run())


def test_oversized_desktop_openai_resume_uses_private_http_provider(
        monkeypatch):
    async def run():
        manager = _Manager(["/managed/codex", "app-server", "proxy"])
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            raise RuntimeError("captured HTTP fallback")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/managed/codex")
        monkeypatch.setattr(
            handle_module, "_newer_private_core_for_oversized_resume",
            lambda _bin, _sid: None,
        )
        monkeypatch.setattr(
            handle_module, "_oversized_desktop_openai_resume_requires_http",
            lambda _sid: True,
        )
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)

        with pytest.raises(RuntimeError, match="captured HTTP fallback"):
            await CodexHandle(
                _Cfg(), daemon_mode="auto", daemon_manager=manager,
            ).connect(resume_id="oversized-thread", cwd="/tmp")

        argv = spawned[0]
        assert argv[:3] == [
            "/managed/codex", "app-server", "--stdio",
        ]
        assert any(
            item.endswith("supports_websockets=false") for item in argv)
        assert manager.proxy_calls == 0

    asyncio.run(run())


def test_established_shared_session_never_falls_back_to_private_stdio(
    monkeypatch,
):
    async def run():
        manager = _Manager(None)
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            raise AssertionError("private stdio must not be started")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        handle = CodexHandle(
            _Cfg(), daemon_mode="auto", daemon_manager=manager)
        handle._daemon_proxy_established = True

        with pytest.raises(RuntimeError, match="shared Codex app-server"):
            await handle.connect(resume_id="shared-thread", cwd="/tmp")

        assert handle.shared_daemon_affinity is True
        assert handle.using_daemon_proxy is False
        assert spawned == []

    asyncio.run(run())


def test_proxy_handshake_failure_falls_back_to_stdio(monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        manager = _Manager(["/usr/bin/codex", "app-server", "proxy"])
        processes = [
            _Process(_Reader(_handshake_response(nonce, accept="wrong")), 50001),
            _Process(_Reader(block_at_eof=True), 50002),
        ]
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return processes.pop(0)

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            handle_module.os, "killpg", lambda *_args: None, raising=False)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        handle.model = "gpt-test"
        handle.effort = None

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            if method == "initialize":
                return {"serverInfo": {"version": "0.144.1"}}
            if method == "thread/start":
                return {"thread": {"id": "fallback-thread"}}
            if method == "thread/settings/update":
                handle._thread_settings_updated.set()
                return {}
            raise AssertionError(method)

        handle._read_loop = idle  # type: ignore[method-assign]
        handle._request = request  # type: ignore[method-assign]
        handle._notify = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]
        await handle.connect(cwd="/tmp")

        assert spawned == [
            ["/usr/bin/codex", "app-server", "proxy"],
            ["/usr/bin/codex", "app-server", "--stdio"],
        ]
        assert handle.using_daemon_proxy is False
        assert manager.invalidations == 1
        await handle.disconnect()

    asyncio.run(run())


def test_verified_shared_proxy_failure_never_falls_back_to_stdio(monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        manager = _Manager(
            ["/usr/bin/codex", "app-server", "proxy"],
            strict_shared=True,
        )
        process = _Process(
            _Reader(_handshake_response(nonce, accept="wrong")), 50004)
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return process

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            handle_module.os, "killpg", lambda *_args: None, raising=False)

        with pytest.raises(CodexProxyProtocolError):
            await CodexHandle(_Cfg(), daemon_manager=manager).connect(cwd="/tmp")

        assert spawned == [["/usr/bin/codex", "app-server", "proxy"]]
        assert manager.invalidations == 1

    asyncio.run(run())


def test_daemon_upgrade_error_is_not_converted_to_stdio(monkeypatch):
    async def run():
        class _UpgradeManager(_Manager):
            async def proxy_args(self, _bin, _env):
                raise CodexDaemonUpgradeRequired("upgrade required")

        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            raise AssertionError("private stdio must not be started")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)

        with pytest.raises(CodexDaemonUpgradeRequired, match="upgrade required"):
            await CodexHandle(
                _Cfg(), daemon_manager=_UpgradeManager()).connect(cwd="/tmp")
        assert spawned == []

    asyncio.run(run())


def test_proxy_connect_exposes_shared_state_and_disconnect_keeps_manager(
        monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        manager = _Manager(["/usr/bin/codex", "app-server", "proxy"])
        process = _Process(_Reader(_handshake_response(nonce)), 50003)
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return process

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(
            handle_module.os, "killpg", lambda *_args: None, raising=False)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        handle.model = "gpt-test"
        handle.effort = None

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            if method == "initialize":
                return {"serverInfo": {"version": "0.144.1"}}
            if method == "thread/start":
                return {"thread": {"id": "shared-thread"}}
            if method == "thread/settings/update":
                handle._thread_settings_updated.set()
                return {}
            raise AssertionError(method)

        handle._read_loop = idle  # type: ignore[method-assign]
        handle._request = request  # type: ignore[method-assign]
        handle._notify = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]
        await handle.connect(cwd="/tmp")
        assert spawned == [["/usr/bin/codex", "app-server", "proxy"]]
        assert handle.using_daemon_proxy is True

        await handle.disconnect()
        assert handle.using_daemon_proxy is False
        # Normal session teardown owns only the proxy and keeps daemon liveness
        # cached for the other clients.
        assert manager.invalidations == 0

    asyncio.run(run())


def test_shared_approval_without_callback_waits_for_resolved():
    async def run():
        handle = CodexHandle(_Cfg(), daemon_mode="off")
        handle._using_daemon_proxy = True
        handle.approval = "on-request"
        sent = []
        handle._send = lambda message: asyncio.sleep(  # type: ignore[method-assign]
            0, result=sent.append(message))

        await handle._dispatch({
            "id": 7, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "t", "turnId": "u", "itemId": "i"},
        })
        assert sent == []
        assert handle._pending_server_request_ids == {7}
        await handle._dispatch({
            "method": "serverRequest/resolved",
            "params": {"threadId": "t", "requestId": 7},
        })
        assert sent == []
        assert handle._pending_server_request_ids == set()

    asyncio.run(run())


def test_shared_approval_first_response_wins_and_cancels_local_callback():
    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def approve(_method, _params):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        handle = CodexHandle(
            _Cfg(), daemon_mode="off", approval_callback=approve)
        handle._using_daemon_proxy = True
        handle.approval = "on-request"
        sent = []
        handle._send = lambda message: asyncio.sleep(  # type: ignore[method-assign]
            0, result=sent.append(message))
        await handle._dispatch({
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "t", "turnId": "u", "itemId": "i"},
        })
        await asyncio.wait_for(started.wait(), timeout=1)
        await handle._dispatch({
            "method": "serverRequest/resolved",
            "params": {"threadId": "t", "requestId": "approval-1"},
        })
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        await asyncio.sleep(0)
        assert sent == []
        assert handle._pending_server_request_ids == set()

    asyncio.run(run())


def test_shared_approval_timeout_and_task_cap_do_not_decline(monkeypatch):
    async def run():
        monkeypatch.setattr(handle_module, "_APPROVAL_TIMEOUT", 0.01)
        monkeypatch.setattr(handle_module, "_MAX_SERVER_REQUEST_TASKS", 1)

        async def approve(_method, _params):
            await asyncio.Event().wait()

        handle = CodexHandle(
            _Cfg(), daemon_mode="off", approval_callback=approve)
        handle._using_daemon_proxy = True
        handle.approval = "on-request"
        sent = []
        handle._send = lambda message: asyncio.sleep(  # type: ignore[method-assign]
            0, result=sent.append(message))
        for request_id in (1, 2):
            await handle._dispatch({
                "id": request_id,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "t", "turnId": "u",
                           "itemId": str(request_id)},
            })
        await asyncio.gather(*list(handle._server_request_tasks))
        assert sent == []
        assert handle._pending_server_request_ids == set()

    asyncio.run(run())
