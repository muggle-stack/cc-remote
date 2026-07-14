"""Detect Codex sessions owned by another local process.

Codex rollout files are written asynchronously, so file growth alone cannot tell
whether a write came from this wrapper or from a native terminal.  The primary
signal here is stronger: another process has the same rollout inode open for
writing.  Turn markers provide a fallback for short-lived writers and tell the
wrapper when an externally-produced transcript must be reloaded.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


MAX_PROC_SCAN = 8192
MAX_FDS_PER_PROCESS = 8192
MAX_PARTIAL_RECORD_BYTES = 16 * 1024 * 1024
MAX_CMDLINE_BYTES = 64 * 1024
_TERMINAL_EVENTS = frozenset({
    "task_complete", "turn_aborted", "task_failed", "task_cancelled",
})


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class TurnMarkers:
    started: frozenset[str]
    finished: frozenset[str]
    partial: bytes
    ordered: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class HolderScan:
    holders: dict[str, set[ProcessIdentity]]
    complete: bool
    # Headless app-server processes keep rollout FDs open while idle. They are
    # writers for turn attribution, but are not by themselves an interactive
    # terminal owner; their task markers drive the active lock instead.
    passive_holders: dict[str, set[ProcessIdentity]] = field(default_factory=dict)


def _process_stat(proc_dir: Path) -> tuple[int, int, int] | None:
    """Return (parent pid, start ticks, tty number) from /proc stat."""
    try:
        raw = (proc_dir / "stat").read_bytes()
        end = raw.rfind(b") ")
        if end < 0:
            return None
        fields = raw[end + 2:].split()  # starts at field 3 (state)
        return int(fields[1]), int(fields[19]), int(fields[4])
    except (OSError, ValueError, IndexError):
        return None


def _process_start_ticks(proc_dir: Path) -> int | None:
    stat = _process_stat(proc_dir)
    return stat[1] if stat is not None else None


def process_identity(pid: int, *, proc_root: str = "/proc",
                     parent_pid: int | None = None) -> ProcessIdentity | None:
    stat = _process_stat(Path(proc_root) / str(pid))
    if stat is None or (parent_pid is not None and stat[0] != parent_pid):
        return None
    return ProcessIdentity(pid, stat[1])


def _process_cmdline(proc_dir: Path) -> tuple[bytes, ...] | None:
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > MAX_CMDLINE_BYTES:
        return ()
    return tuple(arg for arg in raw.split(b"\0") if arg)


def _is_passive_app_server(
    proc_dir: Path, tty_nr: int, args: tuple[bytes, ...] | None = None,
) -> bool:
    """True for a headless Codex app-server, not an interactive TUI process."""
    if tty_nr != 0:
        return False
    if args is None:
        args = _process_cmdline(proc_dir)
    return bool(args and b"app-server" in args)


def _codex_resume_sids(
    args: tuple[bytes, ...] | None,
    sid_by_arg: Mapping[bytes, str],
) -> set[str]:
    """Map an interactive ``codex resume SID`` TUI to its logical session.

    Modern Codex TUIs can talk to a persistent app-server and never open the
    rollout themselves. Their explicit resume command is therefore the only
    exact idle-session ownership signal available in /proc.
    """
    if not args or b"resume" not in args:
        return set()
    command_names = {arg.rsplit(b"/", 1)[-1] for arg in args[:2]}
    if not command_names.intersection({b"codex", b"codex.exe", b"codex.js"}):
        return set()
    resume_at = args.index(b"resume")
    return {
        sid_by_arg[arg] for arg in args[resume_at + 1:] if arg in sid_by_arg
    }


def _fd_is_writable(proc_dir: Path, fd_name: str) -> bool | None:
    try:
        with (proc_dir / "fdinfo" / fd_name).open() as stream:
            for line in stream:
                if not line.startswith("flags:"):
                    continue
                flags = int(line.split(":", 1)[1].strip(), 8)
                return (flags & os.O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
    except (OSError, ValueError):
        return None
    return None


def writable_rollout_holders(
    paths: Mapping[str, str],
    own_processes: Iterable[ProcessIdentity] = (),
    *,
    proc_root: str = "/proc",
) -> HolderScan:
    """Return writable holders of each rollout, excluding exact wrapper children.

    Matching uses ``(st_dev, st_ino)`` rather than path text, so symlinks and
    renamed paths cannot create false negatives.  PID start ticks are checked
    before and after the FD walk to reject PID/FD reuse races.
    """
    by_inode: dict[tuple[int, int], set[str]] = {}
    result = {sid: set() for sid in paths}
    passive = {sid: set() for sid in paths}
    sid_by_arg = {sid.encode(): sid for sid in paths}
    for sid, path in paths.items():
        try:
            st = os.stat(path)
        except OSError:
            continue
        by_inode.setdefault((st.st_dev, st.st_ino), set()).add(sid)
    if not by_inode:
        return HolderScan(result, True, passive)

    own = set(own_processes)
    root = Path(proc_root)
    if own:
        own_fd_visible = False
        for identity in own:
            try:
                # Opening the directory is sufficient; it may legitimately be empty
                # during reconnect. This detects hidepid/ProtectProc-style setups
                # without treating ordinary process-exit races as scan failures.
                with os.scandir(root / str(identity.pid) / "fd"):
                    own_fd_visible = True
                    break
            except OSError:
                continue
        if not own_fd_visible:
            return HolderScan(result, False, passive)
    complete = True
    try:
        processes = (entry for entry in root.iterdir() if entry.name.isdigit())
        for index, proc_dir in enumerate(processes):
            if index >= MAX_PROC_SCAN:
                complete = False
                break
            pid = int(proc_dir.name)
            proc_stat = _process_stat(proc_dir)
            if proc_stat is None:
                continue
            _, start, tty_nr = proc_stat
            identity = ProcessIdentity(pid, start)
            if identity in own:
                continue
            args = _process_cmdline(proc_dir)
            logical_sids = (
                _codex_resume_sids(args, sid_by_arg) if tty_nr != 0 else set())
            matched: set[str] = set()
            try:
                fds = proc_dir.joinpath("fd").iterdir()
                for fd_index, fd_path in enumerate(fds):
                    if fd_index >= MAX_FDS_PER_PROCESS:
                        complete = False
                        break
                    try:
                        st = fd_path.stat()
                    except OSError:
                        continue
                    sids = by_inode.get((st.st_dev, st.st_ino))
                    if not sids:
                        continue
                    writable = _fd_is_writable(proc_dir, fd_path.name)
                    if writable is None:
                        complete = False
                        continue
                    if not writable:
                        continue
                    # Recheck the exact descriptor after fdinfo: the process may
                    # have closed and reused the same number during our scan.
                    try:
                        current = fd_path.stat()
                    except OSError:
                        continue
                    if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino):
                        continue
                    matched.update(sids)
            except OSError:
                pass
            if not matched and not logical_sids:
                continue
            # The process may have exited or the PID may have been reused while
            # its descriptors/cmdline were scanned. Only accept a stable identity.
            if _process_start_ticks(proc_dir) != start:
                continue
            for sid in logical_sids:
                result[sid].add(identity)
            for sid in matched:
                result[sid].add(identity)
                if _is_passive_app_server(proc_dir, tty_nr, args):
                    passive[sid].add(identity)
    except OSError:
        return HolderScan(result, False, passive)
    return HolderScan(result, complete, passive)


def parse_turn_markers(data: bytes, partial: bytes = b"") -> TurnMarkers:
    """Parse complete JSONL records and preserve one incomplete trailing record."""
    combined = partial + data
    lines = combined.splitlines(keepends=True)
    carry = b""
    if lines and not lines[-1].endswith((b"\n", b"\r")):
        carry = lines.pop()
        if len(carry) > MAX_PARTIAL_RECORD_BYTES:
            carry = b""

    started: set[str] = set()
    finished: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
            continue
        kind = payload.get("type")
        if kind == "task_started":
            started.add(turn_id)
            ordered.append((kind, turn_id))
        elif kind in _TERMINAL_EVENTS:
            finished.add(turn_id)
            ordered.append((kind, turn_id))
    return TurnMarkers(
        frozenset(started), frozenset(finished), carry, tuple(ordered))
