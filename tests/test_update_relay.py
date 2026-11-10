"""Offline end-to-end updater coordination; no SSH or live service mutations."""
import json

import pytest

from cc_remote import update_relay
from cc_remote import update as updater
from cc_remote.__main__ import main
from cc_remote.update import UpdateError
from tests.test_update import _bundle, _installation


def launch_request(command):
    if len(command) == 7 and command[:2] == ["sudo", "-n"] and command[3:5] == ["-I", "-c"]:
        return json.loads(command[-1])
    return None


@pytest.mark.parametrize("relay_current", [False, True])
def test_device_update_updates_relay_first_or_skips_it_when_current(tmp_path, monkeypatch, relay_current):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.1" if relay_current else "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    calls = []
    phases = []

    def ssh(self, command, **kwargs):
        assert not marker.exists(), "device was activated before the Relay"
        assert "ready" not in phases, "Relay I/O must not reserve the local Wrapper"
        calls.append(command)
        if request := launch_request(command):
            assert request["version"] == "4.0.1"
            remote["version"] = "4.0.1"
            return '{"status":"submitted"}'
        if "-c" in command:
            return json.dumps({"domain": "remote.example.test", "version": remote["version"]})
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert updater.update(role="wrapper", target_version="4.0.1", check=False,
                          relay_ssh="operator@relay", progress=phases.append) == 0
    assert phases == ["downloading", "validating", "updating_relay", "ready", "installing", "verifying"]
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
        if launch_request(command):
            launches.append(command)
            raise UpdateError("SSH acknowledgement lost")
        if "-c" in command:
            return '{"domain":"remote.example.test","version":"4.0.0"}'
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


def test_ssh_failure_before_remote_submission_is_retried_with_the_same_identity(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    monkeypatch.setattr(update_relay.time, "sleep", lambda _: None)
    requests = []

    def ssh(self, command, **kwargs):
        if request := launch_request(command):
            if request.get("operation") == "inspect":
                return '{"status":"missing"}'
            requests.append(request)
            if len(requests) == 1:
                raise UpdateError("SSH connection refused before command delivery")
            remote["version"] = "4.0.1"
            return '{"status":"submitted"}'
        if "-c" in command:
            return '{"domain":"remote.example.test"}'
        if remote["version"] == "4.0.0":
            return "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 1
    assert not marker.exists()
    pending = json.loads((installation.root / "upstream-update.json").read_text())
    assert pending["launch_receipt"] == 1 and pending["complete"] is False
    assert main(["update", "--version", "4.0.1"]) == 0
    assert requests[0] == requests[1]
    assert marker.exists()


@pytest.mark.parametrize("failure", ["before-delivery", "lost-ack", "status-disconnect"])
def test_transient_ssh_failure_finishes_original_transaction_without_user_retry(tmp_path, monkeypatch, failure):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    monkeypatch.setattr(update_relay.time, "sleep", lambda _: None)
    requests, activations, statuses = [], [], []

    def ssh(self, command, **kwargs):
        if request := launch_request(command):
            if request.get("operation") == "inspect":
                return '{"status":"missing"}'
            requests.append(request)
            if failure == "before-delivery" and len(requests) == 1:
                raise update_relay.RelayConnectionError("SSH connection reset")
            activations.append(request["unit"])
            remote["version"] = "4.0.1"
            if failure == "lost-ack" and len(requests) == 1:
                raise update_relay.RelayConnectionError("SSH acknowledgement lost")
            return '{"status":"submitted"}'
        if "-c" in command:
            return '{"domain":"remote.example.test"}'
        statuses.append(command)
        if failure == "status-disconnect" and len(statuses) == 1:
            raise update_relay.RelayConnectionError("SSH status response lost")
        if not activations:
            return "LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 0
    assert marker.exists() and len(activations) == 1
    assert len(requests) == (2 if failure == "before-delivery" else 1)
    assert all(request == requests[0] for request in requests)
    assert json.loads((installation.root / "upstream-update.json").read_text())["complete"]


def test_legacy_missing_job_is_not_guessed_to_have_never_started(tmp_path, monkeypatch, capsys):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    transaction = {"unit": "cc-remote-update-" + "a" * 32, "origin": "https://remote.example.test",
                   "ssh": "relay", "version": "4.0.1", "protocol": 72, "complete": False}
    journal = installation.root / "upstream-update.json"
    journal.write_text(json.dumps(transaction))
    before = journal.read_bytes()

    def ssh(self, command, **kwargs):
        assert "systemctl" in command and not launch_request(command)
        return "LoadState=not-found\n"

    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 1
    assert "no server launch receipt" in capsys.readouterr().err
    assert journal.read_bytes() == before and not marker.exists()


@pytest.mark.parametrize("failure", ["wrong-host", "failed-job", "unknown-job", "no-access"])
def test_failed_upstream_verification_never_activates_device(tmp_path, monkeypatch, failure):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    monkeypatch.setattr(update_relay, "relay_release", lambda _: {"version": "4.0.0", "protocol": 72})

    def ssh(self, command, **kwargs):
        if failure == "no-access":
            raise UpdateError("no SSH access")
        if launch_request(command):
            return json.dumps({"status": "uncertain" if failure == "unknown-job" else "submitted"})
        if "-c" in command:
            return json.dumps({"domain": "wrong.example" if failure == "wrong-host" else "remote.example.test"})
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


def test_verified_server_rollback_finishes_old_intent_and_allows_real_retry(tmp_path, monkeypatch):
    installation = _installation(tmp_path, monkeypatch)
    mirror, marker, _ = _bundle(tmp_path, installation)
    monkeypatch.setenv("CC_REMOTE_RELEASE_BASE_URL", mirror.as_uri())
    remote = {"version": "4.0.0", "protocol": 72}
    monkeypatch.setattr(update_relay, "relay_release", lambda _: dict(remote))
    monkeypatch.setattr(update_relay.time, "sleep", lambda _: None)
    launches = []
    def ssh(self, command, **kwargs):
        if request := launch_request(command):
            if request.get("operation") == "inspect":
                return '{"status":"failed_safe"}'
            launches.append(request["unit"])
            return '{"status":"submitted"}'
        if "-c" in command:
            return '{"domain":"remote.example.test"}'
        if len(launches) == 1:
            return "LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\nExecMainStatus=1\n"
        remote["version"] = "4.0.1"
        return "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n"
    monkeypatch.setattr(update_relay.RelayUpdate, "_ssh", ssh)
    assert main(["update", "--version", "4.0.1", "--relay-ssh", "relay"]) == 1
    original = json.loads((installation.root / "upstream-update.json").read_text())
    assert original["complete"] and original["outcome"] == "failed"
    assert not marker.exists()
    assert main(["update", "--version", "4.0.1"]) == 0
    assert len(set(launches)) == 2 and marker.exists()
