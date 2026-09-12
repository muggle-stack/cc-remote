"""Private per-thread Codex controls that app-server does not persist."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Callable
from uuid import uuid4


CODEX_APPROVAL_POLICIES = frozenset({"untrusted", "on-request", "never"})
CODEX_WEB_SEARCH_MODES = frozenset({"cached", "live"})
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 1024 * 1024


class CodexControlStoreError(RuntimeError):
    """The Remote-owned Codex control store is unsafe or malformed."""


@dataclass(frozen=True)
class CodexControls:
    approval_policy: str | None = None
    permission_profile: str | None = None
    web_search: str | None = None
    cwd_override: str | None = None
    context_max_tokens: int | None = None
    context_window_tokens: int | None = None
    context_settings_set: bool = False

    def as_dict(self) -> dict[str, object]:
        result = {}
        if self.approval_policy in CODEX_APPROVAL_POLICIES:
            result["approval_policy"] = self.approval_policy
        if _permission_profile(self.permission_profile) is not None:
            result["permission_profile"] = self.permission_profile
        if self.web_search in CODEX_WEB_SEARCH_MODES:
            result["web_search"] = self.web_search
        if _cwd_override(self.cwd_override) is not None:
            result["cwd_override"] = self.cwd_override
        if _token_count(self.context_max_tokens) and _token_count(self.context_window_tokens):
            result["context_max_tokens"] = self.context_max_tokens
            result["context_window_tokens"] = self.context_window_tokens
        if self.context_settings_set:
            result["context_settings_set"] = True
        return result


def _session_id(value: object) -> str:
    if not isinstance(value, str) or not _SESSION_ID.fullmatch(value):
        raise CodexControlStoreError("Codex session id is invalid")
    return value


def _permission_profile(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _cwd_override(value: object) -> str | None:
    if (not isinstance(value, str) or not os.path.isabs(value)
            or not 0 < len(value) <= 4096):
        return None
    return value


def _token_count(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= 100_000_000 else None


def _controls(values: object) -> CodexControls:
    raw = values if isinstance(values, dict) else {}
    return CodexControls(
        approval_policy=(
            raw.get("approval_policy")
            if raw.get("approval_policy") in CODEX_APPROVAL_POLICIES
            else None
        ),
        permission_profile=_permission_profile(
            raw.get("permission_profile")),
        web_search=(
            raw.get("web_search")
            if raw.get("web_search") in CODEX_WEB_SEARCH_MODES
            else None
        ),
        cwd_override=_cwd_override(raw.get("cwd_override")),
        # Preserve the number entered in pre-v60 controls as the requested
        # usable capacity. Old native window/threshold arithmetic is revalidated.
        context_max_tokens=_token_count(raw.get("context_max_tokens")
                                       if "context_max_tokens" in raw
                                       else raw.get("context_threshold_tokens")),
        context_window_tokens=_token_count(raw.get("context_window_tokens")),
        context_settings_set=raw.get("context_settings_set") is True,
    )


class CodexControlStore:
    """Atomic, bounded Remote preferences that survive app-server restarts."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "codex-session-controls.json"
        self._lock = threading.RLock()
        self._profile_revision = 0
        self._sessions = self._load()

    def get(self, session_id: str) -> CodexControls:
        session_id = _session_id(session_id)
        with self._lock:
            raw = dict(self._sessions.get(session_id, {}))
        return _controls(raw)

    def cwd_overrides(self) -> dict[str, str]:
        """Return a stable in-memory projection for sidebar catalog overlay."""
        with self._lock:
            return {
                session_id: cwd
                for session_id, values in self._sessions.items()
                if (cwd := _controls(values).cwd_override) is not None
            }

    def namespace_legacy_sessions(self, profile_id: str) -> int:
        """Move pre-multi-account native keys into the default namespace."""
        if not isinstance(profile_id, str) or not profile_id:
            raise CodexControlStoreError("Codex profile id is invalid")
        with self._lock:
            updated = dict(self._sessions)
            migrated = 0
            for session_id in tuple(self._sessions):
                if "@" in session_id:
                    continue
                target = _session_id(f"{profile_id}@{session_id}")
                if target not in updated:
                    updated[target] = updated[session_id]
                updated.pop(session_id, None)
                migrated += 1
            if migrated:
                self._persist(updated)
                self._sessions = updated
            return migrated

    def denamespace_profile_sessions(self, profile_id: str) -> int:
        """Expose one active profile through the legacy single-account keys.

        Other profile-prefixed rows stay dormant so a temporary configuration
        downgrade cannot erase their controls.
        """
        if not isinstance(profile_id, str) or not profile_id or "@" in profile_id:
            raise CodexControlStoreError("Codex profile id is invalid")
        prefix = f"{profile_id}@"
        with self._lock:
            updated = dict(self._sessions)
            migrated = 0
            for session_id in tuple(self._sessions):
                if not session_id.startswith(prefix):
                    continue
                native_id = _session_id(session_id[len(prefix):])
                updated[native_id] = updated[session_id]
                updated.pop(session_id, None)
                migrated += 1
            if migrated:
                self._persist(updated)
                self._sessions = updated
            return migrated

    def remap_profile_sessions(self, remaps: dict[str, str]) -> int:
        """Rename persisted profile prefixes after a same-home id rename."""
        with self._lock:
            updated = dict(self._sessions)
            moves: list[tuple[str, str, dict[str, object]]] = []
            for session_id in tuple(self._sessions):
                if "@" not in session_id:
                    continue
                old_id, native_id = session_id.split("@", 1)
                new_id = remaps.get(old_id)
                if not new_id or new_id == old_id:
                    continue
                target = _session_id(f"{new_id}@{native_id}")
                moves.append((session_id, target, updated[session_id]))
            sources = {source for source, _target, _value in moves}
            for source in sources:
                updated.pop(source, None)
            for _source, target, value in moves:
                if target not in updated or target in sources:
                    updated[target] = value
            migrated = len(moves)
            if migrated:
                self._persist(updated)
                self._sessions = updated
            return migrated

    def migrate_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate every wire id once for a topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise CodexControlStoreError(
                "Codex profile revision is invalid")
        with self._lock:
            if self._profile_revision >= profile_revision:
                return 0
            updated: dict[str, dict[str, str]] = {}
            migrated = 0
            for session_id, values in self._sessions.items():
                target = _session_id(transform(session_id))
                if target in updated and updated[target] != values:
                    raise CodexControlStoreError(
                        "Codex profile migration collides")
                updated[target] = values
                migrated += target != session_id
            self._persist(updated, profile_revision=profile_revision)
            self._sessions = updated
            self._profile_revision = profile_revision
            return migrated

    def update(
        self,
        session_id: str,
        *,
        approval_policy: str | None,
        permission_profile: str | None,
        web_search: str | None,
    ) -> CodexControls:
        session_id = _session_id(session_id)
        with self._lock:
            existing = _controls(self._sessions.get(session_id))
            controls = CodexControls(
                approval_policy=(
                    approval_policy
                    if approval_policy in CODEX_APPROVAL_POLICIES else None
                ),
                permission_profile=_permission_profile(permission_profile),
                web_search=(
                    web_search if web_search in CODEX_WEB_SEARCH_MODES else None
                ),
                # Runtime control changes must not clear an explicit cwd
                # migration that is waiting for its next durable native turn.
                cwd_override=existing.cwd_override,
                context_max_tokens=existing.context_max_tokens,
                context_window_tokens=existing.context_window_tokens,
                context_settings_set=existing.context_settings_set,
            )
            payload = controls.as_dict()
            updated = dict(self._sessions)
            updated.pop(session_id, None)
            if payload:
                updated[session_id] = payload
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def inherit_if_absent(
        self,
        session_id: str,
        *,
        approval_policy: str | None,
        permission_profile: str | None,
        web_search: str | None,
    ) -> CodexControls:
        """Seed a new fork once without overwriting later child choices."""
        session_id = _session_id(session_id)
        controls = CodexControls(
            approval_policy=(
                approval_policy
                if approval_policy in CODEX_APPROVAL_POLICIES else None
            ),
            permission_profile=_permission_profile(permission_profile),
            web_search=(
                web_search if web_search in CODEX_WEB_SEARCH_MODES else None
            ),
        )
        payload = controls.as_dict()
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return _controls(existing)
            if not payload:
                return controls
            updated = dict(self._sessions)
            updated[session_id] = payload
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def set_context(self, session_id: str, max_tokens: int | None, window: int | None) -> CodexControls:
        session_id = _session_id(session_id)
        if max_tokens is not None and (
            _token_count(max_tokens) is None or _token_count(window) is None or max_tokens > window
        ):
            raise CodexControlStoreError("Codex context preference is invalid")
        with self._lock:
            existing = _controls(self._sessions.get(session_id))
            controls = replace(existing, context_max_tokens=max_tokens,
                               context_window_tokens=window if max_tokens is not None else None,
                               context_settings_set=True)
            updated = dict(self._sessions)
            updated[session_id] = controls.as_dict()
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def delete(self, session_id: str) -> None:
        session_id = _session_id(session_id)
        with self._lock:
            if session_id not in self._sessions:
                return
            updated = dict(self._sessions)
            updated.pop(session_id, None)
            self._persist(updated)
            self._sessions = updated

    def set_cwd_override(
        self, session_id: str, cwd_override: str | None,
    ) -> CodexControls:
        """Persist only the Remote-owned cwd while preserving other controls."""
        session_id = _session_id(session_id)
        if cwd_override is not None and _cwd_override(cwd_override) is None:
            raise CodexControlStoreError("Codex cwd override is invalid")
        with self._lock:
            existing = _controls(self._sessions.get(session_id))
            controls = CodexControls(
                approval_policy=existing.approval_policy,
                permission_profile=existing.permission_profile,
                web_search=existing.web_search,
                cwd_override=cwd_override,
                context_max_tokens=existing.context_max_tokens,
                context_window_tokens=existing.context_window_tokens,
                context_settings_set=existing.context_settings_set,
            )
            payload = controls.as_dict()
            updated = dict(self._sessions)
            updated.pop(session_id, None)
            if payload:
                updated[session_id] = payload
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def clear_cwd_override_if_matches(
        self, session_id: str, expected: str,
    ) -> CodexControls:
        """Clear one stale cwd without overwriting a concurrent migration."""
        session_id = _session_id(session_id)
        if _cwd_override(expected) is None:
            raise CodexControlStoreError("expected Codex cwd override is invalid")
        with self._lock:
            existing = _controls(self._sessions.get(session_id))
            if existing.cwd_override != expected:
                return existing
            controls = CodexControls(
                approval_policy=existing.approval_policy,
                permission_profile=existing.permission_profile,
                web_search=existing.web_search,
                cwd_override=None,
                context_max_tokens=existing.context_max_tokens,
                context_window_tokens=existing.context_window_tokens,
                context_settings_set=existing.context_settings_set,
            )
            payload = controls.as_dict()
            updated = dict(self._sessions)
            updated.pop(session_id, None)
            if payload:
                updated[session_id] = payload
            self._persist(updated)
            self._sessions = updated
        return controls

    def restore_cwd_override_after_failed_set(
        self,
        session_id: str,
        attempted: str,
        previous: str | None,
    ) -> CodexControls:
        """Undo an uncertain write only when disk still has its attempted cwd."""
        session_id = _session_id(session_id)
        if _cwd_override(attempted) is None:
            raise CodexControlStoreError(
                "attempted Codex cwd override is invalid")
        if previous is not None and _cwd_override(previous) is None:
            raise CodexControlStoreError(
                "previous Codex cwd override is invalid")
        with self._lock:
            # _persist() can raise after os.replace() committed the new file.
            # Re-read disk instead of trusting the deliberately not-yet-updated
            # in-memory projection, then compare before restoring.
            durable = self._load()
            self._sessions = durable
            existing = _controls(durable.get(session_id))
            if existing.cwd_override != attempted:
                return existing
            controls = CodexControls(
                approval_policy=existing.approval_policy,
                permission_profile=existing.permission_profile,
                web_search=existing.web_search,
                cwd_override=previous,
                context_max_tokens=existing.context_max_tokens,
                context_window_tokens=existing.context_window_tokens,
                context_settings_set=existing.context_settings_set,
            )
            payload = controls.as_dict()
            updated = dict(durable)
            updated.pop(session_id, None)
            if payload:
                updated[session_id] = payload
            try:
                self._persist(updated)
            except Exception:
                # A second post-replace failure is uncertain for the same
                # reason. Keep catalog overlays aligned with what is currently
                # readable on disk before propagating the failure.
                try:
                    self._sessions = self._load()
                except Exception:
                    pass
                raise
            self._sessions = updated
        return controls

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _MAX_FILE_BYTES):
            raise CodexControlStoreError(
                "Codex control store is not a private bounded file")
        try:
            raw = json.loads(self.path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexControlStoreError(
                "Codex control store is unreadable") from exc
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if (not isinstance(raw, dict) or raw.get("version") != 1
                or not isinstance(sessions, dict)
                or len(sessions) > _MAX_ENTRIES):
            raise CodexControlStoreError(
                "Codex control store has invalid shape")
        profile_revision = raw.get("profile_revision", 0)
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 0
        ):
            raise CodexControlStoreError(
                "Codex control store has invalid profile revision")
        loaded: dict[str, dict[str, str]] = {}
        for raw_id, values in sessions.items():
            if not isinstance(values, dict):
                continue
            try:
                session_id = _session_id(raw_id)
            except CodexControlStoreError:
                continue
            controls = _controls(values)
            if controls.as_dict():
                loaded[session_id] = controls.as_dict()
        self._profile_revision = profile_revision
        return loaded

    def _persist(
        self,
        sessions: dict[str, dict[str, str]],
        *,
        profile_revision: int | None = None,
    ) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        payload = json.dumps(
            {
                "version": 1,
                "profile_revision": (
                    self._profile_revision
                    if profile_revision is None else profile_revision
                ),
                "sessions": sessions,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise CodexControlStoreError(
                "Codex control store exceeds size limit")
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise CodexControlStoreError(
                "Codex control store could not be persisted") from exc
