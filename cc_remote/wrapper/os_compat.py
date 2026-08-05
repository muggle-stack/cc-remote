"""Small OS compatibility helpers that preserve Unix durability semantics."""
from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from os import PathLike


def current_uid() -> int:
    """Return the Unix uid, or the stable Windows ``st_uid`` placeholder."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


def fchmod(fd: int, path: str | PathLike[str], mode: int) -> None:
    """Set a file mode using the descriptor where the platform supports it."""
    native_fchmod = getattr(os, "fchmod", None)
    if native_fchmod is not None:
        native_fchmod(fd, mode)
    else:
        os.chmod(path, mode)


def fsync_directory(path: str | PathLike[str]) -> None:
    """Persist a directory entry on Unix; Windows has no directory fsync."""
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _win32_long_path(path: str) -> str:
    """Prefix an absolute Windows path with \\\\?\\ so it bypasses MAX_PATH.

    Without this, any path at or beyond 260 characters silently misbehaves
    on delete (directory entries can be enumerated but not reliably
    unlinked/rmdir'd) unless the machine has opted into the
    ``LongPathsEnabled`` registry setting, which cc-remote cannot assume.
    """
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def force_rmtree(path: str | PathLike[str]) -> None:
    """Remove a directory tree, clearing Windows read-only bits first.

    Git always creates loose objects read-only. POSIX directory write
    permission is enough to unlink them; Windows blocks the unlink outright
    unless the read-only attribute is cleared first. The path is also
    resolved through the \\\\?\\ long-path form so deletion isn't silently
    unreliable for paths at or beyond MAX_PATH (260 chars), which ordinary
    temp-directory nesting reaches easily. A short retry absorbs the
    transient "directory not empty" Windows reports while another process
    (antivirus, search indexing) briefly holds a just-deleted file.
    """
    if sys.platform != "win32":
        shutil.rmtree(path)
        return

    def _clear_readonly_and_retry(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except FileNotFoundError:
            pass

    long_path = _win32_long_path(os.fspath(path))
    attempts = 5
    for attempt in range(attempts):
        try:
            shutil.rmtree(long_path, onerror=_clear_readonly_and_retry)
            return
        except OSError:
            if not os.path.exists(long_path):
                return
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))


def pread(fd: int, length: int, offset: int) -> bytes:
    """Positioned read, backed by lseek+read where the platform lacks pread.

    Callers only use this on descriptors they exclusively own for the
    duration of the call, so relocating the file position is safe.
    """
    native_pread = getattr(os, "pread", None)
    if native_pread is not None:
        return native_pread(fd, length, offset)
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)


def pwrite(fd: int, data: bytes, offset: int) -> int:
    """Positioned write, backed by lseek+write where the platform lacks pwrite."""
    native_pwrite = getattr(os, "pwrite", None)
    if native_pwrite is not None:
        return native_pwrite(fd, data, offset)
    saved = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, data)
    finally:
        os.lseek(fd, saved, os.SEEK_SET)
