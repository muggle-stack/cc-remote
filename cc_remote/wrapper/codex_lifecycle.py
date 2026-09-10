"""Small source-bound terminal ledger for Codex History recovery.

The official app-server owns lifecycle.  Rollout History is only a content
projection and can lag a terminal notification while a large source is being
indexed.  This store remembers a bounded set of exact native turn terminals so
newest-page History can carry them independently from narrative parsing.

Persistent records are valid only while the current rollout is a strict
append-only continuation of the captured source boundary.  Rotation,
truncation, rollback, inode reuse, corruption, or an unreadable witness all
degrade to an empty snapshot; none can manufacture a successful terminal.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable, Iterable

from cc_remote.protocol import CodexTerminalFence


# v1 could persist task_complete(error=...) as a successful terminal.
# Rebuild these source-derived fences instead of replaying a false success.
_SCHEMA_VERSION = 2
_FILENAME = "codex-terminal-ledger.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SESSIONS = 512
_MAX_FENCES_PER_SESSION = 16
_WITNESS_BYTES = 64 * 1024


class CodexTerminalLedgerError(RuntimeError):
    """A lifecycle record could not be validated or persisted."""


@dataclass(frozen=True)
class _SourceWitness:
    path: str
    device: int
    inode: int
    size: int
    window_start: int
    window_sha256: str


@dataclass(frozen=True)
class _PersistentSession:
    source: _SourceWitness
    fences: tuple[CodexTerminalFence, ...]
    updated_at: float


@dataclass
class _VolatileSession:
    revision: str
    source_identity: tuple[str, int, int, int] | None
    fences: OrderedDict[str, CodexTerminalFence]


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise CodexTerminalLedgerError("invalid Codex lifecycle identity")
    return value


def _clean_fence(value: object) -> CodexTerminalFence:
    try:
        fence = (
            value
            if isinstance(value, CodexTerminalFence)
            else CodexTerminalFence.model_validate(value)
        )
    except Exception as exc:
        raise CodexTerminalLedgerError(
            "invalid Codex terminal fence") from exc
    _safe_id(fence.turn_id)
    return CodexTerminalFence.model_validate(
        fence.model_dump(mode="python"))


def _real_path(path: str | os.PathLike[str]) -> str:
    value = os.path.realpath(os.fspath(path))
    if not value or "\x00" in value:
        raise CodexTerminalLedgerError("invalid Codex rollout path")
    return value


def _capture_witness(path: str | os.PathLike[str]) -> _SourceWitness:
    real = _real_path(path)
    before = os.stat(real)
    with open(real, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise CodexTerminalLedgerError(
                "Codex rollout rotated while capturing terminal witness")
        size = int(opened.st_size)
        start = max(0, size - _WITNESS_BYTES)
        stream.seek(start)
        boundary = stream.read(size - start)
        finished = os.fstat(stream.fileno())
    after = os.stat(real)
    if (
        finished.st_dev != opened.st_dev
        or finished.st_ino != opened.st_ino
        or finished.st_size < size
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
        or after.st_size < size
        or len(boundary) != size - start
    ):
        raise CodexTerminalLedgerError(
            "Codex rollout changed while capturing terminal witness")
    return _SourceWitness(
        path=real,
        device=int(before.st_dev),
        inode=int(before.st_ino),
        size=size,
        window_start=start,
        window_sha256=hashlib.sha256(boundary).hexdigest(),
    )


def _witness_matches(
    witness: _SourceWitness,
    path: str | os.PathLike[str],
) -> bool:
    try:
        real = _real_path(path)
        before = os.stat(real)
        if (
            real != witness.path
            or int(before.st_dev) != witness.device
            or int(before.st_ino) != witness.inode
            or int(before.st_size) < witness.size
            or witness.window_start < 0
            or witness.window_start > witness.size
            or witness.size - witness.window_start > _WITNESS_BYTES
        ):
            return False
        with open(real, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                int(opened.st_dev) != witness.device
                or int(opened.st_ino) != witness.inode
                or int(opened.st_size) < witness.size
            ):
                return False
            stream.seek(witness.window_start)
            boundary = stream.read(witness.size - witness.window_start)
            finished = os.fstat(stream.fileno())
        after = os.stat(real)
        return (
            int(finished.st_dev) == witness.device
            and int(finished.st_ino) == witness.inode
            and int(finished.st_size) >= witness.size
            and int(after.st_dev) == witness.device
            and int(after.st_ino) == witness.inode
            and int(after.st_size) >= witness.size
            and len(boundary) == witness.size - witness.window_start
            and hashlib.sha256(boundary).hexdigest()
            == witness.window_sha256
        )
    except (OSError, CodexTerminalLedgerError):
        return False


def _source_identity_matches(
    identity: tuple[str, int, int, int],
    path: str,
) -> bool:
    try:
        current = os.stat(path)
    except OSError:
        return False
    identity_path, device, inode, boundary_size = identity
    return (
        identity_path == path
        and device == int(current.st_dev)
        and inode == int(current.st_ino)
        and int(current.st_size) >= boundary_size
    )


class CodexTerminalLedger:
    """Bounded volatile + durable exact-turn terminal projection."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser() / _FILENAME
        # Durable writes intentionally serialize behind their own lock because
        # every update atomically replaces one bounded JSON file.  Volatile
        # terminal publication has a separate lock: it runs on the wrapper's
        # event loop and must never wait behind a background fsync.
        self._lock = threading.RLock()
        self._volatile_lock = threading.RLock()
        self._profile_revision = 0
        self._sessions = self._load()
        self._volatile: OrderedDict[str, _VolatileSession] = OrderedDict()

    def _load(self) -> OrderedDict[str, _PersistentSession]:
        sessions: OrderedDict[str, _PersistentSession] = OrderedDict()
        try:
            if self.path.stat().st_size > _MAX_FILE_BYTES:
                raise ValueError("terminal ledger exceeds size limit")
            raw_text = self.path.read_text(encoding="utf-8")
            if len(raw_text.encode("utf-8", "surrogatepass")) > _MAX_FILE_BYTES:
                raise ValueError("terminal ledger exceeds size limit")
            raw = json.loads(raw_text)
            if (
                not isinstance(raw, dict)
                or raw.get("version") != _SCHEMA_VERSION
                or not isinstance(raw.get("sessions"), dict)
                or len(raw["sessions"]) > _MAX_SESSIONS
            ):
                raise ValueError("terminal ledger has an invalid shape")
            profile_revision = raw.get("profile_revision", 0)
            if (
                isinstance(profile_revision, bool)
                or not isinstance(profile_revision, int)
                or profile_revision < 0
            ):
                raise ValueError("terminal ledger profile revision is invalid")
            self._profile_revision = profile_revision
            for session_id, value in raw["sessions"].items():
                session_id = _safe_id(session_id)
                sessions[session_id] = self._decode_session(value)
        except FileNotFoundError:
            pass
        except Exception:
            # This is a rebuildable projection.  A malformed file is never
            # partially trusted and must not keep the wrapper from starting.
            sessions.clear()
            self._profile_revision = 0
        return sessions

    @staticmethod
    def _decode_session(value: object) -> _PersistentSession:
        if not isinstance(value, dict) or set(value) != {
            "source", "fences", "updated_at",
        }:
            raise ValueError("invalid terminal session")
        source = value["source"]
        if not isinstance(source, dict) or set(source) != {
            "path", "device", "inode", "size", "window_start",
            "window_sha256",
        }:
            raise ValueError("invalid terminal source witness")
        path = source.get("path")
        digest = source.get("window_sha256")
        integers = [
            source.get("device"), source.get("inode"), source.get("size"),
            source.get("window_start"),
        ]
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or "\x00" in path
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or any(isinstance(item, bool) or not isinstance(item, int)
                   or item < 0 for item in integers)
            or source["window_start"] > source["size"]
            or source["size"] - source["window_start"] > _WITNESS_BYTES
        ):
            raise ValueError("invalid terminal source witness")
        raw_fences = value.get("fences")
        if (
            not isinstance(raw_fences, list)
            or len(raw_fences) > _MAX_FENCES_PER_SESSION
        ):
            raise ValueError("invalid terminal fence list")
        fences: OrderedDict[str, CodexTerminalFence] = OrderedDict()
        for raw_fence in raw_fences:
            fence = _clean_fence(raw_fence)
            fences.pop(fence.turn_id, None)
            fences[fence.turn_id] = fence
        updated_at = value.get("updated_at")
        if (
            isinstance(updated_at, bool)
            or not isinstance(updated_at, (int, float))
            or not math.isfinite(updated_at)
            or updated_at < 0
        ):
            raise ValueError("invalid terminal timestamp")
        return _PersistentSession(
            source=_SourceWitness(
                path=path,
                device=source["device"],
                inode=source["inode"],
                size=source["size"],
                window_start=source["window_start"],
                window_sha256=digest,
            ),
            fences=tuple(fences.values()),
            updated_at=float(updated_at),
        )

    @staticmethod
    def _encode_session(value: _PersistentSession) -> dict[str, object]:
        return {
            "source": {
                "path": value.source.path,
                "device": value.source.device,
                "inode": value.source.inode,
                "size": value.source.size,
                "window_start": value.source.window_start,
                "window_sha256": value.source.window_sha256,
            },
            "fences": [
                fence.model_dump(mode="json", exclude_none=True)
                for fence in value.fences
            ],
            "updated_at": value.updated_at,
        }

    def _persist(
        self,
        sessions: OrderedDict[str, _PersistentSession],
        *,
        profile_revision: int | None = None,
    ) -> None:
        revision = (
            self._profile_revision
            if profile_revision is None else profile_revision
        )
        payload = json.dumps({
            "version": _SCHEMA_VERSION,
            "profile_revision": revision,
            "sessions": {
                session_id: self._encode_session(value)
                for session_id, value in sessions.items()
            },
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise CodexTerminalLedgerError(
                "Codex terminal ledger exceeds size limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _bounded_fences(
        values: Iterable[CodexTerminalFence],
    ) -> tuple[CodexTerminalFence, ...]:
        bounded: OrderedDict[str, CodexTerminalFence] = OrderedDict()
        for raw in values:
            fence = _clean_fence(raw)
            bounded.pop(fence.turn_id, None)
            bounded[fence.turn_id] = fence
            while len(bounded) > _MAX_FENCES_PER_SESSION:
                bounded.popitem(last=False)
        return tuple(bounded.values())

    def remember(
        self,
        session_id: str,
        fence: CodexTerminalFence,
        *,
        revision: str,
        source_identity: tuple[str, int, int, int] | None = None,
    ) -> None:
        """Publish a zero-I/O process-local fence before History can race it."""
        session_id = _safe_id(session_id)
        revision = _safe_id(revision)
        fence = _clean_fence(fence)
        identity: tuple[str, int, int, int] | None = None
        if source_identity is not None:
            raw_path, device, inode, size = source_identity
            if (
                isinstance(device, bool)
                or not isinstance(device, int)
                or device < 0
                or isinstance(inode, bool)
                or not isinstance(inode, int)
                or inode < 0
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise CodexTerminalLedgerError(
                    "invalid volatile source identity")
            identity = (_real_path(raw_path), device, inode, size)
        with self._volatile_lock:
            current = self._volatile.get(session_id)
            source_changed = False
            if current is not None:
                previous_identity = current.source_identity
                if (previous_identity is None) != (identity is None):
                    source_changed = True
                elif previous_identity is not None and identity is not None:
                    source_changed = (
                        previous_identity[:3] != identity[:3]
                        or identity[3] < previous_identity[3]
                    )
            if (
                current is None
                or current.revision != revision
                or source_changed
            ):
                current = _VolatileSession(
                    revision=revision,
                    source_identity=identity,
                    fences=OrderedDict(),
                )
                self._volatile[session_id] = current
            elif identity is not None:
                # An append-only continuation advances the minimum source
                # boundary without discarding earlier exact-turn fences.
                current.source_identity = identity
            current.fences.pop(fence.turn_id, None)
            current.fences[fence.turn_id] = fence
            while len(current.fences) > _MAX_FENCES_PER_SESSION:
                current.fences.popitem(last=False)
            self._volatile.move_to_end(session_id)
            while len(self._volatile) > _MAX_SESSIONS:
                self._volatile.popitem(last=False)

    def rebase_revision(
        self,
        session_id: str,
        *,
        previous_revision: str,
        revision: str,
    ) -> bool:
        """Carry a source-bound volatile fence across a read-side revision.

        Switching History projection families or learning an exact display
        alias advances the browser revision without mutating the rollout.  A
        cold/live fence which is already bound to that rollout must remain
        immediately visible.  Unbound fences deliberately stay revision-local;
        destructive invalidation never calls this method.
        """
        session_id = _safe_id(session_id)
        previous_revision = _safe_id(previous_revision)
        revision = _safe_id(revision)
        with self._volatile_lock:
            current = self._volatile.get(session_id)
            if (
                current is None
                or current.revision != previous_revision
                or current.source_identity is None
            ):
                return False
            current.revision = revision
            self._volatile.move_to_end(session_id)
            return True

    def persist(
        self,
        session_id: str,
        fence: CodexTerminalFence,
        source_path: str | os.PathLike[str],
        *,
        expected_source_identity: tuple[str, int, int, int] | None = None,
    ) -> None:
        """Capture one bounded append witness and atomically persist the fence."""
        session_id = _safe_id(session_id)
        fence = _clean_fence(fence)
        witness = _capture_witness(source_path)
        if expected_source_identity is not None:
            raw_path, device, inode, size = expected_source_identity
            if (
                isinstance(device, bool)
                or not isinstance(device, int)
                or device < 0
                or isinstance(inode, bool)
                or not isinstance(inode, int)
                or inode < 0
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or (_real_path(raw_path), device, inode) != (
                    witness.path, witness.device, witness.inode)
                or witness.size < size
            ):
                raise CodexTerminalLedgerError(
                    "Codex rollout source changed before terminal persistence")
        with self._lock:
            if not _witness_matches(witness, source_path):
                raise CodexTerminalLedgerError(
                    "Codex rollout source changed before terminal persistence")
            existing = self._sessions.get(session_id)
            existing_matches = bool(
                existing is not None
                and existing.source.path == witness.path
                and existing.source.device == witness.device
                and existing.source.inode == witness.inode
                and _witness_matches(existing.source, witness.path)
            )
            previous = (
                existing.fences
                if existing is not None and existing_matches
                else ()
            )
            stored_witness = (
                existing.source
                if existing is not None
                and existing_matches
                and existing.source.size >= witness.size
                else witness
            )
            incoming_is_older = bool(
                existing is not None
                and existing_matches
                and witness.size < existing.source.size
            )
            fences = self._bounded_fences(
                (fence, *previous)
                if incoming_is_older else (*previous, fence)
            )
            updated = OrderedDict(self._sessions)
            updated.pop(session_id, None)
            updated[session_id] = _PersistentSession(
                source=stored_witness,
                fences=fences,
                updated_at=time.time(),
            )
            while len(updated) > _MAX_SESSIONS:
                updated.popitem(last=False)
            self._persist(updated)
            self._sessions = updated

    def snapshot(
        self,
        session_id: str,
        source_path: str | os.PathLike[str] | None,
        *,
        revision: str,
    ) -> tuple[CodexTerminalFence, ...]:
        """Return only exact fences still bound to this revision/source."""
        session_id = _safe_id(session_id)
        revision = _safe_id(revision)
        real: str | None = None
        if source_path is not None:
            try:
                real = _real_path(source_path)
            except (OSError, CodexTerminalLedgerError):
                real = None
        merged: OrderedDict[str, CodexTerminalFence] = OrderedDict()
        with self._lock:
            persistent = self._sessions.get(session_id)
            if (
                persistent is not None
                and real is not None
                and _witness_matches(persistent.source, real)
            ):
                for fence in persistent.fences:
                    merged[fence.turn_id] = fence
        with self._volatile_lock:
            volatile = self._volatile.get(session_id)
            volatile_matches_source = bool(
                volatile is not None
                and (
                    volatile.source_identity is None
                    or (
                        real is not None
                        and _source_identity_matches(
                            volatile.source_identity, real)
                    )
                )
            )
            if (
                volatile is not None
                and volatile.revision == revision
                and volatile_matches_source
            ):
                for fence in volatile.fences.values():
                    merged.pop(fence.turn_id, None)
                    merged[fence.turn_id] = fence
            while len(merged) > _MAX_FENCES_PER_SESSION:
                merged.popitem(last=False)
            return tuple(merged.values())

    def migrate_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Replay-safely migrate routed session keys across profile topology."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise CodexTerminalLedgerError(
                "invalid Codex profile revision")
        with self._lock:
            if self._profile_revision >= profile_revision:
                return 0
            updated: OrderedDict[str, _PersistentSession] = OrderedDict()
            migrated = 0
            for session_id, value in self._sessions.items():
                target = _safe_id(transform(session_id))
                previous = updated.get(target)
                if previous is not None and previous != value:
                    raise CodexTerminalLedgerError(
                        "Codex terminal profile migration collides")
                updated[target] = value
                migrated += target != session_id
            self._persist(updated, profile_revision=profile_revision)
            self._sessions = updated
            self._profile_revision = profile_revision
            return migrated
