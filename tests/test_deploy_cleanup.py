"""Cleanup protects live dependencies and validates retirement before deletion."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from deploy import cleanup as module
from deploy.install_lock import acquire_install_lock

REAL_PROCESS_REFERENCES = module.process_references


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "installation"
    releases = root / "releases"
    releases.mkdir(parents=True)
    current, previous, old = [releases / name for name in ["current-build", "previous-build", "old-build"]]
    for path in [current, previous, old]:
        path.mkdir()
        (path / "payload").write_text("must not be lost")
    (root / "current").symlink_to(current)
    transaction = root / "activation.json"
    transaction.write_text(json.dumps({"phase": "committed"}))
    inventory = root / "cleanup.json"
    data = {
        "schema": 1, "installation_root": str(root), "current_release": str(current),
        "rollback_paths": [str(previous)], "cleanup_roots": [str(releases)],
        "candidates": [str(old)],
        "transactions": [{"path": str(transaction), "field": "phase", "equals": "committed"}],
        "checks": [{"name": "fixture health", "argv": [sys.executable, "-c", "pass"], "cwd": str(root)}],
    }
    inventory.write_text(json.dumps(data))
    monkeypatch.setattr(module, "process_references", lambda paths: {p: [] for p in paths})
    return root, current, previous, old, inventory, data


def update_inventory(installation, **changes):
    *_, inventory, data = installation
    data.update(changes)
    inventory.write_text(json.dumps(data))


def test_preview_never_moves_deletes_or_runs_acceptance(installation, monkeypatch):
    root, _, _, old, inventory, _ = installation
    monkeypatch.setattr(module, "run_checks", lambda plan: pytest.fail("preview must not run commands"))
    result = module.cleanup(inventory)
    assert result["status"] == "preview"
    assert result["candidates"][0]["state"] == "planned"
    assert old.is_dir()
    assert not (root / module.JOURNAL).exists()


@pytest.mark.parametrize('context', [None, {}, {'home': 'relative'}, {'home': '/', 'unknown': True}])
def test_invalid_service_discovery_context_never_retires(installation, context):
    _, _, _, old, inventory, _ = installation
    update_inventory(installation, service_context=context)
    with pytest.raises(module.CleanupError):
        module.cleanup(inventory, apply=True)
    assert (old / 'payload').read_text() == 'must not be lost'


def test_success_retains_complete_rollback_and_checks_after_removal(installation, monkeypatch):
    root, current, previous, old, inventory, _ = installation
    observed = []
    def checks(plan):
        observed.append((old.exists(), bool(list(old.parent.glob(".cc-remote-retired-*")))))
    monkeypatch.setattr(module, "run_checks", checks)
    result = module.cleanup(inventory, apply=True)
    assert observed == [(True, False), (False, True), (False, False)]
    assert result["status"] == "complete"
    assert result["removed_allocated_bytes"] > 0
    assert current.is_dir() and previous.is_dir() and not old.exists()
    assert json.loads((root / module.JOURNAL).read_text())["status"] == "complete"
    assert (root / module.JOURNAL).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("protected", ["current", "rollback", "dependency"])
def test_protected_artifact_cannot_be_retired(installation, protected):
    _, current, previous, old, inventory, data = installation
    if protected == "current":
        update_inventory(installation, candidates=[str(current)])
    elif protected == "rollback":
        update_inventory(installation, candidates=[str(previous)])
    else:
        update_inventory(installation, protected_paths=[str(old / "payload")])
    result = module.cleanup(inventory, apply=True)
    assert result["status"] == "deferred"
    assert all(Path(p).exists() for p in data["candidates"])


def test_retained_venv_symlink_protects_an_older_release(installation):
    _, current, _, old, inventory, _ = installation
    (current / ".venv").symlink_to(old)
    result = module.cleanup(inventory, apply=True)
    assert result["status"] == "deferred"
    assert "symlink dependency" in " ".join(result["candidates"][0]["reasons"])
    assert old.is_dir()


def test_shared_venv_dependencies_are_transitive_and_cycles_are_bounded(installation):
    root, current, _, old, inventory, _ = installation
    shared = root / "shared-runtime"
    shared.mkdir()
    (current / ".venv").symlink_to(shared)
    (shared / "package").symlink_to(old)
    (old / "cycle").symlink_to(shared)
    assert module.cleanup(inventory, apply=True)["status"] == "deferred"
    assert old.is_dir()


def test_nested_protection_keeps_sibling_dependency_during_single_candidate_rescan(installation):
    _, _, _, old, inventory, _ = installation
    retained = old.parent / "retained-build"
    retained.mkdir()
    protected = retained / "service-config"
    protected.write_text("protected")
    (retained / ".venv").symlink_to(old)
    update_inventory(installation, candidates=[str(old), str(retained)],
                     protected_paths=[str(protected)])
    # Pre-rename checks request one candidate, but another retained candidate
    # still protects its dependencies even without a live process reference.
    reasons = module.protections(module.load_inventory(inventory), [old])[old]
    assert "symlink dependency" in " ".join(reasons)
    assert module.cleanup(inventory, apply=True)["status"] == "deferred"
    assert old.is_dir() and retained.is_dir()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_dependency_on_candidate_child_retains_whole_dependency_closure(installation, apply, reverse):
    root, current, _, old, inventory, _ = installation
    retained = old.parent / "retained-build"
    retained.mkdir()
    (retained / "package").mkdir()
    (current / "package").symlink_to(retained / "package")
    (retained / ".venv").symlink_to(old)
    (old / "cycle").symlink_to(retained)
    candidates = [old, retained]
    if reverse:
        candidates.reverse()
    update_inventory(installation, candidates=[str(p) for p in candidates])
    report = module.cleanup(inventory, apply=apply)
    assert all(row["state"] == "deferred" for row in report["candidates"])
    assert old.is_dir() and retained.is_dir()
    assert (retained / ".venv").resolve(strict=True) == old
    assert not list(old.parent.glob(".cc-remote-retired-*"))
    if apply:
        assert json.loads((root / module.JOURNAL).read_text())["status"] == "deferred"


def test_newly_busy_candidate_protects_its_dependency_before_other_candidate_moves(installation, monkeypatch):
    _, _, _, old, inventory, _ = installation
    busy = old.parent / "busy-build"
    busy.mkdir()
    (busy / ".venv").symlink_to(old)
    update_inventory(installation, candidates=[str(old), str(busy)])
    calls = 0
    def scan(paths):
        nonlocal calls
        calls += 1
        return {p: (["pid 42: cwd"] if p == busy and calls >= 2 else []) for p in paths}
    monkeypatch.setattr(module, "process_references", scan)
    report = module.cleanup(inventory, apply=True)
    assert all(row["state"] == "deferred" for row in report["candidates"])
    assert old.is_dir() and busy.is_dir()


@pytest.mark.parametrize("fault", ["broken", "limit"])
def test_incomplete_dependency_scan_never_deletes(installation, monkeypatch, fault):
    _, current, _, old, inventory, _ = installation
    if fault == "broken":
        (current / ".venv").symlink_to(current / "missing")
    else:
        monkeypatch.setattr(module, "MAX_DEPENDENCY_ENTRIES", 1)
    with pytest.raises((OSError, module.CleanupError)):
        module.cleanup(inventory, apply=True)
    assert old.is_dir()


@pytest.mark.parametrize("when", [1, 2, 3])
def test_new_live_reference_defers_and_restores_candidate(installation, monkeypatch, when):
    _, _, _, old, inventory, _ = installation
    calls = 0
    def scan(paths):
        nonlocal calls
        calls += 1
        return {p: (["pid 42: cwd"] if calls == when else []) for p in paths}
    monkeypatch.setattr(module, "process_references", scan)
    result = module.cleanup(inventory, apply=True)
    assert result["status"] == "deferred"
    assert (old / "payload").read_text() == "must not be lost"
    assert not list(old.parent.glob(".cc-remote-retired-*"))


def test_config_failure_after_quarantine_restores_original_path(installation, monkeypatch):
    root, _, _, old, inventory, _ = installation
    def config_read(plan):
        if not old.exists():
            raise module.CleanupError("configuration dependency became unavailable")
    monkeypatch.setattr(module, "run_checks", config_read)
    with pytest.raises(module.CleanupError, match="configuration dependency"):
        module.cleanup(inventory, apply=True)
    assert (old / "payload").read_text() == "must not be lost"
    report = json.loads((root / module.JOURNAL).read_text())
    assert report["status"] == "aborted"
    assert report["candidates"][0]["state"] == "restored"


def test_partial_delete_is_not_misreported_as_restored(installation, monkeypatch):
    root, _, _, old, inventory, _ = installation
    def fail_remove(path, *, dir_fd):
        os.unlink(f"{path}/payload", dir_fd=dir_fd)
        raise OSError("injected interrupted removal")
    fail_remove.avoids_symlink_attacks = True
    monkeypatch.setattr(module.shutil, "rmtree", fail_remove)
    with pytest.raises(OSError, match="interrupted removal"):
        module.cleanup(inventory, apply=True)
    report = json.loads((root / module.JOURNAL).read_text())
    assert report["status"] == "requires_inspection"
    assert report["candidates"][0]["state"] == "remove_pending"
    assert not old.exists()
    with pytest.raises(module.CleanupError, match="unfinished cleanup"):
        module.cleanup(inventory, apply=True)


@pytest.mark.parametrize("fault", ["transaction", "current", "candidate", "symlink", "scan", "lock", "journal"])
def test_uncertain_state_never_deletes(installation, monkeypatch, fault):
    root, _, previous, old, inventory, _ = installation
    lock = None
    if fault == "transaction":
        (root / "activation.json").write_text('{"phase":"activating"}')
    elif fault == "current":
        (root / "current").unlink()
        (root / "current").symlink_to(previous)
    elif fault == "candidate":
        update_inventory(installation, candidates=[str(root)])
    elif fault == "symlink":
        alias = old.parent / "alias"
        alias.symlink_to(old)
        update_inventory(installation, candidates=[str(alias)])
    elif fault == "scan":
        def scan(paths):
            raise module.CleanupError("incomplete scan")
        monkeypatch.setattr(module, "process_references", scan)
    elif fault == "lock":
        lock = acquire_install_lock(root)
    elif fault == "journal":
        (root / module.JOURNAL).write_text('{"status":"quarantining"}')
    try:
        with pytest.raises((ValueError, OSError)):
            module.cleanup(inventory, apply=True)
        assert old.is_dir()
    finally:
        if lock is not None:
            os.close(lock)


def test_transaction_change_after_precheck_stops_before_retirement(installation, monkeypatch):
    root, _, _, old, inventory, _ = installation
    def checks(plan):
        (root / "activation.json").write_text('{"phase":"committed","changed":true}')
    monkeypatch.setattr(module, "run_checks", checks)
    with pytest.raises(module.CleanupError, match="transaction changed"):
        module.cleanup(inventory, apply=True)
    assert old.is_dir()


@pytest.mark.parametrize("fault", ["inventory", "rollback"])
def test_replaced_provenance_stops_cleanup(installation, monkeypatch, fault):
    _, _, previous, old, inventory, _ = installation
    def checks(plan):
        if fault == "inventory":
            inventory.write_text(inventory.read_text() + "\n")
        else:
            previous.rename(previous.with_name("moved-rollback"))
            previous.mkdir()
    monkeypatch.setattr(module, "run_checks", checks)
    with pytest.raises(module.CleanupError, match="changed|replaced"):
        module.cleanup(inventory, apply=True)
    assert old.is_dir()


def test_lsof_parser_preserves_space_paths_and_cwd_without_argv():
    assert module.parse_lsof(b'p42\0\nfcwd\0n/opt/old release\0\nf9\0n/opt/file name\0\n') == [
        (42, "cwd", Path("/opt/old release")), (42, "9", Path("/opt/file name")),
    ]


@pytest.mark.parametrize("record", [
    b'p42\0\nfNOFD\0nPermission denied\0',
    b'p42\0\nfcwd\0n/proc/42/cwd (readlink: Permission denied)\0',
    b'p42\0\nfrtd\0n/proc/42/root (readlink: Operation not permitted)\0',
])
def test_unobservable_process_record_stops_cleanup(installation, monkeypatch, record):
    root, _, _, old, inventory, _ = installation
    monkeypatch.setattr(module, "scan_command", lambda argv: record)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/lsof")
    monkeypatch.setattr(module, "process_references", REAL_PROCESS_REFERENCES)
    with pytest.raises(module.CleanupError, match="not fully observable"):
        module.cleanup(inventory, apply=True)
    assert old.is_dir()
    assert not list(old.parent.glob(".cc-remote-retired-*"))
    assert not (root / module.JOURNAL).exists()


@pytest.mark.parametrize("failure", ["missing", "warning", "exit"])
def test_incomplete_lsof_never_becomes_empty_safe_result(monkeypatch, failure):
    monkeypatch.setattr(module.shutil, "which", lambda name: None if failure == "missing" else "/usr/bin/lsof")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
        subprocess.CompletedProcess(a, int(failure == "exit"), b'p1\0fcwd\0n/\0',
                                    b'cannot stat' if failure == "warning" else b''))
    with pytest.raises(module.CleanupError):
        module.process_references([Path("/candidate")])


def test_check_timeout_never_discloses_private_arguments(installation, monkeypatch):
    _, _, _, old, inventory, _ = installation
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["check", "secret-authorization-value"], 60)
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(module.CleanupError, match="did not complete") as error:
        module.cleanup(inventory, apply=True)
    assert "secret" not in str(error.value)
    assert old.is_dir()


@pytest.mark.skipif(not shutil.which("lsof"), reason="live process regression requires lsof")
def test_cli_previews_and_applies_isolated_installation_with_real_scans(installation):
    root, current, previous, old, inventory, _ = installation
    command = [sys.executable, str(Path(module.__file__).resolve()), str(inventory)]
    for apply in (False, True):
        result = subprocess.run([*command, *(["--apply"] if apply else [])],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Hosted Linux runners can have same-user nondumpable processes.
            # The real CLI must refuse cleanup in that environment, not ignore
            # denied /proc records or require elevated test-suite privileges.
            assert result.returncode == 1
            assert result.stderr.strip() == (
                "Cleanup stopped (CleanupError): process files are not fully observable")
            assert not result.stdout.strip()
            assert old.is_dir()
            assert not (root / module.JOURNAL).exists()
        else:
            report = json.loads(result.stdout)
            assert report["status"] == ("complete" if apply else "preview")
            assert report["candidates"][0]["state"] == ("removed" if apply else "planned")
            assert old.exists() is not apply
        assert current.is_dir() and previous.is_dir()
        assert not list(old.parent.glob(".cc-remote-retired-*"))


@pytest.mark.skipif(not shutil.which("lsof"), reason="live process regression requires lsof")
@pytest.mark.parametrize("reference", ["cwd", "file"])
def test_real_process_reference_absent_from_command_line_is_protected(installation, monkeypatch, reference):
    _, _, _, old, inventory, _ = installation
    code = ("import os,time; "
            "f=open(os.environ['CLEANUP_TEST_FILE']) if os.environ['CLEANUP_TEST_KIND']=='file' else None; "
            "print('ready',flush=True); time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code],
            cwd=old if reference == "cwd" else old.parent,
            env={**os.environ, "CLEANUP_TEST_FILE": str(old / "payload"), "CLEANUP_TEST_KIND": reference},
            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        scan_command = module.scan_command
        def scan_fixture_process(argv):
            # Exercise real lsof and ps against this fixture's owned process,
            # independently of unrelated runner processes with restricted /proc.
            if Path(argv[0]).name == "lsof":
                argv = [*argv, "-a", "-p", str(proc.pid)]
            return scan_command(argv)
        monkeypatch.setattr(module, "scan_command", scan_fixture_process)
        found = REAL_PROCESS_REFERENCES([old])[old]
        assert any(reason.startswith(f"pid {proc.pid}:") for reason in found)
        assert f"pid {proc.pid}: argv" not in found
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_post_delete_health_failure_never_reports_success(installation, monkeypatch):
    root, _, _, old, inventory, _ = installation
    def check(plan):
        if not old.exists() and not list(old.parent.glob(".cc-remote-retired-*")):
            raise module.CleanupError("late health failure")
    monkeypatch.setattr(module, "run_checks", check)
    with pytest.raises(module.CleanupError, match="late health failure"):
        module.cleanup(inventory, apply=True)
    assert json.loads((root / module.JOURNAL).read_text())["status"] == "requires_inspection"
