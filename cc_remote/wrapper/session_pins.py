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
from typing import Literal
from uuid import uuid4

from cc_remote.wrapper.os_compat import fsync_directory

Engine = Literal["claude", "codex"]

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 1024 * 1024


class SessionPinStoreError(RuntimeError):
    """The pin preference file could not be read or persisted safely."""


class SessionPinStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "session-pins.json"
        self._lock = threading.RLock()
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

    @staticmethod
    def _validate_identity(engine: object, session_id: object) -> None:
        if engine not in {"claude", "codex"}:
            raise SessionPinStoreError("invalid session pin engine")
        if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise SessionPinStoreError("invalid pinned session id")

    def _load(self) -> dict[Engine, set[str]]:
        empty: dict[Engine, set[str]] = {"claude": set(), "codex": set()}
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
                raise ValueError("session pin store is not a bounded regular file")
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("session pin store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"claude", "codex"}:
                raise ValueError("session pin store has an invalid shape")
            loaded: dict[Engine, set[str]] = {"claude": set(), "codex": set()}
            for engine in ("claude", "codex"):
                values = raw.get(engine)
                if not isinstance(values, list):
                    raise ValueError("session pin list is invalid")
                for session_id in values:
                    self._validate_identity(engine, session_id)
                    loaded[engine].add(session_id)
            if sum(len(values) for values in loaded.values()) > _MAX_ENTRIES:
                raise ValueError("session pin store has too many entries")
            return loaded
        except FileNotFoundError:
            return empty
        except Exception as exc:
            raise SessionPinStoreError("session pin store is unreadable") from exc

    def _persist(self, pins: dict[Engine, set[str]]) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            payload = json.dumps({
                engine: sorted(pins[engine]) for engine in ("claude", "codex")
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
            fsync_directory(self.path.parent)
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise SessionPinStoreError("session pin store could not be persisted") from exc
