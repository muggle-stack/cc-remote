"""Offline activation rollback: whole profile transactions, not single stores."""

import hashlib
import io
import json
import stat
import tarfile

import pytest

from cc_remote.claude_profiles import ClaudeProfileTopologyStore
from cc_remote.config import WrapperConfig
from cc_remote.wrapper.claude_controls import ClaudeControlStore
from cc_remote.wrapper.codex_checkpoints import (
    migrate_codex_checkpoint_profiles,
)
from cc_remote.wrapper import codex_checkpoints
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.viewer_pages import PageRef, PageScope
from deploy import work_registry_snapshot as snapshots


STATE_FILES = {
    "claude-session-controls.json", "codex-session-controls.json",
    "codex-turn-leases.json", "session-pins.json", "session-aliases.json",
    "claude-forks.json", "codex-forks.json", "private-btw-sessions.json",
    "session-plans.json", "session-presentation.json", "viewer-pages.json",
    "claude-profile-transition.json", "codex-profile-transition.json",
    "claude-profile-topology.json", "codex-profile-topology.json",
}
SID = "11111111-1111-4111-8111-111111111111"
ALIAS = "tmp-" + "2" * 32


def _roots(tmp_path):
    return {engine: tmp_path / f"{engine}-work" for engine in ("claude", "codex")}


def _write(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


@pytest.mark.parametrize("exists", [False, True])
def test_snapshot_restores_every_participant_and_absence(tmp_path, exists):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    for name in STATE_FILES:
        if exists:
            _write(state / name, b'{"before":true}\n')
    # Operator configuration is deliberately outside this rollback boundary.
    _write(state / "codex-profiles.json", b'{"operator":true}')
    snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    # The SQLite participant has separate native-image/receipt rollback tests.
    assert set(manifest["wrapper_state"]["files"]) == STATE_FILES | {"claude-plans.sqlite3", "collaboration.sqlite3"}
    for name in STATE_FILES:
        _write(state / name, b'{"after":true}')
    _write(state / "codex-profiles.json", b'{"operator":"preserved"}')

    for _ in range(2):  # Recovery can safely repeat a restore after lost SSH.
        snapshots.restore_snapshot(snapshot)
        for name in STATE_FILES:
            path = state / name
            assert path.exists() == exists
            if exists:
                assert path.read_bytes() == b'{"before":true}\n'
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (state / "codex-profiles.json").read_bytes() == b'{"operator":"preserved"}'
    assert not (snapshot / "codex-profiles.json").exists()


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_legacy_snapshots_restore_only_their_original_scope(tmp_path, legacy_version):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    control = state / "claude-session-controls.json"
    _write(control, b'{"before":true}')
    manifest_path = snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = legacy_version
    if legacy_version == 2:
        manifest["wrapper_state"] = manifest["wrapper_state"]["files"][control.name]
    else:
        del manifest["wrapper_state"]
    manifest_path.write_text(json.dumps(manifest))
    _write(control, b'{"after":true}')
    _write(state / "claude-profile-topology.json", b'{"not_in_old_snapshot":true}')
    snapshots.restore_snapshot(snapshot)
    assert control.read_bytes() == (b'{"before":true}' if legacy_version == 2 else b'{"after":true}')
    assert (state / "claude-profile-topology.json").exists()


@pytest.mark.parametrize("damage", ["missing", "extra", "path", "directory", "checksum"])
def test_incomplete_transaction_is_rejected_before_any_restore(tmp_path, damage):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    controls = state / "claude-session-controls.json"
    _write(controls, b"before")
    roots = _roots(tmp_path)
    manifest_path = snapshots.create_snapshot(snapshot, roots, state_dir=state)
    manifest = json.loads(manifest_path.read_text())
    files = manifest["wrapper_state"]["files"]
    if damage == "missing":
        del files["claude-profile-topology.json"]
    elif damage == "extra":
        files["credentials.json"] = files[controls.name]
    elif damage == "path":
        files[controls.name]["path"] = str(state / "credentials.json")
    elif damage == "directory":
        manifest["wrapper_state"]["directory"] = str(tmp_path)
    else:
        (snapshot / controls.name).write_bytes(b"corrupt")
    manifest_path.write_text(json.dumps(manifest))
    _write(controls, b"after")
    # This file was absent at snapshot time; a premature database restore
    # would delete it before noticing the missing migration participant.
    marker = roots["claude"] / "registry.sqlite3"
    _write(marker, b"must not be touched")
    with pytest.raises(snapshots.WorkRegistrySnapshotError):
        snapshots.restore_snapshot(snapshot)
    assert controls.read_bytes() == b"after"
    assert marker.read_bytes() == b"must not be touched"


def test_snapshot_accepts_real_store_limits_above_one_megabyte(tmp_path):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    path = state / "session-presentation.json"
    payload = b" " * (2 * 1024 * 1024)
    _write(path, payload)
    snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    _write(path, b"after")
    snapshots.restore_snapshot(snapshot)
    assert path.read_bytes() == payload


@pytest.mark.parametrize("filename", ["viewer-pages.json", "codex-checkpoints"])
def test_snapshot_refuses_symlinks(tmp_path, filename):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    state.mkdir()
    external = tmp_path / "outside"
    _write(external, b"not snapshot data")
    (state / filename).symlink_to(external)
    with pytest.raises((OSError, snapshots.WorkRegistrySnapshotError)):
        snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    assert not (snapshot / "manifest.json").exists()
    assert external.read_bytes() == b"not snapshot data"


class _Transport:
    on_connected = None


@pytest.mark.parametrize("old_topology", ["absent", "single", "multi"])
@pytest.mark.parametrize("complete", [False, True])
def test_failed_claude_upgrade_rolls_back_and_retries_whole_migration(
    tmp_path, monkeypatch, old_topology, complete,
):
    personal, company = tmp_path / "personal", tmp_path / "company"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    roots = _roots(tmp_path)
    cfg.claude_work_root, cfg.codex_work_root = roots["claude"], roots["codex"]
    cfg.codex_profiles_json = ""

    def profiles(first, second):
        return json.dumps({
            "personal": {"label": "Personal", "config_dir": str(first), "default": True},
            "company": {"label": "Company", "config_dir": str(second)},
        })

    cfg.claude_profiles_json = profiles(personal, company) if old_topology == "multi" else ""
    initial = None if old_topology == "absent" else WrapperMachine(cfg, _Transport())
    old_sid = f"personal@{SID}" if old_topology == "multi" else SID
    controls = initial._claude_controls if initial else ClaudeControlStore(cfg.state_dir)
    controls.update(old_sid, model=None, effort="high", permission_mode="plan")
    if initial:
        initial._session_pins.set_pinned("claude", old_sid, True)
        initial._remember_private_btw(old_sid, "/test-project")
        initial._remember_session_alias(ALIAS, old_sid, "/test-project", engine="claude")
        initial.viewer_pages.store.associate(
            PageScope(engine="claude", space="code", sid=old_sid),
            [PageRef(machine_id="device", site_id="demo", entry="/index.html", label="Page")],
            automatic=False,
        )
    before = {name: (cfg.state_dir / name).read_bytes()
              for name in STATE_FILES if (cfg.state_dir / name).exists()}
    snapshot = tmp_path / "snapshot"
    snapshots.create_snapshot(snapshot, roots, state_dir=cfg.state_dir)
    cfg.claude_profiles_json = profiles(company, personal) if old_topology == "multi" else profiles(personal, company)
    expected_sid = f"company@{SID}" if old_topology == "multi" else f"personal@{SID}"
    with monkeypatch.context() as failure:
        if not complete:
            def fail_complete(*_args):
                raise OSError("activation stopped during migration")
            failure.setattr(ClaudeProfileTopologyStore, "complete", fail_complete)
        migrated = WrapperMachine(cfg, _Transport())
        assert migrated._claude_controls.get(expected_sid).permission_mode == "plan"
        assert migrated._claude_profile_migration_ok == complete

    snapshots.restore_snapshot(snapshot)
    assert {name: (cfg.state_dir / name).read_bytes()
            for name in STATE_FILES if (cfg.state_dir / name).exists()} == before
    retried = WrapperMachine(cfg, _Transport())
    assert retried._claude_profile_migration_ok
    assert retried._codex_profile_migration_ok
    assert retried._claude_controls.get(expected_sid).permission_mode == "plan"
    assert not (cfg.state_dir / "claude-profile-transition.json").exists()
    if initial:
        assert expected_sid in retried._private_btw_sessions
        assert retried._session_aliases[ALIAS]["session_id"] == expected_sid
        assert retried.viewer_pages.store.list(PageScope(engine="claude", space="code", sid=expected_sid))
    assert retried.sessions == {}


@pytest.mark.parametrize("interrupted", [False, True])
def test_codex_checkpoint_directory_swap_and_bytes_roll_back_together(tmp_path, monkeypatch, interrupted):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    repository = state / "codex-checkpoints" / "repository"
    for sid in (f"first@{SID}", f"second@{SID}"):
        key = hashlib.sha256(sid.encode()).hexdigest()[:20]
        _write(repository / key / "manifest.json", json.dumps({"session_id": sid, "profile_revision": 1}).encode())
        _write(repository / key / "objects" / "test", sid.encode())
    before = {p.relative_to(repository): p.read_bytes() for p in repository.rglob("*") if p.is_file()}
    snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)

    def swap(sid):
        return sid.replace("first@", "second@") if sid.startswith("first@") else sid.replace("second@", "first@")

    with monkeypatch.context() as failure:
        if interrupted:
            write = codex_checkpoints._atomic_write

            def fail_after_write(path, data):
                write(path, data)
                raise OSError("failed after first checkpoint was staged")

            failure.setattr(codex_checkpoints, "_atomic_write", fail_after_write)
            with pytest.raises(OSError):
                migrate_codex_checkpoint_profiles(state, swap, profile_revision=2)
        else:
            assert migrate_codex_checkpoint_profiles(state, swap, profile_revision=2) == 2
    assert {p.relative_to(repository): p.read_bytes() for p in repository.rglob("*") if p.is_file()} != before
    for _ in range(2):
        snapshots.restore_snapshot(snapshot)
        assert {p.relative_to(repository): p.read_bytes() for p in repository.rglob("*") if p.is_file()} == before
    assert migrate_codex_checkpoint_profiles(state, swap, profile_revision=2) == 2


def test_checkpoint_snapshot_cannot_follow_a_swapped_directory(tmp_path, monkeypatch):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    repository = state / "codex-checkpoints" / "repository"
    repository.mkdir(mode=0o700, parents=True)
    outside = tmp_path / "outside"
    _write(outside / "private", b"not in scope")
    original_open = snapshots.os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == "repository" and kwargs.get("dir_fd") is not None:
            repository.rename(repository.with_name("saved"))
            repository.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(snapshots.os, "open", swap_before_open)
    with pytest.raises(OSError):
        snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    assert not (snapshot / "manifest.json").exists()
    with tarfile.open(snapshot / "codex-checkpoints.tar") as archive:
        assert all("private" not in member.name for member in archive)


def test_absent_checkpoint_tree_is_retired_without_deleting_data(tmp_path):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    _write(state / "codex-checkpoints" / "new" / "manifest.json", b"new data")
    snapshots.restore_snapshot(snapshot)
    assert not (state / "codex-checkpoints").exists()
    retained = list(state.glob(".checkpoint-displaced-*/codex-checkpoints/new/manifest.json"))
    assert len(retained) == 1 and retained[0].read_bytes() == b"new data"


@pytest.mark.parametrize("unsafe", ["../escape", "/escape", "codex-checkpoints/link"])
def test_checkpoint_restore_rejects_unsafe_archive_even_with_matching_hash(tmp_path, unsafe):
    state, snapshot = tmp_path / "state", tmp_path / "snapshot"
    manifest_path = snapshots.create_snapshot(snapshot, _roots(tmp_path), state_dir=state)
    archive_path = snapshot / "codex-checkpoints.tar"
    with tarfile.open(archive_path, "w") as archive:
        root = tarfile.TarInfo("codex-checkpoints")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        entry = tarfile.TarInfo(unsafe)
        if unsafe.endswith("link"):
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../../escape"
            archive.addfile(entry)
        else:
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b"x"))
    manifest = json.loads(manifest_path.read_text())
    manifest["wrapper_state"]["checkpoints"] = {"exists": True, "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(snapshots.WorkRegistrySnapshotError, match="unsafe checkpoint"):
        snapshots.restore_snapshot(snapshot)
    assert not (tmp_path / "escape").exists()
