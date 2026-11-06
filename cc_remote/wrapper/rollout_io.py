"""Read native JSONL or Zstandard rollouts without rewriting native storage.

History pagination uses byte offsets and backward seeks. Compressed sources
therefore get a bounded, private, process-local decoded cache. Callers retain
the native path as their identity and receive independent read-only file handles;
evicting a cache entry cannot invalidate an already open reader.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
import threading

try:
    from compression import zstd
except ImportError:  # Python 3.10–3.13.
    from backports import zstd


_CACHE_ENTRIES = 8
_CACHE_BYTES = 1024 * 1024 * 1024
_MAX_DECODED_BYTES = 4 * 1024 * 1024 * 1024
_CHUNK_BYTES = 128 * 1024
_OPTIONS = {zstd.DecompressionParameter.window_log_max: 27}
_lock = threading.RLock()
_directory: tempfile.TemporaryDirectory | None = None
_cache: OrderedDict[str, tuple[tuple[int, ...], str, int]] = OrderedDict()


@dataclass(frozen=True)
class _ReadStat:
    """Native identity with coordinates measured in decoded JSONL bytes."""
    native: os.stat_result
    st_size: int

    def __getattr__(self, name):
        return getattr(self.native, name)


def is_compressed_rollout(path) -> bool:
    return os.fspath(path).endswith(".jsonl.zst")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _discard(path: str) -> None:
    entry = _cache.pop(path, None)
    if entry is not None:
        Path(entry[1]).unlink(missing_ok=True)


def _decoded_path(path: str, *, follow_symlinks=True) -> str:
    """Called under _lock; returned path must be opened before releasing it."""
    global _directory
    before = os.stat(path, follow_symlinks=follow_symlinks)
    if not stat.S_ISREG(before.st_mode):
        raise OSError("Codex rollout is not a regular file")
    identity = _identity(before)
    entry = _cache.get(path)
    if entry is not None and entry[0] == identity:
        _cache.move_to_end(path)
        return entry[1]
    _discard(path)
    if _directory is None:
        _directory = tempfile.TemporaryDirectory(prefix="cc-remote-rollouts-")
    fd, target = tempfile.mkstemp(dir=_directory.name, suffix=".jsonl")
    try:
        size = 0
        with os.fdopen(fd, "wb") as output, _native_open(path, "rb", follow_symlinks=follow_symlinks) as raw:
            if _identity(os.fstat(raw.fileno())) != identity:
                raise OSError("Codex rollout changed before decompression")
            with zstd.ZstdFile(raw, "rb", options=_OPTIONS) as source:
                while chunk := source.read(_CHUNK_BYTES):
                    size += len(chunk)
                    if size > _MAX_DECODED_BYTES:
                        raise OSError("Decoded Codex rollout exceeds the read limit")
                    output.write(chunk)
            if (_identity(os.fstat(raw.fileno())) != identity
                    or _identity(os.stat(path, follow_symlinks=follow_symlinks)) != identity):
                raise OSError("Codex rollout changed during decompression")
        _cache[path] = (identity, target, size)
        # One large entry may exceed the soft cache budget, but never the hard
        # decode limit. Plain, currently streaming rollouts never enter here.
        while len(_cache) > 1 and (
            len(_cache) > _CACHE_ENTRIES
            or sum(entry[2] for entry in _cache.values()) > _CACHE_BYTES
        ):
            _discard(next(iter(_cache)))
        return target
    except BaseException as exc:
        Path(target).unlink(missing_ok=True)
        if isinstance(exc, (zstd.ZstdError, EOFError)):
            raise OSError("Compressed Codex rollout is incomplete or invalid") from exc
        raise


def _native_open(path, mode, *, follow_symlinks=True, **kwargs):
    if not follow_symlinks:
        kwargs["opener"] = lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    return open(path, mode, **kwargs)


def rollout_open(path, mode="rb", *, follow_symlinks=True, **kwargs):
    """Open a seekable read view; never open a native rollout for writing."""
    if mode not in {"r", "rt", "rb"}:
        raise ValueError("Rollout views are read-only")
    if not is_compressed_rollout(path):
        return _native_open(path, mode, follow_symlinks=follow_symlinks, **kwargs)
    with _lock:
        native = os.path.realpath(path) if follow_symlinks else os.path.abspath(path)
        decoded = (_decoded_path(native) if follow_symlinks
                   else _decoded_path(native, follow_symlinks=False))
        info = os.stat(native, follow_symlinks=follow_symlinks)
        identity, _, size = _cache[native]
        if _identity(info) != identity:
            raise OSError("Codex rollout changed before opening read view")
        stream = open(decoded, mode, **kwargs)
        stream.rollout_read_stat = _ReadStat(info, size)
        return stream


def rollout_fstat(stream):
    view = getattr(stream, "rollout_read_stat", None)
    return view if view is not None else os.fstat(stream.fileno())


def rollout_stat(path, *, follow_symlinks=True):
    """File identity and size of the same byte view returned by rollout_open."""
    if not is_compressed_rollout(path):
        return os.stat(path, follow_symlinks=follow_symlinks)
    if not follow_symlinks and os.path.islink(path):
        return os.lstat(path)
    with rollout_open(path) as source:
        return rollout_fstat(source)


def rollout_size(path) -> int:
    return rollout_stat(path).st_size


def rollout_first_line(path, limit: int, *, source=None) -> bytes:
    """Metadata discovery must not decompress/cache an entire cold session."""
    with (open(path, "rb") if source is None else nullcontext(source)) as raw:
        if not is_compressed_rollout(path):
            return raw.readline(limit)
        try:
            with zstd.ZstdFile(raw, "rb", options=_OPTIONS) as source:
                return source.readline(limit)
        except (zstd.ZstdError, EOFError) as exc:
            raise OSError("Compressed Codex rollout metadata is unavailable") from exc
