"""Client-owned Git checkpoints for undoing one or more Codex turns.

Codex app-server's deprecated ``thread/rollback`` only prunes conversation
history.  It explicitly leaves filesystem restoration to the client.  This
module supplies that filesystem half without touching the user's Git index:

* a private temporary index snapshots the complete visible worktree into a
  journal-owned alternate object database;
* each completed turn records its pre/post trees and the index entries for the
  paths it changed;
* rollback first verifies every current path still matches the recorded
  post-image, then restores only those paths;
* any later file or staging change is a conflict, and no path is overwritten.

Ignored files and dirty submodule worktrees are intentionally outside this
checkpoint boundary.  Empty directories are not representable in Git trees.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from cc_remote.wrapper.child_env import sanitized_child_env
from cc_remote.wrapper.file_lock_compat import flock, LOCK_EX, LOCK_UN
from cc_remote.wrapper.os_compat import force_rmtree


_FORMAT_VERSION = 2
_GIT_TIMEOUT = 120
_ERROR_TEXT_MAX = 2000
_TURN_ID_MAX = 512
# File rollback intentionally keeps a smaller window than app-server's native
# 1000-turn conversation rollback. This bounds each long-lived session on small
# self-hosted disks; requests beyond the retained file window fail safely while
# conversation-only rollback remains available and count alignment is preserved.
_MAX_RETAINED_TURNS = 100
_MAX_OBJECT_BYTES = 256 * 1024 * 1024


class CheckpointError(RuntimeError):
    """A safe, user-displayable checkpoint failure."""


class NotGitWorkspaceError(CheckpointError):
    """The requested working directory is not inside a usable Git repository."""


class CheckpointIndexChanged(CheckpointError):
    """A turn changed the user's Git index, which Remote never rewrites."""

    def __init__(self, paths: tuple[str, ...]):
        self.paths = paths
        super().__init__(
            "Git index changed during the checkpointed turn: "
            + ", ".join(paths[:8])
        )


class CheckpointConflict(CheckpointError):
    """Current paths no longer match the checkpoint's expected post-image."""

    def __init__(
        self,
        paths: tuple[str, ...],
        *,
        index_paths: tuple[str, ...] = (),
    ):
        self.paths = paths
        self.index_paths = index_paths
        details: list[str] = []
        if paths:
            details.append("files=" + ", ".join(paths[:8]))
        if index_paths:
            details.append("index=" + ", ".join(index_paths[:8]))
        super().__init__("Checkpoint conflicts: " + "; ".join(details))


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    oid: str


@dataclass(frozen=True)
class CompletedCheckpoint:
    turn_id: str
    changed_paths: tuple[str, ...]
    files_available: bool = True


@dataclass(frozen=True)
class RollbackResult:
    turn_ids: tuple[str, ...]
    restored_paths: tuple[str, ...]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _run(
    argv: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    check: bool = True,
    stdin: Optional[bytes] = None,
) -> subprocess.CompletedProcess[bytes]:
    child_env = sanitized_child_env(env)
    child_env["GIT_OPTIONAL_LOCKS"] = "0"
    child_env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckpointError("Git checkpoint operation timed out") from exc
    except OSError as exc:
        raise CheckpointError(f"Unable to start Git: {exc}") from exc
    if check and result.returncode != 0:
        detail = _decode(result.stderr or result.stdout or b"git command failed")
        raise CheckpointError(detail.strip()[-_ERROR_TEXT_MAX:])
    return result


def _discover_repository(cwd: str) -> tuple[str, str, str]:
    source = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(source):
        raise NotGitWorkspaceError(f"Working directory does not exist: {cwd}")
    root_result = _run(
        ["git", "-C", source, "rev-parse", "--show-toplevel"],
        check=False,
    )
    if root_result.returncode != 0:
        raise NotGitWorkspaceError("Code rollback requires a Git repository")
    root = os.path.realpath(_decode(root_result.stdout).strip())
    if not root or not os.path.isdir(root):
        raise NotGitWorkspaceError("Git repository root is unavailable")

    git_dir_raw = _decode(
        _run(
            ["git", "-C", root, "rev-parse", "--absolute-git-dir"],
        ).stdout
    ).strip()
    common_raw = _decode(
        _run(
            ["git", "-C", root, "rev-parse", "--git-common-dir"],
        ).stdout
    ).strip()
    git_dir = os.path.realpath(git_dir_raw)
    common_dir = os.path.realpath(
        common_raw if os.path.isabs(common_raw) else os.path.join(root, common_raw)
    )
    if not os.path.isdir(git_dir) or not os.path.isdir(common_dir):
        raise NotGitWorkspaceError("Git metadata directory is unavailable")
    return root, git_dir, common_dir


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class CodexCheckpointJournal:
    """Persistent per-session checkpoint journal for one Git worktree."""

    def __init__(self, cwd: str, state_dir: Path, session_id: str):
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        self.repository_root, self.git_dir, self.common_git_dir = _discover_repository(
            cwd
        )
        self.session_id = session_id
        self.state_root = Path(os.path.realpath(Path(state_dir).expanduser()))
        try:
            if (
                os.path.commonpath((self.repository_root, str(self.state_root)))
                == self.repository_root
            ):
                raise CheckpointError(
                    "Checkpoint state directory must be outside the repository"
                )
        except ValueError:
            pass
        self.state_root.mkdir(mode=0o700, parents=True, exist_ok=True)

        repository_key = hashlib.sha256(
            self.repository_root.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:20]
        session_key = hashlib.sha256(
            session_id.encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24]
        self.session_dir = (
            self.state_root / "codex-checkpoints" / repository_key / session_key
        )
        self.objects_dir = self.session_dir / "objects"
        self.temp_dir = self.session_dir / "tmp"
        self.manifest_path = self.session_dir / "manifest.json"
        self.lock_path = self.session_dir / "journal.lock"
        self._closed = False

        self.temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize_object_store()

        with self._locked():
            if self.manifest_path.exists():
                manifest = self._load_manifest()
                if manifest.get("restore") is not None:
                    self._recover_restore(manifest)
            else:
                self._save_manifest(self._new_manifest())

    def _initialize_object_store(self) -> None:
        (self.objects_dir / "info").mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.objects_dir / "pack").mkdir(mode=0o700, parents=True, exist_ok=True)
        main_objects = Path(self.common_git_dir) / "objects"
        if "\n" in str(main_objects):
            raise CheckpointError(
                "Git object directory contains an unsupported newline"
            )
        alternates = self.objects_dir / "info" / "alternates"
        expected_alternates = (str(main_objects) + "\n").encode("utf-8")
        if alternates.exists():
            if alternates.read_bytes() != expected_alternates:
                raise CheckpointError(
                    "Checkpoint object alternate does not match repository"
                )
        else:
            _atomic_write(alternates, expected_alternates)

    def _new_manifest(self) -> dict[str, Any]:
        return {
            "version": _FORMAT_VERSION,
            "repository_root": self.repository_root,
            "session_id": self.session_id,
            "active": None,
            "restore": None,
            "turns": [],
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise CheckpointError("Checkpoint journal is closed")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._ensure_open()
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flock(fd, LOCK_EX)
            yield
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    def _load_manifest(self) -> dict[str, Any]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CheckpointError("Checkpoint manifest is corrupt") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != _FORMAT_VERSION
            or manifest.get("repository_root") != self.repository_root
            or manifest.get("session_id") != self.session_id
            or not isinstance(manifest.get("turns"), list)
            or "restore" not in manifest
        ):
            raise CheckpointError("Checkpoint manifest does not match this session")
        return manifest

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        payload = json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        _atomic_write(self.manifest_path, payload)

    def _active_snapshot_path(self, active: dict[str, Any]) -> Path:
        checkpoint_id = active.get("checkpoint_id")
        if not isinstance(checkpoint_id, str):
            raise CheckpointError("Active checkpoint metadata is invalid")
        try:
            parsed_id = uuid.UUID(checkpoint_id)
        except ValueError as exc:
            raise CheckpointError("Active checkpoint metadata is invalid") from exc
        expected_name = f"tmp/{parsed_id.hex}.index"
        if (
            checkpoint_id != parsed_id.hex
            or active.get("index_snapshot") != expected_name
        ):
            raise CheckpointError("Active checkpoint metadata is invalid")
        return self.temp_dir / f"{parsed_id.hex}.index"

    def _git_env(self, *, index_file: Optional[Path] = None) -> dict[str, str]:
        env = sanitized_child_env()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env["GIT_OBJECT_DIRECTORY"] = str(self.objects_dir)
        if index_file is not None:
            env["GIT_INDEX_FILE"] = str(index_file)
        return env

    def _git(
        self,
        *args: str,
        env: Optional[dict[str, str]] = None,
        check: bool = True,
        stdin: Optional[bytes] = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return _run(
            ["git", "-C", self.repository_root, *args],
            env=env,
            check=check,
            stdin=stdin,
        )

    def _capture_tree(self) -> str:
        fd, temporary_name = tempfile.mkstemp(prefix="index-", dir=self.temp_dir)
        os.close(fd)
        os.unlink(temporary_name)  # Git requires a missing path, not an empty index.
        index_path = Path(temporary_name)
        env = self._git_env(index_file=index_path)
        try:
            head = self._git(
                "rev-parse",
                "--verify",
                "--quiet",
                "HEAD^{tree}",
                env=env,
                check=False,
            )
            if head.returncode == 0:
                self._git("read-tree", "HEAD", env=env)
            else:
                self._git("read-tree", "--empty", env=env)
            # The tree is a filesystem image, so preserve executable bits even
            # when the user's repository disables filemode comparisons.
            self._git("-c", "core.filemode=true", "add", "-A", "--", ".", env=env)
            tree = _decode(self._git("write-tree", env=env).stdout).strip()
            if len(tree) < 40:
                raise CheckpointError("Git did not return a checkpoint tree")
            return tree
        finally:
            try:
                index_path.unlink()
            except FileNotFoundError:
                pass

    def _capture_stable_state(
        self,
    ) -> tuple[
        str,
        bytes,
        dict[str, tuple[str, ...]],
        dict[str, int],
        Optional[str],
    ]:
        """Capture HEAD/index/tree together without accepting a mixed image."""
        head_before = self._head_oid()
        index_before, _ = self._index_snapshot()
        first_tree = self._capture_tree()
        first_modes = self._permission_overrides(first_tree)
        head_middle = self._head_oid()
        index_middle, _ = self._index_snapshot()
        second_tree = self._capture_tree()
        second_modes = self._permission_overrides(second_tree)
        head_after = self._head_oid()
        index_after, index_entries = self._index_snapshot()
        if not (head_before == head_middle == head_after):
            raise CheckpointError("Git HEAD changed while capturing checkpoint")
        if not (index_before == index_middle == index_after):
            raise CheckpointError("Git index changed while capturing checkpoint")
        if first_tree != second_tree or first_modes != second_modes:
            raise CheckpointError("Worktree changed while capturing checkpoint")
        return second_tree, index_after, index_entries, second_modes, head_after

    def _head_oid(self) -> Optional[str]:
        result = self._git(
            "rev-parse", "--verify", "--quiet", "HEAD", check=False
        )
        if result.returncode != 0:
            return None
        oid = _decode(result.stdout).strip()
        if len(oid) < 40:
            raise CheckpointError("Git returned an invalid HEAD")
        return oid

    def _clean_head_advance(
        self,
        pre_head: Optional[str],
        post_head: Optional[str],
        post_tree: str,
    ) -> bool:
        """Accept only a forward commit with a clean index and worktree."""
        if not pre_head or not post_head or pre_head == post_head:
            return False
        if self._git(
            "merge-base", "--is-ancestor", pre_head, post_head, check=False
        ).returncode != 0:
            return False
        head_tree_result = self._git(
            "rev-parse", "--verify", f"{post_head}^{{tree}}", check=False
        )
        index_tree_result = self._git(
            "write-tree", env=self._git_env(), check=False
        )
        if head_tree_result.returncode != 0 or index_tree_result.returncode != 0:
            return False
        head_tree = _decode(head_tree_result.stdout).strip()
        index_tree = _decode(index_tree_result.stdout).strip()
        return bool(head_tree) and post_tree == head_tree == index_tree

    @staticmethod
    def _trim_turn_window(manifest: dict[str, Any]) -> None:
        turns = manifest["turns"]
        if len(turns) > _MAX_RETAINED_TURNS:
            manifest["turns"] = turns[-_MAX_RETAINED_TURNS:]

    def _object_store_exceeds_limit(self) -> bool:
        total = 0
        try:
            for root, _dirs, files in os.walk(self.objects_dir):
                for name in files:
                    total += os.lstat(os.path.join(root, name)).st_size
                    if total > _MAX_OBJECT_BYTES:
                        return True
        except OSError as exc:
            raise CheckpointError("Unable to measure checkpoint object store") from exc
        return False

    def _invalidate_file_history_for_retention(
        self, manifest: dict[str, Any]
    ) -> None:
        """Bound disk use without breaking native count-based turn alignment."""
        for record in manifest["turns"]:
            record["available"] = False
            record["files_restored"] = False
            record["reason"] = "checkpoint retention limit reached"
            record["paths"] = []
            record.pop("pre_tree", None)
            record.pop("post_tree", None)
            record.pop("index_entries", None)
            record.pop("pre_modes", None)
            record.pop("post_modes", None)
        # Persist tombstones before deleting their backing objects. A crash can
        # therefore leave extra disk usage, never a manifest pointing at data
        # that has already disappeared.
        self._save_manifest(manifest)
        retired = self.objects_dir.with_name(
            f".{self.objects_dir.name}.retired-{uuid.uuid4().hex}"
        )
        try:
            os.replace(self.objects_dir, retired)
            self._initialize_object_store()
            force_rmtree(retired)
        except OSError as exc:
            raise CheckpointError("Unable to compact checkpoint object store") from exc

    def _tree_entries(self, tree: str) -> dict[str, TreeEntry]:
        output = self._git(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            tree,
            env=self._git_env(),
        ).stdout
        entries: dict[str, TreeEntry] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                raw_mode, raw_kind, raw_oid = header.split(b" ", 2)
            except ValueError as exc:
                raise CheckpointError("Git returned an invalid tree record") from exc
            path = os.fsdecode(raw_path)
            entries[path] = TreeEntry(
                mode=raw_mode.decode("ascii"),
                kind=raw_kind.decode("ascii"),
                oid=raw_oid.decode("ascii"),
            )
        return entries

    @staticmethod
    def _canonical_permission(entry: Optional[TreeEntry]) -> Optional[int]:
        if entry is None or entry.mode == "120000":
            return None
        return 0o755 if entry.mode == "100755" else 0o644

    def _permission_overrides(self, tree: str) -> dict[str, int]:
        """Capture permission bits Git trees otherwise collapse to 0644/0755."""
        overrides: dict[str, int] = {}
        for path, entry in self._tree_entries(tree).items():
            canonical = self._canonical_permission(entry)
            if canonical is None:
                continue
            target = self._safe_path(path)
            try:
                raw_mode = target.lstat().st_mode
            except FileNotFoundError as exc:
                raise CheckpointError(
                    "Worktree changed while capturing file permissions"
                ) from exc
            if not stat.S_ISREG(raw_mode):
                raise CheckpointError(
                    f"Checkpoint tree path is not a regular file: {path}"
                )
            permission = stat.S_IMODE(raw_mode)
            if permission != canonical:
                overrides[path] = permission
        return overrides

    @classmethod
    def _permission_for(
        cls,
        path: str,
        entry: Optional[TreeEntry],
        overrides: dict[str, Any],
    ) -> Optional[int]:
        canonical = cls._canonical_permission(entry)
        if canonical is None:
            return None
        value = overrides.get(path, canonical)
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= 0o7777:
            raise CheckpointError("Checkpoint file permissions are invalid")
        return value

    def _index_snapshot(self) -> tuple[bytes, dict[str, tuple[str, ...]]]:
        output = self._git(
            "ls-files",
            "--stage",
            "-v",
            "-z",
            env=sanitized_child_env(),
        ).stdout
        entries: dict[str, list[str]] = {}
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
            except ValueError as exc:
                raise CheckpointError("Git returned an invalid index record") from exc
            path = os.fsdecode(raw_path)
            entries.setdefault(path, []).append(header.decode("ascii"))
        return output, {path: tuple(values) for path, values in entries.items()}

    @staticmethod
    def _validate_turn_id(turn_id: str) -> str:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("turn_id is required")
        if len(turn_id) > _TURN_ID_MAX:
            raise ValueError(f"turn_id exceeds {_TURN_ID_MAX} characters")
        return turn_id

    def begin_turn(self, turn_id: str) -> str:
        """Capture the pre-turn worktree and mark one checkpoint active."""
        turn_id = self._validate_turn_id(turn_id)
        with self._locked():
            manifest = self._load_manifest()
            if manifest.get("active") is not None:
                raise CheckpointError("A checkpoint is already active")
            if any(turn.get("turn_id") == turn_id for turn in manifest["turns"]):
                raise CheckpointError("This turn already has a checkpoint")

            pre_tree, index_before, _, pre_modes, pre_head = (
                self._capture_stable_state()
            )

            checkpoint_id = uuid.uuid4().hex
            index_snapshot_name = f"tmp/{checkpoint_id}.index"
            _atomic_write(self.session_dir / index_snapshot_name, index_before)
            manifest["active"] = {
                "checkpoint_id": checkpoint_id,
                "turn_id": turn_id,
                "pre_tree": pre_tree,
                "pre_head": pre_head,
                "pre_modes": pre_modes,
                "index_snapshot": index_snapshot_name,
                "accepted": False,
                "started_at_ns": time.time_ns(),
            }
            self._save_manifest(manifest)
            return pre_tree

    def accept_turn(self, turn_id: str) -> None:
        """Durably mark that app-server accepted the active native turn."""
        turn_id = self._validate_turn_id(turn_id)
        with self._locked():
            manifest = self._load_manifest()
            active = manifest.get("active")
            if not isinstance(active, dict) or active.get("turn_id") != turn_id:
                raise CheckpointError("No matching active checkpoint")
            if active.get("accepted") is True:
                return
            active["accepted"] = True
            self._save_manifest(manifest)

    def finish_turn(self, turn_id: str) -> CompletedCheckpoint:
        """Capture the post-image and append an immutable completed record."""
        turn_id = self._validate_turn_id(turn_id)
        with self._locked():
            manifest = self._load_manifest()
            active = manifest.get("active")
            if not isinstance(active, dict) or active.get("turn_id") != turn_id:
                raise CheckpointError("No matching active checkpoint")
            if active.get("accepted") is not True:
                raise CheckpointError("Active checkpoint turn was not accepted")
            snapshot_path = self._active_snapshot_path(active)
            try:
                pre_index_raw = snapshot_path.read_bytes()
            except OSError as exc:
                raise CheckpointError(
                    "Active checkpoint index snapshot is missing"
                ) from exc

            post_tree, post_index_raw, post_index_entries, post_modes, post_head = (
                self._capture_stable_state()
            )

            pre_entries = self._tree_entries(str(active["pre_tree"]))
            post_entries = self._tree_entries(post_tree)
            pre_modes = active.get("pre_modes", {})
            if not isinstance(pre_modes, dict):
                raise CheckpointError("Active checkpoint permissions are invalid")
            changed_paths = tuple(
                sorted(
                    path
                    for path in set(pre_entries) | set(post_entries)
                    if (
                        pre_entries.get(path) != post_entries.get(path)
                        or self._permission_for(
                            path, pre_entries.get(path), pre_modes
                        ) != self._permission_for(
                            path, post_entries.get(path), post_modes
                        )
                    )
                )
            )
            unsupported = tuple(
                path
                for path in changed_paths
                if any(
                    entry is not None and entry.kind != "blob"
                    for entry in (pre_entries.get(path), post_entries.get(path))
                )
            )
            if unsupported:
                manifest["active"] = None
                self._save_manifest(manifest)
                snapshot_path.unlink(missing_ok=True)
                raise CheckpointError(
                    "Changed submodule entries cannot be checkpointed: "
                    + ", ".join(unsupported[:8])
                )

            pre_index_entries = self._parse_index_snapshot(pre_index_raw)
            # The journal deliberately never mutates the user's real index.
            # Therefore *any* staging change during a turn makes file rollback
            # incomplete, including pure `git add` with no worktree tree delta.
            changed_index_paths = tuple(
                sorted(
                    path
                    for path in set(pre_index_entries) | set(post_index_entries)
                    if pre_index_entries.get(path, ())
                    != post_index_entries.get(path, ())
                )
            )
            if pre_index_raw != post_index_raw and not changed_index_paths:
                # Unknown/new index extensions changed. Fail closed rather than
                # claiming a checkpoint that cannot restore the same Git state.
                changed_index_paths = ("<index-metadata>",)
            pre_head = active.get("pre_head")
            if pre_head is not None and not isinstance(pre_head, str):
                raise CheckpointError("Active checkpoint HEAD is invalid")
            if self._clean_head_advance(
                pre_head, post_head, post_tree
            ):
                # A normal commit legitimately advances both HEAD and index.
                # File rollback cannot rewrite Git history, but this tombstone
                # keeps native conversation rollback counts aligned.
                manifest["turns"].append(
                    {
                        "checkpoint_id": active["checkpoint_id"],
                        "turn_id": turn_id,
                        "available": False,
                        "files_restored": False,
                        "reason": "clean HEAD advance",
                        "paths": [],
                        "started_at_ns": active["started_at_ns"],
                        "finished_at_ns": time.time_ns(),
                    }
                )
                manifest["active"] = None
                self._trim_turn_window(manifest)
                self._save_manifest(manifest)
                snapshot_path.unlink(missing_ok=True)
                if self._object_store_exceeds_limit():
                    self._invalidate_file_history_for_retention(manifest)
                    raise CheckpointError("Checkpoint retention limit reached")
                return CompletedCheckpoint(
                    turn_id=turn_id,
                    changed_paths=(),
                    files_available=False,
                )
            if changed_index_paths:
                manifest["active"] = None
                self._save_manifest(manifest)
                snapshot_path.unlink(missing_ok=True)
                raise CheckpointIndexChanged(changed_index_paths)

            record = {
                "checkpoint_id": active["checkpoint_id"],
                "turn_id": turn_id,
                "available": True,
                "files_restored": False,
                "pre_tree": active["pre_tree"],
                "post_tree": post_tree,
                "pre_modes": {
                    path: pre_modes[path]
                    for path in changed_paths
                    if path in pre_modes
                },
                "post_modes": {
                    path: post_modes[path]
                    for path in changed_paths
                    if path in post_modes
                },
                "paths": list(changed_paths),
                "index_entries": {
                    path: list(post_index_entries.get(path, ()))
                    for path in changed_paths
                },
                "started_at_ns": active["started_at_ns"],
                "finished_at_ns": time.time_ns(),
            }
            manifest["turns"].append(record)
            manifest["active"] = None
            self._trim_turn_window(manifest)
            self._save_manifest(manifest)
            snapshot_path.unlink(missing_ok=True)
            if self._object_store_exceeds_limit():
                self._invalidate_file_history_for_retention(manifest)
                raise CheckpointError("Checkpoint retention limit reached")
            return CompletedCheckpoint(turn_id=turn_id, changed_paths=changed_paths)

    def record_unavailable(self, turn_id: str, reason: str = "") -> None:
        """Keep conversation/checkpoint counts aligned after capture failure.

        A missing record would make every later count-based rollback target the
        wrong native turn.  The tombstone intentionally cannot restore files,
        but conversation-only rollback can still discard it in lockstep with
        app-server history.
        """
        turn_id = self._validate_turn_id(turn_id)
        with self._locked():
            manifest = self._load_manifest()
            if manifest.get("active") is not None:
                raise CheckpointError(
                    "Cannot record an unavailable turn while a checkpoint is active"
                )
            if any(turn.get("turn_id") == turn_id for turn in manifest["turns"]):
                return
            manifest["turns"].append(
                {
                    "checkpoint_id": uuid.uuid4().hex,
                    "turn_id": turn_id,
                    "available": False,
                    "files_restored": False,
                    "reason": str(reason)[:200],
                    "paths": [],
                    "finished_at_ns": time.time_ns(),
                }
            )
            self._trim_turn_window(manifest)
            self._save_manifest(manifest)

    def recover_active_as_unavailable(
        self, reason: str = ""
    ) -> Optional[tuple[str, bool]]:
        """Recover a crash-left capture without inventing a native turn.

        Only an active record whose acceptance boundary was durably crossed is
        converted into a count-preserving tombstone. A pre-acceptance capture is
        simply aborted because app-server may never have created that turn.
        """
        with self._locked():
            manifest = self._load_manifest()
            active = manifest.get("active")
            if active is None:
                return None
            if not isinstance(active, dict):
                raise CheckpointError("Active checkpoint metadata is invalid")
            turn_id = self._validate_turn_id(active.get("turn_id"))
            accepted = active.get("accepted") is True
            snapshot_path = self._active_snapshot_path(active)
            if accepted and not any(
                turn.get("turn_id") == turn_id for turn in manifest["turns"]
            ):
                manifest["turns"].append(
                    {
                        "checkpoint_id": active["checkpoint_id"],
                        "turn_id": turn_id,
                        "available": False,
                        "files_restored": False,
                        "reason": str(reason)[:200],
                        "paths": [],
                        "finished_at_ns": time.time_ns(),
                    }
                )
            manifest["active"] = None
            self._trim_turn_window(manifest)
            self._save_manifest(manifest)
            snapshot_path.unlink(missing_ok=True)
            return turn_id, accepted

    @staticmethod
    def _parse_index_snapshot(raw: bytes) -> dict[str, tuple[str, ...]]:
        entries: dict[str, list[str]] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
            except ValueError as exc:
                raise CheckpointError("Stored index snapshot is invalid") from exc
            entries.setdefault(os.fsdecode(raw_path), []).append(header.decode("ascii"))
        return {path: tuple(values) for path, values in entries.items()}

    def abort_turn(self, turn_id: str) -> bool:
        """Discard an unfinished checkpoint, for example after turn/start fails."""
        turn_id = self._validate_turn_id(turn_id)
        with self._locked():
            manifest = self._load_manifest()
            active = manifest.get("active")
            if active is None:
                return False
            if not isinstance(active, dict) or active.get("turn_id") != turn_id:
                raise CheckpointError("Another turn owns the active checkpoint")
            snapshot_path = self._active_snapshot_path(active)
            manifest["active"] = None
            self._save_manifest(manifest)
            snapshot_path.unlink(missing_ok=True)
            return True

    def completed_turn_ids(self) -> tuple[str, ...]:
        with self._locked():
            return tuple(
                str(turn["turn_id"]) for turn in self._load_manifest()["turns"]
            )

    def has_active_turn(self) -> bool:
        with self._locked():
            return self._load_manifest().get("active") is not None

    @staticmethod
    def _serialize_entry(entry: Optional[TreeEntry]) -> Optional[dict[str, str]]:
        if entry is None:
            return None
        return {"mode": entry.mode, "kind": entry.kind, "oid": entry.oid}

    @staticmethod
    def _deserialize_entry(value: Any) -> Optional[TreeEntry]:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"mode", "kind", "oid"}:
            raise CheckpointError("Checkpoint restore metadata is invalid")
        if not all(isinstance(value.get(key), str) for key in value):
            raise CheckpointError("Checkpoint restore metadata is invalid")
        return TreeEntry(
            mode=value["mode"], kind=value["kind"], oid=value["oid"]
        )

    def _snapshot_matches(
        self,
        paths: tuple[str, ...],
        current_entries: dict[str, TreeEntry],
        current_modes: dict[str, int],
        expected_entries: dict[str, Optional[TreeEntry]],
        expected_modes: dict[str, Any],
    ) -> bool:
        for path in paths:
            current_entry = current_entries.get(path)
            expected_entry = expected_entries.get(path)
            if current_entry != expected_entry:
                return False
            if self._permission_for(path, current_entry, current_modes) != (
                self._permission_for(path, expected_entry, expected_modes)
            ):
                return False
        return True

    def _finalize_restore(
        self, manifest: dict[str, Any], restore: dict[str, Any]
    ) -> None:
        checkpoint_ids = restore.get("checkpoint_ids")
        if not isinstance(checkpoint_ids, list) or not checkpoint_ids:
            raise CheckpointError("Checkpoint restore metadata is invalid")
        turns = manifest["turns"]
        selected = turns[-len(checkpoint_ids):]
        if [record.get("checkpoint_id") for record in selected] != checkpoint_ids:
            raise CheckpointError("Checkpoint restore tail no longer matches")
        if restore.get("consume") is True:
            manifest["turns"] = turns[:-len(selected)]
        else:
            for record in selected:
                record["files_restored"] = True
        manifest["restore"] = None
        self._save_manifest(manifest)

    def _recover_restore(self, manifest: dict[str, Any]) -> None:
        """Recover a crash between worktree mutation and manifest commit."""
        restore = manifest.get("restore")
        if not isinstance(restore, dict):
            raise CheckpointError("Checkpoint restore metadata is invalid")
        raw_paths = restore.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths \
                or not all(isinstance(path, str) for path in raw_paths):
            raise CheckpointError("Checkpoint restore metadata is invalid")
        paths = tuple(raw_paths)
        original_tree = restore.get("original_tree")
        if not isinstance(original_tree, str):
            raise CheckpointError("Checkpoint restore metadata is invalid")
        original_tree_entries = self._tree_entries(original_tree)
        original_entries = {
            path: original_tree_entries.get(path) for path in paths
        }
        raw_targets = restore.get("target_entries")
        if not isinstance(raw_targets, dict) or set(raw_targets) != set(paths):
            raise CheckpointError("Checkpoint restore metadata is invalid")
        target_entries = {
            path: self._deserialize_entry(raw_targets[path]) for path in paths
        }
        original_modes = restore.get("original_modes") or {}
        target_modes = restore.get("target_modes") or {}
        if not isinstance(original_modes, dict) or not isinstance(target_modes, dict):
            raise CheckpointError("Checkpoint restore metadata is invalid")

        current_tree, _, _, current_modes, _ = self._capture_stable_state()
        current_entries = self._tree_entries(current_tree)
        if self._snapshot_matches(
            paths, current_entries, current_modes, target_entries, target_modes
        ):
            self._finalize_restore(manifest, restore)
            return
        if self._snapshot_matches(
            paths, current_entries, current_modes, original_entries, original_modes
        ):
            manifest["restore"] = None
            self._save_manifest(manifest)
            return

        # A process can die between per-file atomic replaces. Recover only when
        # every path is still exactly one of the two recorded images; any third
        # state may be a later user edit and must never be overwritten.
        for path in paths:
            if self._snapshot_matches(
                (path,), current_entries, current_modes,
                {path: original_entries.get(path)}, original_modes,
            ):
                continue
            if self._snapshot_matches(
                (path,), current_entries, current_modes,
                {path: target_entries.get(path)}, target_modes,
            ):
                continue
            raise CheckpointConflict((path,))
        original_payloads = self._materialize_payloads(
            original_entries, original_modes
        )
        self._apply_entries(original_payloads)
        manifest["restore"] = None
        self._save_manifest(manifest)

    def rollback(self, num_turns: int = 1, *, consume: bool = True) -> RollbackResult:
        """Restore completed turns newest-first after an all-path preflight.

        ``consume=False`` keeps count alignment with native conversation turns.
        It is the integration mode for file-only restore and for the first half
        of a combined restore. A later successful conversation rollback calls
        :meth:`discard` to consume the corresponding records. Repeating the
        same non-consuming restore is idempotent.
        """
        if (
            not isinstance(num_turns, int)
            or isinstance(num_turns, bool)
            or num_turns < 1
        ):
            raise ValueError("num_turns must be a positive integer")
        with self._locked():
            manifest = self._load_manifest()
            if manifest.get("active") is not None:
                raise CheckpointError("Cannot rollback while a checkpoint is active")
            turns = manifest["turns"]
            if num_turns > len(turns):
                raise CheckpointError("Not enough completed checkpoints")
            selected = turns[-num_turns:]
            unavailable = tuple(
                str(record.get("turn_id") or "unknown")
                for record in selected
                if record.get("available", True) is not True
            )
            if unavailable:
                raise CheckpointError(
                    "Code checkpoint unavailable for turns: "
                    + ", ".join(unavailable[:8])
                )

            (
                current_tree,
                current_index_raw,
                current_index_entries,
                current_modes,
                _,
            ) = self._capture_stable_state()
            current_entries = self._tree_entries(current_tree)
            target_entries = dict(current_entries)
            target_modes: dict[str, Optional[int]] = {}
            touched: set[str] = set()
            index_conflicts: set[str] = set()

            for record in reversed(selected):
                paths = tuple(str(path) for path in record.get("paths", ()))
                expected_index = record.get("index_entries") or {}
                for path in paths:
                    if tuple(expected_index.get(path, ())) != current_index_entries.get(
                        path, ()
                    ):
                        index_conflicts.add(path)

                pre_entries = self._tree_entries(str(record["pre_tree"]))
                post_entries = self._tree_entries(str(record["post_tree"]))
                expected_entries = (
                    pre_entries if record.get("files_restored") else post_entries
                )
                expected_modes = (
                    record.get("pre_modes", {})
                    if record.get("files_restored")
                    else record.get("post_modes", {})
                )
                for path in paths:
                    if path not in target_modes:
                        target_modes[path] = self._permission_for(
                            path, target_entries.get(path), current_modes
                        )
                file_conflicts = tuple(
                    path
                    for path in paths
                    if (
                        target_entries.get(path) != expected_entries.get(path)
                        or target_modes.get(path) != self._permission_for(
                            path, expected_entries.get(path), expected_modes
                        )
                    )
                )
                if file_conflicts:
                    raise CheckpointConflict(
                        tuple(sorted(set(file_conflicts))),
                        index_paths=tuple(sorted(index_conflicts)),
                    )
                touched.update(paths)
                if not record.get("files_restored"):
                    for path in paths:
                        pre_entry = pre_entries.get(path)
                        if pre_entry is None:
                            target_entries.pop(path, None)
                        else:
                            target_entries[path] = pre_entry
                        target_modes[path] = self._permission_for(
                            path, pre_entry, record.get("pre_modes", {})
                        )

            if index_conflicts:
                raise CheckpointConflict((), index_paths=tuple(sorted(index_conflicts)))

            needs_apply = any(not record.get("files_restored") for record in selected)
            payloads = {}
            if needs_apply:
                # Materialize every blob before the second preflight, so a missing
                # or corrupt object can never leave a partially-restored worktree.
                payloads = self._materialize_payloads(
                    {path: target_entries.get(path) for path in touched},
                    target_modes,
                )

            # Narrow the race between validation and mutation.  Wrapper ownership
            # prevents ordinary concurrent turns, while this catches a terminal or
            # editor change that arrived during object materialization.
            (
                latest_tree,
                latest_index_raw,
                latest_index_entries,
                latest_modes,
                _,
            ) = self._capture_stable_state()
            latest_entries = self._tree_entries(latest_tree)
            if latest_index_raw != current_index_raw:
                changed_index = tuple(
                    sorted(
                        path
                        for path in touched
                        if latest_index_entries.get(path, ())
                        != current_index_entries.get(path, ())
                    )
                )
                if changed_index:
                    raise CheckpointConflict((), index_paths=changed_index)
            changed_files = tuple(
                sorted(
                    path
                    for path in touched
                    if (
                        latest_entries.get(path) != current_entries.get(path)
                        or self._permission_for(
                            path, latest_entries.get(path), latest_modes
                        ) != self._permission_for(
                            path, current_entries.get(path), current_modes
                        )
                    )
                )
            )
            if changed_files:
                raise CheckpointConflict(changed_files)

            if needs_apply:
                original_targets = {path: current_entries.get(path) for path in touched}
                original_modes = {
                    path: self._permission_for(
                        path, current_entries.get(path), current_modes
                    )
                    for path in touched
                }
                original_payloads = self._materialize_payloads(
                    original_targets, original_modes
                )
                base_manifest = copy.deepcopy(manifest)
                manifest["restore"] = {
                    "checkpoint_ids": [
                        record["checkpoint_id"] for record in selected
                    ],
                    "consume": consume,
                    "paths": sorted(touched),
                    "original_tree": current_tree,
                    "original_modes": original_modes,
                    "target_entries": {
                        path: self._serialize_entry(target_entries.get(path))
                        for path in touched
                    },
                    "target_modes": target_modes,
                }
                self._save_manifest(manifest)
                try:
                    self._apply_entries(payloads)
                except Exception as exc:
                    try:
                        self._apply_entries(original_payloads)
                    except Exception as recovery_exc:
                        raise CheckpointError(
                            "Checkpoint restore failed and worktree recovery also failed"
                        ) from recovery_exc
                    base_manifest["restore"] = None
                    try:
                        self._save_manifest(base_manifest)
                    except CheckpointError:
                        # The durable restore marker remains; startup recovery
                        # observes the original image and safely clears it.
                        pass
                    raise CheckpointError("Checkpoint restore failed") from exc
                committed = copy.deepcopy(manifest)
                try:
                    self._finalize_restore(committed, committed["restore"])
                except Exception as exc:
                    try:
                        self._apply_entries(original_payloads)
                    except Exception as recovery_exc:
                        raise CheckpointError(
                            "Checkpoint restore metadata failed and worktree "
                            "recovery also failed"
                        ) from recovery_exc
                    base_manifest["restore"] = None
                    try:
                        self._save_manifest(base_manifest)
                    except CheckpointError:
                        pass
                    raise CheckpointError(
                        "Checkpoint restore metadata could not be committed"
                    ) from exc
            else:
                if consume:
                    manifest["turns"] = turns[:-num_turns]
                else:
                    for record in selected:
                        record["files_restored"] = True
                self._save_manifest(manifest)
            return RollbackResult(
                turn_ids=tuple(str(record["turn_id"]) for record in reversed(selected)),
                restored_paths=tuple(sorted(touched)),
            )

    def discard(
        self, num_turns: int = 1, *, allow_partial: bool = False
    ) -> tuple[str, ...]:
        """Consume records after native conversation rollback succeeds."""
        if (
            not isinstance(num_turns, int)
            or isinstance(num_turns, bool)
            or num_turns < 1
        ):
            raise ValueError("num_turns must be a positive integer")
        with self._locked():
            manifest = self._load_manifest()
            if manifest.get("active") is not None:
                raise CheckpointError(
                    "Cannot discard checkpoints while a checkpoint is active"
                )
            turns = manifest["turns"]
            if num_turns > len(turns) and not allow_partial:
                raise CheckpointError("Not enough completed checkpoints")
            count = min(num_turns, len(turns))
            if count == 0:
                return ()
            selected = turns[-count:]
            manifest["turns"] = turns[:-count]
            self._save_manifest(manifest)
            return tuple(str(record["turn_id"]) for record in reversed(selected))

    def _materialize_payloads(
        self,
        entries: dict[str, Optional[TreeEntry]],
        permissions: Optional[dict[str, Any]] = None,
    ) -> dict[str, tuple[Optional[TreeEntry], Optional[bytes], Optional[int]]]:
        payloads: dict[
            str, tuple[Optional[TreeEntry], Optional[bytes], Optional[int]]
        ] = {}
        permissions = permissions or {}
        for path, entry in entries.items():
            self._safe_path(path)
            if entry is None:
                payloads[path] = (None, None, None)
                continue
            if entry.kind != "blob" or entry.mode not in {"100644", "100755", "120000"}:
                raise CheckpointError(f"Unsupported Git entry for rollback: {path}")
            if entry.mode == "120000":
                content = self._git(
                    "cat-file", "blob", entry.oid, env=self._git_env()
                ).stdout
            else:
                content = self._git(
                    "cat-file",
                    "--filters",
                    f"--path={path}",
                    entry.oid,
                    env=self._git_env(),
                ).stdout
            payloads[path] = (
                entry,
                content,
                self._permission_for(path, entry, permissions),
            )
        return payloads

    def _safe_path(self, relative: str) -> Path:
        if not relative or os.path.isabs(relative) or "\0" in relative:
            raise CheckpointError("Checkpoint contains an invalid path")
        parts = Path(relative).parts
        if any(part in {"", os.curdir, os.pardir} for part in parts):
            raise CheckpointError("Checkpoint path escapes repository")
        target = Path(self.repository_root).joinpath(*parts)
        current = Path(self.repository_root)
        for part in parts[:-1]:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise CheckpointConflict((relative,))
        return target

    def _apply_entries(
        self,
        payloads: dict[
            str, tuple[Optional[TreeEntry], Optional[bytes], Optional[int]]
        ],
    ) -> None:
        deletions = sorted(
            (path for path, (entry, _, _) in payloads.items() if entry is None),
            key=lambda path: (len(Path(path).parts), path),
            reverse=True,
        )
        for path in deletions:
            target = self._safe_path(path)
            try:
                mode = target.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                target.rmdir()
            else:
                target.unlink()
            self._prune_empty_parents(target.parent)

        writes = sorted(
            (path for path, (entry, _, _) in payloads.items() if entry is not None),
            key=lambda path: (len(Path(path).parts), path),
        )
        for path in writes:
            entry, content, permission = payloads[path]
            assert entry is not None and content is not None
            target = self._safe_path(path)
            self._ensure_parent(target.parent, path)
            try:
                current_mode = target.lstat().st_mode
            except FileNotFoundError:
                current_mode = None
            if (
                current_mode is not None
                and stat.S_ISDIR(current_mode)
                and not stat.S_ISLNK(current_mode)
            ):
                target.rmdir()
            temporary = target.with_name(
                f".{target.name}.cc-remote-{uuid.uuid4().hex}.tmp"
            )
            try:
                if entry.mode == "120000":
                    os.symlink(content, os.fsencode(temporary))
                else:
                    assert permission is not None
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        permission,
                    )
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temporary, permission)
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _ensure_parent(self, parent: Path, checkpoint_path: str) -> None:
        root = Path(self.repository_root)
        relative_parts = parent.relative_to(root).parts
        current = root
        for part in relative_parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                current.mkdir(mode=0o755)
                continue
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise CheckpointConflict((checkpoint_path,))

    def _prune_empty_parents(self, parent: Path) -> None:
        root = Path(self.repository_root)
        current = parent
        while current != root:
            try:
                current.rmdir()
            except (FileNotFoundError, OSError):
                return
            current = current.parent

    def cleanup(self, *, force: bool = False) -> None:
        """Remove the manifest and private object database for this session."""
        tombstone: Optional[Path] = None
        # Windows cannot rename a directory while a file inside it (the
        # journal lock we are about to release) is still held open, so the
        # rename is deferred until after the lock's own finally closes it.
        defer_rename = sys.platform == "win32"
        with self._locked():
            if not force:
                manifest = self._load_manifest()
                if manifest.get("active") is not None:
                    raise CheckpointError("Cannot clean an active checkpoint")
            # force=True is also the corruption/misalignment quarantine path.
            # It must retire a directory whose manifest no longer parses.
            tombstone = self.session_dir.with_name(
                f".{self.session_dir.name}.delete-{uuid.uuid4().hex}"
            )
            if not defer_rename:
                os.replace(self.session_dir, tombstone)
            self._closed = True
        if defer_rename:
            os.replace(self.session_dir, tombstone)
        if tombstone is not None:
            force_rmtree(tombstone)
