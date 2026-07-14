"""Durable correlation and crash recovery for Claude transcript forks.

``claude_agent_sdk.fork_session`` creates a persistent transcript locally.  A
wrapper crash after that file is created but before the browser command is
acknowledged must not create a second child on retry.  The wrapper therefore
journals the mutation boundary and supplies a unique temporary title to the
SDK fork.  That title is recoverable from ``list_sessions`` and is verified
against the raw transcript's ``forkedFrom`` metadata before it is trusted.

The human-facing rename must happen only after ``complete`` is durable:
``rename_session`` appends a new custom title, which deliberately hides the
temporary marker from subsequent list calls.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from claude_agent_sdk import list_sessions
from claude_agent_sdk._internal.session_mutations import (
    _find_session_file_with_dir,
)


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MARKER_PREFIX = "cc-remote-fork:"
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_SESSIONS = 4096
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_TAIL_BYTES = 2 * _MAX_RECORD_BYTES
_MAX_CWD_BYTES = 4096
_MAX_ERROR_CHARS = 512
_STATUSES = {
    "intent", "alias", "submitted", "uncertain", "complete", "rejected",
}
_IDENTITY_FIELDS = ("parent_session_id", "cutoff_message_id", "cwd")
_ALLOWED_ENTRY_FIELDS = {
    *_IDENTITY_FIELDS,
    "marker",
    "canonical_request_id",
    "status",
    "session_id",
    "error_message",
    "created_at",
}


class ClaudeForkJournalError(RuntimeError):
    """Claude fork state cannot be read, persisted, or reconciled safely."""


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ClaudeForkJournalError(f"invalid {label}")
    return value


def _canonical_cwd(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ClaudeForkJournalError("invalid fork cwd")
    try:
        size = len(value.encode("utf-8", "surrogatepass"))
    except UnicodeEncodeError as exc:
        raise ClaudeForkJournalError("invalid fork cwd") from exc
    if size > _MAX_CWD_BYTES:
        raise ClaudeForkJournalError("invalid fork cwd")
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise ClaudeForkJournalError("fork cwd must be absolute")
    canonical = os.path.realpath(expanded)
    if len(canonical.encode("utf-8", "surrogatepass")) > _MAX_CWD_BYTES:
        raise ClaudeForkJournalError("invalid fork cwd")
    return canonical


def claude_fork_marker(request_id: str) -> str:
    """Return the exact temporary title for a canonical reliable request."""
    return _MARKER_PREFIX + _safe_id(request_id, "fork request id")


def _marker_request_id(marker: Any) -> str:
    if not isinstance(marker, str) or not marker.startswith(_MARKER_PREFIX):
        raise ClaudeForkJournalError("invalid Claude fork marker")
    request_id = marker[len(_MARKER_PREFIX):]
    if claude_fork_marker(request_id) != marker:
        raise ClaudeForkJournalError("invalid Claude fork marker")
    return request_id


class ClaudeForkJournal:
    """Atomic intent/result journal keyed by browser fork request id."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "claude-forks.json"
        self._lock = threading.RLock()
        self.entries = self._load()

    def _load(self) -> OrderedDict[str, dict[str, Any]]:
        entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        try:
            st = self.path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE_BYTES:
                raise ValueError("Claude fork journal is not a bounded file")
            with self.path.open("rb") as stream:
                raw_bytes = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("Claude fork journal exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(raw, dict) or len(raw) > _MAX_ENTRIES:
                raise ValueError("Claude fork journal has an invalid shape")
            for request_id, entry in raw.items():
                self._validate_entry(request_id, entry)
                entries[request_id] = dict(entry)
            self._validate_aliases(entries)
        except FileNotFoundError:
            pass
        except Exception as exc:
            # Ignoring malformed state could duplicate a transcript fork.
            raise ClaudeForkJournalError(
                "Claude fork journal is unreadable") from exc
        return entries

    @staticmethod
    def _validate_entry(request_id: Any, entry: Any) -> None:
        request_id = _safe_id(request_id, "fork request id")
        if not isinstance(entry, dict):
            raise ValueError("invalid Claude fork journal entry")
        if set(entry) - _ALLOWED_ENTRY_FIELDS:
            raise ValueError("unknown Claude fork journal fields")
        for field in ("parent_session_id", "cutoff_message_id"):
            _safe_id(entry.get(field), field.replace("_", " "))
        cwd = _canonical_cwd(entry.get("cwd"))
        if entry.get("cwd") != cwd:
            raise ValueError("Claude fork cwd is not canonical")
        canonical_id = entry.get("canonical_request_id")
        if canonical_id is not None:
            canonical_id = _safe_id(canonical_id, "canonical fork request id")
        source_id = canonical_id or request_id
        if entry.get("marker") != claude_fork_marker(source_id):
            raise ValueError("invalid Claude fork marker")
        status_value = entry.get("status")
        if status_value not in _STATUSES:
            raise ValueError("invalid Claude fork status")
        if status_value == "alias" and canonical_id is None:
            raise ValueError("Claude fork alias is missing its canonical request")
        if status_value != "alias" and canonical_id is not None:
            # Resolved aliases keep canonical_request_id, so only unresolved
            # non-alias roots are forbidden here.
            if status_value not in {"complete", "rejected"}:
                raise ValueError("invalid Claude fork alias status")
        if status_value == "complete":
            _safe_id(entry.get("session_id"), "forked session id")
        elif entry.get("session_id") is not None:
            raise ValueError("unresolved Claude fork has a child session id")
        if status_value == "rejected":
            error = entry.get("error_message")
            if (not isinstance(error, str) or not error
                    or len(error) > _MAX_ERROR_CHARS):
                raise ValueError("invalid Claude fork rejection")
        elif entry.get("error_message") is not None:
            raise ValueError("non-rejected Claude fork has an error")
        created_at = entry.get("created_at")
        if (isinstance(created_at, bool)
                or not isinstance(created_at, (int, float))
                or not math.isfinite(created_at)
                or created_at < 0):
            raise ValueError("invalid Claude fork timestamp")

    @staticmethod
    def _validate_aliases(
        entries: OrderedDict[str, dict[str, Any]],
    ) -> None:
        for entry in entries.values():
            canonical_id = entry.get("canonical_request_id")
            if canonical_id is None:
                continue
            canonical = entries.get(canonical_id)
            if canonical is None or canonical.get("canonical_request_id") is not None:
                raise ValueError("Claude fork alias has no canonical root")
            if any(entry.get(field) != canonical.get(field)
                   for field in (*_IDENTITY_FIELDS, "marker")):
                raise ValueError("Claude fork alias identity differs from its root")
            compatible = {
                "alias": {"intent", "submitted", "uncertain"},
                "complete": {"complete"},
                "rejected": {"rejected"},
            }
            if canonical.get("status") not in compatible.get(entry.get("status"), set()):
                raise ValueError("Claude fork alias and root states differ")
            if (entry.get("status") == "complete"
                    and entry.get("session_id") != canonical.get("session_id")):
                raise ValueError("Claude fork aliases have different children")
            if (entry.get("status") == "rejected"
                    and entry.get("error_message") != canonical.get("error_message")):
                raise ValueError("Claude fork aliases have different rejections")

    def begin(
        self,
        request_id: str,
        parent_session_id: str,
        cutoff_message_id: str,
        cwd: str,
    ) -> dict[str, Any]:
        request_id = _safe_id(request_id, "fork request id")
        parent_session_id = _safe_id(parent_session_id, "parent session id")
        cutoff_message_id = _safe_id(cutoff_message_id, "cutoff message id")
        cwd = _canonical_cwd(cwd)
        identity = {
            "parent_session_id": parent_session_id,
            "cutoff_message_id": cutoff_message_id,
            "cwd": cwd,
        }
        with self._lock:
            existing = self.entries.get(request_id)
            if existing is not None:
                if any(existing.get(field) != value
                       for field, value in identity.items()):
                    raise ClaudeForkJournalError(
                        "fork request id was already used for another source message")
                self.entries.move_to_end(request_id)
                return dict(existing)

            canonical: Optional[tuple[str, dict[str, Any]]] = None
            for key, value in self.entries.items():
                if value.get("status") not in {
                    "intent", "alias", "submitted", "uncertain",
                }:
                    continue
                if all(value.get(field) == expected
                       for field, expected in identity.items()):
                    root_id = value.get("canonical_request_id") or key
                    canonical = (root_id, self.entries[root_id])
                    break

            updated = OrderedDict(self.entries)
            while len(updated) >= _MAX_ENTRIES:
                removable = self._terminal_group_for_compaction(updated)
                if not removable:
                    raise ClaudeForkJournalError(
                        "Claude fork journal capacity exhausted")
                for key in removable:
                    updated.pop(key)

            if canonical is None:
                entry = {
                    **identity,
                    "marker": claude_fork_marker(request_id),
                    "status": "intent",
                    "created_at": time.time(),
                }
            else:
                canonical_id, canonical_entry = canonical
                entry = {
                    **identity,
                    "marker": canonical_entry["marker"],
                    "canonical_request_id": canonical_id,
                    "status": "alias",
                    "created_at": time.time(),
                }
            updated[request_id] = entry
            self._persist(updated)
            self.entries = updated
            return dict(entry)

    @staticmethod
    def _terminal_group_for_compaction(
        entries: OrderedDict[str, dict[str, Any]],
    ) -> list[str]:
        seen: set[str] = set()
        for entry in entries.values():
            marker = entry.get("marker")
            if not isinstance(marker, str) or marker in seen:
                continue
            seen.add(marker)
            group = [
                (key, candidate) for key, candidate in entries.items()
                if candidate.get("marker") == marker
            ]
            statuses = {candidate.get("status") for _, candidate in group}
            if statuses == {"complete"}:
                children = {candidate.get("session_id") for _, candidate in group}
                if len(children) == 1:
                    return [key for key, _ in group]
            if statuses == {"rejected"}:
                errors = {candidate.get("error_message") for _, candidate in group}
                if len(errors) == 1:
                    return [key for key, _ in group]
        return []

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        request_id = _safe_id(request_id, "fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            return dict(entry) if entry is not None else None

    def get_canonical(self, request_id: str) -> Optional[dict[str, Any]]:
        """Return the canonical root state for a request or unresolved alias."""
        request_id = _safe_id(request_id, "fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            if entry is None:
                return None
            canonical_id = entry.get("canonical_request_id") or request_id
            canonical = self.entries.get(canonical_id)
            if canonical is None:
                raise ClaudeForkJournalError(
                    "canonical fork intent is missing")
            return dict(canonical)

    def claim_submission(self, request_id: str) -> bool:
        """Persist the at-most-once boundary; only one alias may return true."""
        request_id = _safe_id(request_id, "fork request id")
        with self._lock:
            entry = self.entries.get(request_id)
            if entry is None:
                raise ClaudeForkJournalError("fork intent is missing")
            canonical_id = entry.get("canonical_request_id") or request_id
            canonical = self.entries.get(canonical_id)
            if canonical is None:
                raise ClaudeForkJournalError("canonical fork intent is missing")
            if canonical.get("status") != "intent":
                return False
            updated = OrderedDict(self.entries)
            submitted = dict(canonical)
            submitted["status"] = "submitted"
            updated[canonical_id] = submitted
            updated.move_to_end(canonical_id)
            self._persist(updated)
            self.entries = updated
            return True

    def mark_submitted(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            self.claim_submission(request_id)
            entry = self.entries.get(request_id)
            return dict(entry) if entry is not None else {}

    def mark_uncertain(self, request_id: str) -> dict[str, Any]:
        request_id = _safe_id(request_id, "fork request id")
        with self._lock:
            canonical_id, canonical = self._canonical(request_id)
            if canonical.get("status") == "uncertain":
                return dict(self.entries[request_id])
            if canonical.get("status") != "submitted":
                raise ClaudeForkJournalError(
                    "only a submitted Claude fork may become uncertain")
            updated = OrderedDict(self.entries)
            value = dict(canonical)
            value["status"] = "uncertain"
            updated[canonical_id] = value
            updated.move_to_end(canonical_id)
            self._persist(updated)
            self.entries = updated
            return dict(updated[request_id])

    def complete(self, request_id: str, session_id: str) -> dict[str, Any]:
        request_id = _safe_id(request_id, "fork request id")
        session_id = _safe_id(session_id, "forked session id")
        with self._lock:
            _, canonical = self._canonical(request_id)
            status_value = canonical.get("status")
            if status_value == "rejected":
                raise ClaudeForkJournalError("rejected fork request cannot complete")
            if status_value == "intent":
                raise ClaudeForkJournalError("fork submission was not claimed")
            if (status_value == "complete"
                    and canonical.get("session_id") != session_id):
                raise ClaudeForkJournalError(
                    "fork request resolved to two child sessions")
            updated = OrderedDict(self.entries)
            marker = canonical["marker"]
            for key, value in list(updated.items()):
                if value.get("marker") != marker:
                    continue
                resolved = dict(value)
                resolved["status"] = "complete"
                resolved["session_id"] = session_id
                resolved.pop("error_message", None)
                updated[key] = resolved
                updated.move_to_end(key)
            self._persist(updated)
            self.entries = updated
            return dict(updated[request_id])

    def reject(self, request_id: str, message: str) -> dict[str, Any]:
        request_id = _safe_id(request_id, "fork request id")
        bounded = str(message or "Claude SDK rejected the fork")[:_MAX_ERROR_CHARS]
        with self._lock:
            _, canonical = self._canonical(request_id)
            if canonical.get("status") == "complete":
                raise ClaudeForkJournalError("completed fork request cannot reject")
            updated = OrderedDict(self.entries)
            marker = canonical["marker"]
            for key, value in list(updated.items()):
                if value.get("marker") != marker:
                    continue
                rejected = dict(value)
                rejected["status"] = "rejected"
                rejected["error_message"] = bounded
                rejected.pop("session_id", None)
                updated[key] = rejected
                updated.move_to_end(key)
            self._persist(updated)
            self.entries = updated
            return dict(updated[request_id])

    def _canonical(self, request_id: str) -> tuple[str, dict[str, Any]]:
        entry = self.entries.get(request_id)
        if entry is None:
            raise ClaudeForkJournalError("fork intent is missing")
        canonical_id = entry.get("canonical_request_id") or request_id
        canonical = self.entries.get(canonical_id)
        if canonical is None:
            raise ClaudeForkJournalError("canonical fork intent is missing")
        return canonical_id, canonical

    def _persist(self, entries: OrderedDict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = json.dumps(entries, separators=(",", ":")).encode("utf-8")
            if len(payload) > _MAX_FILE_BYTES:
                raise ValueError("Claude fork journal exceeds size limit")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ClaudeForkJournalError(
                "Claude fork journal could not be persisted") from exc


def _decode_record(raw: bytes) -> Optional[dict[str, Any]]:
    if not raw or len(raw) > _MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _verified_transcript_fork(
    path: Path,
    child_session_id: str,
    marker: str,
    parent_session_id: str,
    cutoff_message_id: str,
) -> bool:
    """Verify marker, source parent, and inclusive cutoff using bounded reads."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
                return False
            first_raw = stream.readline(_MAX_RECORD_BYTES + 1)
            if len(first_raw) > _MAX_RECORD_BYTES or not first_raw.endswith(b"\n"):
                return False
            first = _decode_record(first_raw.rstrip(b"\r\n"))
            tail_size = min(st.st_size, _MAX_TAIL_BYTES)
            stream.seek(st.st_size - tail_size)
            tail = stream.read(tail_size)
    except (OSError, ValueError):
        return False
    if first is None:
        return False
    forked_from = first.get("forkedFrom")
    if (first.get("sessionId") != child_session_id
            or not isinstance(forked_from, dict)
            or forked_from.get("sessionId") != parent_session_id):
        return False

    if tail_size < st.st_size:
        newline = tail.find(b"\n")
        if newline < 0:
            return False
        tail = tail[newline + 1:]
    raw_lines = [line.rstrip(b"\r") for line in tail.split(b"\n") if line]
    if not raw_lines or any(len(line) > _MAX_RECORD_BYTES for line in raw_lines):
        return False
    last = _decode_record(raw_lines[-1])
    if (last is None or last.get("type") != "custom-title"
            or last.get("sessionId") != child_session_id
            or last.get("customTitle") != marker):
        return False

    leaf: Optional[dict[str, Any]] = None
    for raw in reversed(raw_lines[:-1]):
        record = _decode_record(raw)
        if isinstance(record, dict) and isinstance(record.get("forkedFrom"), dict):
            leaf = record
            break
    if leaf is None or leaf.get("sessionId") != child_session_id:
        return False
    leaf_source = leaf["forkedFrom"]
    return (
        leaf_source.get("sessionId") == parent_session_id
        and leaf_source.get("messageUuid") == cutoff_message_id
    )


def find_claude_fork(
    marker: str,
    parent_session_id: str,
    cutoff_message_id: str,
    cwd: str,
    *,
    max_sessions: int = 1000,
) -> Optional[dict[str, str]]:
    """Find one marker-titled child and verify its raw fork boundary.

    An exact marker must identify at most one listed session.  Ambiguity and
    SDK/list failures raise instead of being treated as "not found", ensuring a
    submitted or uncertain journal entry is never made eligible for replay.
    """
    _marker_request_id(marker)
    parent_session_id = _safe_id(parent_session_id, "parent session id")
    cutoff_message_id = _safe_id(cutoff_message_id, "cutoff message id")
    cwd = _canonical_cwd(cwd)
    if (isinstance(max_sessions, bool) or not isinstance(max_sessions, int)
            or not 1 <= max_sessions <= _MAX_SESSIONS):
        raise ClaudeForkJournalError("invalid Claude fork scan bound")
    try:
        sessions = list_sessions(limit=max_sessions)
    except Exception as exc:
        raise ClaudeForkJournalError(
            "Claude session list is unavailable during fork recovery") from exc
    exact = [
        info for info in sessions
        if getattr(info, "custom_title", None) == marker
    ]
    if len(exact) > 1:
        raise ClaudeForkJournalError(
            "Claude fork marker resolved to multiple sessions")
    if not exact:
        return None
    info = exact[0]
    session_id = _safe_id(getattr(info, "session_id", None), "forked session id")
    info_cwd = getattr(info, "cwd", None)
    if info_cwd is not None and _canonical_cwd(info_cwd) != cwd:
        return None
    try:
        located = _find_session_file_with_dir(session_id, cwd)
    except Exception as exc:
        raise ClaudeForkJournalError(
            "Claude fork transcript lookup failed") from exc
    if located is None:
        return None
    path, _project_dir = located
    path = Path(path)
    if path.name != f"{session_id}.jsonl":
        return None
    if not _verified_transcript_fork(
        path, session_id, marker, parent_session_id, cutoff_message_id,
    ):
        return None
    return {"session_id": session_id, "cwd": cwd, "marker": marker}
