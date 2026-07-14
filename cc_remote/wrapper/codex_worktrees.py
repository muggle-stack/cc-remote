"""Create deterministic, wrapper-owned Git worktrees for persistent Codex forks.

The app-server's ``thread/fork`` accepts an existing ``cwd`` but deliberately
does not create a Git worktree.  This module owns that filesystem step.  Client
input only influences a bounded slug; target paths always stay below the
wrapper state directory (or a safe sibling fallback when the state directory
itself lives inside the source repository).
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from cc_remote.wrapper.child_env import sanitized_child_env


_GIT_TIMEOUT = 60
_ERROR_TEXT_MAX = 2000
_SLUG_MAX = 40


class WorktreeError(RuntimeError):
    """A safe, user-displayable worktree preparation failure."""


@dataclass(frozen=True)
class WorktreeSpec:
    repository_root: str
    worktree_root: str
    cwd: str
    branch: str
    created: bool
    branch_created: bool


def _slug(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:_SLUG_MAX]
    return slug or fallback


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=sanitized_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError("Git 操作超时") from exc
    except OSError as exc:
        raise WorktreeError(f"无法启动 Git: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise WorktreeError(detail[-_ERROR_TEXT_MAX:])
    return result


def _registered_worktrees(repository_root: str) -> dict[str, str | None]:
    result = _git(repository_root, "worktree", "list", "--porcelain")
    out: dict[str, str | None] = {}
    path: str | None = None
    branch: str | None = None
    for line in (result.stdout + "\n").splitlines():
        if line.startswith("worktree "):
            path = os.path.realpath(line[len("worktree "):])
            branch = None
        elif line.startswith("branch "):
            branch = line[len("branch "):]
        elif not line and path:
            out[path] = branch
            path = None
            branch = None
    return out


def prepare_worktree(
    source_cwd: str,
    requested_name: str,
    request_id: str,
    state_dir: Path,
) -> WorktreeSpec:
    """Create or recover the deterministic worktree for one reliable request.

    Reusing ``request_id`` yields the same branch/path.  That turns an ACK-loss
    replay into recovery instead of creating a second worktree.
    """
    source = os.path.realpath(os.path.expanduser(source_cwd))
    if not os.path.isdir(source):
        raise WorktreeError(f"会话目录不存在: {source_cwd}")

    root_result = _git(source, "rev-parse", "--show-toplevel")
    repository_root = os.path.realpath(root_result.stdout.strip())
    if not repository_root or not os.path.isdir(repository_root):
        raise WorktreeError("当前会话不在 Git 仓库中")
    try:
        relative_cwd = os.path.relpath(source, repository_root)
        if relative_cwd == os.pardir or relative_cwd.startswith(os.pardir + os.sep):
            raise ValueError
    except ValueError as exc:
        raise WorktreeError("会话目录不属于当前 Git 仓库") from exc
    _git(repository_root, "rev-parse", "--verify", "HEAD")

    label = _slug(requested_name, "fork")
    token = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:10]
    repo_key = (
        f"{_slug(os.path.basename(repository_root), 'repo')}-"
        f"{hashlib.sha256(repository_root.encode('utf-8')).hexdigest()[:8]}"
    )
    base = os.path.realpath(os.path.expanduser(str(Path(state_dir) / "worktrees" / repo_key)))
    try:
        if os.path.commonpath((repository_root, base)) == repository_root:
            base = os.path.realpath(os.path.join(
                os.path.dirname(repository_root), f".cc-remote-worktrees-{repo_key}"))
    except ValueError:
        pass
    os.makedirs(base, mode=0o700, exist_ok=True)

    worktree_root = os.path.realpath(os.path.join(base, f"{label}-{token}"))
    if os.path.commonpath((base, worktree_root)) != base:
        raise WorktreeError("工作树目标路径无效")
    branch = f"cc-remote/{label}-{token}"
    expected_ref = f"refs/heads/{branch}"

    registered = _registered_worktrees(repository_root)
    if worktree_root in registered:
        if registered[worktree_root] != expected_ref:
            raise WorktreeError("目标路径已被另一个 Git 工作树占用")
        fork_cwd = (
            worktree_root if relative_cwd == os.curdir
            else os.path.join(worktree_root, relative_cwd)
        )
        if not os.path.isdir(fork_cwd):
            raise WorktreeError("已存在的工作树缺少原会话子目录")
        return WorktreeSpec(
            repository_root=repository_root,
            worktree_root=worktree_root,
            cwd=os.path.realpath(fork_cwd),
            branch=branch,
            created=False,
            branch_created=False,
        )
    if os.path.lexists(worktree_root):
        raise WorktreeError("目标路径已存在但不是当前仓库的工作树")

    branch_exists = _git(
        repository_root, "show-ref", "--verify", "--quiet", expected_ref,
        check=False,
    ).returncode == 0
    if branch_exists:
        _git(repository_root, "worktree", "add", worktree_root, branch)
    else:
        _git(repository_root, "worktree", "add", "-b", branch, worktree_root, "HEAD")

    fork_cwd = (
        worktree_root if relative_cwd == os.curdir
        else os.path.join(worktree_root, relative_cwd)
    )
    if not os.path.isdir(fork_cwd):
        spec = WorktreeSpec(
            repository_root=repository_root,
            worktree_root=worktree_root,
            cwd=os.path.realpath(fork_cwd),
            branch=branch,
            created=True,
            branch_created=not branch_exists,
        )
        rollback_worktree(spec)
        raise WorktreeError("新工作树缺少原会话子目录")
    return WorktreeSpec(
        repository_root=repository_root,
        worktree_root=worktree_root,
        cwd=os.path.realpath(fork_cwd),
        branch=branch,
        created=True,
        branch_created=not branch_exists,
    )


def rollback_worktree(spec: WorktreeSpec) -> None:
    """Best-effort rollback for a worktree created by this invocation only."""
    if not spec.created:
        return
    _git(
        spec.repository_root, "worktree", "remove", "--force", spec.worktree_root,
        check=False,
    )
    if spec.branch_created:
        _git(spec.repository_root, "branch", "-D", spec.branch, check=False)
