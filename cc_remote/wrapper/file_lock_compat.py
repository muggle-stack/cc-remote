"""Cross-platform advisory file locking for Windows and Unix."""
from __future__ import annotations

import os
import sys
import time

if sys.platform == "win32":
    import msvcrt

    LOCK_EX = 0x1
    LOCK_UN = 0x0

    def flock(fd: int, operation: int) -> None:
        """Lock the first byte without changing the caller's file position."""
        if operation not in {LOCK_EX, LOCK_UN}:
            raise ValueError(f"Unsupported lock operation: {operation}")
        # ``os.open`` defaults to text mode on Windows. Journals use raw byte
        # reads/writes, so leave the descriptor in binary mode and avoid CRLF
        # translation while the lock marker is updated.
        msvcrt.setmode(fd, os.O_BINARY)
        original_offset = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if operation == LOCK_UN:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return
            for attempt in range(100):
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if attempt < 99:
                        time.sleep(0.1)
                    else:
                        raise
        finally:
            os.lseek(fd, original_offset, os.SEEK_SET)
else:
    import fcntl

    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN
    flock = fcntl.flock
