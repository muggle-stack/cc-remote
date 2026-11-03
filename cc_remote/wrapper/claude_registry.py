"""Read Claude Code's PID registry without treating stale files as owners.

The native CLI updates sessionId after an interactive /resume. This is an
optional observation source (2.1.311); older CLIs keep the argv/cwd fallback.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from uuid import UUID
from typing import Collection

from cc_remote.wrapper.process_scan import (
    ProcessIdentity, process_identity, process_owner_uid,
)


def registered_pids(roots: Collection[str]) -> tuple[set[int], bool]:
    """Bounded argv-scan hints, never ownership evidence on their own."""
    pids: set[int] = set()
    for root in set(roots):
        directory = Path(root) / "sessions"
        try:
            if directory.is_symlink():
                return pids, False
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 4096:
                        return pids, False
                    stem, suffix = os.path.splitext(entry.name)
                    if suffix == ".json" and stem.isascii() and stem.isdecimal():
                        pids.add(int(stem))
        except FileNotFoundError:
            continue
        except OSError:
            return pids, False
    return pids, True


@lru_cache(maxsize=256)
def _native_process_start(identity: ProcessIdentity) -> str:
    # Match Claude's own fingerprint, including its fixed locale and timezone.
    # Cache only by the OS identity, never by PID alone.
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(identity.pid)],
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            capture_output=True, text=True, timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("Claude process start unavailable") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise OSError("Claude process start unavailable")
    return result.stdout.strip()


def _recorded_start_matches(identity: ProcessIdentity, recorded: object) -> bool:
    # Claude on Linux records the /proc start ticks; elsewhere it records
    # ``ps lstart`` text.
    if sys.platform.startswith("linux") and recorded == str(identity.start_ticks):
        return True
    fingerprint = _native_process_start(identity)
    return bool(fingerprint) and recorded == fingerprint


def registered_session(
    identity: ProcessIdentity,
    config_root: str | None,
    *,
    proc_root: str = "/proc",
) -> tuple[bool, str | None]:
    """Return (complete, session id); missing registry permits legacy fallback.

    A present but unreadable/mismatched record must not fall back to stale
    launch arguments: the process could already have switched conversations.
    A valid unwatched session is still authoritative and suppresses fallback.
    """
    if config_root is None:
        return True, None
    path = Path(config_root) / "sessions" / f"{identity.pid}.json"
    try:
        if path.parent.is_symlink():
            return False, None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True, None
    except OSError:
        return False, None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_size > 64 * 1024):
                return False, None
            raw = json.loads(stream.read(64 * 1024 + 1))
        if not isinstance(raw, dict) or type(raw.get("pid")) is not int:
            return False, None
        sid = raw.get("sessionId")
        if not isinstance(sid, str) or str(UUID(sid)) != sid:
            return False, None
        if raw["pid"] != identity.pid or raw.get("spare") is True:
            return False, None
        if process_identity(identity.pid, proc_root=proc_root) != identity:
            return False, None
        if process_owner_uid(identity.pid, proc_root=proc_root) != os.getuid():
            return False, None
        if not _recorded_start_matches(identity, raw.get("procStart")):
            return False, None
        if process_identity(identity.pid, proc_root=proc_root) != identity:
            return False, None
        return True, sid
    except (OSError, ValueError, TypeError):
        return False, None
