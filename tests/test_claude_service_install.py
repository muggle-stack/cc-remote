"""Validate service syntax with the system manager that consumes it."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.install_claude_service import unit_text
from deploy import claude_service_upgrade


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("systemd-analyze"),
                    reason="systemd unit verification requires Linux")
def test_service_unit_with_spaces_is_accepted_by_systemd(tmp_path):
    source = tmp_path / "release with spaces"
    source.mkdir()
    state = tmp_path / "state with spaces"
    state.mkdir()
    unit = tmp_path / "claude-test.service"
    unit.write_text(unit_text(source, state))
    result = subprocess.run(["systemd-analyze", "verify", str(unit)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("failure", [None, "new-handshake", "new-exited", "old-busy"])
def test_upgrade_preserves_service_binding_and_restores_original_runtime(tmp_path, monkeypatch, platform, failure):
    old = tmp_path / "old release"
    target = tmp_path / "new release"
    socket = tmp_path / "private" / "service.sock"
    socket.parent.mkdir(mode=0o700)
    monkeypatch.setattr(claude_service_upgrade.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    service = claude_service_upgrade.Service(str(socket))
    service.path.parent.mkdir(parents=True)
    old_definition = service.definition(old)
    service.path.write_bytes(old_definition)
    pid, runtime, registered = 1, old, True
    calls = []

    def manager(*args):
        nonlocal pid, runtime, registered
        if args[0] == "print":
            if not registered:
                return subprocess.CompletedProcess(args, 113, stdout="")
            stdout = f"\tpath = {service.path}\n\tpid = {pid}\n"
        elif args[0] == "show":
            stdout = f"LoadState=loaded\nMainPID={pid}\nFragmentPath={service.path}\nDropInPaths=\n"
        elif args[0] in {"bootout", "stop"}:
            calls.append("stop")
            if platform == "darwin":
                registered = False
            pid, stdout = 0, ""
        elif args[0] in {"bootstrap", "start"}:
            if platform == "darwin":
                assert not registered
                registered = True
            runtime = old if service.path.read_bytes() == old_definition else target
            calls.append("start")
            pid, stdout = (0 if runtime == target and failure == "new-exited" else 2), ""
        else:
            assert args[0] == "daemon-reload"
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout)

    monkeypatch.setattr(claude_service_upgrade, "manager", manager)
    monkeypatch.setattr(claude_service_upgrade.Service, "probe",
                        lambda self, source, sdk, *, pid, require_idle=False:
                        source == runtime and pid > 0 and not (require_idle and failure == "old-busy"))

    def wait(_self, source, _sdk):
        if failure in {"new-handshake", "new-exited"} and source == target:
            raise claude_service_upgrade.MigrationError("new service failed readiness")
        assert runtime == source

    monkeypatch.setattr(claude_service_upgrade.Service, "wait", wait)
    current = {"socket": str(socket), "source": str(old), "pid": 1, "sdk": "0.2.174"}
    if failure == "old-busy":
        with pytest.raises(claude_service_upgrade.MigrationError, match="unfinished work"):
            claude_service_upgrade.migrate("switch", current, target, "0.2.184")
        claude_service_upgrade.migrate("restore", current, target, "0.2.184")
        assert calls == [] and service.path.read_bytes() == old_definition
    elif failure in {"new-handshake", "new-exited"}:
        with pytest.raises(claude_service_upgrade.MigrationError, match="failed readiness"):
            claude_service_upgrade.migrate("switch", current, target, "0.2.184")
        claude_service_upgrade.migrate("restore", current, target, "0.2.184")
        claude_service_upgrade.migrate("restore", current, target, "0.2.184")
        assert calls == (["stop", "start", "start"] if platform == "linux" and failure == "new-exited"
                         else ["stop", "start", "stop", "start"])
        assert service.path.read_bytes() == old_definition
    else:
        claude_service_upgrade.migrate("plan", current, target, "0.2.184")
        assert calls == []
        claude_service_upgrade.migrate("switch", current, target, "0.2.184")
        claude_service_upgrade.migrate("verify", current, target, "0.2.184")
        assert calls == ["stop", "start"]
        assert service.path.read_bytes() == service.definition(target)
