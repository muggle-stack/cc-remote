"""Intentional Codex daemon restart barrier tests (no daemon/model calls)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from cc_remote import codex_daemon_restart as restart_module
from cc_remote.codex_daemon_restart import (
    _DEFAULT_DRAIN_GRACE,
    _DEFAULT_WORKER_TIMEOUT,
    _codex_binary,
    _scheduled_worker,
    read_restart_state,
    restart_managed_daemon,
    restart_outcome_timeout,
    restart_state_is_stale,
    restart_state_path,
    schedule_managed_daemon_restart,
    write_restart_state,
)


def test_bare_explicit_codex_binary_uses_path_lookup(monkeypatch):
    monkeypatch.setattr(
        "cc_remote.codex_daemon_restart.shutil.which",
        lambda value: "/opt/bin/codex" if value == "codex" else None,
    )
    assert _codex_binary("codex") == "/opt/bin/codex"


def test_restart_state_round_trip_is_private_and_rejects_malformed(tmp_path):
    path = restart_state_path(tmp_path)
    state = write_restart_state(
        path,
        epoch="a" * 32,
        phase="restarting",
    )

    assert state.epoch == "a" * 32
    assert read_restart_state(path) == state
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert state.deadline_at > state.updated_at

    path.write_text(json.dumps({
        "version": 1,
        "epoch": "not-an-epoch",
        "phase": "ready",
        "updated_at": 1,
    }))
    assert read_restart_state(path) is None


def test_legacy_restart_state_gets_a_bounded_compatibility_deadline(tmp_path):
    path = restart_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "epoch": "a" * 32,
        "phase": "restarting",
        "updated_at": 100.0,
    }))

    state = read_restart_state(path)
    assert state is not None
    assert state.deadline_at == 100.0 + restart_outcome_timeout()
    assert restart_state_is_stale(state, now=state.deadline_at)


@pytest.mark.parametrize("phase", ["restarting", "failed"])
def test_non_ready_restart_state_expires_at_its_published_deadline(
    tmp_path, phase,
):
    state = write_restart_state(
        restart_state_path(tmp_path),
        epoch="b" * 32,
        phase=phase,
    )
    assert restart_state_is_stale(
        state, now=state.deadline_at - 0.001) is False
    assert restart_state_is_stale(state, now=state.deadline_at) is True


def test_restart_command_publishes_barrier_then_ready(tmp_path, monkeypatch):
    path = restart_state_path(tmp_path)
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs, read_restart_state(path)))
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"status":"restarted"}\n', stderr="")

    monkeypatch.setattr(restart_module.subprocess, "run", run)
    result = restart_managed_daemon(
        codex_bin="/opt/codex",
        state_dir=tmp_path,
        timeout=12,
    )

    assert result.returncode == 0
    argv, kwargs, during = observed[0]
    expected_bin = (
        os.path.realpath("/opt/codex") if sys.platform == "win32"
        else "/opt/codex"
    )
    assert argv == [
        expected_bin, "app-server", "daemon", "restart"]
    assert kwargs["timeout"] == 12
    assert during is not None and during.phase == "restarting"
    final = read_restart_state(path)
    assert final is not None and final.phase == "ready"
    assert final.epoch == during.epoch


def test_restart_command_failure_is_fail_closed(tmp_path, monkeypatch):
    path = restart_state_path(tmp_path)

    monkeypatch.setattr(
        restart_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 7, stdout="", stderr="failed"),
    )
    result = restart_managed_daemon(
        codex_bin="/opt/codex",
        state_dir=tmp_path,
    )

    assert result.returncode == 7
    state = read_restart_state(path)
    assert state is not None and state.phase == "failed"


def test_restart_command_exception_is_fail_closed(tmp_path, monkeypatch):
    path = restart_state_path(tmp_path)

    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["codex"], 1)

    monkeypatch.setattr(restart_module.subprocess, "run", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        restart_managed_daemon(
            codex_bin="/opt/codex",
            state_dir=tmp_path,
        )

    state = read_restart_state(path)
    assert state is not None and state.phase == "failed"


def test_detached_restart_publishes_barrier_before_spawning(
    tmp_path, monkeypatch,
):
    path = restart_state_path(tmp_path)
    observed = {}

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            observed["argv"] = argv
            observed["kwargs"] = kwargs
            observed["state"] = read_restart_state(path)

    monkeypatch.setattr(restart_module.subprocess, "Popen", FakeProcess)
    epoch = schedule_managed_daemon_restart(
        codex_bin="/opt/codex",
        state_dir=tmp_path,
    )

    assert observed["state"].epoch == epoch
    assert observed["state"].phase == "restarting"
    assert (
        observed["state"].deadline_at - observed["state"].updated_at
    ) == pytest.approx(
        restart_outcome_timeout(), abs=0.1
    )
    assert observed["argv"][:3] == [
        restart_module.sys.executable,
        "-m",
        "cc_remote.codex_daemon_restart",
    ]
    assert observed["kwargs"]["start_new_session"] is True
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    timeout_index = observed["argv"].index("--timeout") + 1
    assert float(observed["argv"][timeout_index]) == _DEFAULT_WORKER_TIMEOUT
    grace_index = observed["argv"].index("--drain-grace") + 1
    assert float(observed["argv"][grace_index]) == _DEFAULT_DRAIN_GRACE


def test_scheduled_worker_waits_for_turn_and_publishes_ready(
    tmp_path, monkeypatch,
):
    path = restart_state_path(tmp_path)
    epoch = "b" * 32
    write_restart_state(path, epoch=epoch, phase="restarting")
    monkeypatch.setattr(
        restart_module,
        "_run_official_restart",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["/opt/codex"], 0, stdout='{"status":"restarted"}\n', stderr="",
        ),
    )
    sleeps = []
    monkeypatch.setattr(restart_module.time, "sleep", sleeps.append)

    assert _scheduled_worker(
        epoch=epoch,
        codex_bin="/opt/codex",
        state_dir=tmp_path,
        timeout=None,
    ) == 0
    state = read_restart_state(path)
    assert state is not None and state.epoch == epoch
    assert state.phase == "ready"
    assert sleeps == [_DEFAULT_DRAIN_GRACE]


def test_superseded_worker_does_not_restart_or_overwrite_new_epoch(
    tmp_path, monkeypatch,
):
    path = restart_state_path(tmp_path)
    newer_epoch = "c" * 32
    write_restart_state(path, epoch=newer_epoch, phase="restarting")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("superseded worker must not restart the daemon")

    monkeypatch.setattr(restart_module, "_run_official_restart", unexpected)
    assert _scheduled_worker(
        epoch="d" * 32,
        codex_bin="/opt/codex",
        state_dir=tmp_path,
        timeout=None,
        drain_grace=0,
    ) == 0
    state = read_restart_state(path)
    assert state is not None and state.epoch == newer_epoch
    assert state.phase == "restarting"


def test_scheduled_worker_rechecks_epoch_after_drain_grace(
    tmp_path, monkeypatch,
):
    path = restart_state_path(tmp_path)
    old_epoch = "d" * 32
    newer_epoch = "e" * 32
    write_restart_state(path, epoch=old_epoch, phase="restarting")

    def supersede(_seconds):
        write_restart_state(path, epoch=newer_epoch, phase="restarting")

    monkeypatch.setattr(restart_module.time, "sleep", supersede)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("superseded worker must not restart the daemon")

    monkeypatch.setattr(restart_module, "_run_official_restart", unexpected)

    assert _scheduled_worker(
        epoch=old_epoch,
        codex_bin="/opt/codex",
        state_dir=tmp_path,
        timeout=None,
    ) == 0
    state = read_restart_state(path)
    assert state is not None and state.epoch == newer_epoch
    assert state.phase == "restarting"
