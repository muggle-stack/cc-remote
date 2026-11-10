"""Durable launch receipts; all systemd calls are offline fixtures."""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from deploy import relay_update_job as job


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()
    (root / "installation.json").write_text(json.dumps({"role": "relay", "domain": "relay.example.test"}))
    request = {"unit": "cc-remote-update-" + "a" * 32, "version": "5.1.0", "protocol": 83,
               "domain": "relay.example.test", "allow_protocol_change": False}
    state = {"loaded": False, "launches": 0, "lost_ack": False}

    def run(command, **kwargs):
        if command[0] == "systemctl":
            output = "LoadState=loaded\nActiveState=active\nSubState=running\n" if state["loaded"] else (
                "LoadState=not-found\nActiveState=inactive\nSubState=dead\n")
            return SimpleNamespace(returncode=0, stdout=output)
        assert command[0] == "systemd-run"
        assert command[command.index("--unit") + 1] == request["unit"]
        assert command[-5:] == ["update", "--role", "relay", "--version", request["version"]]
        receipt = root / "update-jobs" / (request["unit"] + ".json")
        assert json.loads(receipt.read_text())["phase"] == "submitting"
        state["launches"] += 1
        state["loaded"] = True
        if state["lost_ack"]:
            raise subprocess.TimeoutExpired(command, 20)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(job, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    return root, request, state


def test_repeated_submission_starts_one_job_and_binds_the_receipt(server):
    root, request, state = server
    assert job.submit(root, request) == {"status": "submitted"}
    assert job.submit(root, request) == {"status": "existing"}
    assert state["launches"] == 1
    receipt = root / "update-jobs" / (request["unit"] + ".json")
    assert json.loads(receipt.read_text())["request"] == request
    assert receipt.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="does not match"):
        job.submit(root, {**request, "version": "5.2.0"})
    assert state["launches"] == 1


def test_verified_unchanged_server_fences_failed_request_and_allows_new_attempt(server, monkeypatch):
    root, request, state = server
    previous = root / "releases/old"
    previous.mkdir(parents=True)
    (previous / "release-manifest.json").write_text(json.dumps({"product_version": "5.0.0"}))
    (root / "current").symlink_to(previous)
    assert job.submit(root, request)["status"] == "submitted"
    path = root / "update-jobs" / (request["unit"] + ".json")
    prior = json.loads(path.read_text())
    monkeypatch.setattr(job, "_server_health", lambda release, origin: release == previous)
    result = job.inspect_original(root, request, prior,
                                  {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed"}, path)
    assert result == {"status": "failed_safe"}
    assert job.submit(root, request) == result
    assert state["launches"] == 1, "late original requests must not relaunch"
    request["unit"] = "cc-remote-update-" + "b" * 32
    state["loaded"] = False
    assert job.submit(root, request)["status"] == "submitted"
    assert state["launches"] == 2


@pytest.mark.parametrize("lost_ack", [False, True])
def test_removed_unit_or_reboot_does_not_erase_launch_evidence(server, lost_ack):
    root, request, state = server
    state["lost_ack"] = lost_ack
    assert job.submit(root, request)["status"] == ("uncertain" if lost_ack else "submitted")
    assert job.submit(root, request) == {"status": "existing"}
    state["loaded"] = False  # systemd transient units can disappear on reboot.
    assert job.submit(root, request) == {"status": "uncertain"}
    assert state["launches"] == 1


def test_existing_unit_without_receipt_is_remembered_before_it_can_disappear(server):
    root, request, state = server
    state["loaded"] = True
    assert job.submit(root, request) == {"status": "existing"}
    state["loaded"] = False
    assert job.submit(root, request) == {"status": "uncertain"}
    assert state["launches"] == 0


def test_late_first_request_and_retry_share_one_launch_lock(server, monkeypatch):
    root, request, state = server
    entered, release = threading.Event(), threading.Event()
    run = job.subprocess.run

    def delayed(command, **kwargs):
        if command[0] == "systemd-run":
            entered.set()
            assert release.wait(5)
        return run(command, **kwargs)

    monkeypatch.setattr(job.subprocess, "run", delayed)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(job.submit, root, request)
        try:
            assert entered.wait(5)
            with pytest.raises(BlockingIOError):
                job.submit(root, request)
        finally:
            release.set()
        assert first.result(timeout=5) == {"status": "submitted"}
    assert job.submit(root, request) == {"status": "existing"}
    assert state["launches"] == 1


@pytest.mark.parametrize("failure", ["inspection", "launch"])
def test_remote_command_errors_do_not_claim_a_submission_succeeded(server, monkeypatch, failure):
    root, request, state = server
    run = job.subprocess.run

    def failing(command, **kwargs):
        if command[0] == ("systemctl" if failure == "inspection" else "systemd-run"):
            return SimpleNamespace(returncode=1, stdout="", stderr="private helper secret")
        return run(command, **kwargs)

    monkeypatch.setattr(job.subprocess, "run", failing)
    result = job.submit(root, request)
    assert result["status"] == ("unavailable" if failure == "inspection" else "launch-error")
    assert "private" not in json.dumps(result)
    monkeypatch.setattr(job.subprocess, "run", run)
    # Only failure before any submission intent may be retried automatically.
    assert job.submit(root, request)["status"] == ("submitted" if failure == "inspection" else "uncertain")


@pytest.mark.parametrize("target", ["directory", "lock", "receipt"])
def test_receipt_symlinks_are_not_followed(server, tmp_path, target):
    root, request, state = server
    outside = tmp_path / "outside"
    folder = root / "update-jobs"
    if target == "directory":
        outside.mkdir()
        folder.symlink_to(outside)
    else:
        folder.mkdir(mode=0o700)
        outside.write_text("keep me")
        (folder / (request["unit"] + (".lock" if target == "lock" else ".json"))).symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        job.submit(root, request)
    assert state["launches"] == 0
    if outside.is_file():
        assert outside.read_text() == "keep me"


def test_launcher_program_needs_only_the_old_relays_standard_library():
    script = Path(job.__file__).read_text()
    result = subprocess.run([sys.executable, "-I", "-c", script, "{}"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "unavailable"}
    assert not result.stderr
