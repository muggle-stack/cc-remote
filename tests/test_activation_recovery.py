"""Installer-owned admission and recovery: real locks, sockets and state images."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from cc_remote import activation
from cc_remote.wrapper.local_control import LocalControl
from cc_remote.wrapper.updates import UIUpdates
from deploy import activation as installer
from deploy import release_retention, ui_update_job
from deploy.install_lock import acquire_install_lock, LOCK_FD_ENV
from deploy.work_registry_snapshot import create_snapshot
from tests.test_multisession import _mk_machine
from tests.test_ui_updates import managed as managed
from tests.test_update import _manifest

IDENTITY = "c" * 32


def record(root, previous, **fields):
    target = root / "releases/new"
    target.mkdir(exist_ok=True)
    (target / "release-manifest.json").write_text(json.dumps(_manifest(version="4.0.1")))
    value = {"schema": 1, "id": IDENTITY, "root": str(root), "phase": "waiting",
             "previous": str(previous), "release": str(target), "uid": os.getuid(),
             "control_socket": "/tmp/unused-activation.sock", "generation": None,
             "ui_job": None, "snapshot": None, **fields}
    activation.save(root, value)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["terminal", "ui"])
async def test_terminal_and_ui_share_live_socket_admission(managed, monkeypatch, origin):
    root, previous, store = managed
    machine, _ = _mk_machine()
    updates = machine._ui_updates = UIUpdates(machine, root)
    with tempfile.TemporaryDirectory(prefix="ccr-admission-", dir="/tmp") as short:
        local = LocalControl(SimpleNamespace(cfg=SimpleNamespace(state_dir=Path(short)), _ui_updates=updates))
        await local.start()
        value = record(root, previous, control_socket=str(local.socket), ui_job=IDENTITY if origin == "ui" else None)
        if origin == "ui":
            store.create(IDENTITY, "4.0.1", "4.0.0")
        fd = acquire_install_lock(root)
        monkeypatch.setenv(LOCK_FD_ENV, str(fd))
        try:
            assert not updates.guarded(), "staging must leave chat open"
            await asyncio.to_thread(installer.prepare, root, IDENTITY)
            assert activation.receipt(root)["phase"] == "quiesced"
            assert updates.guarded() and updates.reserved is None
            from cc_remote.protocol import Query, GetHistory
            assert (await updates.reject(Query(prompt="next", msg_id="new", sid="history"))).code == "update_in_progress"
            assert await updates.reject(GetHistory(session_id="history")) is None
            installer.finish(root, value["id"], "unchanged")
            assert not updates.guarded()
        finally:
            os.close(fd)
            await local.close()
            await machine._command_scheduler.close()


def test_surviving_installer_child_keeps_lock_after_controller_exits(managed):
    root, previous, _ = managed
    record(root, previous, phase="quiesced")
    descriptor = acquire_install_lock(root)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], pass_fds=(descriptor,))
    os.close(descriptor)
    try:
        assert activation.install_locked(root)
        assert UIUpdates(SimpleNamespace(), root).guarded()
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert not activation.install_locked(root)
    assert not UIUpdates(SimpleNamespace(), root).guarded(), "no stop intent was committed"


def test_unfinished_switch_guard_is_installer_state_not_ui_state(managed):
    root, previous, store = managed
    record(root, previous, phase="verifying")
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "failed"})
    assert UIUpdates(SimpleNamespace(), root).guarded(), "provisional data still needs recovery"
    activation.save(root, {**activation.receipt(root), "phase": "rolled_back"})
    store.save({**state, "phase": "unknown"})
    assert not UIUpdates(SimpleNamespace(), root).guarded(), "a stale UI receipt cannot close a healthy installation"


def test_reconcile_fences_late_worker_without_erasing_original_request(managed, monkeypatch):
    root, previous, store = managed
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "unknown", "original_release": str(previous), "created_at": 0})
    monkeypatch.setattr(installer, "health", lambda value, release: release == str(previous))
    monkeypatch.setattr(ui_update_job, "spawn_worker", lambda *a, **k: pytest.fail("no recovery installer needed"))
    monkeypatch.setattr(ui_update_job, "update", lambda **kw: pytest.fail("late worker must not install"))
    assert ui_update_job.reconcile(root, IDENTITY)["phase"] == "failed"
    ui_update_job.run(root, IDENTITY)
    assert store.state(IDENTITY)["reason"] == "worker_exited"
    assert ui_update_job.launch(root, IDENTITY, "4.0.1")["phase"] == "failed"


def test_exact_replay_reads_receipt_while_worker_owns_progress_lock(managed, monkeypatch):
    root, _, store = managed
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "installing"})
    monkeypatch.setattr(ui_update_job, "spawn_worker", lambda *args: pytest.fail("duplicate OS launch"))
    with store.lock(blocking=True):
        assert ui_update_job.launch(root, IDENTITY, "4.0.1")["phase"] == "installing"
        with pytest.raises(ValueError, match="identity was reused"):
            ui_update_job.launch(root, IDENTITY, "4.0.2")


@pytest.mark.parametrize("outcome,phase", [("succeeded", "succeeded"), ("rolled_back", "failed"), ("unchanged", "failed")])
def test_old_terminal_receipt_settles_after_later_cli_activation(managed, monkeypatch, outcome, phase):
    root, current, store = managed
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "unknown", "attempt_started": True})
    record(root, root / "releases/older", phase=outcome)
    monkeypatch.setattr(installer, "health", lambda value, release: release == str(current))
    monkeypatch.setattr(ui_update_job, "spawn_worker", lambda *args: pytest.fail("must not undo later CLI activation"))
    assert ui_update_job.reconcile(root, IDENTITY)["phase"] == phase


def test_os_restarted_controller_recovers_original_instead_of_installing_again(managed, monkeypatch):
    root, previous, store = managed
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "installing", "worker_pid": 99999999,
                "worker_start": "departed", "attempt_started": True, "original_release": str(previous)})
    monkeypatch.setattr(ui_update_job, "update", lambda **kw: pytest.fail("must not resubmit accepted installation"))
    monkeypatch.setattr(installer, "health", lambda *args: True)
    ui_update_job.run(root, IDENTITY)
    assert store.state(IDENTITY)["phase"] == "failed"
    assert store.state(IDENTITY)["reason"] == "worker_exited"
    assert not activation.guarded(root)


def test_restarted_controller_waits_for_surviving_installer_result(managed, monkeypatch):
    root, previous, store = managed
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "installing", "attempt_started": True})
    record(root, previous, phase="verifying", ui_job=IDENTITY)
    fd = acquire_install_lock(root)
    monkeypatch.setenv(LOCK_FD_ENV, str(fd))
    waits = []
    def finish(_):
        waits.append(True)
        installer.finish(root, IDENTITY, "succeeded")
    monkeypatch.setattr(ui_update_job.time, "sleep", finish)
    monkeypatch.setattr(ui_update_job, "update", lambda **kw: pytest.fail("must follow the live installer"))
    try:
        ui_update_job.run(root, IDENTITY)
        assert waits == [True]
        assert store.state(IDENTITY)["phase"] == "succeeded"
        assert not activation.guarded(root)
    finally:
        os.close(fd)


def test_interrupted_journal_creation_can_settle_without_touching_service(managed, monkeypatch):
    root, previous, _ = managed
    value = record(root, previous)
    config = root / "service.conf"
    config.write_text("original")
    generation = release_retention.begin(root, Path(value["release"]), str(previous), role="wrapper", home=root,
                                         service="fixture", service_file=config, configs=[])
    monkeypatch.setattr(installer, "health", lambda *args: True)
    monkeypatch.setattr(installer, "service", lambda *args: pytest.fail("staging never stopped this service"))
    fd = acquire_install_lock(root)
    monkeypatch.setenv(LOCK_FD_ENV, str(fd))
    try:
        assert installer.recover(root, IDENTITY) == "failed"
        assert activation.receipt(root)["phase"] == "unchanged"
        assert release_retention.generation(root, generation)[1]["phase"] == "aborted"
        assert config.read_text() == "original"
    finally:
        os.close(fd)


def test_recovery_launch_is_generation_fenced_and_bounded(managed, monkeypatch):
    root, previous, store = managed
    record(root, previous, phase="stopped")
    state, _ = store.create(IDENTITY, "4.0.1", "4.0.0")
    store.save({**state, "phase": "unknown", "attempt_started": True})
    monkeypatch.setattr(installer, "health", lambda *a: False)
    launched = []
    monkeypatch.setattr(ui_update_job, "spawn_worker", lambda *a: launched.append(a[-1]))
    assert ui_update_job.reconcile(root, IDENTITY)["phase"] == "verifying"
    ui_update_job.reconcile(root, IDENTITY)
    assert launched == [1]
    # A superseded recovery process cannot restore state or stop a service.
    monkeypatch.setattr(installer, "recover", lambda *a: pytest.fail("stale generation ran"))
    ui_update_job.recover(root, IDENTITY, 0)
    state = store.state(IDENTITY)
    store.save({**state, "recovery_attempts": 3, "recovery_started_at": 0})
    ui_update_job.reconcile(root, IDENTITY)
    assert launched == [1]
    ui_update_job.reconcile(root, IDENTITY, explicit=True)
    assert launched == [1, 2]


@pytest.mark.parametrize("failure", [None, "stop", "corrupt-snapshot"])
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_interrupted_switch_restores_actual_database_and_configuration(managed, monkeypatch, failure, platform):
    root, previous, _ = managed
    value = record(root, previous, phase="verifying")
    target = Path(value["release"])
    config = root / "service.conf"
    config.write_text("old service configuration\n")
    home = root / "home"
    home.mkdir()
    identity = release_retention.begin(root, target, str(previous), role="wrapper", home=home,
                                       service="fixture", service_file=config, configs=[config])
    work = home / "claude-work"
    work.mkdir()
    database = work / "registry.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("create table jobs (value text)")
        connection.execute("insert into jobs values ('before')")
    snapshot = root / "rollback-data/original"
    create_snapshot(snapshot, {"claude": work, "codex": home / "codex-work"}, state_dir=home / "state")
    with sqlite3.connect(database) as connection:
        connection.execute("update jobs set value='after'")
    config.write_text("new service configuration\n")
    (root / "current").unlink()
    (root / "current").symlink_to(target)
    activation.save(root, {**value, "generation": identity, "snapshot": str(snapshot)})
    if failure == "corrupt-snapshot":
        (snapshot / "claude.sqlite3").write_bytes(b"damaged")
    calls = []
    def service(row, action):
        calls.append(action)
        if failure == "stop":
            raise ValueError("cannot confirm stop")
    monkeypatch.setattr(installer, "service", service)
    monkeypatch.setattr(installer.sys, "platform", platform)
    def system_command(argv, **kwargs):
        assert argv == ["systemctl", "daemon-reload"]
        assert kwargs["check"] and kwargs["pass_fds"] == (fd,)
        calls.append("daemon-reload")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(installer.subprocess, "run", system_command)
    monkeypatch.setattr(installer, "health", lambda v, release: calls[-1:] == ["start"] and release == str(previous))
    fd = acquire_install_lock(root)
    monkeypatch.setenv(LOCK_FD_ENV, str(fd))
    try:
        if failure:
            with pytest.raises((ValueError, RuntimeError)):
                installer.recover(root, IDENTITY)
            assert (root / "current").resolve() == target
            assert config.read_text() == "new service configuration\n"
            assert calls == ["stop"]
        else:
            assert installer.recover(root, IDENTITY) == "failed"
            assert calls == ["stop", *(["daemon-reload"] if platform == "linux" else []), "start"]
            assert (root / "current").resolve() == previous
            assert config.read_text() == "old service configuration\n"
            assert activation.receipt(root)["phase"] == "rolled_back"
            assert release_retention.generation(root, identity)[1]["phase"] == "aborted"
        with sqlite3.connect(database) as connection:
            assert connection.execute("select value from jobs").fetchone()[0] == ("after" if failure else "before")
    finally:
        os.close(fd)
