"""Repair process-local Codex HTTP provider aliases in durable thread state.

The oversized Desktop/OpenAI compatibility path registers a private provider
alias so one owned app-server process can disable Responses WebSockets.  Codex
persists that runtime provider in its SQLite thread index and in subagent
``session_meta`` records.  Ordinary Codex App/CLI processes do not know the
private alias and therefore cannot resume those durable threads.

This module deliberately treats the immutable rollout metadata as the trust
boundary.  A direct thread is repairable only when its own first record says it
was created by Codex Desktop with the canonical ``openai`` provider.  A
subagent whose first record already contains the private alias is repairable
only through a bounded, acyclic ``thread_spawn`` parent chain rooted at such a
direct thread.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from importlib import import_module
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any, Iterable, Optional

from cc_remote.wrapper.os_compat import fsync_directory, pread, pwrite


CANONICAL_OPENAI_PROVIDER_ID = "openai"
HTTP_COMPAT_PROVIDER_ID = "cc_remote_openai_http"
MAX_META_BYTES = 1024 * 1024
MAX_REPAIR_ROWS = 512
MAX_PARENT_DEPTH = 32
_SAFE_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_PROVIDER_FIELD = re.compile(
    rb'("model_provider"\s*:\s*)"cc_remote_openai_http"',
)
_STATE_DB = re.compile(r"^state_(\d+)\.sqlite$")


def _load_tomllib():
    try:
        return import_module("tomllib")
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
        return import_module("tomli")


tomllib = _load_tomllib()


class CodexProviderRepairError(RuntimeError):
    """The durable provider state could not be repaired safely."""


@dataclass(frozen=True)
class ProviderRepairCandidate:
    thread_id: str
    rollout_path: str
    archived: bool
    metadata_provider: str
    root_thread_id: str
    parent_thread_id: Optional[str]
    patch_rollout: bool


@dataclass(frozen=True)
class ProviderRepairReport:
    candidates: tuple[ProviderRepairCandidate, ...]
    deferred_thread_ids: tuple[str, ...]
    rejected_thread_ids: tuple[str, ...]
    changed_db_thread_ids: tuple[str, ...] = ()
    changed_rollout_thread_ids: tuple[str, ...] = ()


@dataclass
class _ThreadRecord:
    thread_id: str
    rollout_path: Path
    db_provider: str
    archived: bool
    db_source: Any
    metadata: Optional[dict[str, Any]] = None
    first_line: Optional[bytes] = None


def _codex_home(path: Optional[os.PathLike[str] | str]) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    return Path(configured or "~/.codex").expanduser().resolve()


def _sqlite_home(codex_home: Path) -> Path:
    config_path = codex_home / "config.toml"
    try:
        if config_path.stat().st_size > 4 * 1024 * 1024:
            raise CodexProviderRepairError("Codex config.toml exceeds size limit")
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        return codex_home
    except CodexProviderRepairError:
        raise
    except Exception as exc:
        raise CodexProviderRepairError(
            "unable to read Codex sqlite_home",
        ) from exc
    value = config.get("sqlite_home")
    if value is None:
        return codex_home
    if not isinstance(value, str) or not value.strip():
        raise CodexProviderRepairError("Codex sqlite_home is invalid")
    resolved = Path(value).expanduser()
    if not resolved.is_absolute():
        resolved = codex_home / resolved
    return resolved.resolve()


def _state_db_path(codex_home: Path) -> Path:
    sqlite_home = _sqlite_home(codex_home)
    candidates: list[tuple[int, Path]] = []
    try:
        entries = list(sqlite_home.iterdir())
    except OSError as exc:
        raise CodexProviderRepairError(
            "unable to inspect Codex SQLite directory",
        ) from exc
    for entry in entries:
        match = _STATE_DB.fullmatch(entry.name)
        if match and entry.is_file():
            candidates.append((int(match.group(1)), entry))
    if not candidates:
        raise CodexProviderRepairError("Codex state database was not found")
    return max(candidates, key=lambda item: item[0])[1]


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _read_first_record(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        with path.open("rb") as stream:
            line = stream.readline(MAX_META_BYTES + 1)
    except OSError as exc:
        raise CodexProviderRepairError("unable to read rollout metadata") from exc
    return _read_first_record_bytes(line)


def _read_first_record_bytes(
    line: bytes,
) -> tuple[bytes, dict[str, Any]]:
    if (
        not line
        or len(line) > MAX_META_BYTES
        or not line.endswith(b"\n")
    ):
        raise CodexProviderRepairError("rollout metadata is incomplete")
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise CodexProviderRepairError("rollout metadata is invalid") from exc
    if (
        not isinstance(record, dict)
        or record.get("type") != "session_meta"
        or not isinstance(record.get("payload"), dict)
    ):
        raise CodexProviderRepairError("rollout session_meta is missing")
    return line, record["payload"]


def _thread_link(
    metadata: dict[str, Any],
) -> Optional[tuple[str, int]]:
    source = metadata.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, dict):
        return None
    parent = spawn.get("parent_thread_id")
    if not isinstance(parent, str) or not _SAFE_THREAD_ID.fullmatch(parent):
        return None
    depth = spawn.get("depth")
    if (
        not isinstance(depth, int)
        or isinstance(depth, bool)
        or not 1 <= depth <= MAX_PARENT_DEPTH
    ):
        return None
    forked_from = metadata.get("forked_from_id")
    if (
        isinstance(forked_from, str)
        and forked_from
        and forked_from != parent
    ):
        return None
    return parent, depth


def _normalized_db_source(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith(("{", "[", '"')):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _load_threads(
    connection: sqlite3.Connection,
    codex_home: Path,
) -> dict[str, _ThreadRecord]:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(threads)")
    }
    required = {"id", "rollout_path", "model_provider", "archived", "source"}
    if not required.issubset(columns):
        raise CodexProviderRepairError("unsupported Codex threads schema")
    rows = connection.execute(
        "SELECT id, rollout_path, model_provider, archived, source FROM threads",
    ).fetchall()
    if len(rows) > 100_000:
        raise CodexProviderRepairError("Codex thread inventory exceeds limit")
    session_root = (codex_home / "sessions").resolve()
    archive_root = (codex_home / "archived_sessions").resolve()
    out: dict[str, _ThreadRecord] = {}
    for thread_id, rollout_path, provider, archived, source in rows:
        if (
            not isinstance(thread_id, str)
            or not _SAFE_THREAD_ID.fullmatch(thread_id)
            or not isinstance(rollout_path, str)
            or not isinstance(provider, str)
            or archived not in (0, 1)
        ):
            continue
        path = Path(rollout_path).expanduser()
        expected_root = archive_root if archived else session_root
        if not _is_below(path, expected_root):
            continue
        out[thread_id] = _ThreadRecord(
            thread_id=thread_id,
            rollout_path=path,
            db_provider=provider,
            archived=bool(archived),
            db_source=_normalized_db_source(source),
        )
    return out


def _record_metadata(record: _ThreadRecord) -> Optional[dict[str, Any]]:
    if record.metadata is not None:
        return record.metadata
    try:
        first_line, metadata = _read_first_record(record.rollout_path)
    except CodexProviderRepairError:
        return None
    if metadata.get("id") != record.thread_id:
        return None
    if metadata.get("source") != record.db_source:
        return None
    record.first_line = first_line
    record.metadata = metadata
    return metadata


def _trusted_root(
    thread_id: str,
    records: dict[str, _ThreadRecord],
    memo: dict[str, Optional[tuple[str, int]]],
    visiting: set[str],
    depth: int = 0,
) -> Optional[tuple[str, int]]:
    if thread_id in memo:
        return memo[thread_id]
    if depth > MAX_PARENT_DEPTH or thread_id in visiting:
        memo[thread_id] = None
        return None
    record = records.get(thread_id)
    if record is None:
        memo[thread_id] = None
        return None
    metadata = _record_metadata(record)
    if metadata is None:
        memo[thread_id] = None
        return None
    if metadata.get("originator") != "Codex Desktop":
        memo[thread_id] = None
        return None
    provider = metadata.get("model_provider")
    if provider not in {
        CANONICAL_OPENAI_PROVIDER_ID,
        HTTP_COMPAT_PROVIDER_ID,
    }:
        memo[thread_id] = None
        return None
    link = _thread_link(metadata)
    if link is None:
        if provider == CANONICAL_OPENAI_PROVIDER_ID:
            result = (thread_id, 0)
            memo[thread_id] = result
            return result
        memo[thread_id] = None
        return None
    parent, declared_depth = link
    visiting.add(thread_id)
    parent_lineage = _trusted_root(
        parent, records, memo, visiting, depth + 1)
    visiting.remove(thread_id)
    if (
        parent_lineage is None
        or declared_depth != parent_lineage[1] + 1
    ):
        memo[thread_id] = None
        return None
    result = (parent_lineage[0], declared_depth)
    memo[thread_id] = result
    return result


def _scan_candidates(
    connection: sqlite3.Connection,
    codex_home: Path,
    *,
    roots: Optional[set[str]],
    include_thread_ids: Optional[set[str]],
) -> ProviderRepairReport:
    records = _load_threads(connection, codex_home)
    aliased = [
        record for record in records.values()
        if record.db_provider == HTTP_COMPAT_PROVIDER_ID
    ]
    if len(aliased) > MAX_REPAIR_ROWS:
        raise CodexProviderRepairError("Codex provider repair set exceeds limit")
    memo: dict[str, Optional[tuple[str, int]]] = {}
    candidates: list[ProviderRepairCandidate] = []
    deferred: list[str] = []
    rejected: list[str] = []
    for record in sorted(aliased, key=lambda item: item.thread_id):
        lineage = _trusted_root(record.thread_id, records, memo, set())
        root = lineage[0] if lineage is not None else None
        explicitly_included = bool(
            include_thread_ids and record.thread_id in include_thread_ids
        )
        if root is None or (
            roots is not None and root not in roots and not explicitly_included
        ):
            rejected.append(record.thread_id)
            continue
        metadata = _record_metadata(record)
        if metadata is None:  # defensive: _trusted_root already loaded it
            rejected.append(record.thread_id)
            continue
        metadata_provider = metadata.get("model_provider")
        link = _thread_link(metadata)
        parent = link[0] if link is not None else None
        patch_rollout = metadata_provider == HTTP_COMPAT_PROVIDER_ID
        if patch_rollout and not record.archived:
            deferred.append(record.thread_id)
            continue
        candidates.append(ProviderRepairCandidate(
            thread_id=record.thread_id,
            rollout_path=str(record.rollout_path),
            archived=record.archived,
            metadata_provider=str(metadata_provider),
            root_thread_id=root,
            parent_thread_id=parent,
            patch_rollout=patch_rollout,
        ))
    return ProviderRepairReport(
        candidates=tuple(candidates),
        deferred_thread_ids=tuple(deferred),
        rejected_thread_ids=tuple(rejected),
    )


def _replacement_first_line(original: bytes) -> bytes:
    replaced, count = _PROVIDER_FIELD.subn(
        rb'\1"openai"',
        original,
        count=1,
    )
    if count != 1 or len(replaced) >= len(original):
        raise CodexProviderRepairError(
            "rollout provider field cannot be patched safely",
        )
    newline = b"\r\n" if original.endswith(b"\r\n") else b"\n"
    body = replaced[:-len(newline)]
    replacement = body + (b" " * (len(original) - len(replaced))) + newline
    if len(replacement) != len(original):
        raise CodexProviderRepairError("rollout metadata size changed")
    try:
        before = json.loads(original)
        after = json.loads(replacement)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise CodexProviderRepairError(
            "patched rollout metadata is invalid",
        ) from exc
    before_payload = dict(before["payload"])
    before_payload["model_provider"] = CANONICAL_OPENAI_PROVIDER_ID
    expected = dict(before)
    expected["payload"] = before_payload
    if after != expected:
        raise CodexProviderRepairError(
            "rollout metadata patch changed unrelated fields",
        )
    return replacement


def _pwrite_all(descriptor: int, payload: bytes, offset: int = 0) -> None:
    written = 0
    while written < len(payload):
        count = pwrite(descriptor, payload[written:], offset + written)
        if count <= 0:
            raise OSError("short pwrite while repairing rollout metadata")
        written += count


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("short write while saving provider repair journal")
        written += count


def _journal_path(journal_dir: Path, thread_id: str) -> Path:
    if not _SAFE_THREAD_ID.fullmatch(thread_id):
        raise CodexProviderRepairError("unsafe repair journal thread id")
    return journal_dir / f"{thread_id}.json"


def _write_journal(
    journal_dir: Path,
    candidate: ProviderRepairCandidate,
    original: bytes,
    replacement: bytes,
) -> Path:
    journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _journal_path(journal_dir, candidate.thread_id)
    payload = json.dumps({
        "version": 1,
        "thread_id": candidate.thread_id,
        "rollout_path": candidate.rollout_path,
        "original": base64.b64encode(original).decode("ascii"),
        "replacement": base64.b64encode(replacement).decode("ascii"),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)
    fsync_directory(journal_dir)
    return path


def _patch_rollout(
    candidate: ProviderRepairCandidate,
    journal_dir: Path,
) -> tuple[bytes, bytes, Path]:
    path = Path(candidate.rollout_path)
    original, metadata = _read_first_record(path)
    if (
        metadata.get("id") != candidate.thread_id
        or metadata.get("model_provider") != HTTP_COMPAT_PROVIDER_ID
    ):
        raise CodexProviderRepairError(
            "rollout metadata changed before repair",
        )
    replacement = _replacement_first_line(original)
    journal = _write_journal(
        journal_dir, candidate, original, replacement,
    )
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < len(original):
            raise CodexProviderRepairError("rollout identity changed")
        current = pread(descriptor, len(original), 0)
        if current != original:
            raise CodexProviderRepairError(
                "rollout metadata changed before write",
            )
        _pwrite_all(descriptor, replacement)
        os.fsync(descriptor)
        if pread(descriptor, len(replacement), 0) != replacement:
            raise CodexProviderRepairError(
                "rollout metadata verification failed",
            )
    except BaseException:
        try:
            _pwrite_all(descriptor, original)
            os.fsync(descriptor)
        except Exception:
            pass
        raise
    finally:
        os.close(descriptor)
    return original, replacement, journal


def _recover_journals(
    journal_dir: Path,
    codex_home: Path,
) -> dict[str, Path]:
    if not journal_dir.exists():
        return {}
    try:
        entries = sorted(journal_dir.glob("*.json"))
    except OSError as exc:
        raise CodexProviderRepairError(
            "unable to inspect provider repair journal",
        ) from exc
    if len(entries) > MAX_REPAIR_ROWS:
        raise CodexProviderRepairError("provider repair journal exceeds limit")
    session_root = (codex_home / "sessions").resolve()
    archive_root = (codex_home / "archived_sessions").resolve()
    recovered: dict[str, Path] = {}
    for journal in entries:
        try:
            journal_stat = journal.lstat()
            if (
                not stat.S_ISREG(journal_stat.st_mode)
                or journal_stat.st_size > 4 * 1024 * 1024
            ):
                raise CodexProviderRepairError(
                    "provider repair journal is not a bounded regular file",
                )
            payload = json.loads(journal.read_text(encoding="utf-8"))
            thread_id = payload.get("thread_id")
            rollout = Path(payload.get("rollout_path", "")).expanduser()
            original = base64.b64decode(payload.get("original", ""), validate=True)
            replacement = base64.b64decode(
                payload.get("replacement", ""), validate=True)
        except CodexProviderRepairError:
            raise
        except Exception as exc:
            raise CodexProviderRepairError(
                "provider repair journal is invalid",
            ) from exc
        if (
            payload.get("version") != 1
            or not isinstance(thread_id, str)
            or not _SAFE_THREAD_ID.fullmatch(thread_id)
            or journal != _journal_path(journal_dir, thread_id)
            or len(original) != len(replacement)
            or not original
            or len(original) > MAX_META_BYTES
            or (
                not _is_below(rollout, session_root)
                and not _is_below(rollout, archive_root)
            )
        ):
            raise CodexProviderRepairError(
                "provider repair journal failed validation",
            )
        try:
            _line, original_metadata = _read_first_record_bytes(original)
            expected_replacement = _replacement_first_line(original)
        except CodexProviderRepairError as exc:
            raise CodexProviderRepairError(
                "provider repair journal preimage is invalid",
            ) from exc
        if (
            original_metadata.get("id") != thread_id
            or expected_replacement != replacement
        ):
            raise CodexProviderRepairError(
                "provider repair journal preimage does not match thread",
            )
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(rollout, flags)
        try:
            identity = os.fstat(descriptor)
            if not stat.S_ISREG(identity.st_mode):
                raise CodexProviderRepairError(
                    "journal rollout is not a regular file",
                )
            current = pread(descriptor, len(original), 0)
            if current not in {original, replacement}:
                _pwrite_all(descriptor, original)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        recovered[thread_id] = journal
    return recovered


def _backup_repair_set(
    connection: sqlite3.Connection,
    db_path: Path,
    report: ProviderRepairReport,
    backup_dir: Path,
) -> None:
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise CodexProviderRepairError("repair backup directory is not empty")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    backup_db = backup_dir / db_path.name
    with sqlite3.connect(backup_db) as target:
        connection.backup(target)
    first_lines = []
    for candidate in report.candidates:
        if not candidate.patch_rollout:
            continue
        line, _ = _read_first_record(Path(candidate.rollout_path))
        first_lines.append({
            "thread_id": candidate.thread_id,
            "rollout_path": candidate.rollout_path,
            "first_line": base64.b64encode(line).decode("ascii"),
        })
    manifest = backup_dir / "rollout-first-lines.json"
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(
            {"version": 1, "records": first_lines},
            stream,
            sort_keys=True,
            separators=(",", ":"),
        )
        stream.flush()
        os.fsync(stream.fileno())


def _cleanup_completed_journals(
    connection: sqlite3.Connection,
    codex_home: Path,
    journals: dict[str, Path],
) -> None:
    for thread_id, journal in journals.items():
        row = connection.execute(
            "SELECT rollout_path, model_provider FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if (
            row is None
            or row[1] != CANONICAL_OPENAI_PROVIDER_ID
            or not isinstance(row[0], str)
        ):
            continue
        try:
            _line, metadata = _read_first_record(Path(row[0]))
        except CodexProviderRepairError:
            continue
        if (
            metadata.get("id") == thread_id
            and metadata.get("model_provider")
            == CANONICAL_OPENAI_PROVIDER_ID
        ):
            try:
                journal.unlink()
            except FileNotFoundError:
                pass


def repair_http_provider_records(
    *,
    codex_home: Optional[os.PathLike[str] | str] = None,
    apply: bool = False,
    roots: Optional[Iterable[str]] = None,
    include_thread_ids: Optional[Iterable[str]] = None,
    backup_dir: Optional[os.PathLike[str] | str] = None,
    journal_dir: Optional[os.PathLike[str] | str] = None,
) -> ProviderRepairReport:
    """Discover or repair verified private-provider durable records.

    Dry-run is the default.  ``roots`` restricts descendant repair to trusted
    canonical root ids.  ``include_thread_ids`` additionally admits direct
    canonical metadata rows, which is used for an explicit fork returned by the
    current app-server.  Alias-bearing active rollouts are always deferred:
    rewriting metadata while a live subagent may append is not safe.
    """
    home = _codex_home(codex_home)
    root_set = set(roots) if roots is not None else None
    included = set(include_thread_ids) if include_thread_ids is not None else None
    for values in (root_set, included):
        if values is not None and any(
            not isinstance(value, str) or not _SAFE_THREAD_ID.fullmatch(value)
            for value in values
        ):
            raise CodexProviderRepairError("unsafe repair thread id")
    db_path = _state_db_path(home)
    connection = (
        sqlite3.connect(db_path, timeout=5.0)
        if apply else sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        if not apply:
            connection.execute("PRAGMA query_only=ON")
        effective_journal = Path(
            journal_dir
            or (Path.home() / ".cc-remote" / "codex-provider-repair-journal")
        )
        recovered_journals = (
            _recover_journals(effective_journal, home) if apply else {}
        )
        report = _scan_candidates(
            connection,
            home,
            roots=root_set,
            include_thread_ids=included,
        )
        if not apply or not report.candidates:
            if apply and recovered_journals:
                _cleanup_completed_journals(
                    connection, home, recovered_journals)
            return report
        if backup_dir is not None:
            _backup_repair_set(
                connection, db_path, report, Path(backup_dir),
            )
        patched: list[tuple[bytes, bytes, Path, Path]] = []
        try:
            for candidate in report.candidates:
                if not candidate.patch_rollout:
                    continue
                original, replacement, journal = _patch_rollout(
                    candidate, effective_journal,
                )
                patched.append((
                    original,
                    replacement,
                    journal,
                    Path(candidate.rollout_path),
                ))
            connection.execute("BEGIN IMMEDIATE")
            changed: list[str] = []
            for candidate in report.candidates:
                cursor = connection.execute(
                    "UPDATE threads SET model_provider=? "
                    "WHERE id=? AND model_provider=?",
                    (
                        CANONICAL_OPENAI_PROVIDER_ID,
                        candidate.thread_id,
                        HTTP_COMPAT_PROVIDER_ID,
                    ),
                )
                if cursor.rowcount != 1:
                    raise CodexProviderRepairError(
                        "Codex provider row changed during repair",
                    )
                changed.append(candidate.thread_id)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            for original, replacement, _journal, path in reversed(patched):
                try:
                    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    descriptor = os.open(path, flags)
                    try:
                        current = pread(descriptor, len(replacement), 0)
                        if current == replacement:
                            _pwrite_all(descriptor, original)
                            os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except Exception:
                    pass
            raise
        completed_journals = {
            journal for _original, _replacement, journal, _path in patched
        }
        completed_journals.update(
            journal
            for thread_id, journal in recovered_journals.items()
            if thread_id in changed
        )
        for journal in completed_journals:
            try:
                journal.unlink()
            except FileNotFoundError:
                pass
        changed_rollouts = tuple(
            candidate.thread_id
            for candidate in report.candidates
            if candidate.patch_rollout
        )
        return ProviderRepairReport(
            candidates=report.candidates,
            deferred_thread_ids=report.deferred_thread_ids,
            rejected_thread_ids=report.rejected_thread_ids,
            changed_db_thread_ids=tuple(changed),
            changed_rollout_thread_ids=changed_rollouts,
        )
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise CodexProviderRepairError(
            "Codex state database repair failed",
        ) from exc
    finally:
        connection.close()


def canonical_thread_provider_is_restored(
    thread_id: str,
    *,
    codex_home: Optional[os.PathLike[str] | str] = None,
) -> bool:
    """Return whether one direct Desktop/OpenAI thread is canonical in DB+rollout."""
    if not isinstance(thread_id, str) or not _SAFE_THREAD_ID.fullmatch(thread_id):
        return False
    home = _codex_home(codex_home)
    try:
        db_path = _state_db_path(home)
        with sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro",
            uri=True,
            timeout=1.0,
        ) as connection:
            row = connection.execute(
                "SELECT rollout_path, model_provider, archived "
                "FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
        if row is None or row[1] != CANONICAL_OPENAI_PROVIDER_ID:
            return False
        path = Path(row[0]).expanduser()
        expected_root = home / (
            "archived_sessions" if row[2] else "sessions"
        )
        if not _is_below(path, expected_root):
            return False
        _line, metadata = _read_first_record(path)
        return bool(
            metadata.get("id") == thread_id
            and metadata.get("originator") == "Codex Desktop"
            and metadata.get("model_provider")
            == CANONICAL_OPENAI_PROVIDER_ID
        )
    except (OSError, sqlite3.Error, CodexProviderRepairError):
        return False


def _report_payload(report: ProviderRepairReport) -> dict[str, Any]:
    return {
        "candidates": [{
            "thread_id": candidate.thread_id,
            "archived": candidate.archived,
            "metadata_provider": candidate.metadata_provider,
            "root_thread_id": candidate.root_thread_id,
            "parent_thread_id": candidate.parent_thread_id,
            "patch_rollout": candidate.patch_rollout,
        } for candidate in report.candidates],
        "deferred_thread_ids": list(report.deferred_thread_ids),
        "rejected_thread_ids": list(report.rejected_thread_ids),
        "changed_db_thread_ids": list(report.changed_db_thread_ids),
        "changed_rollout_thread_ids": list(
            report.changed_rollout_thread_ids),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or repair cc-remote's process-local Codex HTTP provider "
            "from verified Desktop/OpenAI durable threads."
        ),
    )
    parser.add_argument("--codex-home")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    parser.add_argument("--journal-dir")
    parser.add_argument(
        "--confirm-writers-stopped",
        action="store_true",
        help="confirm Codex App/CLI/wrapper/app-server writers are stopped",
    )
    args = parser.parse_args(argv)
    if args.apply and (
        not args.confirm_writers_stopped or not args.backup_dir
    ):
        parser.error(
            "--apply requires --backup-dir and --confirm-writers-stopped")
    dry_run = repair_http_provider_records(codex_home=args.codex_home)
    if args.apply and (
        dry_run.deferred_thread_ids or dry_run.rejected_thread_ids
    ):
        raise CodexProviderRepairError(
            "repair set contains deferred or rejected provider rows")
    report = (
        repair_http_provider_records(
            codex_home=args.codex_home,
            apply=True,
            backup_dir=args.backup_dir,
            journal_dir=args.journal_dir,
        )
        if args.apply else dry_run
    )
    print(json.dumps(
        _report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a module CLI
    raise SystemExit(main())
