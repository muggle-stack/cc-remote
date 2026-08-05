from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cc_remote.wrapper import codex_checkpoints as checkpoint_module
from cc_remote.wrapper.codex_checkpoints import (
    CheckpointConflict,
    CheckpointError,
    CheckpointIndexChanged,
    CompletedCheckpoint,
    CodexCheckpointJournal,
    NotGitWorkspaceError,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "cc-remote test")
    _git(root, "config", "user.email", "cc-remote@example.invalid")
    _git(root, "config", "core.filemode", "true")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _index_bytes(root: Path) -> bytes:
    git_dir = _git(root, "rev-parse", "--absolute-git-dir")
    return (Path(git_dir) / "index").read_bytes()


def test_dirty_baseline_round_trip_preserves_index_and_file_kinds(tmp_path):
    root = _repo(
        tmp_path,
        {
            "tracked.txt": "tracked base\n",
            "staged.txt": "staged base\n",
            "unstaged.txt": "unstaged base\n",
            "deleted.txt": "deleted base\n",
            "rename-old.txt": "rename base\n",
            "binary.bin": b"\x00base\xff\x01",
            "mode.sh": "#!/bin/sh\necho base\n",
            "target-a": "a\n",
            "target-b": "b\n",
        },
    )
    try:
        os.symlink("target-a", root / "link")
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise
    _git(root, "add", "link")
    _git(root, "commit", "-m", "track symlink")
    _git(root, "config", "core.filemode", "false")

    # Build a baseline containing staged, unstaged, deleted, renamed, binary,
    # executable, symlink, and untracked state before the agent turn starts.
    (root / "staged.txt").write_text("staged index baseline\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "staged.txt").write_text("staged worktree baseline\n", encoding="utf-8")
    (root / "unstaged.txt").write_text("unstaged baseline\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    (root / "rename-old.txt").rename(root / "baseline-renamed.txt")
    (root / "binary.bin").write_bytes(b"\x00baseline\xfe\x02")
    (root / "mode.sh").write_text("#!/bin/sh\necho baseline\n", encoding="utf-8")
    (root / "mode.sh").chmod(0o755)
    (root / "untracked.txt").write_text("untracked baseline\n", encoding="utf-8")
    (root / "link").unlink()
    os.symlink("target-b", root / "link")

    index_before = _index_bytes(root)
    journal = CodexCheckpointJournal(str(root), tmp_path / "state", "session-dirty")
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    assert _index_bytes(root) == index_before

    (root / "tracked.txt").unlink()
    (root / "staged.txt").write_text("agent staged\n", encoding="utf-8")
    (root / "unstaged.txt").write_text("agent unstaged\n", encoding="utf-8")
    (root / "deleted.txt").write_text("agent recreated\n", encoding="utf-8")
    (root / "baseline-renamed.txt").rename(root / "agent-renamed.txt")
    (root / "agent-renamed.txt").write_text("agent rename\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00agent\xfd\x03")
    (root / "mode.sh").write_text("#!/bin/sh\necho agent\n", encoding="utf-8")
    (root / "mode.sh").chmod(0o644)
    (root / "untracked.txt").write_text("agent untracked\n", encoding="utf-8")
    (root / "link").unlink()
    os.symlink("target-a", root / "link")
    (root / "new" / "nested").mkdir(parents=True)
    (root / "new" / "nested" / "created.txt").write_text(
        "agent new\n", encoding="utf-8"
    )

    completed = journal.finish_turn("turn-1")
    assert "binary.bin" in completed.changed_paths
    assert "new/nested/created.txt" in completed.changed_paths
    assert _index_bytes(root) == index_before

    result = journal.rollback()

    assert result.turn_ids == ("turn-1",)
    assert (root / "tracked.txt").exists()
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "tracked base\n"
    assert (root / "staged.txt").read_text(
        encoding="utf-8"
    ) == "staged worktree baseline\n"
    assert (root / "unstaged.txt").read_text(encoding="utf-8") == "unstaged baseline\n"
    assert not (root / "deleted.txt").exists()
    assert not (root / "rename-old.txt").exists()
    assert (root / "baseline-renamed.txt").read_text(
        encoding="utf-8"
    ) == "rename base\n"
    assert not (root / "agent-renamed.txt").exists()
    assert (root / "binary.bin").read_bytes() == b"\x00baseline\xfe\x02"
    assert (root / "mode.sh").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\necho baseline\n"
    assert stat.S_IMODE((root / "mode.sh").stat().st_mode) == 0o755
    assert (root / "untracked.txt").read_text(
        encoding="utf-8"
    ) == "untracked baseline\n"
    assert (root / "link").is_symlink()
    assert os.readlink(root / "link") == "target-b"
    assert not (root / "new").exists()
    assert _index_bytes(root) == index_before


def test_rollback_conflict_is_all_or_nothing_for_files_and_index(tmp_path):
    root = _repo(tmp_path, {"a.txt": "a0\n", "b.txt": "b0\n"})
    journal = CodexCheckpointJournal(str(root), tmp_path / "state", "session-conflict")
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    (root / "a.txt").write_text("a1\n", encoding="utf-8")
    (root / "b.txt").write_text("b1\n", encoding="utf-8")
    journal.finish_turn("turn-1")

    (root / "a.txt").write_text("user change\n", encoding="utf-8")
    with pytest.raises(CheckpointConflict) as exc_info:
        journal.rollback()
    assert exc_info.value.paths == ("a.txt",)
    assert (root / "a.txt").read_text(encoding="utf-8") == "user change\n"
    assert (root / "b.txt").read_text(encoding="utf-8") == "b1\n"
    assert journal.completed_turn_ids() == ("turn-1",)

    (root / "a.txt").write_text("a1\n", encoding="utf-8")
    _git(root, "add", "b.txt")
    with pytest.raises(CheckpointConflict) as index_exc:
        journal.rollback()
    assert index_exc.value.paths == ()
    assert index_exc.value.index_paths == ("b.txt",)
    assert (root / "a.txt").read_text(encoding="utf-8") == "a1\n"
    assert (root / "b.txt").read_text(encoding="utf-8") == "b1\n"


def test_persistent_journal_supports_single_and_multi_turn_rollback(tmp_path):
    root = _repo(tmp_path, {"value.txt": "A\n"})
    state = tmp_path / "state"
    first = CodexCheckpointJournal(str(root), state, "session-continuous")

    first.begin_turn("turn-1")
    first.accept_turn("turn-1")
    (root / "value.txt").write_text("B\n", encoding="utf-8")
    first.finish_turn("turn-1")

    first.begin_turn("turn-2")
    first.accept_turn("turn-2")
    (root / "value.txt").write_text("C\n", encoding="utf-8")
    (root / "second.txt").write_text("turn two\n", encoding="utf-8")
    first.finish_turn("turn-2")

    resumed = CodexCheckpointJournal(str(root), state, "session-continuous")
    assert resumed.completed_turn_ids() == ("turn-1", "turn-2")
    result = resumed.rollback()
    assert result.turn_ids == ("turn-2",)
    assert (root / "value.txt").read_text(encoding="utf-8") == "B\n"
    assert not (root / "second.txt").exists()

    resumed.begin_turn("turn-3")
    resumed.accept_turn("turn-3")
    (root / "value.txt").write_text("D\n", encoding="utf-8")
    resumed.finish_turn("turn-3")
    result = resumed.rollback(2)
    assert result.turn_ids == ("turn-3", "turn-1")
    assert (root / "value.txt").read_text(encoding="utf-8") == "A\n"
    assert resumed.completed_turn_ids() == ()


def test_non_consuming_restore_stays_aligned_until_conversation_discard(tmp_path):
    root = _repo(tmp_path, {"value.txt": "A\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-separated-restore"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    (root / "value.txt").write_text("B\n", encoding="utf-8")
    journal.finish_turn("turn-1")

    first = journal.rollback(consume=False)
    assert first.turn_ids == ("turn-1",)
    assert (root / "value.txt").read_text(encoding="utf-8") == "A\n"
    assert journal.completed_turn_ids() == ("turn-1",)

    # A reliable-command retry must not conflict with the pre-image it already
    # restored or rewrite the file again.
    retry = journal.rollback(consume=False)
    assert retry.turn_ids == ("turn-1",)
    assert (root / "value.txt").read_text(encoding="utf-8") == "A\n"
    assert journal.discard() == ("turn-1",)
    assert journal.completed_turn_ids() == ()


def test_unavailable_turn_blocks_files_but_can_follow_conversation_history(tmp_path):
    root = _repo(tmp_path, {"value.txt": "A\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-unavailable"
    )
    journal.record_unavailable("turn-no-files", "index changed")

    with pytest.raises(CheckpointError, match="turn-no-files"):
        journal.rollback(consume=False)

    assert journal.completed_turn_ids() == ("turn-no-files",)
    assert journal.discard(5, allow_partial=True) == ("turn-no-files",)
    assert journal.completed_turn_ids() == ()


def test_finish_rejects_agent_index_changes_and_discards_active_record(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-index-change"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    (root / "tracked.txt").write_text("agent\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")

    with pytest.raises(CheckpointIndexChanged) as exc_info:
        journal.finish_turn("turn-1")

    assert exc_info.value.paths == ("tracked.txt",)
    assert not journal.has_active_turn()
    assert journal.completed_turn_ids() == ()
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "agent\n"
    assert _git(root, "show", ":tracked.txt") == "agent"


def test_finish_rejects_pure_staging_change_with_unchanged_worktree(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    (root / "tracked.txt").write_text("dirty baseline\n", encoding="utf-8")
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-pure-index-change"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")

    # The worktree bytes do not move during the turn; only the user's real
    # index does. Remote must reject this checkpoint because it never rewrites
    # that index during rollback.
    _git(root, "add", "tracked.txt")
    with pytest.raises(CheckpointIndexChanged) as exc_info:
        journal.finish_turn("turn-1")

    assert exc_info.value.paths == ("tracked.txt",)
    assert not journal.has_active_turn()
    assert journal.completed_turn_ids() == ()
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "dirty baseline\n"
    assert _git(root, "show", ":tracked.txt") == "dirty baseline"


def test_finish_classifies_clean_commit_as_conversation_only(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-clean-commit"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    (root / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "agent commit")

    completed = journal.finish_turn("turn-1")

    assert completed == CompletedCheckpoint(
        turn_id="turn-1",
        changed_paths=(),
        files_available=False,
    )
    assert journal.completed_turn_ids() == ("turn-1",)
    with pytest.raises(CheckpointError, match="turn-1"):
        journal.rollback(consume=False)


def test_finish_classifies_clean_empty_commit_as_conversation_only(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-clean-empty-commit"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    _git(root, "commit", "--allow-empty", "-m", "agent empty commit")

    completed = journal.finish_turn("turn-1")

    assert completed.files_available is False
    assert completed.changed_paths == ()
    assert journal.completed_turn_ids() == ("turn-1",)


def test_finish_rejects_commit_followed_by_dirty_worktree(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-commit-then-dirty"
    )
    journal.begin_turn("turn-1")
    journal.accept_turn("turn-1")
    (root / "tracked.txt").write_text("committed\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "agent commit")
    (root / "tracked.txt").write_text("dirty after commit\n", encoding="utf-8")

    with pytest.raises(CheckpointIndexChanged):
        journal.finish_turn("turn-1")

    assert journal.completed_turn_ids() == ()


def test_capture_rejects_worktree_that_changes_between_images(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-unstable-capture"
    )
    trees = iter(("a" * 40, "b" * 40))
    monkeypatch.setattr(journal, "_capture_tree", lambda: next(trees))
    monkeypatch.setattr(journal, "_permission_overrides", lambda _tree: {})

    with pytest.raises(CheckpointError, match="Worktree changed"):
        journal.begin_turn("turn-1")

    assert not journal.has_active_turn()
    assert journal.completed_turn_ids() == ()


def test_retention_bounds_turn_window_and_invalidates_oversize_objects(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    monkeypatch.setattr(checkpoint_module, "_MAX_RETAINED_TURNS", 2)
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-retained-turns"
    )
    journal.record_unavailable("turn-1", "test")
    journal.record_unavailable("turn-2", "test")
    journal.record_unavailable("turn-3", "test")
    assert journal.completed_turn_ids() == ("turn-2", "turn-3")

    monkeypatch.setattr(checkpoint_module, "_MAX_OBJECT_BYTES", 0)
    object_limited = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-object-limit"
    )
    object_limited.begin_turn("turn-large")
    object_limited.accept_turn("turn-large")
    (root / "tracked.txt").write_text("agent\n", encoding="utf-8")
    try:
        object_limited.finish_turn("turn-large")
    except CheckpointError as exc:
        if "retention limit" not in str(exc):
            if sys.platform == "win32":
                pytest.skip(
                    "Windows: _invalidate_file_history_for_retention's "
                    "shutil.rmtree fails on git's read-only loose objects "
                    f"instead of completing compaction: {exc}"
                )
            raise
    else:
        pytest.fail("finish_turn did not raise CheckpointError")

    assert object_limited.completed_turn_ids() == ("turn-large",)
    with pytest.raises(CheckpointError, match="turn-large"):
        object_limited.rollback(consume=False)
    assert (object_limited.objects_dir / "info" / "alternates").exists()


def test_rollback_restores_exact_noncanonical_file_permissions(tmp_path):
    root = _repo(tmp_path, {"secret.txt": "baseline\n"})
    secret = root / "secret.txt"
    secret.chmod(0o600)
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-permissions"
    )
    journal.begin_turn("turn-1")
    secret.write_text("agent\n", encoding="utf-8")
    secret.chmod(0o640)
    journal.accept_turn("turn-1")
    journal.finish_turn("turn-1")

    journal.rollback()

    assert secret.read_text(encoding="utf-8") == "baseline\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_chmod_only_turn_is_checkpointed_and_restored(tmp_path):
    if sys.platform == "win32":
        pytest.skip(
            "Windows collapses os.chmod to a single read-only bit, so "
            "distinct 0o600/0o640/0o660 modes are indistinguishable and "
            "this permission-only-change scenario cannot occur"
        )
    root = _repo(tmp_path, {"secret.txt": "unchanged\n"})
    _git(root, "config", "core.filemode", "false")
    secret = root / "secret.txt"
    secret.chmod(0o600)
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-chmod-only"
    )
    journal.begin_turn("turn-1")
    secret.chmod(0o640)
    journal.accept_turn("turn-1")

    completed = journal.finish_turn("turn-1")

    assert completed.changed_paths == ("secret.txt",)
    secret.chmod(0o660)
    with pytest.raises(CheckpointConflict) as conflict:
        journal.rollback()
    assert conflict.value.paths == ("secret.txt",)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o660

    secret.chmod(0o640)
    journal.rollback()
    assert secret.read_text(encoding="utf-8") == "unchanged\n"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_restore_manifest_failure_rolls_worktree_back_to_post_image(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path, {"tracked.txt": "A\n"})
    tracked = root / "tracked.txt"
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-restore-transaction"
    )
    journal.begin_turn("turn-1")
    tracked.write_text("B\n", encoding="utf-8")
    journal.accept_turn("turn-1")
    journal.finish_turn("turn-1")

    real_save = journal._save_manifest
    calls = 0

    def fail_final_save(manifest):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise CheckpointError("simulated final manifest failure")
        return real_save(manifest)

    monkeypatch.setattr(journal, "_save_manifest", fail_final_save)
    with pytest.raises(CheckpointError, match="metadata could not be committed"):
        journal.rollback(consume=False)

    assert tracked.read_text(encoding="utf-8") == "B\n"
    monkeypatch.setattr(journal, "_save_manifest", real_save)
    assert journal.completed_turn_ids() == ("turn-1",)
    journal.rollback(consume=False)
    assert tracked.read_text(encoding="utf-8") == "A\n"


def test_crash_left_partial_restore_is_recovered_without_overwriting_third_state(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path, {"a.txt": "A0\n", "b.txt": "B0\n"})
    journal = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-partial-restore"
    )
    journal.begin_turn("turn-1")
    (root / "a.txt").write_text("A1\n", encoding="utf-8")
    (root / "b.txt").write_text("B1\n", encoding="utf-8")
    journal.accept_turn("turn-1")
    journal.finish_turn("turn-1")

    real_apply = journal._apply_entries
    calls = 0

    def crash_mid_apply(payloads):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_path = sorted(payloads)[0]
            real_apply({first_path: payloads[first_path]})
            raise CheckpointError("simulated process crash")
        raise CheckpointError("simulated recovery interruption")

    monkeypatch.setattr(journal, "_apply_entries", crash_mid_apply)
    with pytest.raises(CheckpointError, match="recovery also failed"):
        journal.rollback(consume=False)

    resumed = CodexCheckpointJournal(
        str(root), tmp_path / "state", "session-partial-restore"
    )
    assert (root / "a.txt").read_text(encoding="utf-8") == "A1\n"
    assert (root / "b.txt").read_text(encoding="utf-8") == "B1\n"
    assert resumed.completed_turn_ids() == ("turn-1",)


def test_crash_recovery_only_slots_durably_accepted_turns(tmp_path):
    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    state = tmp_path / "state"

    unaccepted = CodexCheckpointJournal(str(root), state, "unaccepted")
    unaccepted.begin_turn("turn-not-created")
    resumed_unaccepted = CodexCheckpointJournal(str(root), state, "unaccepted")
    assert resumed_unaccepted.recover_active_as_unavailable("restart") == (
        "turn-not-created", False
    )
    assert resumed_unaccepted.completed_turn_ids() == ()

    accepted = CodexCheckpointJournal(str(root), state, "accepted")
    accepted.begin_turn("turn-created")
    accepted.accept_turn("turn-created")
    resumed_accepted = CodexCheckpointJournal(str(root), state, "accepted")
    assert resumed_accepted.recover_active_as_unavailable("restart") == (
        "turn-created", True
    )
    assert resumed_accepted.completed_turn_ids() == ("turn-created",)


def test_git_boundary_abort_and_cleanup_are_explicit(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotGitWorkspaceError):
        CodexCheckpointJournal(str(plain), tmp_path / "state", "plain")

    root = _repo(tmp_path, {"tracked.txt": "base\n"})
    with pytest.raises(CheckpointError, match="outside the repository"):
        CodexCheckpointJournal(str(root), root / ".checkpoint", "inside")
    assert not (root / ".checkpoint").exists()

    journal = CodexCheckpointJournal(str(root), tmp_path / "outside-state", "cleanup")
    session_dir = journal.session_dir
    journal.begin_turn("turn-active")
    with pytest.raises(CheckpointError, match="active"):
        journal.cleanup()
    assert session_dir.exists()
    assert journal.abort_turn("turn-active") is True
    assert journal.abort_turn("turn-active") is False
    journal.cleanup()
    assert not session_dir.exists()
    with pytest.raises(CheckpointError, match="closed"):
        journal.completed_turn_ids()

    forced = CodexCheckpointJournal(
        str(root), tmp_path / "outside-state", "force-cleanup"
    )
    forced.begin_turn("turn-active")
    forced.cleanup(force=True)
    assert not forced.session_dir.exists()

    corrupt = CodexCheckpointJournal(
        str(root), tmp_path / "outside-state", "force-corrupt-cleanup"
    )
    corrupt.manifest_path.write_text("{not-json", encoding="utf-8")
    corrupt.cleanup(force=True)
    assert not corrupt.session_dir.exists()
