"""Durable, exact-file capabilities for previews outside a session cwd."""
from __future__ import annotations

import math
import os
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
from typing import Literal
from uuid import uuid4

from cc_remote.log import logger

log = logger("cc_remote.wrapper.preview_capabilities")

PreviewMode = Literal["read", "read_write"]
PreviewCapabilitySource = Literal[
    "engine_observed", "structured_write", "user_approved",
]

_GLOBAL_CAP = 4096
_SESSION_CAP = 256
PREVIEW_PATH_MAX_BYTES = 4096
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01
_EPOCH_FILE_MAX_BYTES = 64
_INVALIDATION_FILE_MAX_BYTES = 128


class PreviewCapabilityError(ValueError):
    """A requested external file cannot safely become a preview capability."""


@dataclass(frozen=True)
class PreviewCapability:
    engine: str
    space: str
    session_id: str
    path: str
    device: int
    inode: int
    uid: int
    mode: PreviewMode
    source: PreviewCapabilitySource
    granted_at: float

    def matches(
        self,
        file_stat: os.stat_result,
        *,
        require_write: bool = False,
    ) -> bool:
        return (
            stat.S_ISREG(file_stat.st_mode)
            and file_stat.st_dev == self.device
            and file_stat.st_ino == self.inode
            and getattr(file_stat, "st_uid", -1) == self.uid
            and (not require_write or (
                self.mode == "read_write" and self.uid == os.geteuid()))
        )


CapabilityKey = tuple[str, str, str, str]


class PreviewCapabilityStore:
    """Bounded in-memory lookup with an atomic SQLite durability layer.

    The state is only an allow capability, never source-of-truth file content.
    If the database is unavailable or malformed, the store starts empty and
    therefore fails closed by asking the user again.
    """

    def __init__(self, state_dir: Path):
        state_dir = Path(state_dir)
        self._path = state_dir / "preview-capabilities.sqlite3"
        self._lock_path = state_dir / ".preview-capabilities.lock"
        self._invalidation_path = (
            state_dir / ".preview-capabilities.invalidated"
        )
        self._epoch_path = state_dir / ".preview-capabilities.epoch"
        self._epoch: str | None = None
        self._mutation_lock = threading.RLock()
        self._entries: OrderedDict[
            CapabilityKey, PreviewCapability
        ] = OrderedDict()
        self._persistent = True
        try:
            self._recover_existing_invalidation()
            with self._exclusive_lock():
                if self._invalidation_path.exists():
                    raise RuntimeError(
                        "preview capability invalidation is still active")
                self._ensure_epoch_locked()
                self._initialize()
                self._load()
        except Exception as exc:
            self._persistent = False
            self._entries.clear()
            log.warning(
                "preview capability store unavailable; using fail-closed memory",
                error_type=type(exc).__name__,
            )

    @contextmanager
    def _exclusive_lock(self):
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        descriptor = os.open(
            self._lock_path,
            os.O_RDWR | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_uid", -1) != os.geteuid()
                    or info.st_nlink != 1):
                raise OSError("preview capability lock file is unsafe")
            os.fchmod(descriptor, 0o600)
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(
                        descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            "preview capability lock acquisition timed out")
                    time.sleep(_LOCK_RETRY_SECONDS)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _fsync_parent(self) -> None:
        descriptor = os.open(
            self._path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "preview invalidation ownership timed out")
                time.sleep(_LOCK_RETRY_SECONDS)

    def _marker_matches(
        self, descriptor: int, token: str | None = None,
    ) -> bool:
        try:
            path_info = self._invalidation_path.lstat()
            descriptor_info = os.fstat(descriptor)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or path_info.st_uid != os.geteuid()
            or stat.S_IMODE(path_info.st_mode) & 0o077
            or path_info.st_size > _INVALIDATION_FILE_MAX_BYTES
            or path_info.st_dev != descriptor_info.st_dev
            or path_info.st_ino != descriptor_info.st_ino
        ):
            raise OSError("preview invalidation marker is unsafe")
        raw = os.pread(descriptor, _INVALIDATION_FILE_MAX_BYTES + 1, 0)
        if len(raw) > _INVALIDATION_FILE_MAX_BYTES:
            raise OSError("preview invalidation marker is oversized")
        try:
            value = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise OSError("preview invalidation marker is invalid") from exc
        if token is None and value == "preview capabilities invalidated":
            return True
        if len(value) != 32 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise OSError("preview invalidation marker is invalid")
        return token is None or value == token

    def _recover_existing_invalidation(self) -> None:
        """Recover one abandoned marker without racing its live owner."""
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._invalidation_path, flags)
        except FileNotFoundError:
            return
        try:
            self._lock_descriptor(descriptor)
            with self._exclusive_lock():
                if not self._marker_matches(descriptor):
                    return
                self._recover_invalidation_locked(descriptor)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _create_invalidation_intent(self) -> tuple[int, str]:
        """Publish an already-locked token before the durable store lock."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        while True:
            token = uuid4().hex
            temporary = self._invalidation_path.with_name(
                f".{self._invalidation_path.name}.{os.getpid()}.{token}.tmp"
            )
            descriptor = os.open(
                temporary,
                os.O_RDWR | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            linked = False
            returned = False
            try:
                payload = (token + "\n").encode("ascii")
                if os.write(descriptor, payload) != len(payload):
                    raise OSError("preview invalidation marker write was short")
                os.fsync(descriptor)
                # The inode is locked before the public link exists, so another
                # process can never mistake this live intent for crash residue.
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    os.link(temporary, self._invalidation_path)
                    linked = True
                except FileExistsError:
                    pass
                if linked:
                    temporary.unlink()
                    self._fsync_parent()
                    returned = True
                    return descriptor, token
            finally:
                if not returned:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            self._recover_existing_invalidation()

    @contextmanager
    def _invalidation_intent(self):
        descriptor, token = self._create_invalidation_intent()
        try:
            yield descriptor, token
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _clear_invalidation_locked(
        self, descriptor: int | None = None, token: str | None = None,
    ) -> None:
        if descriptor is not None and not self._marker_matches(descriptor, token):
            raise OSError("preview invalidation ownership changed")
        self._invalidation_path.unlink()
        self._fsync_parent()

    def _write_epoch_locked(self) -> None:
        value = uuid4().hex
        temporary = self._epoch_path.with_name(
            f".{self._epoch_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write((value + "\n").encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._epoch_path)
            os.chmod(self._epoch_path, 0o600)
            self._fsync_parent()
            self._epoch = value
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_epoch(self) -> str:
        descriptor = os.open(
            self._epoch_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_uid", -1) != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_size > _EPOCH_FILE_MAX_BYTES):
                raise ValueError("preview capability epoch file is unsafe")
            raw = os.read(descriptor, _EPOCH_FILE_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _EPOCH_FILE_MAX_BYTES:
            raise ValueError("preview capability epoch exceeds its size limit")
        value = raw.decode("ascii").strip()
        if len(value) != 32 or any(
            ch not in "0123456789abcdef" for ch in value
        ):
            raise ValueError("preview capability epoch is invalid")
        return value

    def _ensure_epoch_locked(self) -> None:
        try:
            value = self._read_epoch()
        except FileNotFoundError:
            self._write_epoch_locked()
            return
        self._epoch = value

    def _observe_epoch(self) -> None:
        """Drop stale in-memory grants changed by another wrapper process."""
        try:
            value = self._read_epoch()
            if self._invalidation_path.exists() or value != self._epoch:
                self._entries = OrderedDict()
                self._epoch = value
        except Exception as exc:
            self._entries = OrderedDict()
            self._persistent = False
            log.warning(
                "preview capability epoch unavailable; clearing memory",
                error_type=type(exc).__name__,
            )

    def _sync_epoch_locked(self) -> bool:
        value = self._read_epoch()
        unchanged = value == self._epoch
        if not unchanged:
            self._entries = OrderedDict()
            self._epoch = value
        return unchanged

    def _recover_invalidation_locked(self, descriptor: int) -> None:
        """Discard every old grant before clearing a crash marker."""
        self._initialize()
        with self._connect() as connection:
            connection.execute("DELETE FROM preview_capabilities")
        self._write_epoch_locked()
        self._clear_invalidation_locked(descriptor)
        self._entries = OrderedDict()

    @staticmethod
    def _key(
        engine: str,
        space: str,
        session_id: str,
        path: str,
    ) -> CapabilityKey:
        return engine, space, session_id, path

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        connection = sqlite3.connect(self._path, timeout=1.0)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            connection.close()
            raise
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS preview_capabilities (
                    engine TEXT NOT NULL,
                    space TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    device TEXT NOT NULL,
                    inode TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    PRIMARY KEY (engine, space, session_id, path)
                )
                """
            )

    @staticmethod
    def _valid_text(value: object, *, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and "\x00" not in value
            and len(value.encode("utf-8", "surrogatepass")) <= maximum
        )

    @classmethod
    def _from_row(cls, row: tuple[object, ...]) -> PreviewCapability | None:
        (
            engine, space, session_id, path, device, inode, uid,
            mode, source, granted_at,
        ) = row
        if (
            engine not in {"claude", "codex", "dsh"}
            or space not in {"code", "work"}
            or not cls._valid_text(session_id, maximum=128)
            or not cls._valid_text(path, maximum=PREVIEW_PATH_MAX_BYTES)
            or mode not in {"read", "read_write"}
            or source not in {
                "engine_observed", "structured_write", "user_approved",
            }
            or not isinstance(granted_at, (int, float))
            or not math.isfinite(float(granted_at))
            or granted_at < 0
        ):
            return None
        try:
            numeric = tuple(int(value) for value in (device, inode, uid))
        except (TypeError, ValueError):
            return None
        if any(value < 0 for value in numeric):
            return None
        return PreviewCapability(
            engine=engine,
            space=space,
            session_id=session_id,
            path=path,
            device=numeric[0],
            inode=numeric[1],
            uid=numeric[2],
            mode=mode,
            source=source,
            granted_at=float(granted_at),
        )

    def _read_entries(self) -> OrderedDict[CapabilityKey, PreviewCapability]:
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM preview_capabilities"
            ).fetchone()[0]
            if count > _GLOBAL_CAP:
                connection.execute(
                    """
                    DELETE FROM preview_capabilities
                    WHERE rowid IN (
                        SELECT rowid
                        FROM preview_capabilities
                        ORDER BY granted_at ASC
                        LIMIT ?
                    )
                    """,
                    (count - _GLOBAL_CAP,),
                )
            rows = connection.execute(
                """
                SELECT engine, space, session_id, path, device, inode, uid,
                       mode, source, granted_at
                FROM preview_capabilities
                ORDER BY granted_at ASC
                """,
            ).fetchall()
        entries: OrderedDict[CapabilityKey, PreviewCapability] = OrderedDict()
        for row in rows:
            capability = self._from_row(row)
            if capability is None:
                raise ValueError("preview capability store contains invalid data")
            key = self._key(
                capability.engine,
                capability.space,
                capability.session_id,
                capability.path,
            )
            entries[key] = capability
        removed = self._trim_entries(entries)
        if removed:
            with self._connect() as connection:
                for key in removed:
                    connection.execute(
                        """
                        DELETE FROM preview_capabilities
                        WHERE engine=? AND space=? AND session_id=? AND path=?
                        """,
                        key,
                    )
        return entries

    def _load(self) -> None:
        self._entries = self._read_entries()

    def _trim_entries(
        self,
        entries: OrderedDict[CapabilityKey, PreviewCapability] | None = None,
    ) -> list[CapabilityKey]:
        target = self._entries if entries is None else entries
        removed: list[CapabilityKey] = []
        counts: dict[tuple[str, str, str], int] = {}
        for key in target:
            scope = key[:3]
            counts[scope] = counts.get(scope, 0) + 1
        for key in tuple(target):
            scope = key[:3]
            if counts[scope] <= _SESSION_CAP:
                continue
            target.pop(key, None)
            counts[scope] -= 1
            removed.append(key)
        while len(target) > _GLOBAL_CAP:
            dropped, _ = target.popitem(last=False)
            removed.append(dropped)
        return removed

    @staticmethod
    def _canonical_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or len(path.encode("utf-8", "surrogatepass"))
            > PREVIEW_PATH_MAX_BYTES
        ):
            raise PreviewCapabilityError("预览路径无效")
        canonical = os.path.realpath(path)
        if (
            not canonical
            or "\x00" in canonical
            or len(canonical.encode("utf-8", "surrogatepass"))
            > PREVIEW_PATH_MAX_BYTES
        ):
            raise PreviewCapabilityError("预览路径过长")
        return canonical

    @staticmethod
    def _inspect_regular_file(path: str) -> os.stat_result:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise PreviewCapabilityError("文件不存在") from exc
        except PermissionError as exc:
            raise PreviewCapabilityError("没有权限读取该文件") from exc
        except OSError as exc:
            raise PreviewCapabilityError("无法安全打开该文件") from exc
        try:
            file_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise PreviewCapabilityError("预览目标必须是普通文件")
        # os.open already enforces the process user's read permission, including
        # groups and ACLs. Readable project files may be owned by root or another
        # account (for example container-generated files).
        return file_stat

    def _persist_grant(
        self,
        capability: PreviewCapability,
        removed: list[CapabilityKey],
    ) -> OrderedDict[CapabilityKey, PreviewCapability] | None:
        if not self._persistent:
            return None
        try:
            with self._exclusive_lock():
                if not self._sync_epoch_locked():
                    removed = []
                if self._invalidation_path.exists():
                    raise RuntimeError(
                        "preview capability store is invalidated")
                with self._connect() as connection:
                    for key in removed:
                        connection.execute(
                            """
                            DELETE FROM preview_capabilities
                            WHERE engine=? AND space=? AND session_id=? AND path=?
                            """,
                            key,
                        )
                    connection.execute(
                        """
                        INSERT INTO preview_capabilities (
                            engine, space, session_id, path, device, inode, uid,
                            mode, source, granted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(engine, space, session_id, path) DO UPDATE SET
                            device=excluded.device,
                            inode=excluded.inode,
                            uid=excluded.uid,
                            mode=excluded.mode,
                            source=excluded.source,
                            granted_at=excluded.granted_at
                        """,
                        (
                            capability.engine,
                            capability.space,
                            capability.session_id,
                            capability.path,
                            str(capability.device),
                            str(capability.inode),
                            str(capability.uid),
                            capability.mode,
                            capability.source,
                            capability.granted_at,
                        ),
                    )
                entries = self._read_entries()
                self._persistent = True
                return entries
        except Exception as exc:
            invalidated = self._invalidation_path.exists()
            self._persistent = False
            if invalidated:
                self._entries = OrderedDict()
            log.warning(
                "preview capability grant not persisted",
                error_type=type(exc).__name__,
            )
            if invalidated:
                raise PreviewCapabilityError(
                    "预览授权存储正在安全恢复，请稍后重试") from exc
            return None

    def grant_path(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
        *,
        mode: PreviewMode,
        source: PreviewCapabilitySource,
        persist: bool = True,
    ) -> PreviewCapability:
        with self._mutation_lock:
            self._observe_epoch()
            if persist and self._invalidation_path.exists():
                raise PreviewCapabilityError(
                    "预览授权存储正在安全恢复，请稍后重试")
            capability = self.inspect_path(
                engine,
                space,
                session_id,
                path,
                mode=mode,
                source=source,
            )
            canonical = capability.path
            key = self._key(engine, space, session_id, canonical)
            entries = OrderedDict(self._entries)
            previous = entries.get(key)
            same_identity = (
                previous is not None
                and previous.device == capability.device
                and previous.inode == capability.inode
                and previous.uid == capability.uid
            )
            if same_identity and previous.mode == "read_write":
                capability = PreviewCapability(
                    engine=capability.engine,
                    space=capability.space,
                    session_id=capability.session_id,
                    path=capability.path,
                    device=capability.device,
                    inode=capability.inode,
                    uid=capability.uid,
                    mode="read_write",
                    source=previous.source,
                    granted_at=capability.granted_at,
                )
            entries.pop(key, None)
            entries[key] = capability

            removed = self._trim_entries(entries)
            persisted = (
                self._persist_grant(capability, removed) if persist else None)
            self._entries = persisted if persisted is not None else entries
            return capability

    def inspect_path(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
        *,
        mode: PreviewMode,
        source: PreviewCapabilitySource,
    ) -> PreviewCapability:
        """Inspect one exact regular file without retaining a capability.

        Successful engine-side image reads use this to bind a one-shot byte
        snapshot to the file identity observed at that tool boundary. They must
        not silently turn a transient read into durable future path access.
        """
        if engine not in {"claude", "codex", "dsh"} or space not in {"code", "work"}:
            raise PreviewCapabilityError("会话范围无效")
        if not self._valid_text(session_id, maximum=128):
            raise PreviewCapabilityError("会话标识无效")
        if mode not in {"read", "read_write"}:
            raise PreviewCapabilityError("预览权限无效")
        if source not in {
            "engine_observed", "structured_write", "user_approved",
        }:
            raise PreviewCapabilityError("预览权限来源无效")
        canonical = self._canonical_path(path)
        file_stat = self._inspect_regular_file(canonical)
        if mode == "read_write" and file_stat.st_uid != os.geteuid():
            raise PreviewCapabilityError("只允许编辑当前用户拥有的文件")
        return PreviewCapability(
            engine=engine,
            space=space,
            session_id=session_id,
            path=canonical,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            uid=file_stat.st_uid,
            mode=mode,
            source=source,
            granted_at=time.time(),
        )

    def snapshot(
        self,
        engine: str,
        space: str,
        session_id: str,
        *,
        require_write: bool = False,
    ) -> dict[str, PreviewCapability]:
        self._observe_epoch()
        return {
            path: capability
            for (entry_engine, entry_space, entry_sid, path), capability
            in self._entries.items()
            if (
                entry_engine == engine
                and entry_space == space
                and entry_sid == session_id
                and (not require_write or capability.mode == "read_write")
            )
        }

    def revoke(
        self,
        engine: str,
        space: str,
        session_id: str,
        path: str,
    ) -> None:
        with self._mutation_lock:
            self._observe_epoch()
            canonical = self._canonical_path(path)
            key = self._key(engine, space, session_id, canonical)
            marker_created = False
            try:
                with self._invalidation_intent() as (descriptor, token):
                    marker_created = True
                    with self._exclusive_lock():
                        if not self._marker_matches(descriptor, token):
                            raise OSError(
                                "preview invalidation ownership changed")
                        self._sync_epoch_locked()
                        self._initialize()
                        with self._connect() as connection:
                            connection.execute(
                                """
                                DELETE FROM preview_capabilities
                                WHERE engine=? AND space=? AND session_id=? AND path=?
                                """,
                                key,
                            )
                        entries = self._read_entries()
                        self._write_epoch_locked()
                        self._clear_invalidation_locked(descriptor, token)
                    self._entries = entries
                    self._persistent = True
            except Exception as exc:
                self._persistent = False
                self._entries = OrderedDict()
                log.warning(
                    "preview capability revoke not persisted",
                    error_type=type(exc).__name__, marker_created=marker_created,
                )

    def _rekey_entries(
        self,
        entries: OrderedDict[CapabilityKey, PreviewCapability],
        engine: str,
        space: str,
        old_session_id: str,
        new_session_id: str,
    ) -> list[CapabilityKey]:
        moving = [
            (key, capability)
            for key, capability in entries.items()
            if key[:3] == (engine, space, old_session_id)
        ]
        for old_key, capability in moving:
            entries.pop(old_key, None)
            new_key = self._key(
                engine, space, new_session_id, capability.path)
            existing = entries.get(new_key)
            same_identity = (
                existing is not None
                and existing.device == capability.device
                and existing.inode == capability.inode
                and existing.uid == capability.uid
            )
            selected = (
                existing
                if existing is not None
                and not same_identity
                and existing.granted_at >= capability.granted_at
                else capability
            )
            mode: PreviewMode = selected.mode
            source = selected.source
            if same_identity and existing is not None:
                mode = (
                    "read_write"
                    if capability.mode == "read_write"
                    or existing.mode == "read_write"
                    else "read"
                )
                source = (
                    capability.source if mode == capability.mode
                    else existing.source
                )
            entries[new_key] = PreviewCapability(
                engine=engine,
                space=space,
                session_id=new_session_id,
                path=selected.path,
                device=selected.device,
                inode=selected.inode,
                uid=selected.uid,
                mode=mode,
                source=source,
                granted_at=(
                    max(capability.granted_at, existing.granted_at)
                    if same_identity and existing is not None
                    else selected.granted_at
                ),
            )
        ordered = OrderedDict(sorted(
            entries.items(), key=lambda item: item[1].granted_at))
        entries.clear()
        entries.update(ordered)
        return self._trim_entries(entries)

    def rekey(
        self,
        engine: str,
        space: str,
        old_session_id: str,
        new_session_id: str,
        *,
        persist: bool = True,
    ) -> None:
        if old_session_id == new_session_id:
            return
        with self._mutation_lock:
            self._observe_epoch()
            if not persist:
                entries = OrderedDict(self._entries)
                moving = any(
                    key[:3] == (engine, space, old_session_id)
                    for key in entries)
                if not moving:
                    return
                self._rekey_entries(
                    entries, engine, space, old_session_id, new_session_id)
                self._entries = entries
                return
            marker_created = False
            try:
                with self._invalidation_intent() as (descriptor, token):
                    marker_created = True
                    with self._exclusive_lock():
                        if not self._marker_matches(descriptor, token):
                            raise OSError(
                                "preview invalidation ownership changed")
                        self._sync_epoch_locked()
                        self._initialize()
                        entries = self._read_entries()
                        removed = self._rekey_entries(
                            entries, engine, space,
                            old_session_id, new_session_id)
                        with self._connect() as connection:
                            connection.execute(
                                """
                                DELETE FROM preview_capabilities
                                WHERE engine=? AND space=? AND session_id=?
                                """,
                                (engine, space, old_session_id),
                            )
                            for key in removed:
                                connection.execute(
                                    """
                                    DELETE FROM preview_capabilities
                                    WHERE engine=? AND space=?
                                      AND session_id=? AND path=?
                                    """,
                                    key,
                                )
                            for key, capability in entries.items():
                                if key[:3] != (
                                    engine, space, new_session_id
                                ):
                                    continue
                                connection.execute(
                                    """
                                    INSERT INTO preview_capabilities (
                                        engine, space, session_id, path,
                                        device, inode, uid, mode, source,
                                        granted_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(
                                        engine, space, session_id, path
                                    ) DO UPDATE SET
                                        device=excluded.device,
                                        inode=excluded.inode,
                                        uid=excluded.uid,
                                        mode=excluded.mode,
                                        source=excluded.source,
                                        granted_at=excluded.granted_at
                                    """,
                                    (
                                        capability.engine,
                                        capability.space,
                                        capability.session_id,
                                        capability.path,
                                        str(capability.device),
                                        str(capability.inode),
                                        str(capability.uid),
                                        capability.mode,
                                        capability.source,
                                        capability.granted_at,
                                    ),
                                )
                        self._write_epoch_locked()
                        self._clear_invalidation_locked(descriptor, token)
                    self._entries = entries
                    self._persistent = True
            except Exception as exc:
                self._persistent = False
                self._entries = OrderedDict()
                log.warning(
                    "preview capability rekey not persisted",
                    error_type=type(exc).__name__, marker_created=marker_created,
                )

    def remove_session(self, engine: str, session_id: str) -> None:
        with self._mutation_lock:
            self._observe_epoch()
            marker_created = False
            try:
                with self._invalidation_intent() as (descriptor, token):
                    marker_created = True
                    with self._exclusive_lock():
                        if not self._marker_matches(descriptor, token):
                            raise OSError(
                                "preview invalidation ownership changed")
                        self._sync_epoch_locked()
                        self._initialize()
                        with self._connect() as connection:
                            connection.execute(
                                """
                                DELETE FROM preview_capabilities
                                WHERE engine=? AND session_id=?
                                """,
                                (engine, session_id),
                            )
                        entries = self._read_entries()
                        self._write_epoch_locked()
                        self._clear_invalidation_locked(descriptor, token)
                    self._entries = entries
                    self._persistent = True
            except Exception as exc:
                self._persistent = False
                self._entries = OrderedDict()
                log.warning(
                    "preview capability session cleanup not persisted",
                    error_type=type(exc).__name__, marker_created=marker_created,
                )
