"""Durable at-most-once journal for destructive session rollback commands.

The browser retries reliable commands after reconnecting.  A wrapper can crash
after an engine has accepted a conversation rollback but before its result is
acknowledged, so an in-memory command cache is not a sufficient idempotency
boundary.  This journal persists the command identity before submission and
never makes a submitted or uncertain command eligible for another mutation.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import uuid4

from cc_remote.wrapper.file_lock_compat import flock, LOCK_EX, LOCK_UN
from cc_remote.wrapper.os_compat import fchmod, fsync_directory


_VERSION = 1
_LOCK_MAGIC = b"cc-remote rollback journal lock v1\n"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
# A protocol-valid prefill may contain 2 Mi characters.  With JSON's ASCII
# escaping, one non-BMP character occupies twelve bytes; keep the file finite
# while still allowing one maximum-sized authoritative result to be recorded.
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_CONFLICTS = 128
_MAX_PATH_BYTES = 4096
_MAX_PREFILL_CHARS = 2 * 1024 * 1024
_MAX_DETAIL_CHARS = 4 * 1024
_STATUSES = {"intent", "submitted", "uncertain", "complete"}
_ENGINES = {"claude", "codex"}
_RESTORE_MODES = {"conversation", "files", "both"}
_RESTORE_OUTCOMES = {"succeeded", "failed", "skipped"}
_IDENTITY_FIELDS = {
    "session_id", "engine", "restore", "num_turns", "checkpoint_id",
}
_ENTRY_FIELDS = {
    "client_id", "cmd_id", "identity", "status", "created_at",
    "updated_at", "result",
}
_RESULT_FIELDS = {
    "session_id", "engine", "restore", "conversation", "files",
    "restored_turns", "conflicts", "prefill_text", "detail",
}
_RESULT_ENVELOPE_FIELDS = {
    "type", "v", "ts", "sid", "seq", "to", "route_id",
}


class RollbackJournalError(RuntimeError):
    """Rollback state cannot be validated or persisted safely."""


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RollbackJournalError(f"invalid {label}")
    return value


def _entry_key(client_id: str, cmd_id: str) -> str:
    # Wire IDs cannot contain '/', so this is both readable and unambiguous.
    return f"{client_id}/{cmd_id}"


def _finite_timestamp(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError(f"invalid rollback {label}")
    return float(value)


def _updated_now(entry: Mapping[str, Any]) -> float:
    """Keep persisted timestamps valid if the wall clock moves backwards."""
    previous = entry.get("updated_at", entry.get("created_at", 0.0))
    if isinstance(previous, bool) or not isinstance(previous, (int, float)):
        raise RollbackJournalError("invalid previous rollback timestamp")
    return max(float(previous), time.time())


def _identity(
    session_id: Any,
    engine: Any,
    restore: Any,
    num_turns: Any,
    checkpoint_id: Any,
) -> dict[str, Any]:
    session_id = _safe_id(session_id, "rollback session id")
    if engine not in _ENGINES:
        raise RollbackJournalError("invalid rollback engine")
    if restore not in _RESTORE_MODES:
        raise RollbackJournalError("invalid rollback restore mode")
    if (isinstance(num_turns, bool) or not isinstance(num_turns, int)
            or not 1 <= num_turns <= 1000):
        raise RollbackJournalError("invalid rollback turn count")
    if engine == "claude":
        checkpoint_id = _safe_id(
            checkpoint_id, "Claude rollback checkpoint id")
    elif checkpoint_id is not None:
        raise RollbackJournalError("Codex rollback cannot use a checkpoint id")
    return {
        "session_id": session_id,
        "engine": engine,
        "restore": restore,
        "num_turns": num_turns,
        "checkpoint_id": checkpoint_id,
    }


def _bounded_text(
    value: Any,
    label: str,
    max_chars: int,
    *,
    optional: bool = True,
) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > max_chars:
        raise RollbackJournalError(f"invalid rollback {label}")
    return value


def _normalize_result(value: Any, identity: Mapping[str, Any]) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        raise RollbackJournalError("invalid rollback result")
    raw = dict(value)
    unknown = set(raw) - _RESULT_FIELDS - _RESULT_ENVELOPE_FIELDS
    if unknown:
        raise RollbackJournalError("unknown rollback result fields")
    if "type" in raw and raw["type"] != "rollback_result":
        raise RollbackJournalError("invalid rollback result type")
    for field in ("session_id", "engine", "restore", "conversation", "files"):
        if field not in raw:
            raise RollbackJournalError(f"rollback result is missing {field}")
    if raw["session_id"] != identity["session_id"]:
        raise RollbackJournalError("rollback result session differs from intent")
    if raw["engine"] != identity["engine"]:
        raise RollbackJournalError("rollback result engine differs from intent")
    if raw["restore"] != identity["restore"]:
        raise RollbackJournalError("rollback result mode differs from intent")
    for field in ("conversation", "files"):
        if raw[field] not in _RESTORE_OUTCOMES:
            raise RollbackJournalError(f"invalid rollback {field} outcome")
    restored_turns = raw.get("restored_turns", 0)
    if (isinstance(restored_turns, bool)
            or not isinstance(restored_turns, int)
            or not 0 <= restored_turns <= identity["num_turns"]):
        raise RollbackJournalError("invalid restored turn count")
    if (identity["restore"] == "conversation" and raw["files"] != "skipped"):
        raise RollbackJournalError("conversation rollback cannot restore files")
    if (identity["restore"] == "files"
            and raw["conversation"] != "skipped"):
        raise RollbackJournalError("file rollback cannot restore conversation")
    if (restored_turns > 0
            and "succeeded" not in {raw["conversation"], raw["files"]}):
        raise RollbackJournalError(
            "failed rollback cannot report restored turns")
    conflicts = raw.get("conflicts", [])
    if not isinstance(conflicts, list) or len(conflicts) > _MAX_CONFLICTS:
        raise RollbackJournalError("invalid rollback conflict list")
    normalized_conflicts: list[str] = []
    for path in conflicts:
        if (not isinstance(path, str) or not path or "\x00" in path
                or len(path.encode("utf-8", "surrogatepass")) > _MAX_PATH_BYTES):
            raise RollbackJournalError("invalid rollback conflict path")
        normalized_conflicts.append(path)
    return {
        "session_id": identity["session_id"],
        "engine": identity["engine"],
        "restore": identity["restore"],
        "conversation": raw["conversation"],
        "files": raw["files"],
        "restored_turns": restored_turns,
        "conflicts": normalized_conflicts,
        "prefill_text": _bounded_text(
            raw.get("prefill_text"), "prefill text", _MAX_PREFILL_CHARS),
        "detail": _bounded_text(
            raw.get("detail"), "result detail", _MAX_DETAIL_CHARS),
    }


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> OrderedDict[str, Any]:
    value: OrderedDict[str, Any] = OrderedDict()
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate rollback journal field")
        value[key] = item
    return value


class RollbackCommandJournal:
    """Atomic rollback command state keyed by ``client_id + cmd_id``.

    ``mark_submitted`` is the at-most-once claim operation.  It returns ``True``
    only for the process that durably changes ``intent`` to ``submitted``;
    retries, including retries after a wrapper restart, return ``False``.
    """

    def __init__(self, state_dir: Path):
        state_dir = Path(state_dir)
        self.path = state_dir / "rollback-commands.json"
        self.lock_path = state_dir / "rollback-commands.lock"
        self._thread_lock = threading.RLock()
        with self._thread_lock, self._file_lock(
            allow_uninitialized=True,
        ) as (lock_fd, lock_created, initialized):
            if initialized:
                self.entries = self._load()
                return

            try:
                self.path.lstat()
                state_exists = True
            except FileNotFoundError:
                state_exists = False
            if lock_created:
                if state_exists:
                    # Validate the unexpected state before reporting the
                    # missing marker so malformed files remain fail closed for
                    # their own reason as well.
                    self._load()
                    # A state file without its persistent marker means some
                    # prior idempotency evidence may have been removed.
                    raise RollbackJournalError(
                        "rollback journal marker is missing")
                self.entries = OrderedDict()
                self._persist_bounded(self.entries)
            else:
                if not state_exists:
                    # The lock marker survives normal atomic state updates.  An
                    # existing empty lock plus a missing state file is not a
                    # fresh install; accepting it would replay old commands.
                    raise RollbackJournalError("rollback journal is missing")
                self.entries = self._load()
                if self.entries:
                    raise RollbackJournalError(
                        "rollback journal marker is unreadable")
            self._initialize_lock(lock_fd)

    @contextmanager
    def _file_lock(
        self,
        *,
        allow_uninitialized: bool = False,
    ) -> Iterator[tuple[int, bool, bool]]:
        fd: Optional[int] = None
        created = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(
                    self.lock_path,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                created = True
            except FileExistsError:
                fd = os.open(self.lock_path, flags)
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode)
                    or st.st_size > len(_LOCK_MAGIC)):
                raise OSError("rollback lock is not a regular file")
<<<<<<< HEAD
            fchmod(fd, self.lock_path, 0o600)
            flock(fd, LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            marker = os.read(fd, len(_LOCK_MAGIC) + 1)
            if marker == _LOCK_MAGIC:
                initialized = True
            elif marker == b"" and allow_uninitialized:
                initialized = False
            else:
                raise OSError("rollback lock marker is invalid")
        except Exception as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise RollbackJournalError(
                "rollback journal lock is unavailable") from exc
        assert fd is not None
        try:
            yield fd, created, initialized
        finally:
            try:
                flock(fd, LOCK_UN)
            finally:
                os.close(fd)

    def _initialize_lock(self, fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = memoryview(_LOCK_MAGIC)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("rollback lock marker write made no progress")
                remaining = remaining[written:]
            os.ftruncate(fd, len(_LOCK_MAGIC))
            os.fsync(fd)
        except Exception as exc:
            raise RollbackJournalError(
                "rollback journal marker could not be persisted") from exc

    def _load(self) -> OrderedDict[str, dict[str, Any]]:
        try:
            st = self.path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE_BYTES:
                raise ValueError("rollback journal is not a bounded file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            with os.fdopen(fd, "rb") as stream:
                raw_bytes = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("rollback journal exceeds size limit")
            raw = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("invalid JSON constant")),
            )
            if (not isinstance(raw, dict) or set(raw) != {"version", "entries"}
                    or isinstance(raw["version"], bool)
                    or raw["version"] != _VERSION
                    or not isinstance(raw["entries"], dict)
                    or len(raw["entries"]) > _MAX_ENTRIES):
                raise ValueError("rollback journal has an invalid shape")
            entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
            for key, entry in raw["entries"].items():
                self._validate_entry(key, entry)
                entries[key] = copy.deepcopy(dict(entry))
            return entries
        except FileNotFoundError as exc:
            raise RollbackJournalError("rollback journal is missing") from exc
        except Exception as exc:
            # Treating corrupt state as empty could repeat a native rollback.
            raise RollbackJournalError("rollback journal is unreadable") from exc

    @staticmethod
    def _validate_entry(key: Any, entry: Any) -> None:
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("invalid rollback journal entry")
        if set(entry) - _ENTRY_FIELDS:
            raise ValueError("unknown rollback journal fields")
        required = _ENTRY_FIELDS - {"result"}
        if not required.issubset(entry):
            raise ValueError("rollback journal entry is incomplete")
        client_id = _safe_id(entry.get("client_id"), "rollback client id")
        cmd_id = _safe_id(entry.get("cmd_id"), "rollback command id")
        if key != _entry_key(client_id, cmd_id):
            raise ValueError("rollback journal key differs from its identity")
        raw_identity = entry.get("identity")
        if not isinstance(raw_identity, dict) or set(raw_identity) != _IDENTITY_FIELDS:
            raise ValueError("invalid rollback journal identity")
        normalized_identity = _identity(**raw_identity)
        if raw_identity != normalized_identity:
            raise ValueError("non-canonical rollback journal identity")
        status_value = entry.get("status")
        if status_value not in _STATUSES:
            raise ValueError("invalid rollback journal status")
        created_at = _finite_timestamp(entry.get("created_at"), "creation time")
        updated_at = _finite_timestamp(entry.get("updated_at"), "update time")
        if updated_at < created_at:
            raise ValueError("rollback journal timestamps are out of order")
        if status_value == "complete":
            result = entry.get("result")
            if not isinstance(result, dict) or set(result) != _RESULT_FIELDS:
                raise ValueError("completed rollback has no structured result")
            if result != _normalize_result(result, normalized_identity):
                raise ValueError("non-canonical rollback journal result")
        elif "result" in entry:
            raise ValueError("unresolved rollback has a result")

    def begin(
        self,
        client_id: str,
        cmd_id: str,
        session_id: str,
        engine: str,
        restore: str,
        num_turns: int,
        checkpoint_id: Optional[str] = None,
    ) -> dict[str, Any]:
        client_id = _safe_id(client_id, "rollback client id")
        cmd_id = _safe_id(cmd_id, "rollback command id")
        identity = _identity(
            session_id, engine, restore, num_turns, checkpoint_id)
        key = _entry_key(client_id, cmd_id)
        with self._thread_lock, self._file_lock():
            current = self._load()
            existing = current.get(key)
            if existing is not None:
                if existing["identity"] != identity:
                    raise RollbackJournalError(
                        "rollback command id was already used for another intent")
                self.entries = current
                return copy.deepcopy(existing)
            now = time.time()
            updated = OrderedDict(current)
            updated[key] = {
                "client_id": client_id,
                "cmd_id": cmd_id,
                "identity": identity,
                "status": "intent",
                "created_at": now,
                "updated_at": now,
            }
            persisted = self._persist_bounded(updated)
            self.entries = persisted
            return copy.deepcopy(persisted[key])

    def get(self, client_id: str, cmd_id: str) -> Optional[dict[str, Any]]:
        client_id = _safe_id(client_id, "rollback client id")
        cmd_id = _safe_id(cmd_id, "rollback command id")
        key = _entry_key(client_id, cmd_id)
        with self._thread_lock, self._file_lock():
            current = self._load()
            self.entries = current
            entry = current.get(key)
            return copy.deepcopy(entry) if entry is not None else None

    def mark_submitted(self, client_id: str, cmd_id: str) -> bool:
        """Claim the native mutation boundary, durably and at most once."""
        key = _entry_key(
            _safe_id(client_id, "rollback client id"),
            _safe_id(cmd_id, "rollback command id"),
        )
        with self._thread_lock, self._file_lock():
            current = self._load()
            entry = current.get(key)
            if entry is None:
                raise RollbackJournalError("rollback intent is missing")
            if entry["status"] != "intent":
                self.entries = current
                return False
            updated = OrderedDict(current)
            submitted = copy.deepcopy(entry)
            submitted["status"] = "submitted"
            submitted["updated_at"] = _updated_now(entry)
            updated[key] = submitted
            persisted = self._persist_bounded(updated)
            self.entries = persisted
            return True

    def mark_uncertain(self, client_id: str, cmd_id: str) -> dict[str, Any]:
        key = _entry_key(
            _safe_id(client_id, "rollback client id"),
            _safe_id(cmd_id, "rollback command id"),
        )
        with self._thread_lock, self._file_lock():
            current = self._load()
            entry = current.get(key)
            if entry is None:
                raise RollbackJournalError("rollback intent is missing")
            if entry["status"] in {"uncertain", "complete"}:
                self.entries = current
                return copy.deepcopy(entry)
            if entry["status"] != "submitted":
                raise RollbackJournalError(
                    "only a submitted rollback may become uncertain")
            updated = OrderedDict(current)
            uncertain = copy.deepcopy(entry)
            uncertain["status"] = "uncertain"
            uncertain["updated_at"] = _updated_now(entry)
            updated[key] = uncertain
            persisted = self._persist_bounded(updated)
            self.entries = persisted
            return copy.deepcopy(persisted[key])

    def complete(
        self,
        client_id: str,
        cmd_id: str,
        result: Any,
    ) -> dict[str, Any]:
        key = _entry_key(
            _safe_id(client_id, "rollback client id"),
            _safe_id(cmd_id, "rollback command id"),
        )
        with self._thread_lock, self._file_lock():
            current = self._load()
            entry = current.get(key)
            if entry is None:
                raise RollbackJournalError("rollback intent is missing")
            normalized = _normalize_result(result, entry["identity"])
            if entry["status"] == "intent":
                raise RollbackJournalError("rollback submission was not claimed")
            if entry["status"] == "complete":
                if entry["result"] != normalized:
                    raise RollbackJournalError(
                        "rollback command resolved to two different results")
                self.entries = current
                return copy.deepcopy(entry)
            updated = OrderedDict(current)
            completed = copy.deepcopy(entry)
            completed["status"] = "complete"
            completed["updated_at"] = _updated_now(entry)
            completed["result"] = normalized
            updated[key] = completed
            persisted = self._persist_bounded(updated)
            self.entries = persisted
            return copy.deepcopy(persisted[key])

    def _persist_bounded(
        self,
        entries: OrderedDict[str, dict[str, Any]],
    ) -> OrderedDict[str, dict[str, Any]]:
        updated = OrderedDict(entries)
        payload = self._encode(updated)
        if len(updated) > _MAX_ENTRIES or len(payload) > _MAX_FILE_BYTES:
            # A complete result is still the only durable evidence that an old
            # reliable command already mutated the engine.  Until the protocol
            # has a persisted client ACK, evicting even terminal entries would
            # let a delayed retry recreate intent and execute rollback again.
            raise RollbackJournalError("rollback journal capacity exhausted")
        self._persist_payload(payload)
        return updated

    @staticmethod
    def _encode(entries: OrderedDict[str, dict[str, Any]]) -> bytes:
        return json.dumps(
            {"version": _VERSION, "entries": entries},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _persist_payload(self, payload: bytes) -> None:
        tmp = self.path.with_suffix(
            f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp, self.path)
            fsync_directory(self.path.parent)
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise RollbackJournalError(
                "rollback journal could not be persisted") from exc
