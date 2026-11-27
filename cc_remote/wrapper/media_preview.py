"""Short-lived, owner-bound descriptors for video and original downloads."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
import re
import secrets
import stat
import threading
import time
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from cc_remote.audio_preview import webm_media_type
from cc_remote.protocol import ARTIFACT_PREVIEW_MAX_BYTES
from cc_remote.viewer import MAX_FILE_BYTES

VIDEO_SUFFIXES = frozenset({".mp4", ".m4v"})
STREAMED_MEDIA_SUFFIXES = VIDEO_SUFFIXES | {".webm"}
MEDIA_ID_RE = re.compile(r"[a-f0-9]{32}\Z")
MEDIA_TTL = 3600
MAX_MEDIA_PREVIEWS = 256
MAX_OWNER_PREVIEWS = 32


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_nlink)


@dataclass(frozen=True)
class MediaPreview:
    owner: str
    engine: str
    sid: str
    expires: float
    identity: tuple
    mime: str
    path: str
    opener: Callable
    authorize: Callable | None
    remote_check: Callable[[], Awaitable[None]] | None = None


class MediaRegistration(BaseModel):
    """A reopening descriptor sent only on the private Gemini service socket."""

    media_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner: str = Field(min_length=1, max_length=128)
    sid: str = Field(min_length=1, max_length=512)
    expires: float = Field(allow_inf_nan=False)
    identity: tuple[int, int, int, int, int, int, int]
    mime: Literal["video/mp4", "video/webm", "application/octet-stream"]
    path: str = Field(min_length=1, max_length=4096)


class MediaPreviews:
    """IDs locate files, never authorize them. No idle open FDs or file copies."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries: OrderedDict[str, MediaPreview] = OrderedDict()
        self.lock = threading.RLock()
        self.generation = 0

    def create(self, owner, engine, sid, opener, authorize=None, *, download_identity=None, resolved_path=None):
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError("请重新连接后再打开文件")
        with self.lock:
            generation = self.generation
        path, stream, info = opener()
        with stream:
            if authorize is not None:
                authorize(info)
            identity = fingerprint(info)
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 限制")
            data = None
            if download_identity is not None:
                if identity != download_identity:
                    raise ValueError("文件已改变，请重新打开")
                mime = "application/octet-stream"
            elif path.lower().endswith(".webm"):
                mime = webm_media_type(stream, info.st_size)
                if mime == "audio/webm":
                    if info.st_size > ARTIFACT_PREVIEW_MAX_BYTES:
                        raise ValueError("音频文件超过 8 MiB 预览限制")
                    stream.seek(0)
                    data = stream.read(ARTIFACT_PREVIEW_MAX_BYTES + 1)
            else:
                header = stream.read(16)
                if len(header) < 16 or header[4:8] != b"ftyp":
                    raise ValueError("该文件不是有效的 MP4 视频")
                mime = "video/mp4"
            if fingerprint(os.fstat(stream.fileno())) != identity:
                raise ValueError("文件已改变，请重新打开")
        preview = {"path": path, "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        with self.lock:
            if generation != self.generation:
                raise ValueError("会话已改变，请重新打开文件")
            if data is not None:
                return {**preview, "format": "audio", "media_type": mime, "data": data}
            media_id = secrets.token_hex(16)
            self._register_locked(media_id, MediaPreview(
                owner, engine, sid, self.clock() + MEDIA_TTL, identity, mime, resolved_path or path, opener, authorize))
        if download_identity is not None:
            return {"media_id": media_id}
        return {**preview, "format": "video", "media_type": mime, "media_id": media_id}

    def _register_locked(self, media_id: str, record: MediaPreview) -> None:
        now = self.clock()
        if record.expires <= now:
            raise ValueError("video expired")
        for key, item in tuple(self.entries.items()):
            if item.expires <= now:
                self.entries.pop(key)
        if media_id in self.entries:
            raise ValueError("video identifier already registered")
        owned = [key for key, item in self.entries.items() if item.owner == record.owner]
        while len(owned) >= MAX_OWNER_PREVIEWS:
            self.entries.pop(owned.pop(0))
        while len(self.entries) >= MAX_MEDIA_PREVIEWS:
            self.entries.popitem(last=False)
        self.entries[media_id] = record

    def gemini_registration(self, media_id: str) -> MediaRegistration | None:
        with self.lock:
            item = self.entries.get(media_id)
            if item is None or item.engine != "antigravity" or item.expires <= self.clock():
                return None
            return MediaRegistration(media_id=media_id, owner=item.owner, sid=item.sid,
                                     expires=item.expires, identity=item.identity, mime=item.mime, path=item.path)

    def register_gemini(self, source: MediaRegistration, check: Callable[[], Awaitable[None]]) -> None:
        if not os.path.isabs(source.path) or "\0" in source.path:
            raise ValueError("invalid video path")

        def opener():
            fd = os.open(source.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise PermissionError("video changed")
                return source.path, os.fdopen(fd, "rb"), info
            except BaseException:
                os.close(fd)
                raise

        record = MediaPreview(source.owner, "antigravity", source.sid, source.expires, source.identity,
                              source.mime, source.path, opener, None, remote_check=check)
        with self.lock:
            existing = self.entries.get(source.media_id)
            if existing and existing.remote_check is not None and self.gemini_registration(source.media_id) == source:
                return
            self._register_locked(source.media_id, record)

    async def check_remote(self, media_id, owner) -> None:
        item = self.get(media_id, owner)
        if item.remote_check is not None:
            await item.remote_check()

    def get(self, media_id, owner):
        with self.lock:
            if not isinstance(media_id, str) or not MEDIA_ID_RE.fullmatch(media_id):
                raise FileNotFoundError("video unavailable")
            item = self.entries.get(media_id)
            if item is None or item.owner != owner:
                raise FileNotFoundError("video unavailable")
            if item.expires <= self.clock():
                self.entries.pop(media_id)
                raise FileNotFoundError("video expired")
            return item

    def open(self, media_id, owner):
        item = self.get(media_id, owner)
        _, stream, info = item.opener()
        try:
            if fingerprint(info) != item.identity:
                raise PermissionError("video changed")
            self.validate(media_id, owner, stream)
            return stream, info, item.mime
        except BaseException:
            stream.close()
            raise

    def validate(self, media_id, owner, stream):
        item = self.get(media_id, owner)
        info = os.fstat(stream.fileno())
        if fingerprint(info) != item.identity:
            raise PermissionError("video changed")
        if item.authorize is not None:
            item.authorize(info)

    def drop(self, engine, sid):
        with self.lock:
            self.generation += 1
            for key, item in tuple(self.entries.items()):
                if item.engine == engine and item.sid == sid:
                    self.entries.pop(key)
