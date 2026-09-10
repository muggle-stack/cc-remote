"""Durable cross-client presentation receipts for native sessions.

Completion badges and Goal dismissal are user-visible session state, not
engine transcript metadata.  Keep their latest bounded projection in the
wrapper so acknowledging either surface on one browser immediately applies to
every other browser and survives a wrapper restart.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable, Literal
from uuid import uuid4


_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 16 * 1024 * 1024
_ENGINES = frozenset({"claude", "codex", "dsh"})
_LEGACY_ENGINE = "legacy"


def _engine(value: object) -> Literal["claude", "codex", "dsh"]:
    if value not in _ENGINES:
        raise SessionPresentationStoreError(
            "session presentation engine is invalid")
    return value  # type: ignore[return-value]


def _scope_key(engine: str, session_id: str) -> str:
    clean_engine = _engine(engine)
    clean_id = _wire_id(session_id)
    assert clean_id is not None
    return f"{clean_engine}\0{clean_id}"


def _legacy_scope_key(session_id: str) -> str:
    clean_id = _wire_id(session_id)
    assert clean_id is not None
    return f"{_LEGACY_ENGINE}\0{clean_id}"


def _split_persisted_scope_key(key: str) -> tuple[str, str]:
    try:
        engine, session_id = key.split("\0", 1)
    except ValueError as exc:
        raise SessionPresentationStoreError(
            "session presentation scope is invalid") from exc
    clean_id = _wire_id(session_id)
    assert clean_id is not None
    if engine == _LEGACY_ENGINE:
        return engine, clean_id
    return _engine(engine), clean_id


class SessionPresentationStoreError(RuntimeError):
    """The Remote-owned presentation receipt store is unsafe or malformed."""


@dataclass(frozen=True)
class SessionPresentationSnapshot:
    completion_id: str | None = None
    completion_unread: bool = False
    completion_revision: int = 0
    dismissed_goal_id: str | None = None
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "completion_id": self.completion_id,
            "completion_unread": self.completion_unread,
            "completion_revision": self.completion_revision,
            "dismissed_goal_id": self.dismissed_goal_id,
            "updated_at": self.updated_at,
        }


def _wire_id(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _WIRE_ID.fullmatch(value):
        raise SessionPresentationStoreError("session presentation id is invalid")
    return value


def _snapshot(value: object) -> SessionPresentationSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "completion_id",
        "completion_unread",
        "completion_revision",
        "dismissed_goal_id",
        "updated_at",
    }:
        raise SessionPresentationStoreError(
            "session presentation snapshot has an invalid shape"
        )
    revision = value.get("completion_revision")
    updated_at = value.get("updated_at")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise SessionPresentationStoreError(
            "session presentation revision is invalid"
        )
    if (
        isinstance(updated_at, bool)
        or not isinstance(updated_at, (int, float))
        or updated_at < 0
    ):
        raise SessionPresentationStoreError(
            "session presentation timestamp is invalid"
        )
    unread = value.get("completion_unread")
    if not isinstance(unread, bool):
        raise SessionPresentationStoreError(
            "session completion receipt is invalid"
        )
    completion_id = _wire_id(value.get("completion_id"), optional=True)
    dismissed_goal_id = _wire_id(
        value.get("dismissed_goal_id"), optional=True
    )
    if unread and completion_id is None:
        raise SessionPresentationStoreError(
            "an unread completion requires an identity"
        )
    return SessionPresentationSnapshot(
        completion_id=completion_id,
        completion_unread=unread,
        completion_revision=revision,
        dismissed_goal_id=dismissed_goal_id,
        updated_at=float(updated_at),
    )


class SessionPresentationStore:
    """Atomic, bounded completion and Goal presentation receipts."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "session-presentation.json"
        self._lock = threading.RLock()
        self._sessions, self._profile_revisions = self._load()
        self._profile_revision = self._profile_revisions["codex"]

    def get(
        self, engine: str, session_id: str,
    ) -> SessionPresentationSnapshot:
        key = _scope_key(engine, session_id)
        with self._lock:
            snapshot = self._sessions.get(key)
            if snapshot is None:
                return SessionPresentationSnapshot()
            self._sessions.move_to_end(key)
            return snapshot

    def completion_engine(
        self, session_id: str, completion_id: str,
    ) -> Literal["claude", "codex", "dsh"] | None:
        """Resolve an engine-less legacy acknowledgement without guessing.

        Protocol v34 predates engine-scoped completion commands. A cold
        acknowledgement can still be routed safely because it echoes the exact
        completion identity. If both engine scopes somehow contain that same
        identity, fail closed and let a later resident acknowledgement resolve
        it instead of clearing the wrong receipt.
        """
        completion_id = _wire_id(completion_id)  # type: ignore[assignment]
        assert completion_id is not None
        matches: list[Literal["claude", "codex", "dsh"]] = []
        with self._lock:
            for engine in ("claude", "codex", "dsh"):
                snapshot = self._sessions.get(_scope_key(engine, session_id))
                if (
                    snapshot is not None
                    and snapshot.completion_id == completion_id
                ):
                    matches.append(engine)
        return matches[0] if len(matches) == 1 else None

    def legacy_ids(self) -> frozenset[str]:
        """Return ambiguous v1 ids still waiting for native ownership proof."""
        with self._lock:
            return frozenset(
                session_id
                for key in self._sessions
                for engine, session_id in [_split_persisted_scope_key(key)]
                if engine == _LEGACY_ENGINE
            )

    def claim_legacy(
        self,
        engine: str,
        session_id: str,
        target_session_id: str | None = None,
    ) -> SessionPresentationSnapshot | None:
        """Move one quarantined v1 receipt after its engine is proven.

        Discovery is deliberately external to this store: callers must first
        establish that exactly one native engine owns the id.  If a newer
        engine-scoped receipt already exists, keep that generation and retire
        the older ambiguous projection instead of overwriting it.
        """
        # Validate the source independently; otherwise a caller-supplied target
        # could bypass the persisted legacy-key identity check.
        session_id = str(_wire_id(session_id))
        target_id = session_id if target_session_id is None else target_session_id
        target = _scope_key(engine, target_id)
        legacy = _legacy_scope_key(session_id)
        with self._lock:
            snapshot = self._sessions.get(legacy)
            if snapshot is None:
                return self._sessions.get(target)
            updated = OrderedDict(self._sessions)
            updated.pop(legacy, None)
            current = updated.get(target)
            if current is None or snapshot.updated_at > current.updated_at:
                updated.pop(target, None)
                updated[target] = snapshot
                claimed = snapshot
            else:
                claimed = current
            self._persist_bounded(updated)
            self._sessions = updated
            return claimed

    def mark_completion(
        self,
        engine: str,
        session_id: str,
        completion_id: str | None = None,
    ) -> SessionPresentationSnapshot:
        key = _scope_key(engine, session_id)
        completion_id = _wire_id(
            completion_id or f"completion-{uuid4().hex}"
        )
        assert session_id is not None and completion_id is not None
        with self._lock:
            current = self._sessions.get(
                key, SessionPresentationSnapshot(),
            )
            if (
                current.completion_id == completion_id
                and current.completion_unread
            ):
                return current
            return self._replace_locked(
                key,
                replace(
                    current,
                    completion_id=completion_id,
                    completion_unread=True,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def acknowledge_completion(
        self,
        engine: str,
        session_id: str,
        completion_id: str,
    ) -> SessionPresentationSnapshot:
        key = _scope_key(engine, session_id)
        completion_id = _wire_id(completion_id)  # type: ignore[assignment]
        assert completion_id is not None
        with self._lock:
            current = self._sessions.get(
                key, SessionPresentationSnapshot(),
            )
            # A delayed acknowledgement for turn N must never clear the unread
            # receipt for a newer turn N+1.
            if (
                current.completion_id != completion_id
                or not current.completion_unread
            ):
                return current
            return self._replace_locked(
                key,
                replace(
                    current,
                    completion_unread=False,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def clear_completion(
        self,
        engine: str,
        session_id: str,
    ) -> SessionPresentationSnapshot:
        key = _scope_key(engine, session_id)
        with self._lock:
            current = self._sessions.get(
                key, SessionPresentationSnapshot(),
            )
            if current.completion_id is None and not current.completion_unread:
                return current
            return self._replace_locked(
                key,
                replace(
                    current,
                    completion_id=None,
                    completion_unread=False,
                    completion_revision=current.completion_revision + 1,
                    updated_at=time.time(),
                ),
            )

    def dismiss_goal(
        self,
        engine: str,
        session_id: str,
        goal_id: str,
    ) -> SessionPresentationSnapshot:
        key = _scope_key(engine, session_id)
        goal_id = _wire_id(goal_id)  # type: ignore[assignment]
        assert goal_id is not None
        with self._lock:
            current = self._sessions.get(
                key, SessionPresentationSnapshot(),
            )
            if current.dismissed_goal_id == goal_id:
                return current
            return self._replace_locked(
                key,
                replace(
                    current,
                    dismissed_goal_id=goal_id,
                    updated_at=time.time(),
                ),
            )

    def reconcile_goal(
        self, engine: str, session_id: str, goal_id: str | None,
    ) -> bool:
        """Return whether *goal_id* is hidden, clearing stale generations."""
        key = _scope_key(engine, session_id)
        goal_id = _wire_id(goal_id, optional=True)  # type: ignore[assignment]
        with self._lock:
            current = self._sessions.get(
                key, SessionPresentationSnapshot(),
            )
            dismissed = current.dismissed_goal_id
            if dismissed is not None and dismissed != goal_id:
                current = self._replace_locked(
                    key,
                    replace(
                        current,
                        dismissed_goal_id=None,
                        updated_at=time.time(),
                    ),
                )
            return goal_id is not None and current.dismissed_goal_id == goal_id

    def move(
        self, engine: str, old_session_id: str, session_id: str,
    ) -> None:
        old_session_id = _wire_id(old_session_id)  # type: ignore[assignment]
        session_id = _wire_id(session_id)  # type: ignore[assignment]
        assert old_session_id is not None and session_id is not None
        if old_session_id == session_id:
            return
        old_key = _scope_key(engine, old_session_id)
        key = _scope_key(engine, session_id)
        with self._lock:
            snapshot = self._sessions.get(old_key)
            if snapshot is None:
                return
            updated = OrderedDict(self._sessions)
            updated.pop(old_key, None)
            target = updated.get(key)
            if target is None or snapshot.updated_at >= target.updated_at:
                updated.pop(key, None)
                updated[key] = snapshot
            self._persist_bounded(updated)
            self._sessions = updated

    def delete(self, engine: str, session_id: str) -> None:
        key = _scope_key(engine, session_id)
        with self._lock:
            if key not in self._sessions:
                return
            updated = OrderedDict(self._sessions)
            updated.pop(key, None)
            self._persist_bounded(updated)
            self._sessions = updated

    def _replace_locked(
        self,
        key: str,
        snapshot: SessionPresentationSnapshot,
    ) -> SessionPresentationSnapshot:
        updated = OrderedDict(self._sessions)
        updated.pop(key, None)
        updated[key] = snapshot
        while len(updated) > _MAX_ENTRIES:
            updated.popitem(last=False)
        self._persist_bounded(updated)
        self._sessions = updated
        return snapshot

    def migrate_codex_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Translate only explicitly Codex-owned presentation scopes."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise SessionPresentationStoreError(
                "invalid Codex profile revision")
        with self._lock:
            if self._profile_revisions["codex"] >= profile_revision:
                return 0
            updated: OrderedDict[str, SessionPresentationSnapshot] = (
                OrderedDict()
            )
            migrated = 0
            for key, snapshot in self._sessions.items():
                engine, session_id = _split_persisted_scope_key(key)
                target_id = (
                    str(_wire_id(transform(session_id)))
                    if engine == "codex" else session_id
                )
                target = (
                    _legacy_scope_key(target_id)
                    if engine == _LEGACY_ENGINE
                    else _scope_key(engine, target_id)
                )
                existing = updated.get(target)
                if existing is not None and existing != snapshot:
                    raise SessionPresentationStoreError(
                        "presentation profile migration collides")
                updated[target] = snapshot
                migrated += target != key
            revisions = dict(self._profile_revisions)
            revisions["codex"] = profile_revision
            self._persist_bounded(
                updated,
                profile_revision=profile_revision,
                profile_revisions=revisions,
            )
            self._sessions = updated
            self._profile_revision = profile_revision
            self._profile_revisions = revisions
            return migrated

    def migrate_claude_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Translate only explicitly Claude-owned presentation scopes."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise SessionPresentationStoreError(
                "invalid Claude profile revision")
        with self._lock:
            if self._profile_revisions["claude"] >= profile_revision:
                return 0
            updated: OrderedDict[str, SessionPresentationSnapshot] = (
                OrderedDict()
            )
            migrated = 0
            for key, snapshot in self._sessions.items():
                engine, session_id = _split_persisted_scope_key(key)
                target_id = (
                    str(_wire_id(transform(session_id)))
                    if engine == "claude" else session_id
                )
                target = (
                    _legacy_scope_key(target_id)
                    if engine == _LEGACY_ENGINE
                    else _scope_key(engine, target_id)
                )
                existing = updated.get(target)
                if existing is not None and existing != snapshot:
                    raise SessionPresentationStoreError(
                        "presentation profile migration collides")
                updated[target] = snapshot
                migrated += target != key
            revisions = dict(self._profile_revisions)
            revisions["claude"] = profile_revision
            self._persist_bounded(updated, profile_revisions=revisions)
            self._sessions = updated
            self._profile_revisions = revisions
            return migrated

    def _load(self) -> tuple[
        OrderedDict[str, SessionPresentationSnapshot],
        dict[str, int],
    ]:
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError(
                    "session presentation store is not a bounded regular file"
                )
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("session presentation store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) not in (
                {"version", "sessions"},
                {"version", "profile_revision", "sessions"},
                {
                    "version", "profile_revision", "profile_revisions",
                    "sessions",
                },
            ):
                raise ValueError(
                    "session presentation store has an invalid envelope"
                )
            if raw.get("version") not in {1, 2, 3, 4} or not isinstance(
                raw.get("sessions"), dict
            ):
                raise ValueError(
                    "session presentation store version is unsupported"
                )
            profile_revision = raw.get("profile_revision", 0)
            if (
                isinstance(profile_revision, bool)
                or not isinstance(profile_revision, int)
                or profile_revision < 0
                or (raw.get("version") == 1 and profile_revision != 0)
            ):
                raise ValueError(
                    "session presentation profile revision is invalid")
            profile_revisions = raw.get("profile_revisions")
            if profile_revisions is None:
                profile_revisions = {
                    "claude": 0,
                    "codex": profile_revision,
                }
            if (
                not isinstance(profile_revisions, dict)
                or set(profile_revisions) != {"claude", "codex"}
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in profile_revisions.values()
                )
            ):
                raise ValueError(
                    "session presentation profile revisions are invalid")
            if profile_revisions["codex"] != profile_revision:
                raise ValueError(
                    "session presentation Codex profile revisions are inconsistent"
                )
            loaded: OrderedDict[
                str, SessionPresentationSnapshot
            ] = OrderedDict()
            for session_id, value in raw["sessions"].items():
                snapshot = _snapshot(value)
                if raw.get("version") == 1:
                    clean_id = _wire_id(session_id)
                    assert clean_id is not None
                    # v1 did not record the engine. The old multi-account wire
                    # form has a provable Codex owner; retain every ambiguous
                    # bare id in quarantine until the native stores prove a
                    # unique owner. Dropping it here permanently loses unread
                    # completion and dismissed Goal state.
                    if "@" in clean_id:
                        loaded[_scope_key("codex", clean_id)] = snapshot
                    else:
                        loaded[_legacy_scope_key(clean_id)] = snapshot
                else:
                    engine, _clean_id = _split_persisted_scope_key(session_id)
                    if raw.get("version") == 2 and engine == _LEGACY_ENGINE:
                        raise ValueError(
                            "v2 session presentation scope cannot be legacy")
                    loaded[session_id] = snapshot
            if len(loaded) > _MAX_ENTRIES:
                raise ValueError("session presentation store has too many entries")
            return loaded, dict(profile_revisions)
        except FileNotFoundError:
            return OrderedDict(), {"claude": 0, "codex": 0}
        except Exception as exc:
            raise SessionPresentationStoreError(
                "session presentation store is unreadable"
            ) from exc

    @staticmethod
    def _payload(
        sessions: OrderedDict[str, SessionPresentationSnapshot],
        *,
        profile_revision: int,
        profile_revisions: dict[str, int],
    ) -> bytes:
        return json.dumps(
            {
                "version": 4,
                "profile_revision": profile_revision,
                "profile_revisions": profile_revisions,
                "sessions": {
                    session_id: snapshot.as_dict()
                    for session_id, snapshot in sessions.items()
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _persist_bounded(
        self,
        sessions: OrderedDict[str, SessionPresentationSnapshot],
        *,
        profile_revision: int | None = None,
        profile_revisions: dict[str, int] | None = None,
    ) -> None:
        bounded = OrderedDict(sessions)
        revision = (
            self._profile_revision
            if profile_revision is None else profile_revision
        )
        revisions = (
            self._profile_revisions
            if profile_revisions is None else profile_revisions
        )
        payload = self._payload(
            bounded,
            profile_revision=revision,
            profile_revisions=revisions,
        )
        while len(payload) > _MAX_FILE_BYTES and len(bounded) > 1:
            bounded.popitem(last=False)
            payload = self._payload(
                bounded,
                profile_revision=revision,
                profile_revisions=revisions,
            )
        if len(payload) > _MAX_FILE_BYTES:
            raise SessionPresentationStoreError(
                "session presentation store exceeds size limit"
            )
        sessions.clear()
        sessions.update(bounded)
        self._persist(payload)

    def _persist(self, payload: bytes) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
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
            raise SessionPresentationStoreError(
                "session presentation store could not be persisted"
            ) from exc
