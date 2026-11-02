"""Short-lived, owner-bound video descriptors; media never enters chat replay."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
import re
import secrets
import threading
import time
from typing import Callable

from cc_remote.viewer import MAX_FILE_BYTES

VIDEO_SUFFIXES = frozenset({".mp4", ".m4v"})
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
    opener: Callable
    authorize: Callable | None


class MediaPreviews:
    """IDs locate files, never authorize them. No idle open FDs or file copies."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries: OrderedDict[str, MediaPreview] = OrderedDict()
        self.lock = threading.RLock()
        self.generation = 0

    def create(self, owner, engine, sid, opener, authorize=None):
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise ValueError("视频预览需要重新连接后再打开")
        with self.lock:
            generation = self.generation
        path, stream, info = opener()
        with stream:
            if authorize is not None:
                authorize(info)
            if info.st_size > MAX_FILE_BYTES:
                raise ValueError(f"视频预览超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 限制")
            header = stream.read(16)
            if len(header) < 16 or header[4:8] != b"ftyp":
                raise ValueError("该文件不是有效的 MP4 视频")
            if fingerprint(os.fstat(stream.fileno())) != fingerprint(info):
                raise ValueError("视频文件已改变，请重新打开")
        with self.lock:
            if generation != self.generation:
                raise ValueError("会话已改变，请重新打开视频")
            now = self.clock()
            for key, item in tuple(self.entries.items()):
                if item.expires <= now:
                    self.entries.pop(key)
            owned = [key for key, item in self.entries.items() if item.owner == owner]
            while len(owned) >= MAX_OWNER_PREVIEWS:
                self.entries.pop(owned.pop(0))
            while len(self.entries) >= MAX_MEDIA_PREVIEWS:
                self.entries.popitem(last=False)
            media_id = secrets.token_hex(16)
            self.entries[media_id] = MediaPreview(owner, engine, sid, now + MEDIA_TTL,
                                                 fingerprint(info), opener, authorize)
        return {"path": path, "format": "video", "media_type": "video/mp4",
                "media_id": media_id, "size": info.st_size, "mtime_ns": info.st_mtime_ns}

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
            return stream, info, "video/mp4"
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
