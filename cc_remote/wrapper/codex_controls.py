"""Private per-thread Codex controls that app-server does not persist."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
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

    def as_dict(self) -> dict[str, str]:
        result = {}
        if self.approval_policy in CODEX_APPROVAL_POLICIES:
            result["approval_policy"] = self.approval_policy
        if _permission_profile(self.permission_profile) is not None:
            result["permission_profile"] = self.permission_profile
        if self.web_search in CODEX_WEB_SEARCH_MODES:
            result["web_search"] = self.web_search
        if _cwd_override(self.cwd_override) is not None:
            result["cwd_override"] = self.cwd_override
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
    )


class CodexControlStore:
    """Atomic, bounded Remote preferences that survive app-server restarts."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "codex-session-controls.json"
        self._lock = threading.RLock()
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
        return loaded

    def _persist(self, sessions: dict[str, dict[str, str]]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        payload = json.dumps(
            {"version": 1, "sessions": sessions},
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
