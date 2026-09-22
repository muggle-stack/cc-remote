"""Profile-explicit access to Claude Code's native session catalog.

The public Agent SDK catalog helpers resolve ``CLAUDE_CONFIG_DIR`` from the
host process.  A multi-account wrapper cannot safely mutate that global while
other profiles are being listed or changed.  These adapters keep the pinned
SDK's parsing/mutation semantics but thread the private config root through
every filesystem operation.
"""
from __future__ import annotations

import errno
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from typing import Iterable

from claude_agent_sdk._internal.session_mutations import (
    ForkSessionResult,
    LITE_READ_BUF_SIZE,
    _build_fork_lines,
    _extract_first_prompt_from_head,
    _extract_last_json_string_field,
    _parse_fork_transcript,
    _sanitize_unicode,
)
from claude_agent_sdk._internal.sessions import (
    MAX_SANITIZED_LENGTH,
    _apply_sort_limit_offset,
    _canonicalize_path,
    _entries_to_session_messages,
    _entries_to_subagent_messages,
    _get_worktree_paths,
    _parse_session_info_from_lite,
    _parse_transcript_entries,
    _parent_ids_from_agent_metadata,
    _read_agent_metadata_sidecar,
    _read_session_lite,
    _sanitize_path,
    _validate_uuid,
)
from claude_agent_sdk.types import SDKSessionInfo, SessionMessage

from cc_remote.attachments import MAX_TOTAL_ATTACHMENT_BYTES


_MAX_PROJECT_DIRS = 16_384
# A native user row can carry the full attachment set as base64. Reserve room
# for the prompt and native metadata as well as the expanded image bytes.
_CWD_RECORD_BYTES = 2 * MAX_TOTAL_ATTACHMENT_BYTES
# Native queue records can repeat those images before the actual user row.
# Bound the scan independently while allowing both copies and leading metadata.
_CWD_SCAN_BYTES = 3 * _CWD_RECORD_BYTES


def projects_dir(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir) / "projects"


def _project_bucket_names(directory: str) -> tuple[str, ...]:
    sdk_name = _sanitize_path(directory)
    if not any(ord(char) > 0xFFFF for char in directory):
        return (sdk_name,)
    # The native JS sanitizer and hash operate on UTF-16 code units. The
    # Python SDK iterates code points, producing different keys for emoji.
    encoded = directory.encode("utf-16-le", errors="surrogatepass")
    native_units = "".join(
        chr(encoded[index] | encoded[index + 1] << 8)
        for index in range(0, len(encoded), 2)
    )
    native_name = _sanitize_path(native_units)
    return (sdk_name,) if native_name == sdk_name else (sdk_name, native_name)


def _matches_project_bucket(
    names: tuple[str, ...], bucket: str, *, allow_legacy_hash: bool = False,
) -> bool:
    if bucket in names:
        return True
    if not allow_legacy_hash:
        return False
    # Older native builds used a different hash. Match the same bounded
    # prefix as the SDK catalog, requiring a nonempty lowercase hash suffix.
    suffix = bucket[MAX_SANITIZED_LENGTH + 1:]
    return (
        suffix.isascii() and suffix.isalnum() and suffix == suffix.lower()
        and any(
            len(name) > MAX_SANITIZED_LENGTH
            and bucket.startswith(name[:MAX_SANITIZED_LENGTH] + "-")
            for name in names
        )
    )


def _project_entries(config_dir: str | os.PathLike[str]) -> list[Path]:
    root = projects_dir(config_dir)
    try:
        result: list[Path] = []
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_PROJECT_DIRS:
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        result.append(Path(entry.path))
                except OSError:
                    continue
        return result
    except OSError:
        return []


def _directory_candidates(
    config_dir: str | os.PathLike[str],
    directory: str,
    *,
    include_worktrees: bool = True,
) -> list[tuple[Path, str]]:
    """Return exact/prefix project dirs using the pinned SDK's path mapping."""
    canonical = _canonicalize_path(directory)
    roots = [canonical]
    if include_worktrees:
        try:
            for worktree in _get_worktree_paths(canonical):
                if worktree not in roots:
                    roots.append(worktree)
        except Exception:
            pass

    available = _project_entries(config_dir)
    by_name = {entry.name: entry for entry in available}
    result: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for project_path in roots:
        names = _project_bucket_names(project_path)
        exact = next((by_name[name] for name in names if name in by_name), None)
        if exact is not None and exact not in seen:
            seen.add(exact)
            result.append((exact, project_path))
            continue
        if all(len(name) <= MAX_SANITIZED_LENGTH for name in names):
            continue
        for entry in available:
            if entry not in seen and _matches_project_bucket(
                names, entry.name, allow_legacy_hash=True,
            ):
                seen.add(entry)
                result.append((entry, project_path))
    return result


def _candidate_dirs(
    config_dir: str | os.PathLike[str],
    directory: str | None,
    *,
    include_worktrees: bool = True,
) -> Iterable[tuple[Path, str | None]]:
    if directory:
        yield from _directory_candidates(
            config_dir,
            directory,
            include_worktrees=include_worktrees,
        )
        return
    for entry in _project_entries(config_dir):
        yield entry, None


def find_session_file(
    config_dir: str | os.PathLike[str],
    session_id: str,
    *,
    directory: str | None = None,
) -> Path | None:
    if not isinstance(session_id, str) or not _validate_uuid(session_id):
        return None
    file_name = f"{session_id}.jsonl"
    for project_dir, _project_path in _candidate_dirs(config_dir, directory):
        path = project_dir / file_name
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_size > 0 and path.is_file():
            return path
    return None


def transcript_presence(
    config_dir: str | os.PathLike[str],
    session_id: str,
) -> bool | None:
    if not isinstance(session_id, str) or not _validate_uuid(session_id):
        return None
    root = projects_dir(config_dir)
    try:
        entries = os.scandir(root)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    file_name = f"{session_id}.jsonl"
    try:
        for index, entry in enumerate(entries):
            if index >= _MAX_PROJECT_DIRS:
                return None
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                info = os.stat(
                    os.path.join(entry.path, file_name),
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError:
                return None
            if info.st_size > 0:
                return True
        return False
    finally:
        entries.close()


def recover_session_cwd(
    info: SDKSessionInfo | None,
    path: str | os.PathLike[str] | None,
) -> SDKSessionInfo | None:
    """Fill a lite-read miss from complete native records, never a prompt.

    An initial queued image can occupy the SDK's entire 64 KiB head window.
    Missing cwd then says nothing about whether the conversation is resumable.
    Read a bounded prefix and require the original transcript's project bucket;
    later messages may record a different cwd after a shell directory change.
    """
    if info is None or info.cwd or path is None:
        return info
    source = Path(path)
    remaining = _CWD_SCAN_BYTES
    discard = False
    try:
        with source.open("rb") as stream:
            while remaining > 0:
                limit = min(_CWD_RECORD_BYTES, remaining)
                line = stream.readline(limit)
                if not line:
                    break
                remaining -= len(line)
                complete = line.endswith(b"\n") or len(line) < limit
                if discard:
                    discard = not complete
                    continue
                if not complete:
                    discard = True
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (not isinstance(row, dict)
                        or row.get("type") not in {"user", "assistant"}
                        or row.get("isSidechain") is True
                        or row.get("sessionId") != info.session_id
                        or not isinstance(row.get("message"), dict)):
                    continue
                cwd = row.get("cwd")
                if (isinstance(cwd, str) and os.path.isabs(cwd)
                        and "\x00" not in cwd
                        and _matches_project_bucket(
                            _project_bucket_names(cwd), source.parent.name,
                            # Only the explicit native root identifies the
                            # original cwd when an old hash cannot be checked.
                            # Later messages can share the same long prefix.
                            allow_legacy_hash=(
                                row.get("type") == "user"
                                and "parentUuid" in row
                                and row["parentUuid"] is None
                            ),
                        )):
                    return replace(info, cwd=cwd)
    except OSError:
        pass
    return info


def list_sessions(
    config_dir: str | os.PathLike[str],
    *,
    directory: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_worktrees: bool = True,
) -> list[SDKSessionInfo]:
    by_id: dict[str, SDKSessionInfo] = {}
    for project_dir, project_path in _candidate_dirs(
        config_dir,
        directory,
        include_worktrees=include_worktrees,
    ):
        try:
            entries = list(project_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".jsonl"):
                continue
            session_id = _validate_uuid(entry.name[:-6])
            if not session_id:
                continue
            lite = _read_session_lite(entry)
            if lite is None:
                continue
            info = _parse_session_info_from_lite(
                session_id, lite, project_path)
            info = recover_session_cwd(info, entry)
            if info is None:
                continue
            previous = by_id.get(session_id)
            if previous is None or info.last_modified > previous.last_modified:
                by_id[session_id] = info
    return _apply_sort_limit_offset(list(by_id.values()), limit, offset)


def get_session_info(
    config_dir: str | os.PathLike[str],
    session_id: str,
    *,
    directory: str | None = None,
) -> SDKSessionInfo | None:
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        return None
    lite = _read_session_lite(path)
    if lite is None:
        return None
    project_path = _canonicalize_path(directory) if directory else None
    return recover_session_cwd(
        _parse_session_info_from_lite(session_id, lite, project_path), path)


def get_session_messages(
    config_dir: str | os.PathLike[str],
    session_id: str,
    *,
    directory: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[SessionMessage]:
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = _parse_transcript_entries(content)
    return _entries_to_session_messages(entries, limit, offset)


def get_subagent_messages(
    config_dir: str | os.PathLike[str],
    session_id: str,
    agent_id: str,
    *,
    directory: str | None = None,
) -> list[SessionMessage]:
    """Read one subagent transcript below an explicit profile root."""
    if not _validate_uuid(session_id) or not agent_id:
        return []
    main = find_session_file(config_dir, session_id, directory=directory)
    if main is None:
        return []
    root = main.with_suffix("") / "subagents"
    match: Path | None = None
    try:
        for index, candidate in enumerate(root.rglob("agent-*.jsonl")):
            if index >= 4096:
                return []
            if candidate.name == f"agent-{agent_id}.jsonl":
                match = candidate
                break
    except OSError:
        return []
    if match is None:
        return []
    try:
        content = match.read_text(encoding="utf-8")
    except OSError:
        return []
    if not content:
        return []
    try:
        metadata = _read_agent_metadata_sidecar(match)
    except OSError:
        metadata = None
    parent_tool_use_id, parent_agent_id = _parent_ids_from_agent_metadata(
        metadata)
    entries = _parse_transcript_entries(content)
    return _entries_to_subagent_messages(
        entries,
        None,
        0,
        parent_tool_use_id,
        parent_agent_id,
    )


def _append(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise FileNotFoundError(f"Claude session not found: {path.stem}") \
                from exc
        raise
    try:
        if os.fstat(fd).st_size <= 0:
            raise FileNotFoundError(f"Claude session not found: {path.stem}")
        os.write(fd, data)
    finally:
        os.close(fd)


def rename_session(
    config_dir: str | os.PathLike[str],
    session_id: str,
    title: str,
    *,
    directory: str | None = None,
) -> None:
    if not _validate_uuid(session_id):
        raise ValueError(f"Invalid session_id: {session_id}")
    stripped = title.strip()
    if not stripped:
        raise ValueError("title must be non-empty")
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    payload = {
        "type": "custom-title",
        "customTitle": stripped,
        "sessionId": session_id,
    }
    _append(path, (json.dumps(payload, separators=(",", ":")) + "\n").encode())


def tag_session(
    config_dir: str | os.PathLike[str],
    session_id: str,
    tag: str | None,
    *,
    directory: str | None = None,
) -> None:
    if not _validate_uuid(session_id):
        raise ValueError(f"Invalid session_id: {session_id}")
    if tag is not None:
        tag = _sanitize_unicode(tag).strip()
        if not tag:
            raise ValueError("tag must be non-empty (use None to clear)")
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    payload = {
        "type": "tag",
        "tag": tag if tag is not None else "",
        "sessionId": session_id,
    }
    _append(path, (json.dumps(payload, separators=(",", ":")) + "\n").encode())


def delete_session(
    config_dir: str | os.PathLike[str],
    session_id: str,
    *,
    directory: str | None = None,
) -> None:
    if not _validate_uuid(session_id):
        raise ValueError(f"Invalid session_id: {session_id}")
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Session {session_id} not found") from exc
    shutil.rmtree(path.parent / session_id, ignore_errors=True)


def fork_session(
    config_dir: str | os.PathLike[str],
    session_id: str,
    *,
    directory: str | None = None,
    up_to_message_id: str | None = None,
    title: str | None = None,
) -> ForkSessionResult:
    if not _validate_uuid(session_id):
        raise ValueError(f"Invalid session_id: {session_id}")
    if up_to_message_id and not _validate_uuid(up_to_message_id):
        raise ValueError(f"Invalid up_to_message_id: {up_to_message_id}")
    path = find_session_file(config_dir, session_id, directory=directory)
    if path is None:
        raise FileNotFoundError(f"Session {session_id} not found")
    content = path.read_bytes()
    if not content:
        raise ValueError(f"Session {session_id} has no messages to fork")
    transcript, replacements = _parse_fork_transcript(content, session_id)

    def derive_title() -> str | None:
        head = content[:LITE_READ_BUF_SIZE].decode("utf-8", errors="replace")
        tail = content[-LITE_READ_BUF_SIZE:].decode("utf-8", errors="replace")
        return (
            _extract_last_json_string_field(tail, "customTitle")
            or _extract_last_json_string_field(head, "customTitle")
            or _extract_last_json_string_field(tail, "aiTitle")
            or _extract_last_json_string_field(head, "aiTitle")
            or _extract_first_prompt_from_head(head)
            or None
        )

    child_id, lines = _build_fork_lines(
        transcript,
        replacements,
        session_id,
        up_to_message_id,
        title,
        derive_title,
    )
    child_path = path.parent / f"{child_id}.jsonl"
    fd = os.open(child_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return ForkSessionResult(session_id=child_id)
