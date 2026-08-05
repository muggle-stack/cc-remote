"""Regression checks for the cross-platform advisory lock wrapper."""
from __future__ import annotations

import os

from cc_remote.wrapper.file_lock_compat import LOCK_EX, LOCK_UN, flock


def test_flock_preserves_offset_and_unlocks_after_io(tmp_path):
    path = tmp_path / "journal.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"header")
        expected_offset = os.lseek(fd, 0, os.SEEK_CUR)

        flock(fd, LOCK_EX)
        assert os.lseek(fd, 0, os.SEEK_CUR) == expected_offset

        os.write(fd, b"-payload\n")
        unlock_offset = os.lseek(fd, 0, os.SEEK_CUR)
        flock(fd, LOCK_UN)
        assert os.lseek(fd, 0, os.SEEK_CUR) == unlock_offset
    finally:
        os.close(fd)
