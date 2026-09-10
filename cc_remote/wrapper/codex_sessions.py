"""Codex session metadata and rollout helpers.

The app-server state DB is authoritative for sidebar metadata such as names and
archive state. Rollout files remain the source for history, cwd fallback, and
per-turn settings.
"""
from __future__ import annotations

import glob
from importlib import import_module
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterable, Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_rpc import codex_rpc


def _load_tomllib():
    """Load TOML support on every advertised Python version."""
    try:
        return import_module("tomllib")
    except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib.
        return import_module("tomli")


tomllib = _load_tomllib()

log = logger("cc_remote.wrapper.codex_sessions")

_CONFIG = os.path.expanduser("~/.codex/config.toml")
_CONFIG_MAX_BYTES = 4 * 1024 * 1024
_ROOT = os.path.expanduser("~/.codex/sessions")
_ARCHIVE_ROOT = os.path.expanduser("~/.codex/archived_sessions")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024
MAX_META_RECORD_BYTES = 1024 * 1024
_LIST_PAGE_SIZE = 100
_LIST_MAX_PER_ARCHIVE_STATE = 200
_LIST_MAX_PAGES = 20
_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})
_STATE_DB = re.compile(r"^state_(\d+)\.sqlite$")
CODEX_EXACT_CATALOG_MAX_IDS = 512
CODEX_THREAD_PARENT_SCAN_MAX_ROWS = 10_000
CODEX_THREAD_PARENT_MAX_IDS = 4_096
CODEX_THREAD_SOURCE_MAX_BYTES = 64 * 1024
_EXACT_CATALOG_COLUMNS = (
    "id",
    "cwd",
    "name",
    "preview",
    "first_user_message",
    "title",
    "recency_at",
    "recency_at_ms",
    "updated_at",
    "updated_at_ms",
    "created_at",
    "created_at_ms",
    "git_branch",
    "archived",
    "model_provider",
)
_EXACT_CATALOG_TEXT_LIMITS = {
    "cwd": 4096,
    "name": 500,
    "preview": 2000,
    "first_user_message": 2000,
    "title": 2000,
    "git_branch": 500,
    "model_provider": 256,
}


def _codex_home(codex_home: str | os.PathLike[str] | None = None) -> str:
    raw = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.realpath(os.path.expanduser(os.fspath(raw)))


def _config_path(codex_home: str | os.PathLike[str] | None = None) -> str:
    if codex_home is None:
        return _CONFIG
    return os.path.join(_codex_home(codex_home), "config.toml")


def _session_roots(
    codex_home: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    if codex_home is None:
        return _ROOT, _ARCHIVE_ROOT
    home = _codex_home(codex_home)
    return os.path.join(home, "sessions"), os.path.join(home, "archived_sessions")


async def list_codex_sessions(
    limit: int = 60,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """List active and archived app-server threads, newest first.

    ``limit`` is applied independently to active and archived threads so a busy
    active list cannot make the archived group disappear. Both result sets are
    bounded and paginated with opaque app-server cursors.
    """
    per_state_limit = max(1, min(limit, _LIST_MAX_PER_ARCHIVE_STATE))
    provider = (
        codex_current_provider()
        if codex_home is None
        else codex_current_provider(codex_home=codex_home)
    ).strip()
    by_id: dict[str, dict[str, Any]] = {}

    for archived in (False, True):
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        accepted_ids: set[str] = set()
        for _ in range(_LIST_MAX_PAGES):
            remaining = per_state_limit - len(accepted_ids)
            if remaining <= 0:
                break
            params: dict[str, Any] = {
                "limit": min(_LIST_PAGE_SIZE, remaining),
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": archived,
            }
            if cursor:
                params["cursor"] = cursor
            if provider:
                params["modelProviders"] = [provider]

            response = (
                await codex_rpc("thread/list", params)
                if codex_home is None
                else await codex_rpc(
                    "thread/list", params,
                    codex_home=os.fspath(codex_home),
                )
            )
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise RuntimeError("codex thread/list returned an invalid response")
            page = response["data"][:remaining]
            for thread in page:
                normalized = _normalize_thread(thread, archived=archived)
                if normalized is not None:
                    by_id[normalized["session_id"]] = normalized
                    accepted_ids.add(normalized["session_id"])

            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("codex thread/list repeated its pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    return sorted(
        by_id.values(), key=lambda item: _updated_sort_key(item.get("last_modified")),
        reverse=True,
    )


def _normalize_thread(thread: Any, *, archived: bool) -> Optional[dict[str, Any]]:
    if not isinstance(thread, dict):
        return None
    # Ephemeral threads are native scratch state (including cc-remote /btw and
    # Codex subagent work), not resumable user sessions. Never project them into
    # the public catalog even if a shared app-server briefly lists them.
    if thread.get("ephemeral") is True:
        return None
    session_id = thread.get("id")
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        return None

    git_info = thread.get("gitInfo")
    branch = git_info.get("branch") if isinstance(git_info, dict) else None
    forked_from = thread.get("forkedFromId")
    if not isinstance(forked_from, str) or not _SAFE_SESSION_ID.fullmatch(forked_from):
        forked_from = None
    raw_status = thread.get("status")
    status = raw_status.get("type") if isinstance(raw_status, dict) else None
    if status not in _THREAD_STATUSES:
        status = None

    updated_at = thread.get("updatedAt")
    if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
            or not math.isfinite(updated_at) or updated_at < 0):
        last_modified = None
    else:
        last_modified = str(updated_at)

    return {
        "session_id": session_id,
        "summary": _bounded_text(thread.get("name"), 500),
        "first_prompt": _bounded_text(thread.get("preview"), 2000),
        "cwd": _bounded_text(thread.get("cwd"), 4096),
        "last_modified": last_modified,
        "git_branch": _bounded_text(branch, 500),
        "forked_from_id": forked_from,
        "status": status,
        "tag": "archived" if archived else None,
    }


def codex_thread_catalog_row(thread: Any) -> Optional[dict[str, Any]]:
    """Normalize one profile-scoped ``thread/read`` result for the sidebar.

    The caller is responsible for selecting the matching ``CODEX_HOME`` before
    obtaining ``thread``. This helper deliberately performs no cross-account
    lookup and accepts only the same bounded fields as ``thread/list``.
    """
    archived = isinstance(thread, dict) and thread.get("archived") is True
    return _normalize_thread(thread, archived=archived)


def _bounded_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:limit] or None


def _updated_sort_key(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return -1.0
    return parsed if math.isfinite(parsed) else -1.0


def codex_session_cwd(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    """The cwd a Codex thread was started in (for resume). None if not found."""
    path = (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return None
    meta = _read_meta(path)
    return meta.get("cwd") if meta else None


def codex_rollout_path(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    """Public: the rollout .jsonl for a Codex thread (for history replay)."""
    return (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )


def _state_db_path(
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    """Resolve the newest app-server state DB without opening it writable."""
    home = _codex_home(codex_home)
    config_path = os.path.join(home, "config.toml")
    try:
        if os.path.getsize(config_path) > _CONFIG_MAX_BYTES:
            return None
        with open(config_path, "rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    except Exception:
        return None
    sqlite_home = config.get("sqlite_home")
    if sqlite_home is None:
        sqlite_root = home
    elif isinstance(sqlite_home, str) and sqlite_home.strip():
        sqlite_root = os.path.expanduser(sqlite_home)
        if not os.path.isabs(sqlite_root):
            sqlite_root = os.path.join(home, sqlite_root)
        sqlite_root = os.path.realpath(sqlite_root)
    else:
        return None
    try:
        candidates = [
            (int(match.group(1)), os.path.join(sqlite_root, entry.name))
            for entry in os.scandir(sqlite_root)
            if entry.is_file(follow_symlinks=False)
            and (match := _STATE_DB.fullmatch(entry.name)) is not None
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def codex_thread_rollout_record(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[tuple[str, bool]]:
    """Read one exact catalog rollout path and archived bit.

    ``None`` preserves uncertainty: callers must not guess that a missing or
    unreadable row is profile-local.  In particular, copied ``CODEX_HOME``
    databases can retain absolute rollout paths into their former home.
    """
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(
        session_id
    ):
        return None
    db_path = _state_db_path(codex_home)
    if db_path is None:
        return None
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            schema = {
                row[1]
                for row in connection.execute('PRAGMA table_info("threads")')
                if isinstance(row[1], str)
            }
            if not {"id", "rollout_path", "archived"}.issubset(schema):
                return None
            row = connection.execute(
                'SELECT "rollout_path", "archived" FROM "threads" '
                'WHERE "id"=? LIMIT 1',
                (session_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if (
        row is None
        or not isinstance(row[0], str)
        or not row[0]
        or len(os.fsencode(row[0])) > 16 * 1024
        or not isinstance(row[1], int)
        or row[1] not in (0, 1)
    ):
        return None
    return row[0], bool(row[1])


def codex_session_presence(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> bool | None:
    """Read one exact native thread id without collapsing I/O failure.

    The app-server SQLite catalog is authoritative for active and archived
    threads. ``None`` means ownership is unknown and callers must not infer a
    different engine from absence.
    """
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(
        session_id
    ):
        return None
    db_path = _state_db_path(codex_home)
    if db_path is None:
        return None
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                "SELECT 1 FROM threads WHERE id=? LIMIT 1", (session_id,)
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None
    return row is not None


def codex_session_confirmed_missing(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> bool:
    """Prove absence from both the readable catalog and rollout roots.

    A truncated listing, locked DB, unreadable directory, or symlink is not
    evidence of deletion. Do not use the best-effort history glob for this.
    """
    home = _codex_home(codex_home)
    if codex_session_presence(session_id, codex_home=home) is not False:
        return False
    scanned = 0

    def failed_scan(error: OSError) -> None:
        raise error

    try:
        for root in _session_roots(home):
            try:
                mode = os.lstat(root).st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                return False
            for directory, dirs, files in os.walk(root, onerror=failed_scan):
                scanned += 1 + len(dirs) + len(files)
                if scanned > 100_000:
                    return False
                if any(os.path.islink(os.path.join(directory, name))
                       for name in dirs):
                    return False
                if any(session_id in name and name.endswith(".jsonl")
                       for name in files):
                    return False
    except OSError:
        return False
    # A create/archive transaction may have committed while scanning.
    return codex_session_presence(session_id, codex_home=home) is False


def codex_thread_archive_states(
    session_ids: Iterable[str],
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[dict[str, bool]]:
    """Read exact archived bits inside one already-selected account home.

    Unlike sidebar rows, commit reconciliation must not filter by the current
    model provider: a historical thread can retain its original provider while
    still being owned and mutated by this exact ``CODEX_HOME`` app-server.
    Missing ids are omitted; ``None`` means the database could not prove state.
    """
    unique_ids: list[str] = []
    seen: set[str] = set()
    for value in session_ids:
        if not isinstance(value, str) or not _SAFE_SESSION_ID.fullmatch(value):
            return None
        if value in seen:
            continue
        seen.add(value)
        unique_ids.append(value)
        # Exact-state callers use this read as a commit boundary.  Silently
        # truncating a larger tree would make an unverified descendant look as
        # though it did not exist, so fail closed instead.
        if len(unique_ids) > CODEX_EXACT_CATALOG_MAX_IDS:
            return None
    if not unique_ids:
        return {}
    db_path = _state_db_path(codex_home)
    if db_path is None:
        return None
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            schema = {
                row[1]
                for row in connection.execute('PRAGMA table_info("threads")')
                if isinstance(row[1], str)
            }
            if not {"id", "archived"}.issubset(schema):
                return None
            placeholders = ",".join("?" for _ in unique_ids)
            records = connection.execute(
                f'SELECT "id", "archived" FROM "threads" '
                f'WHERE "id" IN ({placeholders})',
                unique_ids,
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    states: dict[str, bool] = {}
    for session_id, archived in records:
        if (
            isinstance(session_id, str)
            and session_id in seen
            and isinstance(archived, int)
            and archived in (0, 1)
        ):
            states[session_id] = bool(archived)
    return states


def _catalog_rollout_fork_parent(
    thread_id: str,
    rollout_path: object,
    archived: object,
    *,
    codex_home: str | os.PathLike[str] | None,
) -> tuple[bool, str | None]:
    """Read one immutable parent edge from a profile-local catalog rollout.

    The catalog path, archive bit, regular-file identity, first JSONL record,
    and embedded thread id must all agree. ``False`` preserves uncertainty so
    destructive tree callers fail closed instead of silently dropping an edge.
    """
    if (
        not isinstance(rollout_path, str)
        or not rollout_path
        or len(os.fsencode(rollout_path)) > 16 * 1024
        or not isinstance(archived, int)
        or archived not in (0, 1)
    ):
        return False, None
    expanded = os.path.expanduser(rollout_path)
    if not os.path.isabs(expanded):
        return False, None
    expected_root = os.path.realpath(_session_roots(codex_home)[archived])
    resolved = os.path.realpath(expanded)
    try:
        if os.path.commonpath((expected_root, resolved)) != expected_root:
            return False, None
        before = os.lstat(expanded)
    except (OSError, ValueError):
        return False, None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return False, None

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(expanded, flags)
    except OSError:
        return False, None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            return False, None
        chunks: list[bytes] = []
        received = 0
        newline = False
        while received <= MAX_META_RECORD_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_META_RECORD_BYTES + 1 - received),
            )
            if not chunk:
                break
            marker = chunk.find(b"\n")
            if marker >= 0:
                chunks.append(chunk[:marker + 1])
                received += marker + 1
                newline = True
                break
            chunks.append(chunk)
            received += len(chunk)
        try:
            current = os.lstat(expanded)
        except OSError:
            return False, None
        if (
            (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or not newline
            or received > MAX_META_RECORD_BYTES
        ):
            return False, None
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return False, None
    if not isinstance(record, dict):
        return False, None
    payload = record.get("payload")
    if record.get("type") != "session_meta" or not isinstance(payload, dict):
        return False, None
    if payload.get("id") != thread_id:
        return False, None
    # Recent multi-agent rollouts use ``session_id`` as the shared tree/session
    # identity while ``id`` remains the exact native thread identity.  It is
    # therefore validated as a bounded id, but must not be equated to the row.
    session_id = payload.get("session_id")
    if session_id is not None and (
        not isinstance(session_id, str)
        or not _SAFE_SESSION_ID.fullmatch(session_id)
    ):
        return False, None
    parent_id = payload.get("forked_from_id")
    if parent_id is None:
        return True, None
    if (
        not isinstance(parent_id, str)
        or not _SAFE_SESSION_ID.fullmatch(parent_id)
        or parent_id == thread_id
    ):
        return False, None
    return True, parent_id


def codex_thread_parent_maps(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[tuple[dict[str, str], dict[str, str]]]:
    """Return ``(all parents, ordinary fork parents)`` for one account.

    ``thread/list`` and ``thread/read`` can omit fork ancestry. Background
    subagents persist it in the app-server ``source`` JSON, while ordinary
    forks persist ``forked_from_id`` in the profile-local rollout's immutable
    session metadata. Callers combine both sources; ``None`` means the local
    catalog could not prove a complete bounded projection.
    """
    db_path = _state_db_path(codex_home)
    if db_path is None:
        return None
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            schema = {
                row[1]
                for row in connection.execute('PRAGMA table_info("threads")')
                if isinstance(row[1], str)
            }
            if not {"id", "source", "rollout_path", "archived"}.issubset(
                schema
            ):
                return None
            records = connection.execute(
                'SELECT "id", length(CAST("source" AS BLOB)), '
                'CASE WHEN length(CAST("source" AS BLOB)) <= ? '
                'THEN "source" ELSE NULL END, "rollout_path", "archived" '
                'FROM "threads" LIMIT ?',
                (
                    CODEX_THREAD_SOURCE_MAX_BYTES,
                    CODEX_THREAD_PARENT_SCAN_MAX_ROWS + 1,
                ),
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None
    if len(records) > CODEX_THREAD_PARENT_SCAN_MAX_ROWS:
        return None

    parents: dict[str, str] = {}
    fork_parents: dict[str, str] = {}
    for thread_id, source_size, source, rollout_path, archived in records:
        if (
            not isinstance(thread_id, str)
            or not _SAFE_SESSION_ID.fullmatch(thread_id)
            or not isinstance(source_size, int)
            or source_size < 0
            or source_size > CODEX_THREAD_SOURCE_MAX_BYTES
            or not isinstance(source, str)
        ):
            return None
        source_parent: str | None = None
        stripped = source.lstrip()
        if not stripped.startswith(("{", "[")):
            decoded = None
        else:
            try:
                decoded = json.loads(source)
            except (TypeError, ValueError):
                return None
            if not isinstance(decoded, dict):
                return None
        if isinstance(decoded, dict):
            subagent = decoded.get("subagent")
            if isinstance(subagent, dict) and "thread_spawn" in subagent:
                spawn = subagent["thread_spawn"]
                if not isinstance(spawn, dict):
                    return None
                source_parent = spawn.get("parent_thread_id")
                if (
                    not isinstance(source_parent, str)
                    or not _SAFE_SESSION_ID.fullmatch(source_parent)
                    or source_parent == thread_id
                ):
                    return None

        rollout_ok, rollout_parent = _catalog_rollout_fork_parent(
            thread_id,
            rollout_path,
            archived,
            codex_home=codex_home,
        )
        if not rollout_ok:
            return None
        if (
            source_parent is not None
            and rollout_parent is not None
            and source_parent != rollout_parent
        ):
            return None
        parent_id = rollout_parent or source_parent
        if parent_id is None:
            continue
        parents[thread_id] = parent_id
        # A subagent can persist the same edge in both source JSON and rollout
        # metadata. Only a rollout-only edge is an ordinary user-visible fork;
        # source-backed hidden agents remain eligible for native recursive
        # cleanup after the usual activity/queue protections.
        if rollout_parent is not None and source_parent is None:
            fork_parents[thread_id] = rollout_parent
        if len(parents) > CODEX_THREAD_PARENT_MAX_IDS:
            return None
    return parents, fork_parents


def codex_thread_spawn_parent_map(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[dict[str, str]]:
    """Compatibility projection containing every proven parent edge."""
    projection = codex_thread_parent_maps(codex_home=codex_home)
    return None if projection is None else projection[0]


def codex_exact_catalog_rows(
    session_ids: Iterable[str],
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[list[dict[str, Any]]]:
    """Read bounded, exact sidebar rows which ``thread/list`` omitted.

    Recent app-server builds can keep a real thread addressable through
    ``thread/read`` while hiding it from ``thread/list`` until its preview is
    materialized.  New cc-remote sessions and persistent forks must not vanish
    during that window.  This helper does not scan or merge accounts: callers
    provide native ids for one already-selected ``CODEX_HOME``, and the query
    is additionally filtered to that home's configured model provider.

    ``None`` preserves read uncertainty; an empty list proves that none of the
    exact ids belongs to the selected catalog.
    """
    unique_ids: list[str] = []
    seen: set[str] = set()
    for value in session_ids:
        if (
            not isinstance(value, str)
            or not _SAFE_SESSION_ID.fullmatch(value)
            or value in seen
        ):
            continue
        seen.add(value)
        unique_ids.append(value)
        if len(unique_ids) >= CODEX_EXACT_CATALOG_MAX_IDS:
            break
    if not unique_ids:
        return []

    db_path = _state_db_path(codex_home)
    if db_path is None:
        return None
    provider = codex_current_provider(codex_home=codex_home).strip()
    try:
        uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            connection.row_factory = sqlite3.Row
            schema = {
                row["name"]
                for row in connection.execute('PRAGMA table_info("threads")')
                if isinstance(row["name"], str)
            }
            if "id" not in schema:
                return None
            if provider and "model_provider" not in schema:
                # Older Codex schemas predate provider ownership metadata. The
                # id may exist, but this SQLite snapshot cannot prove it belongs
                # to the configured provider, so absence is not authoritative.
                return None
            projections = []
            for name in _EXACT_CATALOG_COLUMNS:
                if name not in schema:
                    continue
                limit = _EXACT_CATALOG_TEXT_LIMITS.get(name)
                projections.append(
                    f'substr("{name}", 1, {limit}) AS "{name}"'
                    if limit is not None else f'"{name}"'
                )
            placeholders = ",".join("?" for _ in unique_ids)
            predicates = [f'"id" IN ({placeholders})']
            parameters = list(unique_ids)
            if provider:
                predicates.append('"model_provider" = ?')
                parameters.append(provider)
            records = connection.execute(
                f"SELECT {','.join(projections)} FROM \"threads\" "
                f"WHERE {' AND '.join(predicates)}",
                parameters,
            ).fetchall()
    except (OSError, sqlite3.Error):
        return None

    rows: list[dict[str, Any]] = []
    for raw in records:
        record = dict(raw)
        session_id = record.get("id")
        if (
            not isinstance(session_id, str)
            or not _SAFE_SESSION_ID.fullmatch(session_id)
        ):
            continue
        timestamps: list[float] = []
        for name, scale in (
            ("recency_at", 1),
            ("updated_at", 1),
            ("created_at", 1),
            ("recency_at_ms", 1000),
            ("updated_at_ms", 1000),
            ("created_at_ms", 1000),
        ):
            value = record.get(name)
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                timestamps.append(value / scale)
        modified = max(timestamps, default=None)
        modified_text = (
            str(int(modified))
            if modified is not None and modified.is_integer()
            else str(modified) if modified is not None else None
        )
        preview = next((
            value for value in (
                record.get("preview"),
                record.get("first_user_message"),
                record.get("title"),
            )
            if isinstance(value, str) and value
        ), None)
        archived = bool(record.get("archived"))
        rows.append({
            "session_id": session_id,
            "summary": _bounded_text(record.get("name"), 500),
            "first_prompt": _bounded_text(preview, 2000),
            "cwd": _bounded_text(record.get("cwd"), 4096),
            "last_modified": modified_text,
            "git_branch": _bounded_text(record.get("git_branch"), 500),
            "forked_from_id": None,
            "status": None,
            "tag": "archived" if archived else None,
        })
    rows.sort(
        key=lambda item: _updated_sort_key(item.get("last_modified")),
        reverse=True,
    )
    return rows


def codex_model(
    default: str = "",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The model Codex is configured to use (from ~/.codex/config.toml). Used to
    show the right model readout for live Codex sessions (not a Claude model)."""
    return _config_value("model", default, codex_home=codex_home)[:256]


def codex_effort(
    default: str = "",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The default reasoning effort from ~/.codex/config.toml (model_reasoning_effort)."""
    return _config_value(
        "model_reasoning_effort", default, codex_home=codex_home)[:64]


def codex_current_provider(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The provider Codex is configured for right now (config.toml model_provider).
    A codex rollout carries provider-encrypted reasoning, so a session from a
    DIFFERENT provider can't be resumed here — the list is filtered to this one."""
    return _config_value("model_provider", "", codex_home=codex_home)[:256]


def codex_context_window(
    default: int = 256000,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> int:
    """Fallback context window (tokens) for a fresh session before any turn has
    reported one. The AUTHORITATIVE value comes from the live server's
    thread/tokenUsage/updated (tokenUsage.modelContextWindow) and overrides this;
    ~/.codex/config.toml's model_context_window is only a user-declared estimate
    (it can disagree with the server, e.g. 400000 in config vs 258400 live)."""
    try:
        return int(_config_value(
            "model_context_window", str(default), codex_home=codex_home))
    except (ValueError, TypeError):
        return default


def codex_fast_enabled(
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> bool:
    """True for either accepted/reported top-level Codex Fast tier name."""
    return (_config_value(
        "service_tier", "", codex_home=codex_home) or "").lower() in {
        "fast", "priority",
    }


def codex_approval(
    default: str = "never",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The top-level Codex approval policy used for a new thread."""
    value = _config_value(
        "approval_policy", default, codex_home=codex_home)
    return value if value in {"untrusted", "on-request", "never"} else default


def codex_web_search(
    default: str = "cached",
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    """The top-level search mode inherited by a new no-override thread."""
    value = _config_value("web_search", default, codex_home=codex_home)
    return value if value in {"cached", "live"} else default


def codex_session_settings(
    session_id: str, max_bytes: int = 64 * 1024 * 1024,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> dict:
    """The per-thread settings carried by the latest bounded rollout tail.

    Codex appends a `turn_context` record per turn carrying `model`, `effort`,
    and the nested `collaboration_mode` selected for that turn. A live
    `thread/settings/update` is persisted immediately as a
    `thread_settings_applied` event, before another turn necessarily exists.
    Both records are consumed in file order so a wrapper restart cannot restore
    the preceding turn's stale controls over that newer applied snapshot.
    The official thread/resume response is authoritative for settings it exposes;
    this bounded tail is the fallback and remains necessary for collaboration mode,
    which 0.144.1 does not include in that response. Config.toml is never a valid
    resume source because it holds only fresh-thread global defaults.

    Returns {} when the rollout is missing/unreadable; the caller falls back to the
    config defaults (correct for a brand-new session).
    """
    path = (
        _rollout_path(session_id)
        if codex_home is None
        else _rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return {}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    out: dict = {}
    try:
        # A long-running thread can easily exceed 64 MiB. Only its newest
        # settings records matter, so seek to a bounded tail and discard the
        # first partial JSONL record instead of rejecting the entire rollout.
        tail_bytes = max(1, int(max_bytes))
        start = max(0, size - tail_bytes)
        with open(path, "rb") as f:
            if start:
                f.seek(start - 1)
                starts_at_record = f.read(1) == b"\n"
                f.seek(start)
                if not starts_at_record:
                    discarded = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                    if not discarded.endswith(b"\n"):
                        return {}
            while True:
                raw = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_JSONL_RECORD_BYTES:
                    if not raw.endswith(b"\n"):
                        # The remainder is still the same oversized record. Stop:
                        # a boundary cannot be recovered without exceeding our cap.
                        break
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                # Cheap prefilter: most lines are messages, not settings.
                if ('"turn_context"' not in line
                        and '"thread_settings_applied"' not in line):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                record_type = rec.get("type")
                payload = rec.get("payload")
                effort_key = "effort"
                if record_type == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") != "thread_settings_applied":
                        continue
                    payload = payload.get("thread_settings")
                    effort_key = "reasoning_effort"
                elif record_type != "turn_context":
                    continue
                if not isinstance(payload, dict):
                    continue

                model = payload.get("model")
                if isinstance(model, str) and model:
                    out["model"] = model[:256]
                if effort_key in payload:
                    effort = payload.get(effort_key)
                    if isinstance(effort, str) and effort:
                        out["effort"] = effort[:64]
                    elif effort is None:
                        out.pop("effort", None)

                approval = payload.get("approval_policy")
                if (isinstance(approval, str)
                        and approval in {"untrusted", "on-request", "never"}):
                    out["approval_policy"] = approval
                    out.pop("approval_policy_granular", None)
                elif (isinstance(approval, dict)
                        and isinstance(approval.get("granular"), dict)):
                    # The bounded history reader need not duplicate the complete
                    # policy object.  It must, however, distinguish native
                    # granular approval from a missing value so callers do not
                    # replace it with a stale named/UI projection.
                    out.pop("approval_policy", None)
                    out["approval_policy_granular"] = True
                if "service_tier" in payload:
                    tier = payload.get("service_tier")
                    if tier is None or tier == "default":
                        out["service_tier"] = None
                    elif isinstance(tier, str) and tier:
                        out["service_tier"] = tier[:64]
                collaboration = payload.get("collaboration_mode")
                if isinstance(collaboration, dict):
                    mode = collaboration.get("mode")
                    if mode in ("default", "plan"):
                        out["collaboration_mode"] = mode
                # Only thread_settings_applied contains the selected profile id.
                # turn_context.permission_profile is the expanded policy object
                # and intentionally has no stable profile provenance.
                if effort_key == "reasoning_effort":
                    active_profile = payload.get("active_permission_profile")
                    if isinstance(active_profile, dict):
                        profile_id = active_profile.get("id")
                        if (isinstance(profile_id, str) and profile_id
                                and len(profile_id) <= 256):
                            out["permission_profile"] = profile_id
                    elif "active_permission_profile" in payload:
                        out["permission_profile"] = None
    except Exception as e:
        log.warning("read codex session settings failed", session_id=session_id, error=str(e))
    return out


def _config_value(
    key: str,
    default: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> str:
    try:
        target = os.path.realpath(_config_path(codex_home))
        if os.path.getsize(target) > _CONFIG_MAX_BYTES:
            return default
        with open(target, "rb") as f:
            config = tomllib.load(f)
        # tomllib preserves table boundaries. Looking only at the root prevents
        # a profile/provider's nested `model`, effort or service tier from being
        # mistaken for the user's default.
        value = config.get(key)
        if isinstance(value, str):
            return value[:4096] or default
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)[:4096]
    except Exception:
        pass
    return default


# ---- internals ----
def _rollout_path(
    session_id: str,
    *,
    codex_home: str | os.PathLike[str] | None = None,
) -> Optional[str]:
    try:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            return None
        safe_id = glob.escape(session_id)
        scanned = 0
        # thread/archive moves the rollout out of ``sessions``. Archived rows
        # still need history, cwd lookup, and engine detection so unarchive never
        # falls through to the Claude SDK.
        for source_root in _session_roots(codex_home):
            matches = glob.iglob(
                os.path.join(source_root, "**", f"*{safe_id}*.jsonl"),
                recursive=True,
            )
            root = os.path.realpath(source_root)
            for match in matches:
                if scanned >= 1000:
                    return None
                scanned += 1
                resolved = os.path.realpath(match)
                if os.path.commonpath((root, resolved)) == root:
                    return match
        return None
    except Exception:
        return None


def _read_meta(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            line = f.readline(MAX_META_RECORD_BYTES + 1)
            if len(line.encode("utf-8", "surrogatepass")) > MAX_META_RECORD_BYTES:
                return None
            d = json.loads(line)
        if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
            return d["payload"]
    except Exception:
        pass
    return None


def _bounded_lines(file, max_record_bytes: int):
    """Yield complete JSONL records without ever allocating one unbounded line."""
    while True:
        line = file.readline(max_record_bytes + 1)
        if not line:
            return
        if len(line.encode("utf-8", "surrogatepass")) <= max_record_bytes \
                and (line.endswith("\n") or len(line) < max_record_bytes + 1):
            yield line
            continue
        # Oversized record: consume bounded chunks through its newline and skip it.
        while line and not line.endswith("\n"):
            line = file.readline(max_record_bytes + 1)
