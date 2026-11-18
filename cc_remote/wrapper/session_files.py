"""Bounded session file reads for previews and local artifact inspection."""
from __future__ import annotations

import os
import stat
from typing import BinaryIO, Optional

from cc_remote.wrapper.preview_capabilities import PREVIEW_PATH_MAX_BYTES, PreviewCapability


class PreviewAuthorizationNeeded(ValueError):
    """An otherwise valid preview points outside cwd without a capability."""

    def __init__(self, path: str, resolved_path: str):
        super().__init__("外部文件需要确认后才能预览")
        self.path = path
        self.resolved_path = resolved_path


class PreviewCapabilityChanged(PreviewAuthorizationNeeded):
    """The exact file identity previously authorized no longer matches."""


def path_is_below(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False

def open_session_file(
    cwd: str,
    path: str,
    *,
    allowed_suffixes: Optional[frozenset[str]],
    max_bytes: int,
    allow_truncate: bool,
    allowed_external_paths: Optional[
        dict[str, PreviewCapability]
    ] = None,
    workspace_identity: tuple[int, int] | None = None,
) -> tuple[str, BinaryIO, os.stat_result]:
    """Open a bounded regular file below cwd without following an escape.

    ``realpath`` contains relative or tool-reported absolute paths inside
    the session root while rejecting symlinks that escape it. ``O_NONBLOCK``
    prevents a malicious FIFO from hanging the wrapper before ``fstat`` can
    reject the special file.
    """
    if path.startswith("~"):
        raise ValueError("预览路径必须位于当前工作目录")
    try:
        path_bytes = len(path.encode("utf-8", "surrogatepass"))
    except UnicodeEncodeError as exc:
        raise ValueError("预览路径无效") from exc
    if (
        not path
        or "\x00" in path
        or path_bytes > PREVIEW_PATH_MAX_BYTES
    ):
        raise ValueError("预览路径过长")

    root = os.path.realpath(cwd)
    candidate = os.path.realpath(
        path if os.path.isabs(path) else os.path.join(root, path))
    if (
        len(candidate.encode("utf-8", "surrogatepass"))
        > PREVIEW_PATH_MAX_BYTES
    ):
        raise ValueError("预览路径过长")
    inside_root = path_is_below(root, candidate)

    suffix = os.path.splitext(candidate)[1].lower()
    if allowed_suffixes is not None and suffix not in allowed_suffixes:
        raise ValueError("不支持预览该文件类型")
    capability = (
        (allowed_external_paths or {}).get(candidate)
        if not inside_root else None
    )
    if not inside_root and capability is None:
        raise PreviewAuthorizationNeeded(path, candidate)

    access_root = root if inside_root else os.path.dirname(candidate)
    relative = (os.path.relpath(candidate, root)
                if inside_root else os.path.basename(candidate))
    display_path = relative.replace(os.sep, "/") if inside_root else candidate
    if relative in ("", "."):
        raise ValueError("预览目标必须是普通文件")
    parts = relative.split(os.sep)
    file_flags = os.O_RDONLY
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    dir_flags = os.O_RDONLY
    dir_flags |= getattr(os, "O_CLOEXEC", 0)
    dir_flags |= getattr(os, "O_DIRECTORY", 0)
    dir_flags |= getattr(os, "O_NOFOLLOW", 0)
    dir_fd: Optional[int] = None
    try:
        # Walk from an already-open cwd and refuse symlinks at every hop.
        # This closes the realpath/open race where a parent directory could
        # otherwise be replaced by an escaping symlink between both calls.
        dir_fd = os.open(access_root, dir_flags)
        if workspace_identity is not None:
            opened_root = os.fstat(dir_fd)
            if not inside_root or (opened_root.st_dev, opened_root.st_ino) != workspace_identity:
                raise ValueError("The session workspace has changed. Reconnect to inspect files.")
        for part in parts[:-1]:
            next_fd = os.open(part, dir_flags, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise ValueError("文件不存在") from exc
    except PermissionError as exc:
        raise ValueError("没有权限读取该文件") from exc
    except OSError as exc:
        raise ValueError("无法打开该文件") from exc
    finally:
        if dir_fd is not None:
            os.close(dir_fd)

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("预览目标必须是普通文件")
        if (
            not inside_root
            and capability is not None
            and not capability.matches(file_stat)
        ):
            raise PreviewCapabilityChanged(path, candidate)
        if not allow_truncate and file_stat.st_size > max_bytes:
            raise ValueError(
                f"预览文件超过 {max_bytes // (1024 * 1024)} MiB 限制")
        return display_path, os.fdopen(fd, "rb"), file_stat
    except BaseException:
        os.close(fd)
        raise

def read_session_file(
    cwd: str, path: str, *, allowed_suffixes: Optional[frozenset[str]],
    max_bytes: int, allow_truncate: bool,
    allowed_external_paths: Optional[dict[str, PreviewCapability]] = None,
    workspace_identity: tuple[int, int] | None = None,
) -> tuple[str, bytes, os.stat_result, bool]:
    display_path, handle, file_stat = open_session_file(
        cwd, path, allowed_suffixes=allowed_suffixes, max_bytes=max_bytes,
        allow_truncate=allow_truncate, allowed_external_paths=allowed_external_paths,
        workspace_identity=workspace_identity)
    with handle:
        data = handle.read(max_bytes + 1)

    truncated = len(data) > max_bytes or file_stat.st_size > max_bytes
    if truncated and not allow_truncate:
        raise ValueError(
            f"预览文件超过 {max_bytes // (1024 * 1024)} MiB 限制")
    return display_path, data[:max_bytes], file_stat, truncated
