"""Install-time sharing checks use local fixtures, never a model or live daemon."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import plistlib
import socket
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest
from websockets.asyncio.server import unix_serve

from cc_remote.wrapper import codex_daemon as daemon
from cc_remote.wrapper import codex_readiness as readiness
from deploy import check_codex_readiness as installer


def lifecycle(home, **changes):
    return {
        "status": "running", "cliVersion": "0.154.0", "appServerVersion": "0.154.0",
        "managedCodexPath": "/opt/codex", "managedCodexVersion": "0.154.0",
        "socketPath": str(home / "app-server-control/app-server-control.sock"), **changes,
    }


@pytest.mark.parametrize("available", [True, False])
@pytest.mark.parametrize("require_shared", [True, False])
def test_non_disruptive_setup_never_restarts_or_enables_cloud_control(
    tmp_path, monkeypatch, available, require_shared,
):
    calls = []
    manager = daemon.CodexDaemonManager(allow_restart=False, require_shared=require_shared)
    async def run(binary, env, *args):
        calls.append(args)
        assert args[-1] in {"--help", "start", "version"}
        data = lifecycle(tmp_path, appServerVersion="0.153.0") if available else None
        return daemon._CommandResult(0 if available or args[-1] == "--help" else 1,
                                     json.dumps(data).encode(), b"")
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(daemon, "_prepare_profile_standalone", lambda *_: None)
    monkeypatch.setattr(daemon, "_managed_daemon_process_identity", lambda _: None)
    async def check():
        if require_shared and not available:
            with pytest.raises(daemon.CodexProfileDaemonUnavailable):
                await manager.ensure_started("/opt/codex", {"CODEX_HOME": str(tmp_path)})
            return
        result = await manager.ensure_started("/opt/codex", {"CODEX_HOME": str(tmp_path)})
        assert (result is not None) == available
        if available:
            assert manager.strict_shared_affinity
            assert result.verified_remote_control is False
    asyncio.run(check())
    assert sum(command[-1] == "start" for command in calls) == int(not available)


def test_non_disruptive_first_start_reuses_official_lifecycle(tmp_path, monkeypatch):
    manager = daemon.CodexDaemonManager(allow_restart=False)
    calls = []
    started = False
    async def run(binary, env, *args):
        nonlocal started
        calls.append(args[-1])
        if args[-1] == "start":
            started = True
        data = lifecycle(tmp_path) if started else None
        return daemon._CommandResult(0 if started or args[-1] == "--help" else 1,
                                     json.dumps(data).encode(), b"")
    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(daemon, "_managed_daemon_process_identity", lambda _: None)
    result = asyncio.run(manager.ensure_started("/opt/codex", {"CODEX_HOME": str(tmp_path)}))
    assert result is not None
    assert calls == ["--help", "--help", "version", "start", "version"]


@pytest.fixture
def account_socket():
    # macOS Unix paths are limited to 104 bytes; pytest's path can exceed that.
    with tempfile.TemporaryDirectory(prefix="cc-readiness-", dir="/tmp") as directory:
        home = Path(directory).resolve()
        path = home / "app-server-control/app-server-control.sock"
        path.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            listener.listen()
            yield home, path


@pytest.mark.parametrize("case,reason", [
    ("ready", None), ("missing", "daily_cli_missing"),
    ("other_account", "daily_cli_mismatch"), ("wrong_wrapper", "account_socket_mismatch"),
    ("old_cli", "version_mismatch"), ("old_server", "version_mismatch"),
    ("denied", "connection_failed"), ("swapped", "daemon_changed"),
])
def test_profile_checks_endpoint_versions_and_real_handshake(
    monkeypatch, account_socket, case, reason,
):
    home, path = account_socket
    probes = []
    class Manager:
        async def ensure_started(self, binary, env):
            assert env["CODEX_HOME"] == str(home)
            return SimpleNamespace(socket_path=str(path) if case != "wrong_wrapper" else "/other/socket")
        async def version(self, binary, env):
            data = lifecycle(home)
            if case == "other_account" and binary == "/daily/codex":
                data["socketPath"] = "/other/socket"
            if case == "old_cli" and binary == "/daily/codex":
                data["cliVersion"] = "0.153.0"
            if case == "old_server":
                data["appServerVersion"] = "0.153.0"
            return data
    async def probe(binary, env, selected):
        probes.append(binary)
        assert selected == str(path)
        if case == "denied":
            raise RuntimeError("secret-do-not-publish")
    monkeypatch.setattr(readiness, "probe_proxy", probe)
    if case == "swapped":
        identities = iter([(1, 2, 3), (1, 4, 5)])
        monkeypatch.setattr(readiness, "socket_identity", lambda _: next(identities))
    row = asyncio.run(readiness.check_profile(
        "account", str(home), "/wrapper/codex", None if case == "missing" else "/daily/codex",
        {"CODEX_HOME": str(home)}, Manager(),
    ))
    assert row.get("reason") == reason
    assert row["terminal_connection"] == "unverified"
    assert "secret-do-not-publish" not in json.dumps(row)
    if case == "ready":
        assert row["status"] == "ready"
        assert probes == ["/wrapper/codex", "/daily/codex"]


@pytest.mark.parametrize("mode", ["success", "reject", "hang", "oversized", "eof"])
def test_proxy_probe_only_initializes_and_reaps_child(tmp_path, monkeypatch, mode):
    binary = tmp_path / "codex"
    messages = []
    pid_file = tmp_path / "pid"
    binary.write_text(f"""#!{sys.executable}
import os, pathlib, selectors, socket, sys
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))
assert sys.argv[1:4] == ['app-server', 'proxy', '--sock']
peer = socket.socket(socket.AF_UNIX)
peer.connect(sys.argv[4])
poll = selectors.DefaultSelector()
poll.register(0, selectors.EVENT_READ)
poll.register(peer, selectors.EVENT_READ)
while True:
    for key, _ in poll.select():
        data = os.read(key.fd, 65536)
        if not data: sys.exit(0)
        if key.fd == 0: peer.sendall(data)
        else:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
""")
    binary.chmod(0o700)
    monkeypatch.setattr(readiness, "_TIMEOUT", 0.2 if mode == "hang" else 3)
    async def handler(ws):
        request = json.loads(await ws.recv())
        messages.append(request["method"])
        assert request["method"] == "initialize"
        if mode == "hang":
            await ws.wait_closed()
            return
        if mode == "eof":
            await ws.close()
            return
        if mode == "oversized":
            await ws.send("x" * 300000)
        elif mode == "reject":
            await ws.send(json.dumps({"id": 1, "error": {"message": "private"}}))
        else:
            # Exercise ping and fragmentation, not just a synthetic JSON pipe.
            await ws.ping()
            response = json.dumps({"id": 1, "result": {"userAgent": "fixture"}})
            await ws.send([response[:12], response[12:]])
            messages.append(json.loads(await ws.recv())["method"])
        await ws.wait_closed()
    async def check():
        with tempfile.TemporaryDirectory(prefix="cc-proxy-", dir="/tmp") as directory:
            path = str(Path(directory) / "socket")
            async with unix_serve(handler, path, close_timeout=0.2):
                if mode == "success":
                    await readiness.probe_proxy(str(binary), dict(os.environ), path)
                else:
                    with pytest.raises((RuntimeError, ValueError, TimeoutError)):
                        await readiness.probe_proxy(str(binary), dict(os.environ), path)
    asyncio.run(check())
    assert messages == (
        ["initialize", "initialized"] if mode == "success" else ["initialize"])
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_installer_rejects_previous_activation_dead_or_reused_pid(tmp_path, monkeypatch):
    before = time.time()
    readiness.write_report(tmp_path, [{"profile": "test", "status": "disabled"}])
    path = tmp_path / readiness.REPORT_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    assert installer.read_receipt(path, readiness.SOURCE_ROOT, before)
    assert installer.read_receipt(path, tmp_path / "another-release", before) is None
    assert installer.read_receipt(path, readiness.SOURCE_ROOT, time.time() + 1) is None
    monkeypatch.setattr(installer, "process_identity", lambda _: None)
    assert installer.read_receipt(path, readiness.SOURCE_ROOT, before) is None


def test_installer_rejects_symlink_and_bounds_receipt(tmp_path):
    target = tmp_path / "target"
    target.write_text("x" * (64 * 1024 + 1))
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        installer.read_receipt(link, tmp_path, 0)
    with pytest.raises(ValueError):
        installer.read_receipt(target, tmp_path, 0)


def test_installer_checks_listener_again_before_printing_ready(tmp_path, account_socket):
    home, path = account_socket
    row = {"profile": "account", "status": "ready", "socket": str(path),
           "socket_identity": list(readiness.socket_identity(str(path)))}
    before = time.time()
    readiness.write_report(tmp_path, [row])
    receipt = tmp_path / readiness.REPORT_NAME
    assert installer.read_receipt(receipt, readiness.SOURCE_ROOT, before)["profiles"][0]["status"] == "ready"
    path.unlink()
    result = installer.read_receipt(receipt, readiness.SOURCE_ROOT, before)
    assert result["profiles"][0]["status"] == "unavailable"
    assert result["profiles"][0]["reason"] == "daemon_changed"


@pytest.mark.parametrize("kind", ["plist", "env-file"])
def test_installer_uses_service_state_root_without_executing_config(tmp_path, kind, capsys):
    state = tmp_path / "custom state"
    before = time.time()
    readiness.write_report(state, [{"profile": "user", "status": "disabled"}])
    config = tmp_path / "config"
    if kind == "plist":
        config.write_bytes(plistlib.dumps({"EnvironmentVariables": {"CC_REMOTE_STATE_DIR": str(state)}}))
    else:
        config.write_text(f'CC_REMOTE_STATE_DIR="{state}"\nIGNORED=$(touch /bad)\n')
    assert installer.main([
        "--home", str(tmp_path), "--release", str(readiness.SOURCE_ROOT),
        "--after", str(before), f"--{kind}", str(config), "--wait", "0",
    ]) == 0
    assert "保留已有的关闭设置" in capsys.readouterr().out


def test_install_report_keeps_cli_discovery_unverified_and_errors_separate(capsys):
    assert not installer.describe({"profiles": [
        {"profile": "good", "status": "ready"},
        {"profile": "other", "status": "unavailable", "reason": "version_mismatch"},
    ]})
    output = capsys.readouterr().out
    assert "未发送模型消息" in output
    assert "实际连接仍需确认" in output
    assert "版本不一致" in output


def test_missing_receipt_does_not_pass_acceptance(tmp_path, capsys):
    assert installer.main([
        "--home", str(tmp_path), "--release", str(tmp_path), "--after", "0", "--wait", "0",
    ]) == 1
    assert "不能据此确认已共享" in capsys.readouterr().out
