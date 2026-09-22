"""Offline end-to-end updater coordination; no SSH or live service mutations."""
import json

import pytest

from cc_remote import update_relay
from cc_remote.__main__ import main
from cc_remote.update import UpdateError
from tests.test_update import _bundle, _installation


@pytest.mark.parametrize("relay_current", [False, True])
def test_device_update_updates_relay_first_or_skips_it_when_current(tmp_path, monkeypatch, relay_current):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.1" if relay_current else "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    calls = []

    def ssh(self, command, **kwargs):
        assert not marker.exists(), "device was activated before the Relay"
        calls.append(command)
        if "-c" in command:
            return json.dumps({"domain": "remote.example.test", "version": remote["version"]})
        if "systemd-run" in command:
            assert command[-5:] == ["update", "--role", "relay", "--version", "4.0.1"]
            remote["version"] = "4.0.1"
            return ""
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "operator@relay"]) == 0
    assert marker.exists()
    assert bool(calls) is not relay_current
    assert json.loads((installation.root / "update.json").read_text()) == {"relay_ssh": "operator@relay"}
    if not relay_current:
        assert json.loads((installation.root / "upstream-update.json").read_text())["complete"]


def test_interrupted_upstream_update_rechecks_the_same_job_without_relaunch(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    launches = []

    def ssh(self, command, **kwargs):
        if "-c" in command:
            return '{"domain":"remote.example.test","version":"4.0.0"}'
        if "systemd-run" in command:
            launches.append(command)
            raise UpdateError("SSH acknowledgement lost")
        remote["version"] = "4.0.1"
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 1
    assert not marker.exists()
    transaction = json.loads((installation.root / "upstream-update.json").read_text())
    assert transaction["complete"] is False
    assert main(["update", "--version", "4.0.1"]) == 0
    assert len(launches) == 1
    assert marker.exists()


@pytest.mark.parametrize("failure", ["wrong-host", "failed-job", "unknown-job", "no-access"])
def test_failed_upstream_verification_never_activates_device(tmp_path, monkeypatch, failure):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    monkeypatch.setattr(update_relay, "relay_release", lambda _: {"version": "4.0.0", "protocol": 72})

    def ssh(self, command, **kwargs):
        if failure == "no-access":
            raise UpdateError("no SSH access")
        if "-c" in command:
            return json.dumps({"domain": "wrong.example" if failure == "wrong-host" else "remote.example.test"})
        if "systemd-run" in command:
            return ""
        return "LoadState=not-found\n" if failure == "unknown-job" else "LoadState=loaded\nActiveState=failed\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 1
    assert not marker.exists()
    assert (installation.root / "current").resolve() == installation.release


def test_current_device_still_checks_upstream_and_check_mode_never_runs_ssh(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    monkeypatch.setattr(update_relay, "relay_release", lambda _: {"version": "3.9.9", "protocol": 72})
    before = set(installation.root.iterdir())
    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", lambda *a, **kw: pytest.fail("unexpected SSH"))
    assert main(["update", "--check", "--version", "4.0.0", "--relay-ssh", "relay"]) == 0
    assert set(installation.root.iterdir()) == before
    monkeypatch.setattr(update_relay.RelayUpdate, "_verify_host", lambda _: (_ for _ in ()).throw(UpdateError("need SSH")))
    assert main(["update", "--version", "4.0.0"]) == 1


@pytest.mark.parametrize("target", ["-oProxyCommand=bad", "host;touch /tmp/no", "user@host\ncmd", "ssh://host"])
def test_ssh_target_is_data_not_shell_syntax(tmp_path, monkeypatch, target):
    installation = _installation(tmp_path, monkeypatch)
    with pytest.raises(UpdateError, match="SSH host alias"):
        update_relay.RelayUpdate(installation, target)


def test_legacy_relay_requires_web_and_protocol_to_agree(monkeypatch):
    payloads = {"/healthz": {"ok": True, "protocol": 72},
                "/cc-remote-build.json": {"version": "4.0.1", "protocol": 72}}
    monkeypatch.setattr(update_relay, "_get_json", lambda url: payloads[url.removeprefix("https://relay")])
    assert update_relay.relay_release("https://relay") == {"version": "4.0.1", "protocol": 72}
    payloads["/cc-remote-build.json"]["protocol"] = 73
    with pytest.raises(UpdateError, match="inconsistent"):
        update_relay.relay_release("https://relay")
