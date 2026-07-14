from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cc_remote.wrapper.codex_worktrees import (
    WorktreeError,
    prepare_worktree,
    rollback_worktree,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "cc-remote test")
    _git(root, "config", "user.email", "cc-remote@example.invalid")
    (root / "component").mkdir()
    (root / "component" / "tracked.txt").write_text("base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def test_prepare_worktree_preserves_session_subdirectory_and_is_idempotent(tmp_path):
    root = _repo(tmp_path)
    state_dir = tmp_path / "state"

    first = prepare_worktree(
        str(root / "component"), "Feature Name", "request-123", state_dir)

    assert first.created is True
    assert first.branch_created is True
    assert first.cwd == str(Path(first.worktree_root) / "component")
    assert Path(first.cwd, "tracked.txt").read_text() == "base\n"
    assert _git(Path(first.worktree_root), "branch", "--show-current") == first.branch

    replay = prepare_worktree(
        str(root / "component"), "Feature Name", "request-123", state_dir)

    assert replay.created is False
    assert replay.worktree_root == first.worktree_root
    assert replay.cwd == first.cwd
    assert replay.branch == first.branch
    assert _git(root, "worktree", "list", "--porcelain").count(
        f"worktree {first.worktree_root}") == 1

    rollback_worktree(first)
    assert not Path(first.worktree_root).exists()
    refs = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet",
         f"refs/heads/{first.branch}"],
    )
    assert refs.returncode != 0


def test_prepare_worktree_rejects_non_git_directory(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()

    with pytest.raises(WorktreeError):
        prepare_worktree(str(source), "fork", "request-plain", tmp_path / "state")


def test_prepare_worktree_does_not_reuse_unregistered_target(tmp_path):
    root = _repo(tmp_path)
    state_dir = tmp_path / "state"
    created = prepare_worktree(str(root), "collision", "same-request", state_dir)
    rollback_worktree(created)
    Path(created.worktree_root).mkdir(parents=True)

    with pytest.raises(WorktreeError, match="不是当前仓库的工作树"):
        prepare_worktree(str(root), "collision", "same-request", state_dir)
