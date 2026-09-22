"""The downloaded installer must also coordinate an old updater's first upgrade."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from cc_remote import update_relay
from tests.test_update import _installation, _manifest


@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("relay_current", [False, True])
def test_bundle_preflight_updates_relay_before_touching_local_service(
    tmp_path, monkeypatch, registered, relay_current,
):
    installation = _installation(tmp_path, monkeypatch)
    if not registered:
        (installation.root / "installation.json").unlink()
    bundle = tmp_path / "downloaded"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.2")))
    remote = {"version": "4.0.2" if relay_current else "4.0.1", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    monkeypatch.setenv("CC_REMOTE_RELAY_SSH", "operator@relay")
    calls = []

    def ssh(self, command, **kwargs):
        assert (installation.root / "current").resolve() == installation.release
        assert (installation.root / "native-service-alive").read_text() == "still running\n"
        calls.append(command)
        if "-c" in command:
            return '{"domain":"remote.example.test"}'
        if "systemd-run" in command:
            assert command[-2:] == ["--version", "4.0.2"]
            remote["version"] = "4.0.2"
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert update_relay.installer_main([
        "--root", str(installation.root), "--bundle", str(bundle), "--user", "service-user",
    ]) == 0
    assert bool(calls) is not relay_current
    assert (installation.root / "current").resolve() == installation.release
    assert (installation.root / "installation.json").exists() is registered


@pytest.mark.parametrize("outcome", ["current", "unreachable", "protocol-change"])
def test_real_installer_block_runs_new_bundle_preflight_before_local_stop(tmp_path, outcome):
    root = tmp_path / "installed"
    previous = root / "releases/old"
    previous.mkdir(parents=True)
    (root / "current").symlink_to(previous)
    (previous / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.1")))
    target = root / "releases/new"
    (target / ".venv/bin").mkdir(parents=True)
    (target / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.2")))
    if outcome == "protocol-change":
        manifest = _manifest(version="4.0.2")
        manifest["protocol_version"] = 73
        (target / "release-manifest.json").write_text(json.dumps(manifest))
    repo = Path(__file__).resolve().parents[1]
    driver = tmp_path / "new-bundle.py"
    driver.write_text(
        f"import sys; sys.path.insert(0, {str(repo)!r})\n"
        "from cc_remote import update_relay as m\n"
        "m.relay_origin = lambda _: 'https://remote.example.test'\n"
        f"outcome = {outcome!r}\n"
        "def read(origin):\n"
        "    if outcome == 'unreachable': raise m.UpdateError('Relay unavailable')\n"
        "    return {'version': '4.0.2', 'protocol': 72}\n"
        "m.relay_release = read\n"
        "raise SystemExit(m.installer_main(sys.argv[1:]))\n"
    )
    python = target / ".venv/bin/python"
    python.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-m" ] && [ "$2" = "cc_remote.update_relay" ] || exit 98\n'
        f"shift 2\nexec {shlex.quote(sys.executable)} {shlex.quote(str(driver))} \"$@\"\n"
    )
    python.chmod(0o755)
    source = (repo / "deploy/install-wrapper.sh").read_text()
    start = source.index("# Older update commands")
    stop = source.index("# Mutable Work metadata", start)
    assert stop < source.index('if [ "$service_was_running" -eq 1 ]; then', stop)
    settings = {"previous": str(previous), "pair_code": "", "appdir": str(root),
                "target": str(target), "target_user": "service-user", "system": "darwin",
                "service_label": "org.example.wrapper", "relay_ssh": "", "allow_protocol_change": "0"}
    marker = tmp_path / "local-service-stopped"
    # A v4.0.1 caller has no new CLI flags or coordination environment marker.
    harness = "set -euo pipefail\n" + "\n".join(
        f"{key}={shlex.quote(value)}" for key, value in settings.items()
    ) + "\n" + source[start:stop] + f"touch {shlex.quote(str(marker))}\n"
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={key: value for key, value in os.environ.items()
                                 if key != "CC_REMOTE_RELAY_SSH"}, timeout=10)
    assert result.returncode == (0 if outcome == "current" else 1), result.stderr
    assert marker.exists() is (outcome == "current")
    assert (root / "current").resolve() == previous


def test_first_upgrade_can_request_an_existing_ssh_target(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    monkeypatch.delenv("CC_REMOTE_RELAY_SSH", raising=False)
    relay = update_relay.RelayUpdate(installation)
    remote = {"version": "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    monkeypatch.setattr(update_relay.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "operator@relay")
    def verify():
        assert relay.target == "operator@relay"
    monkeypatch.setattr(relay, "_verify_host", verify)
    monkeypatch.setattr(relay, "_ssh", lambda *args, **kwargs: "")
    monkeypatch.setattr(relay, "_finish", lambda _: remote.update(version="4.0.1"))
    relay.ensure("4.0.1", 72, allow_protocol_change=False)
    assert json.loads(relay.settings.read_text()) == {"relay_ssh": "operator@relay"}
