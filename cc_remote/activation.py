"""Installer-owned activation receipts and a process-lifetime admission gate."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat

from cc_remote.update_jobs import job_id, read_json, write_json

ACTIVE_PHASES = frozenset({"quiesced", "stopping", "stopped", "snapshotted", "verifying", "restoring", "restored", "recovery_required"})
TERMINAL_PHASES = frozenset({"succeeded", "rolled_back", "unchanged"})


def install_locked(root: Path) -> bool:
    """A missing installer is never a reason to disable an otherwise live chat."""
    try:
        fd = os.open(root / ".update.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != root.stat().st_uid or info.st_mode & 0o022:
            raise ValueError("unsafe installation lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


def receipt(root: Path, identity: str | None = None) -> dict | None:
    path = root / "activation-jobs" / f"{job_id(identity)}.json" if identity else root / "activation.json"
    try:
        value = read_json(path, root.stat().st_uid)
    except FileNotFoundError:
        return None
    job_id(value.get("id"))
    if value.get("schema") != 1 or value.get("root") != str(root):
        raise ValueError("invalid activation receipt")
    return value


def save(root: Path, value: dict) -> None:
    folder = root / "activation-jobs"
    folder.mkdir(mode=0o755, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != root.stat().st_uid or info.st_mode & 0o022:
        raise ValueError("unsafe activation directory")
    write_json(folder / f"{job_id(value['id'])}.json", value)
    write_json(root / "activation.json", value)


def guarded(root: Path, admitted: str | None = None) -> bool:
    value = receipt(root)
    if not value:
        return False
    # An interrupted switch must be reconciled before a newly started Wrapper
    # accepts writes against provisional private state. This is installer state,
    # shared by CLI and UI, and is actively recovered rather than inferred from
    # the UI worker's liveness. Before stop intent, a departed installer is safe.
    if value.get("phase") in ACTIVE_PHASES - {"quiesced"}:
        return True
    return bool((value.get("phase") == "quiesced" or
                 value.get("phase") == "waiting" and value["id"] == admitted) and install_locked(root))
