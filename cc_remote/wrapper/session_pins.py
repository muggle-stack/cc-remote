"""Small durable store for cc-remote sidebar pins.

Pinning is a product preference shared by Claude and Codex, not native engine
metadata. Keep it in the wrapper state directory so every remote client sees
the same order without modifying provider-owned transcripts or databases.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4


Engine = Literal["claude", "codex", "dsh"]

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 1024 * 1024


class SessionPinStoreError(RuntimeError):
    """The pin preference file could not be read or persisted safely."""


class SessionPinStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "session-pins.json"
        self._lock = threading.RLock()
        self._profile_revision = 0
        self._profile_revisions = {"claude": 0, "codex": 0}
        self._pins = self._load()

    def ids(self, engine: Engine) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pins[engine])

    def set_pinned(self, engine: Engine, session_id: str, pinned: bool) -> None:
        self._validate_identity(engine, session_id)
        with self._lock:
            updated = {name: set(values) for name, values in self._pins.items()}
            if pinned:
                updated[engine].add(session_id)
            else:
                updated[engine].discard(session_id)
            if updated == self._pins:
                return
            if sum(len(values) for values in updated.values()) > _MAX_ENTRIES:
                raise SessionPinStoreError("session pin limit reached")
            self._persist(updated)
            self._pins = updated

    def namespace_legacy_codex_sessions(self, profile_id: str) -> int:
        """Move pre-multi-account Codex pins into the default namespace."""
        if not isinstance(profile_id, str) or not profile_id or "@" in profile_id:
            raise SessionPinStoreError("invalid Codex profile id")
        with self._lock:
            old = self._pins["codex"]
            migrated = {sid for sid in old if "@" not in sid}
            if not migrated:
                return 0
            updated = {name: set(values) for name, values in self._pins.items()}
            updated["codex"].difference_update(migrated)
            updated["codex"].update(
                f"{profile_id}@{session_id}" for session_id in migrated)
            for session_id in updated["codex"]:
                self._validate_identity("codex", session_id)
            self._persist(updated)
            self._pins = updated
            return len(migrated)

    def denamespace_codex_profile_sessions(self, profile_id: str) -> int:
        """Make the active profile native-keyed while retaining dormant pins."""
        if not isinstance(profile_id, str) or not profile_id or "@" in profile_id:
            raise SessionPinStoreError("invalid Codex profile id")
        prefix = f"{profile_id}@"
        with self._lock:
            migrated = {
                sid for sid in self._pins["codex"] if sid.startswith(prefix)
            }
            if not migrated:
                return 0
            updated = {name: set(values) for name, values in self._pins.items()}
            updated["codex"].difference_update(migrated)
            updated["codex"].update(sid[len(prefix):] for sid in migrated)
            for session_id in updated["codex"]:
                self._validate_identity("codex", session_id)
            self._persist(updated)
            self._pins = updated
            return len(migrated)

    def remap_codex_profile_sessions(self, remaps: dict[str, str]) -> int:
        with self._lock:
            updated = {name: set(values) for name, values in self._pins.items()}
            migrated = 0
            remapped: set[str] = set()
            for session_id in self._pins["codex"]:
                if "@" not in session_id:
                    remapped.add(session_id)
                    continue
                old_id, native_id = session_id.split("@", 1)
                new_id = remaps.get(old_id)
                if not new_id or new_id == old_id:
                    remapped.add(session_id)
                    continue
                target = f"{new_id}@{native_id}"
                self._validate_identity("codex", target)
                remapped.add(target)
                migrated += 1
            if migrated:
                updated["codex"] = remapped
                self._persist(updated)
                self._pins = updated
            return migrated

    def migrate_codex_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate Codex pins once per topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise SessionPinStoreError("invalid Codex profile revision")
        with self._lock:
            if self._profile_revisions["codex"] >= profile_revision:
                return 0
            remapped: set[str] = set()
            migrated = 0
            for session_id in self._pins["codex"]:
                target = transform(session_id)
                self._validate_identity("codex", target)
                remapped.add(target)
                migrated += target != session_id
            updated = {name: set(values) for name, values in self._pins.items()}
            updated["codex"] = remapped
            revisions = dict(self._profile_revisions)
            revisions["codex"] = profile_revision
            self._persist(
                updated,
                profile_revision=profile_revision,
                profile_revisions=revisions,
            )
            self._pins = updated
            self._profile_revision = profile_revision
            self._profile_revisions = revisions
            return migrated

    def migrate_claude_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate Claude pins once per topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise SessionPinStoreError("invalid Claude profile revision")
        with self._lock:
            if self._profile_revisions["claude"] >= profile_revision:
                return 0
            remapped: set[str] = set()
            migrated = 0
            for session_id in self._pins["claude"]:
                target = transform(session_id)
                self._validate_identity("claude", target)
                remapped.add(target)
                migrated += target != session_id
            updated = {name: set(values) for name, values in self._pins.items()}
            updated["claude"] = remapped
            revisions = dict(self._profile_revisions)
            revisions["claude"] = profile_revision
            self._persist(updated, profile_revisions=revisions)
            self._pins = updated
            self._profile_revisions = revisions
            return migrated

    @staticmethod
    def _validate_identity(engine: object, session_id: object) -> None:
        if engine not in {"claude", "codex", "dsh"}:
            raise SessionPinStoreError("invalid session pin engine")
        if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise SessionPinStoreError("invalid pinned session id")

    def _load(self) -> dict[Engine, set[str]]:
        empty: dict[Engine, set[str]] = {"claude": set(), "codex": set(), "dsh": set()}
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError("session pin store is not a bounded regular file")
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("session pin store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if (
                not isinstance(raw, dict)
                or set(raw) - {
                    "claude", "codex", "dsh", "profile_revision", "profile_revisions"
                }
                or not {"claude", "codex"}.issubset(raw)
            ):
                raise ValueError("session pin store has an invalid shape")
            profile_revision = raw.get("profile_revision", 0)
            if (
                isinstance(profile_revision, bool)
                or not isinstance(profile_revision, int)
                or profile_revision < 0
            ):
                raise ValueError("session pin profile revision is invalid")
            loaded: dict[Engine, set[str]] = {"claude": set(), "codex": set(), "dsh": set()}
            for engine in ("claude", "codex", "dsh"):
                values = raw.get(engine, [])
                if not isinstance(values, list):
                    raise ValueError("session pin list is invalid")
                for session_id in values:
                    self._validate_identity(engine, session_id)
                    loaded[engine].add(session_id)
            if sum(len(values) for values in loaded.values()) > _MAX_ENTRIES:
                raise ValueError("session pin store has too many entries")
            self._profile_revision = profile_revision
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
                raise ValueError("session pin profile revisions are invalid")
            if profile_revisions["codex"] != profile_revision:
                raise ValueError(
                    "session pin Codex profile revisions are inconsistent"
                )
            self._profile_revisions = dict(profile_revisions)
            return loaded
        except FileNotFoundError:
            return empty
        except Exception as exc:
            raise SessionPinStoreError("session pin store is unreadable") from exc

    def _persist(
        self,
        pins: dict[Engine, set[str]],
        *,
        profile_revision: int | None = None,
        profile_revisions: dict[str, int] | None = None,
    ) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = json.dumps({
                "claude": sorted(pins["claude"]),
                "codex": sorted(pins["codex"]),
                **({"dsh": sorted(pins["dsh"])} if pins.get("dsh") else {}),
                "profile_revision": (
                    self._profile_revision
                    if profile_revision is None else profile_revision
                ),
                "profile_revisions": (
                    self._profile_revisions
                    if profile_revisions is None else profile_revisions
                ),
            }, separators=(",", ":")).encode("utf-8")
            if len(payload) > _MAX_FILE_BYTES:
                raise ValueError("session pin store exceeds size limit")
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
            raise SessionPinStoreError("session pin store could not be persisted") from exc
