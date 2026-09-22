"""Codex shared app-server daemon/proxy regressions (no model calls)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path
import signal
import socket
import tempfile

import pytest

from cc_remote.wrapper import codex_daemon as daemon_module
from cc_remote.wrapper import codex_handle as handle_module
from cc_remote.wrapper.codex_daemon import (
    CodexDaemonManager,
    CodexProfileDaemonUnavailable,
    CodexDaemonUpgradeRequired,
    CodexSocketIdentity,
)
from cc_remote.wrapper.codex_handle import (
    CodexAppServerDisconnected,
    CodexDaemonProxyClosed,
    CodexHandle,
    CodexProxyProtocolError,
    _websocket_client_frame,
)
from cc_remote.wrapper.process_scan import ProcessIdentity


_REAL_ENSURE_MANAGED_DAEMON_NOFILE = (
    daemon_module._ensure_managed_daemon_nofile
)


@pytest.fixture(autouse=True)
def _stub_managed_daemon_nofile_verification(monkeypatch):
    """Lifecycle unit tests do not own a real detached daemon PID."""
    monkeypatch.setattr(
        daemon_module,
        "_ensure_managed_daemon_nofile",
        lambda *_args, **_kwargs: True,
    )


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


def _private_stdio_argv(argv: list[str]) -> list[str]:
    if os.name != "posix":
        return argv
    assert argv[:3] == [
        handle_module.sys.executable,
        handle_module._RLIMIT_EXEC,
        str(handle_module._APP_SERVER_NOFILE_SOFT_LIMIT),
    ]
    return argv[3:]


def test_daemon_lifecycle_child_raises_nofile_without_wrapping_reads(
        monkeypatch):
    captured: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        captured.append(tuple(argv))
        return daemon_module.subprocess.CompletedProcess(
            argv, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(daemon_module.subprocess, "run", run)
    env = {"PATH": "/usr/bin"}
    daemon_module._run_command(
        ("/usr/bin/codex", "app-server", "daemon", "start"),
        env,
        1.0,
    )
    daemon_module._run_command(
        (
            "/usr/bin/codex", "app-server", "daemon",
            "enable-remote-control",
        ),
        env,
        1.0,
    )
    daemon_module._run_command(
        ("/usr/bin/codex", "app-server", "daemon", "version"),
        env,
        1.0,
    )

    if os.name == "posix":
        assert captured[0][:3] == (
            daemon_module.sys.executable,
            daemon_module._RLIMIT_EXEC,
            str(daemon_module._DAEMON_NOFILE_SOFT_LIMIT),
        )
        assert captured[0][3:] == (
            "/usr/bin/codex", "app-server", "daemon", "start",
        )
        assert captured[1][:3] == captured[0][:3]
        assert captured[1][3:] == (
            "/usr/bin/codex", "app-server", "daemon",
            "enable-remote-control",
        )
    else:
        assert captured[0] == (
            "/usr/bin/codex", "app-server", "daemon", "start",
        )
        assert captured[1] == (
            "/usr/bin/codex", "app-server", "daemon",
            "enable-remote-control",
        )
    assert captured[2] == (
        "/usr/bin/codex", "app-server", "daemon", "version",
    )


@pytest.mark.parametrize("listener,allow_local,accepted", [
    ("remote", False, True), ("local", True, True), ("default", True, True),
    ("other", True, False), ("local", False, False), ("ambiguous", True, False),
])
def test_linux_managed_daemon_nofile_is_applied_to_exact_pid(
        monkeypatch, tmp_path, listener, allow_local, accepted):
    proc_root = tmp_path / "proc"
    proc = proc_root / "4321"
    proc.mkdir(parents=True)
    binary = tmp_path / "codex"
    binary.write_bytes(b"codex")
    (proc / "exe").symlink_to(binary)
    socket_path = tmp_path / "codex-home/app-server-control/app-server-control.sock"
    arguments = {
        "remote": "--remote-control",
        "local": f"--listen\0unix://{socket_path}",
        "default": "--listen\0unix://",
        "other": "--listen\0unix:///other/account.sock",
        "ambiguous": f"--listen\0unix://{socket_path}\0--listen\0unix:///other.sock",
    }
    (proc / "cmdline").write_bytes(f"{binary}\0app-server\0{arguments[listener]}\0".encode())
    (proc / "stat").write_bytes(
        b"4321 (codex) " + b" ".join(
            [b"S", *([b"0"] * 18), b"123"]
        )
    )
    daemon_root = tmp_path / "codex-home" / "app-server-daemon"
    daemon_root.mkdir(parents=True)
    (daemon_root / "app-server.pid").write_text('{"pid":4321}')
    calls: list[tuple] = []
    limit = [1024, 524288]

    def prlimit(pid, which, value=None):
        calls.append((pid, which, value))
        if value is not None:
            limit[:] = value
        return tuple(limit)

    monkeypatch.setattr(daemon_module.sys, "platform", "linux")
    monkeypatch.setattr(daemon_module, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(
        daemon_module, "process_owner_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(
        daemon_module.resource, "prlimit", prlimit, raising=False)

    assert _REAL_ENSURE_MANAGED_DAEMON_NOFILE(
        str(binary),
        {"CODEX_HOME": str(tmp_path / "codex-home")},
        {"managedCodexPath": str(binary)},
        allow_local_listener=allow_local,
    ) is accepted
    if not accepted:
        assert calls == []
        return
    assert calls[1][2] == (daemon_module._DAEMON_NOFILE_SOFT_LIMIT, 524288)
    assert calls[-1][2] is None


def test_managed_daemon_identity_uses_same_user_pid_and_start_token(
        monkeypatch, tmp_path):
    daemon_root = tmp_path / "app-server-daemon"
    daemon_root.mkdir()
    (daemon_root / "app-server.pid").write_text(
        '{"pid":4321}', encoding="utf-8")
    identity = ProcessIdentity(4321, 987654)
    monkeypatch.setattr(
        daemon_module, "process_owner_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(
        daemon_module, "process_identity", lambda _pid: identity)

    assert daemon_module._managed_daemon_process_identity(
        tmp_path) == identity

    monkeypatch.setattr(
        daemon_module,
        "process_owner_uid",
        lambda _pid: os.getuid() + 1,
    )
    assert daemon_module._managed_daemon_process_identity(tmp_path) is None


@pytest.fixture
def standalone_listener():
    # Keep the path below macOS's Unix socket length limit.
    with tempfile.TemporaryDirectory(prefix="cc-sock-", dir="/tmp") as root:
        home = Path(root).resolve()
        control = home / "app-server-control"
        control.mkdir(mode=0o700)
        path = control / "app-server-control.sock"
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen()
            yield home, path


def _standalone_manager(home, path):
    manager = CodexDaemonManager("auto", require_shared=True)

    async def command(_bin, _env, *args):
        if args[-1] == "--help":
            return _result(0)
        if args[-1] == "version":
            return _result(0, {
                "status": "running", "socketPath": str(path),
                "managedCodexPath": str(home / "codex"),
                "managedCodexVersion": "0.149.0",
                "cliVersion": "0.153.4", "appServerVersion": "0.149.0",
            })
        assert args[-1] == "enable-remote-control"
        return _result(1)

    manager._run = command
    return manager


def test_standalone_generation_survives_invalidate_and_detects_replacement(
    standalone_listener,
):
    home, path = standalone_listener
    manager = _standalone_manager(home, path)
    asyncio.run(manager.proxy_args("/bin/codex", {"CODEX_HOME": str(home)}))
    original = manager.current_process_identity()
    assert isinstance(original, CodexSocketIdentity)
    assert manager.current_process_identity() == original
    manager.invalidate()
    assert manager.current_process_identity() == original
    handle = CodexHandle(_Cfg(), daemon_manager=manager)
    handle.proc = _Process()
    handle._using_daemon_proxy = True
    handle._daemon_process_identity = original
    assert handle.daemon_process_generation_current is True
    path.unlink()
    with socket.socket(socket.AF_UNIX) as replacement:
        replacement.bind(str(path))
        path.chmod(0o600)
        assert manager.current_process_identity() != original
        assert handle.daemon_process_generation_current is False
    path.unlink()
    assert manager.current_process_identity() is None


@pytest.mark.parametrize("record", ["corrupt", "symlink", "fifo"])
def test_standalone_generation_does_not_ignore_unverifiable_pid_record(
    standalone_listener, record,
):
    home, path = standalone_listener
    daemon_root = home / "app-server-daemon"
    daemon_root.mkdir()
    record_path = daemon_root / "app-server.pid"
    if record == "corrupt":
        record_path.write_text("not a pid")
    elif record == "symlink":
        record_path.symlink_to(home / "missing")
    else:
        os.mkfifo(record_path)
    manager = _standalone_manager(home, path)
    asyncio.run(manager.proxy_args("/bin/codex", {"CODEX_HOME": str(home)}))
    assert manager.current_process_identity() is None


def test_managed_identity_cannot_downgrade_after_pid_disappears(
    monkeypatch, standalone_listener,
):
    home, path = standalone_listener
    manager = _standalone_manager(home, path)
    env = {"CODEX_HOME": str(home)}
    asyncio.run(manager.proxy_args("/bin/codex", env))
    observed = ProcessIdentity(4321, 100)
    monkeypatch.setattr(
        daemon_module, "_managed_daemon_process_identity", lambda _home: observed,
    )
    assert manager.current_process_identity() == observed
    observed = None
    assert manager.current_process_identity() is None
    manager.invalidate()
    asyncio.run(manager.proxy_args("/bin/codex", env))
    assert manager.current_process_identity() is None


@pytest.mark.parametrize("unsafe", [
    "other_profile", "socket_permissions", "parent_permissions", "owner",
    "regular_file", "symlink", "override",
])
def test_standalone_generation_rejects_unsafe_or_cross_profile_socket(
    monkeypatch, standalone_listener, unsafe,
):
    home, path = standalone_listener
    manager = _standalone_manager(home, path)
    if unsafe == "override":
        manager.socket_path = str(home / "other.sock")
    asyncio.run(manager.proxy_args("/bin/codex", {"CODEX_HOME": str(home)}))
    if unsafe == "other_profile":
        manager._ready_codex_home = str(home / "other_profile")
    elif unsafe == "socket_permissions":
        path.chmod(0o666)
    elif unsafe == "parent_permissions":
        path.parent.chmod(0o770)
    elif unsafe == "owner":
        uid = os.getuid()
        monkeypatch.setattr(daemon_module.os, "getuid", lambda: uid + 1)
    elif unsafe in {"regular_file", "symlink"}:
        renamed = path.with_name("original.sock")
        path.rename(renamed)
        if unsafe == "regular_file":
            path.write_text("not a socket")
        else:
            path.symlink_to(renamed)
    assert manager.current_process_identity() is None


@pytest.mark.parametrize("replace_at", [None, "initialize", "thread/resume"])
def test_real_manager_standalone_connect_and_restart_fences(
    monkeypatch, standalone_listener, replace_at,
):
    async def run():
        home, path = standalone_listener
        manager = _standalone_manager(home, path)
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(argv)
            return _Process(_Reader(_handshake_response(nonce)), 50000 + len(spawned))

        monkeypatch.setattr(handle_module.asyncio, "create_subprocess_exec", spawn)
        handle = CodexHandle(_Cfg(), daemon_manager=manager, codex_home=str(home))
        replaced = False

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            nonlocal replaced
            if method == replace_at and not replaced:
                replaced = True
                path.unlink()
                with socket.socket(socket.AF_UNIX) as replacement:
                    replacement.bind(str(path))
                    path.chmod(0o600)
            if method == "initialize":
                return {"serverInfo": {"version": "0.149.0"}}
            assert method == "thread/resume"
            return {"thread": {"id": "existing-thread"}}

        handle._read_loop = idle
        handle._request = request
        handle._notify = lambda *_args: asyncio.sleep(0)
        await handle.connect(resume_id="existing-thread", cwd="/tmp")
        assert len(spawned) == (2 if replace_at else 1)
        assert all(argv[1:3] == ("app-server", "proxy") for argv in spawned)
        assert handle.thread_id == "existing-thread"
        assert handle.daemon_process_generation_current is True
        await handle.disconnect()

    asyncio.run(run())


def test_unavailable_pid_is_not_reported_as_a_confirmed_restart(
    monkeypatch, standalone_listener,
):
    async def run():
        home, path = standalone_listener
        manager = _standalone_manager(home, path)
        manager._managed_identity_required = True
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(argv)
            return _Process(_Reader(_handshake_response(nonce)))

        monkeypatch.setattr(handle_module.asyncio, "create_subprocess_exec", spawn)
        handle = CodexHandle(_Cfg(), daemon_manager=manager, codex_home=str(home))

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            assert method == "initialize"
            return {"serverInfo": {"version": "0.153.4"}}

        handle._read_loop = idle
        handle._request = request
        handle._notify = lambda *_args: asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="Cannot verify Codex app-server identity"):
            await handle.connect(resume_id="existing-thread", cwd="/tmp")
        assert len(spawned) == 2
        assert all(argv[1:3] == ("app-server", "proxy") for argv in spawned)
        assert handle.proc is None

    asyncio.run(run())


def test_linux_managed_daemon_nofile_fails_closed_on_low_hard_limit(
        monkeypatch, tmp_path):
    proc_root = tmp_path / "proc"
    proc = proc_root / "4321"
    proc.mkdir(parents=True)
    binary = tmp_path / "codex"
    binary.write_bytes(b"codex")
    (proc / "exe").symlink_to(binary)
    (proc / "cmdline").write_bytes(
        f"{binary}\0app-server\0--remote-control\0".encode()
    )
    (proc / "stat").write_bytes(
        b"4321 (codex) " + b" ".join(
            [b"S", *([b"0"] * 18), b"123"]
        )
    )
    daemon_root = tmp_path / "codex-home" / "app-server-daemon"
    daemon_root.mkdir(parents=True)
    (daemon_root / "app-server.pid").write_text('{"pid":4321}')

    monkeypatch.setattr(daemon_module.sys, "platform", "linux")
    monkeypatch.setattr(daemon_module, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(
        daemon_module, "process_owner_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(
        daemon_module.resource,
        "prlimit",
        lambda *_args: (1024, 2048),
        raising=False,
    )

    assert _REAL_ENSURE_MANAGED_DAEMON_NOFILE(
        str(binary),
        {"CODEX_HOME": str(tmp_path / "codex-home")},
        {"managedCodexPath": str(binary)},
    ) is False


def test_daemon_manager_starts_enables_versions_and_reconnects(monkeypatch):
    async def run():
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


def test_required_profile_bootstraps_its_own_shared_daemon(monkeypatch):
    async def run():
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        manager = CodexDaemonManager("auto", require_shared=True)
        calls: list[tuple[str, ...]] = []
        bootstrapped = False

        async def command(_bin, _env, *args):
            nonlocal bootstrapped
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                if not bootstrapped:
                    return _result(1)
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": "0.147.0",
                    "socketPath": "/tmp/stack-codex.sock",
                    "cliVersion": "0.147.0",
                    "appServerVersion": "0.147.0",
                })
            if args[-2:] == ("bootstrap", "--remote-control"):
                bootstrapped = True
                return _result(0)
            assert args[-1] == "enable-remote-control"
            return _result(0, {
                "status": "enabled",
                "remoteControlEnabled": True,
                "socketPath": "/tmp/stack-codex.sock",
            })

        manager._run = command  # type: ignore[method-assign]
        assert await manager.proxy_args(
            "/bin/codex", {"CODEX_HOME": "/tmp/codex-stack"},
        ) == [
            "/bin/codex", "app-server", "proxy",
            "--sock", "/tmp/stack-codex.sock",
        ]
        assert manager.strict_shared_affinity is True
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            (
                "app-server", "daemon", "bootstrap",
                "--remote-control",
            ),
            ("app-server", "daemon", "version"),
            ("app-server", "daemon", "enable-remote-control"),
            ("app-server", "daemon", "version"),
        ]

    asyncio.run(run())


def test_profile_bootstrap_reuses_verified_managed_standalone(tmp_path):
    primary = tmp_path / "primary"
    standalone = primary / "packages" / "standalone"
    release = standalone / "releases" / "0.147.0"
    binary = release / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    (release / "codex").symlink_to("bin/codex")
    (standalone / "current").symlink_to(release, target_is_directory=True)
    secondary = tmp_path / "secondary"
    secondary.mkdir()

    assert daemon_module._prepare_profile_standalone(
        str(binary), {"CODEX_HOME": str(secondary)},
    ) is True
    linked_current = secondary / "packages" / "standalone" / "current"
    assert linked_current.is_symlink()
    assert linked_current.readlink() == standalone / "current"
    assert (linked_current / "codex").resolve() == binary


def test_profile_bootstrap_never_replaces_existing_managed_path(tmp_path):
    primary = tmp_path / "primary"
    standalone = primary / "packages" / "standalone"
    release = standalone / "releases" / "0.147.0"
    binary = release / "bin" / "codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    (release / "codex").symlink_to("bin/codex")
    (standalone / "current").symlink_to(release, target_is_directory=True)
    secondary = tmp_path / "secondary"
    existing = secondary / "packages" / "standalone" / "current"
    existing.mkdir(parents=True)
    marker = existing / "keep"
    marker.write_text("user-owned")

    assert daemon_module._prepare_profile_standalone(
        str(binary), {"CODEX_HOME": str(secondary)},
    ) is False
    assert marker.read_text() == "user-owned"
    assert not existing.is_symlink()


def test_profile_bootstrap_atomically_advances_owned_official_current(
    tmp_path,
):
    source_home = tmp_path / "source"
    source_standalone = source_home / "packages" / "standalone"
    source_release = source_standalone / "releases" / "0.148.0"
    source_binary = source_release / "bin" / "codex"
    source_binary.parent.mkdir(parents=True)
    source_binary.write_bytes(b"#!/bin/sh\n")
    source_binary.chmod(0o755)
    (source_release / "codex").symlink_to("bin/codex")
    (source_standalone / "current").symlink_to(
        source_release,
        target_is_directory=True,
    )

    profile_home = tmp_path / "profile"
    profile_standalone = profile_home / "packages" / "standalone"
    old_release = profile_standalone / "releases" / "0.147.0"
    old_binary = old_release / "bin" / "codex"
    old_binary.parent.mkdir(parents=True)
    old_binary.write_bytes(b"#!/bin/sh\n")
    old_binary.chmod(0o755)
    (old_release / "codex").symlink_to("bin/codex")
    current = profile_standalone / "current"
    current.symlink_to(old_release, target_is_directory=True)

    assert daemon_module._prepare_profile_standalone(
        str(source_binary),
        {"CODEX_HOME": str(profile_home)},
    ) is True
    assert current.is_symlink()
    assert current.readlink() == source_standalone / "current"
    assert (current / "codex").resolve() == source_binary
    assert old_binary.exists()


def test_profile_bootstrap_never_replaces_external_current_symlink(tmp_path):
    source_home = tmp_path / "source"
    source_standalone = source_home / "packages" / "standalone"
    source_release = source_standalone / "releases" / "0.148.0"
    source_binary = source_release / "bin" / "codex"
    source_binary.parent.mkdir(parents=True)
    source_binary.write_bytes(b"#!/bin/sh\n")
    source_binary.chmod(0o755)
    (source_release / "codex").symlink_to("bin/codex")
    (source_standalone / "current").symlink_to(
        source_release,
        target_is_directory=True,
    )

    profile_home = tmp_path / "profile"
    current = profile_home / "packages" / "standalone" / "current"
    current.parent.mkdir(parents=True)
    external_release = tmp_path / "external" / "releases" / "0.147.0"
    external_binary = external_release / "bin" / "codex"
    external_binary.parent.mkdir(parents=True)
    external_binary.write_bytes(b"#!/bin/sh\n")
    external_binary.chmod(0o755)
    (external_release / "codex").symlink_to("bin/codex")
    current.symlink_to(external_release, target_is_directory=True)

    assert daemon_module._prepare_profile_standalone(
        str(source_binary),
        {"CODEX_HOME": str(profile_home)},
    ) is False
    assert current.readlink() == external_release
    assert (current / "codex").resolve() == external_binary


def test_required_profile_never_silently_falls_back_to_stdio(monkeypatch):
    async def run():
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v1",))
        monkeypatch.setattr(
            daemon_module,
            "_terminate_stale_darwin_daemon_updater",
            lambda _bin, _env: False,
        )
        manager = CodexDaemonManager("auto", require_shared=True)
        calls: list[tuple[str, ...]] = []

        async def command(_bin, _env, *args):
            calls.append(args)
            if args[-1] == "--help":
                return _result(0)
            return _result(1)

        manager._run = command  # type: ignore[method-assign]
        with pytest.raises(
            CodexProfileDaemonUnavailable,
            match="profile shared daemon",
        ):
            await manager.proxy_args(
                "/bin/codex", {"CODEX_HOME": "/tmp/codex-stack"})
        assert manager.info is None
        assert manager.strict_shared_affinity is True
        assert calls == [
            ("app-server", "daemon", "--help"),
            ("app-server", "proxy", "--help"),
            ("app-server", "daemon", "version"),
            (
                "app-server", "daemon", "bootstrap",
                "--remote-control",
            ),
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

    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        daemon_module, "_darwin_process_info", lambda pid: processes.get(pid))
    monkeypatch.setattr(
        daemon_module, "process_owner_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(
        daemon_module, "_darwin_process_state", lambda _pid: "Z+")

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


def test_lagging_managed_daemon_waits_for_async_replacement(monkeypatch):
    async def run():
        monkeypatch.setattr(
            daemon_module, "_binary_identity", lambda _path: ("codex-v2",))
        monkeypatch.setattr(
            daemon_module,
            "_prepare_profile_standalone",
            lambda _bin, _env: True,
        )
        monkeypatch.setattr(
            daemon_module,
            "_DAEMON_UPGRADE_POLL_INTERVAL",
            0.0,
        )
        manager = CodexDaemonManager("auto")
        version_probes = 0
        restarted = False

        async def command(_bin, _env, *args):
            nonlocal version_probes, restarted
            if args[-1] == "--help":
                return _result(0)
            if args[-1] == "version":
                version_probes += 1
                upgraded = restarted and version_probes >= 5
                version = "0.148.0" if upgraded else "0.147.0"
                return _result(0, {
                    "status": "running",
                    "managedCodexPath": "/opt/codex/current/codex",
                    "managedCodexVersion": version,
                    "socketPath": "/tmp/codex.sock",
                    "cliVersion": "0.148.0",
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
        assert version_probes == 5

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


def test_daemon_capability_cache_invalidates_on_binary_change(tmp_path):
    async def run():
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


def test_handle_detects_managed_daemon_process_generation_change(
        monkeypatch):
    original = ProcessIdentity(4321, 100)
    replacement = ProcessIdentity(4321, 200)
    observed = original
    manager = CodexDaemonManager("auto")
    manager._ready_codex_home = "/tmp/codex-generation-test"
    monkeypatch.setattr(
        daemon_module,
        "_managed_daemon_process_identity",
        lambda _home: observed,
    )
    handle = CodexHandle(
        _Cfg(), daemon_mode="auto", daemon_manager=manager)
    handle.proc = _Process()
    handle._using_daemon_proxy = True
    handle._daemon_process_identity = original

    assert handle.daemon_process_generation_current is True
    observed = replacement
    assert handle.daemon_process_generation_current is False

    # Invalidating one proxy's cached readiness must not erase the profile home
    # used to observe healthy sibling connections.
    manager.invalidate()
    assert manager.current_process_identity() == replacement

    # An identity which was transiently unavailable at connect time must not
    # disable generation checks for the rest of this handle's lifetime.
    handle._daemon_process_identity = None
    assert handle.daemon_process_generation_current is False


def test_proxy_connect_retries_if_daemon_changes_during_initialize(monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        old = ProcessIdentity(4321, 100)
        replacement = ProcessIdentity(4322, 200)

        class _SwappingManager(_Manager):
            def __init__(self):
                super().__init__(
                    ["/usr/bin/codex", "app-server", "proxy"],
                    strict_shared=True,
                )
                self.identities = [old, replacement, replacement, replacement]

            def current_process_identity(self):
                if len(self.identities) > 1:
                    return self.identities.pop(0)
                return self.identities[0]

        manager = _SwappingManager()
        processes = [
            _Process(_Reader(_handshake_response(nonce)), 50010),
            _Process(_Reader(_handshake_response(nonce)), 50011),
        ]
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return processes.pop(0)

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        handle.model = "gpt-test"
        handle.effort = None
        initialize_calls = 0

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            nonlocal initialize_calls
            if method == "initialize":
                initialize_calls += 1
                return {"serverInfo": {"version": "0.150.0"}}
            if method == "thread/start":
                return {"thread": {"id": "stable-thread"}}
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
            ["/usr/bin/codex", "app-server", "proxy"],
        ]
        assert manager.proxy_calls == 2
        assert manager.invalidations == 1
        assert initialize_calls == 2
        assert handle.using_daemon_proxy is True
        assert handle._daemon_process_identity == replacement
        assert handle.daemon_process_generation_current is True
        await handle.disconnect()

    asyncio.run(run())


def test_proxy_connect_rebinds_resume_if_daemon_changes_after_thread_read(
    monkeypatch,
):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        old = ProcessIdentity(4321, 100)
        replacement = ProcessIdentity(4322, 200)

        class _SwappingManager(_Manager):
            def __init__(self):
                super().__init__(
                    ["/usr/bin/codex", "app-server", "proxy"],
                    strict_shared=True,
                )
                self.identities = [
                    old, old, replacement,
                    replacement, replacement, replacement,
                ]

            def current_process_identity(self):
                if len(self.identities) > 1:
                    return self.identities.pop(0)
                return self.identities[0]

        manager = _SwappingManager()
        processes = [
            _Process(_Reader(_handshake_response(nonce)), 50020),
            _Process(_Reader(_handshake_response(nonce)), 50021),
        ]
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return processes.pop(0)

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        resume_calls = []

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, params=None):
            if method == "initialize":
                return {"serverInfo": {"version": "0.150.0"}}
            if method == "thread/resume":
                resume_calls.append(params)
                return {"thread": {"id": "stable-thread"}}
            raise AssertionError(method)

        handle._read_loop = idle  # type: ignore[method-assign]
        handle._request = request  # type: ignore[method-assign]
        handle._notify = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]

        await handle.connect(resume_id="stable-thread", cwd="/tmp")

        assert len(spawned) == 2
        assert len(resume_calls) == 2
        assert all(call["threadId"] == "stable-thread"
                   for call in resume_calls)
        assert manager.invalidations == 1
        assert handle.thread_id == "stable-thread"
        assert handle._daemon_process_identity == replacement
        await handle.disconnect()

    asyncio.run(run())


def test_proxy_connect_resumes_created_thread_if_sticky_settings_disconnect(
    monkeypatch,
):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        old = ProcessIdentity(4321, 100)
        replacement = ProcessIdentity(4322, 200)

        class _SwappingManager(_Manager):
            def __init__(self):
                super().__init__(
                    ["/usr/bin/codex", "app-server", "proxy"],
                    strict_shared=True,
                )
                self.identities = [
                    old, old,
                    replacement, replacement, replacement,
                ]

            def current_process_identity(self):
                if len(self.identities) > 1:
                    return self.identities.pop(0)
                return self.identities[0]

        manager = _SwappingManager()
        processes = [
            _Process(_Reader(_handshake_response(nonce)), 50030),
            _Process(_Reader(_handshake_response(nonce)), 50031),
        ]
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return processes.pop(0)

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        handle.model = "gpt-test"
        start_calls = 0
        resume_calls = 0
        settings_calls = 0

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, _params=None):
            nonlocal start_calls, resume_calls, settings_calls
            if method == "initialize":
                return {"serverInfo": {"version": "0.150.0"}}
            if method == "thread/start":
                start_calls += 1
                return {"thread": {"id": "created-thread"}}
            if method == "thread/settings/update":
                settings_calls += 1
                raise CodexAppServerDisconnected("0.150.0", 7)
            if method == "thread/resume":
                resume_calls += 1
                return {"thread": {"id": "created-thread"}}
            raise AssertionError(method)

        handle._read_loop = idle  # type: ignore[method-assign]
        handle._request = request  # type: ignore[method-assign]
        handle._notify = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]

        await handle.connect(cwd="/tmp")

        assert len(spawned) == 2
        assert start_calls == 1
        assert settings_calls == 1
        assert resume_calls == 1
        assert manager.invalidations == 1
        assert handle.thread_id == "created-thread"
        assert handle._daemon_process_identity == replacement
        await handle.disconnect()

    asyncio.run(run())


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


def test_proxy_close_surfaces_typed_incomplete_managed_boundary():
    async def run():
        manager = _Manager()
        process = _Process(_Reader(_server_frame(
            (1001).to_bytes(2, "big"), opcode=0x8)))
        handle = CodexHandle(
            _Cfg(), daemon_mode="auto", daemon_manager=manager)
        handle.proc = process
        handle._using_daemon_proxy = True
        handle._daemon_proxy_established = True
        handle._dead = False
        handle.app_server_version = "0.149.0"
        handle.thread_id = "managed-thread"
        handle.turn_id = "managed-turn"
        handle.turn_active = True
        handle._open_managed_stream()

        consumer = asyncio.create_task(
            _collect_async(handle.receive_response()))
        await handle._read_loop(process, handle._generation)
        frames = await consumer

        assert len(frames) == 1
        assert isinstance(frames[0], CodexDaemonProxyClosed)
        assert frames[0].app_server_version == "0.149.0"
        assert frames[0].generation == 0
        assert frames[0].close_kind == "eof"
        assert handle.using_daemon_proxy is False

    async def _collect_async(stream):
        return [item async for item in stream]

    asyncio.run(run())


def test_proxy_close_unblocks_pending_rpc_with_generation_identity():
    async def run():
        manager = _Manager()
        process = _Process(_Reader(_server_frame(
            (1001).to_bytes(2, "big"), opcode=0x8)))
        handle = CodexHandle(
            _Cfg(), daemon_mode="auto", daemon_manager=manager)
        handle.proc = process
        handle._using_daemon_proxy = True
        handle._dead = False
        handle.app_server_version = "0.150.0"
        pending = asyncio.get_running_loop().create_future()
        handle._pending[1] = pending

        await handle._read_loop(process, handle._generation)

        with pytest.raises(CodexAppServerDisconnected) as raised:
            await pending
        assert raised.value.app_server_version == "0.150.0"
        assert raised.value.generation == 0

    asyncio.run(run())


def test_proxy_close_never_replaces_buffered_native_terminal():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "terminal-thread"
        handle.turn_id = "terminal-turn"
        handle.turn_active = True
        handle._open_managed_stream()
        queue = handle._turn_q
        assert queue is not None
        await handle._dispatch({
            "method": "turn/completed",
            "params": {
                "threadId": "terminal-thread",
                "turn": {"id": "terminal-turn", "status": "completed"},
            },
        })

        handle._force_turn_sentinel(queue, CodexDaemonProxyClosed(
            "0.149.0", 1, close_kind="eof"))
        frames = [item async for item in handle.receive_response()]

        assert len(frames) == 1
        assert frames[0]["method"] == "turn/completed"

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
        assert _private_stdio_argv(captured[0])[:3] == [
            "/usr/bin/codex", "app-server", "--stdio"]
        assert manager.proxy_calls == (0 if work_mode else 1)

    asyncio.run(run(False))
    asyncio.run(run(True))


def test_oversized_resume_prefers_shared_daemon_before_newer_private_core(
        monkeypatch):
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
            "/managed/codex", "app-server", "proxy",
        ]
        assert _private_stdio_argv(spawned[1])[:3] == [
            "/Applications/Codex.app/Resources/codex",
            "app-server", "--stdio",
        ]
        assert manager.proxy_calls == 1

    asyncio.run(run())


def test_oversized_desktop_openai_resume_prefers_shared_daemon_then_http_stdio(
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

        assert spawned[0][:3] == [
            "/managed/codex", "app-server", "proxy",
        ]
        argv = _private_stdio_argv(spawned[1])
        assert argv[:3] == [
            "/managed/codex", "app-server", "--stdio",
        ]
        assert any(
            item.endswith("supports_websockets=false") for item in argv)
        assert manager.proxy_calls == 1

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
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
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

        assert spawned[0] == ["/usr/bin/codex", "app-server", "proxy"]
        assert _private_stdio_argv(spawned[1]) == [
            "/usr/bin/codex", "app-server", "--stdio",
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
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)

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


def test_strict_daemon_preparation_failure_never_starts_private_stdio(
        monkeypatch):
    async def run():
        class _StrictFailingManager(_Manager):
            async def proxy_args(self, _bin, _env):
                self.proxy_calls += 1
                raise RuntimeError("strict daemon preparation failed")

        manager = _StrictFailingManager(strict_shared=True)
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            raise AssertionError("private stdio must not be started")

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/usr/bin/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)

        with pytest.raises(
            RuntimeError,
            match="strict daemon preparation failed",
        ):
            await CodexHandle(
                _Cfg(), daemon_manager=manager).connect(cwd="/tmp")

        assert manager.proxy_calls == 1
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
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
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


def test_oversized_http_resume_keeps_shared_affinity_and_thread_local_provider(
        monkeypatch):
    async def run():
        nonce = b"0123456789abcdef"
        monkeypatch.setattr(handle_module.os, "urandom", lambda _size: nonce)
        monkeypatch.setattr(
            handle_module,
            "_oversized_desktop_openai_resume_requires_http",
            lambda _sid: True,
        )
        monkeypatch.setattr(
            handle_module,
            "_newer_private_core_for_oversized_resume",
            lambda _bin, _sid: "/Applications/Codex.app/Resources/codex",
        )
        manager = _Manager(["/managed/codex", "app-server", "proxy"])
        spawned = []

        async def spawn(*argv, **_kwargs):
            spawned.append(list(argv))
            return _Process(
                _Reader(_handshake_response(nonce)), 50004 + len(spawned))

        monkeypatch.setattr(
            handle_module, "_resolve_codex_bin", lambda: "/managed/codex")
        monkeypatch.setattr(
            handle_module.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(handle_module.os, "killpg", lambda *_args: None)
        handle = CodexHandle(_Cfg(), daemon_manager=manager)
        resume_params = []

        async def idle(*_args):
            await asyncio.Event().wait()

        async def request(method, params=None):
            if method == "initialize":
                return {"userAgent": "codex_cli_rs/0.147.0 (test)"}
            if method == "thread/resume":
                resume_params.append(params)
                return {"thread": {"id": "oversized-thread"}}
            raise AssertionError(method)

        handle._read_loop = idle  # type: ignore[method-assign]
        handle._request = request  # type: ignore[method-assign]
        handle._notify = lambda *_args: asyncio.sleep(0)  # type: ignore[method-assign]
        handle._restore_http_provider_state = (  # type: ignore[method-assign]
            lambda **_kwargs: asyncio.sleep(0)
        )
        await handle.connect(resume_id="oversized-thread", cwd="/tmp")

        assert spawned == [["/managed/codex", "app-server", "proxy"]]
        assert handle.using_daemon_proxy is True
        assert handle.shared_daemon_affinity is True
        expected_resume = {
            "threadId": "oversized-thread",
            "cwd": "/tmp",
            "modelProvider": handle_module._OPENAI_HTTP_RESUME_PROVIDER_ID,
            "config": handle_module._openai_http_resume_thread_config(),
            "excludeTurns": True,
        }
        assert resume_params == [expected_resume]
        await handle.disconnect()

        # Reconnecting an already-shared thread must retain its HTTP transport
        # override while remaining on the shared daemon.  Shared affinity only
        # disables the private-core fallback, not HTTP-provider detection.
        await handle.connect(resume_id="oversized-thread", cwd="/tmp")
        assert spawned == [
            ["/managed/codex", "app-server", "proxy"],
            ["/managed/codex", "app-server", "proxy"],
        ]
        assert handle.using_daemon_proxy is True
        assert resume_params == [expected_resume, expected_resume]
        await handle.disconnect()

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
