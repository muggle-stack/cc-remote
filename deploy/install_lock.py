"""Share one persistent lock between direct role installers and managed updates."""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import stat

LOCK_FD_ENV = "CC_REMOTE_INSTALL_LOCK_FD"


class InstallLockError(ValueError):
    pass


def verify_install_lock(root: Path, descriptor: int) -> None:
    """Validate the inherited open file and acquire its exclusive lock again.

    An environment marker alone never skips locking. flock is associated with
    the inherited open file description, so a child can verify it without
    deadlocking against its updater parent or releasing that parent's lock.
    """
    if descriptor < 3:
        raise InstallLockError("invalid inherited install lock descriptor")
    opened = os.fstat(descriptor)
    expected = (root / ".update.lock").lstat()
    if (
        not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(expected.st_mode)
        or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        or opened.st_uid != os.geteuid() or opened.st_mode & 0o022
    ):
        raise InstallLockError("install lock does not match this installation")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise InstallLockError(
            "another install or update is already running; inspect it before retrying"
        ) from exc


def acquire_install_lock(root: Path) -> int:
    descriptor = os.open(root / ".update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        verify_install_lock(root, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-fd", type=int)
    parser.add_argument("root", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.verify_fd is not None:
            if args.command:
                parser.error("--verify-fd does not accept a command")
            verify_install_lock(args.root, args.verify_fd)
            return 0
        if not args.command:
            parser.error("a command is required when acquiring the install lock")
        args.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = acquire_install_lock(args.root)
        try:
            os.set_inheritable(descriptor, True)
            environment = {**os.environ, LOCK_FD_ENV: str(descriptor)}
            # Replace the helper so the installer itself holds the lock through
            # activation and its EXIT/INT rollback traps, even if a caller exits.
            os.execvpe(args.command[0], args.command, environment)
        finally:
            os.close(descriptor)
    except (OSError, InstallLockError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
