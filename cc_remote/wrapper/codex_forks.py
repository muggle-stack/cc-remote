"""Durable correlation for persistent Codex ``thread/fork`` requests.

Codex app-server returns a caller supplied ``threadSource`` on the immediate
fork response and writes it to rollout ``session_meta``, but its state DB drops
that value from later ``thread/list``/``thread/read`` responses.  The wrapper
therefore journals intent before issuing the mutating RPC and the child id
before acknowledging the browser command.  The rollout marker closes the small
crash window between those two durable writes.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_META_RECORD_BYTES = 1024 * 1024
_SOURCE_PREFIX = "cc-remote-fork:"


class ForkJournalError(RuntimeError):
    """The durable fork journal could not safely serve a request."""


def fork_thread_source(request_id: str) -> str:
    if not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id):
        raise ForkJournalError("invalid fork request id")
    return _SOURCE_PREFIX + request_id


class CodexForkJournal:
    """Small atomic intent/result map keyed by the reliable request id."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "codex-forks.json"
        self._lock = threading.RLock()
        self.entries = self._load()

    def _load(self) -> OrderedDict[str, dict[str, Any]]:
        entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        try:
            if self.path.stat().st_size > _MAX_FILE_BYTES:
                raise ValueError("fork journal exceeds size limit")
            with self.path.open() as stream:
                raw_text = stream.read(_MAX_FILE_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) > _MAX_FILE_BYTES:
                raise ValueError("fork journal exceeds size limit")
            raw = json.loads(raw_text)
            if not isinstance(raw, dict) or len(raw) > _MAX_ENTRIES:
                raise ValueError("fork journal has an invalid shape")
            for request_id, entry in raw.items():
                self._validate_entry(request_id, entry)
                entries[request_id] = dict(entry)
            self._validate_aliases(entries)
        except FileNotFoundError:
            pass
        except Exception as exc:
            # Ignoring this state could duplicate a persistent fork after the
            # browser retries. Refuse fail-open startup instead.
            raise ForkJournalError("Codex fork journal is unreadable") from exc
        return entries

    @staticmethod
    def _validate_aliases(entries: OrderedDict[str, dict[str, Any]]) -> None:
        identity_fields = (
            "parent_session_id", "last_turn_id", "target", "cwd",
            "thread_source",
        )
        for request_id, entry in entries.items():
            canonical_id = entry.get("canonical_request_id")
            if canonical_id is None:
                continue
            canonical = entries.get(canonical_id)
            if canonical is None or canonical.get("canonical_request_id") is not None:
                raise ValueError("fork alias has no canonical root")
            if any(entry.get(field) != canonical.get(field)
                   for field in identity_fields):
                raise ValueError("fork alias identity differs from its canonical root")
            compatible = {
                "alias": {"intent", "submitted", "uncertain"},
                "complete": {"complete"},
                "rejected": {"rejected"},
            }
            allowed_canonical = compatible.get(entry.get("status"))
            if (allowed_canonical is None
                    or canonical.get("status") not in allowed_canonical):
                raise ValueError("fork alias and canonical states are inconsistent")
            if (entry.get("status") == "complete"
                    and entry.get("session_id") != canonical.get("session_id")):
                raise ValueError("fork alias and canonical child ids differ")
            if (entry.get("status") == "rejected"
                    and entry.get("error_message") != canonical.get("error_message")):
                raise ValueError("fork alias and canonical rejections differ")

    @staticmethod
    def _validate_entry(request_id: Any, entry: Any) -> None:
        if not isinstance(request_id, str) or not _SAFE_ID.fullmatch(request_id):
            raise ValueError("invalid request id")
        if not isinstance(entry, dict):
            raise ValueError("invalid journal entry")
        required_ids = ("parent_session_id", "last_turn_id")
        if any(not isinstance(entry.get(key), str)
               or not _SAFE_ID.fullmatch(entry[key]) for key in required_ids):
            raise ValueError("invalid fork identity")
        canonical_request_id = entry.get("canonical_request_id")
        if canonical_request_id is not None and (
            not isinstance(canonical_request_id, str)
            or not _SAFE_ID.fullmatch(canonical_request_id)
        ):
            raise ValueError("invalid canonical fork request id")
        source_request_id = canonical_request_id or request_id
        if entry.get("thread_source") != fork_thread_source(source_request_id):
            raise ValueError("invalid fork source")
        if entry.get("target") != "same_cwd":
            raise ValueError("invalid fork target")
        cwd = entry.get("cwd")
        if (not isinstance(cwd, str) or not cwd or "\x00" in cwd
                or len(cwd.encode("utf-8", "surrogatepass")) > 4096):
            raise ValueError("invalid fork cwd")
        if entry.get("status") not in {
            "intent", "alias", "submitted", "uncertain", "rejected", "complete",
        }:
            raise ValueError("invalid fork status")
        if entry.get("status") == "alias" and canonical_request_id is None:
            raise ValueError("fork alias is missing its canonical request")
        session_id = entry.get("session_id")
        if entry.get("status") == "complete" and (
            not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id)
        ):
            raise ValueError("invalid child session id")
        if entry.get("status") == "rejected" and (
            not isinstance(entry.get("error_message"), str)
            or not entry["error_message"]
            or len(entry["error_message"]) > 512
        ):
            raise ValueError("invalid fork rejection")
        created_at = entry.get("created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise ValueError("invalid fork timestamp")

    def begin(
        self,
        request_id: str,
        parent_session_id: str,
        last_turn_id: str,
        cwd: str,
    ) -> dict[str, Any]:
        with self._lock:
            return self._begin(
                request_id, parent_session_id, last_turn_id, cwd)

    def _begin(
        self,
        request_id: str,
        parent_session_id: str,
        last_turn_id: str,
        cwd: str,
    ) -> dict[str, Any]:
        source = fork_thread_source(request_id)
        identity = {
            "parent_session_id": parent_session_id,
            "last_turn_id": last_turn_id,
            "target": "same_cwd",
            "cwd": cwd,
            "thread_source": source,
        }
        identity_fields = {
            key: value for key, value in identity.items()
            if key != "thread_source"
        }
        existing = self.entries.get(request_id)
        if existing is not None:
            if any(existing.get(key) != value
                   for key, value in identity_fields.items()):
                raise ForkJournalError(
                    "fork request id was already used for another source turn")
            self.entries.move_to_end(request_id)
            return dict(existing)

        canonical: Optional[tuple[str, dict[str, Any]]] = next((
            (key, value) for key, value in self.entries.items()
            if value.get("status") in {"intent", "alias", "submitted", "uncertain"}
            and all(value.get(field) == expected
                    for field, expected in identity_fields.items())
        ), None)

        updated = OrderedDict(self.entries)
        # Completed commands outside the wrapper's in-memory retry window are
        # the only safe entries to compact. Keep every unresolved intent.
        while len(updated) >= _MAX_ENTRIES:
            removable_group = self._terminal_group_for_compaction(updated)
            if not removable_group:
                raise ForkJournalError("Codex fork journal capacity exhausted")
            for key in removable_group:
                updated.pop(key)
        if canonical is None:
            entry = {
                **identity,
                "status": "intent",
                "created_at": time.time(),
            }
        else:
            canonical_key, canonical_entry = canonical
            canonical_request_id = (
                canonical_entry.get("canonical_request_id") or canonical_key)
            entry = {
                **identity,
                "thread_source": canonical_entry["thread_source"],
                "canonical_request_id": canonical_request_id,
                "status": "alias",
                "created_at": time.time(),
            }
        updated[request_id] = entry
        self._persist(updated)
        self.entries = updated
        return dict(updated[request_id])

    @staticmethod
    def _terminal_group_for_compaction(
        entries: OrderedDict[str, dict[str, Any]],
    ) -> list[str]:
        """Return one whole, internally-consistent terminal source group."""
        seen_sources: set[str] = set()
        for value in entries.values():
            source = value.get("thread_source")
            if not isinstance(source, str) or source in seen_sources:
                continue
            seen_sources.add(source)
            group = [
                (key, candidate) for key, candidate in entries.items()
                if candidate.get("thread_source") == source
            ]
            statuses = {candidate.get("status") for _, candidate in group}
            if statuses == {"complete"}:
                children = {candidate.get("session_id") for _, candidate in group}
                if len(children) == 1:
                    return [key for key, _ in group]
            elif statuses == {"rejected"}:
                rejections = {
                    candidate.get("error_message") for _, candidate in group}
                if len(rejections) == 1:
                    return [key for key, _ in group]
        return []

    def complete(self, request_id: str, session_id: str) -> dict[str, Any]:
        with self._lock:
            return self._complete(request_id, session_id)

    def _complete(self, request_id: str, session_id: str) -> dict[str, Any]:
        existing = self.entries.get(request_id)
        if existing is None:
            raise ForkJournalError("fork intent is missing")
        if existing.get("status") == "rejected":
            raise ForkJournalError("rejected fork request cannot complete")
        if not isinstance(session_id, str) or not _SAFE_ID.fullmatch(session_id):
            raise ForkJournalError("invalid forked session id")
        if (existing.get("status") == "complete"
                and existing.get("session_id") != session_id):
            raise ForkJournalError("fork request resolved to two child sessions")
        updated = OrderedDict(self.entries)
        source = existing.get("thread_source")
        for key, value in list(updated.items()):
            if (value.get("thread_source") == source
                    and value.get("status") in {
                        "intent", "alias", "submitted", "uncertain", "complete",
                    }):
                resolved = dict(value)
                resolved["status"] = "complete"
                resolved["session_id"] = session_id
                updated[key] = resolved
                updated.move_to_end(key)
        self._persist(updated)
        self.entries = updated
        return dict(updated[request_id])

    def mark_submitted(self, request_id: str) -> dict[str, Any]:
        """Durably cross the no-replay boundary before writing the RPC."""
        with self._lock:
            self._claim_submission(request_id)
            entry = self.entries.get(request_id)
            return dict(entry) if entry is not None else {}

    def claim_submission(self, request_id: str) -> bool:
        """Atomically let exactly one canonical/alias handler write the RPC."""
        with self._lock:
            return self._claim_submission(request_id)

    def _claim_submission(self, request_id: str) -> bool:
        entry = self.entries.get(request_id)
        if entry is None:
            raise ForkJournalError("fork intent is missing")
        canonical_id = entry.get("canonical_request_id") or request_id
        canonical = self.entries.get(canonical_id)
        if canonical is None:
            raise ForkJournalError("canonical fork intent is missing")
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

    def mark_uncertain(self, request_id: str) -> dict[str, Any]:
        """Persist that a submitted RPC must only be reconciled, never replayed."""
        with self._lock:
            return self._set_status(request_id, "uncertain")

    def reject(self, request_id: str, message: str) -> dict[str, Any]:
        bounded = str(message or "Codex app-server rejected the fork")[:512]
        with self._lock:
            return self._set_status(
                request_id, "rejected", error_message=bounded)

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self.entries.get(request_id)
            return dict(entry) if entry is not None else None

    def _set_status(
        self, request_id: str, status: str, **fields: Any,
    ) -> dict[str, Any]:
        existing = self.entries.get(request_id)
        if existing is None:
            raise ForkJournalError("fork intent is missing")
        canonical_id = existing.get("canonical_request_id") or request_id
        canonical = self.entries.get(canonical_id)
        if canonical is None:
            raise ForkJournalError("canonical fork intent is missing")
        if canonical.get("status") == "complete":
            return dict(existing)
        if canonical.get("status") == "rejected" and status != "rejected":
            raise ForkJournalError("rejected fork request cannot be resubmitted")
        updated = OrderedDict(self.entries)
        source = canonical.get("thread_source")
        targets = (
            [key for key, value in updated.items()
             if value.get("thread_source") == source]
            if status == "rejected" else [canonical_id]
        )
        for key in targets:
            entry = dict(updated[key])
            entry["status"] = status
            entry.update(fields)
            updated[key] = entry
            updated.move_to_end(key)
        self._persist(updated)
        self.entries = updated
        return dict(updated[request_id])

    def _persist(self, entries: OrderedDict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = json.dumps(entries, separators=(",", ":"))
            if len(payload.encode("utf-8")) > _MAX_FILE_BYTES:
                raise ValueError("fork journal exceeds size limit")
            with tmp.open("w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
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
            raise ForkJournalError("Codex fork journal could not be persisted") from exc


def rollout_fork_meta(path: Optional[str]) -> Optional[dict[str, str]]:
    """Read the bounded session_meta record needed for crash recovery."""
    if not path:
        return None
    try:
        with open(path) as stream:
            # session_meta is normally the first record. Bound both malformed
            # leading data and record size so recovery cannot become a file scan.
            for _ in range(16):
                line = stream.readline(_MAX_META_RECORD_BYTES + 1)
                if not line:
                    return None
                if len(line) > _MAX_META_RECORD_BYTES or not line.endswith("\n"):
                    return None
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    return None
                values: dict[str, str] = {}
                required = {
                    "session_id": payload.get("id"),
                    "thread_source": payload.get("thread_source"),
                    "forked_from_id": payload.get("forked_from_id"),
                }
                if all(isinstance(value, str) for value in required.values()):
                    values.update(required)  # type: ignore[arg-type]
                    if isinstance(payload.get("cwd"), str):
                        values["cwd"] = payload["cwd"]
                    return values
                return None
    except (OSError, UnicodeError):
        return None
    return None


def find_rollout_fork(
    thread_source: str,
    parent_session_id: str,
    cwd: str,
    *,
    roots: Optional[tuple[str, ...]] = None,
    max_files_per_root: int = 1000,
) -> Optional[dict[str, str]]:
    """Find a committed fork by its persistent rollout marker.

    Date-based Codex rollout paths and filenames sort chronologically, so a
    reverse walk finds the just-created child first. Active and archived roots
    each have their own hard file bound; symlinks are never followed.
    """
    if roots is None:
        roots = (
            os.path.expanduser("~/.codex/sessions"),
            os.path.expanduser("~/.codex/archived_sessions"),
        )
    expected_cwd = os.path.realpath(cwd)
    for source_root in roots:
        root = os.path.realpath(source_root)
        scanned = 0
        if not os.path.isdir(root):
            continue
        for directory, dirs, files in os.walk(root, topdown=True):
            dirs.sort(reverse=True)
            files.sort(reverse=True)
            for filename in files:
                if scanned >= max_files_per_root:
                    break
                if not filename.endswith(".jsonl"):
                    continue
                scanned += 1
                path = os.path.realpath(os.path.join(directory, filename))
                try:
                    if os.path.commonpath((root, path)) != root:
                        continue
                except ValueError:
                    continue
                meta = rollout_fork_meta(path)
                if meta is None:
                    continue
                if (
                    meta.get("thread_source") == thread_source
                    and meta.get("forked_from_id") == parent_session_id
                    and (not meta.get("cwd")
                         or os.path.realpath(meta["cwd"]) == expected_cwd)
                ):
                    return meta
            if scanned >= max_files_per_root:
                break
    return None
