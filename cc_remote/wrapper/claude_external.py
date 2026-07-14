"""Detect Claude sessions owned by another local Claude Code process.

An idle Claude TUI does not keep its transcript open, so transcript growth is
not a stable ownership signal.  Prefer an explicit session id from the process
command line and conservatively associate an otherwise-unqualified Claude Code
process with every watched session in the same working directory.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from cc_remote.wrapper.codex_external import (
    MAX_PROC_SCAN,
    HolderScan,
    ProcessIdentity,
    _process_cmdline,
    _process_start_ticks,
    _process_stat,
)


_CLAUDE_COMMANDS = frozenset({"claude", "claude.exe"})
_SESSION_FLAGS = frozenset({b"--resume", b"-r", b"--session-id"})
_BACKGROUND_ROLES = frozenset({
    b"daemon", b"bg-pty-host", b"bg-spare", b"--bg-pty-host", b"--bg-spare",
})


def _is_claude_cli(args: tuple[bytes, ...] | None) -> bool:
    if not args:
        return False
    # Recent native installers leave a daemon and pre-warmed background PTYs
    # alive after the interactive terminal exits. They do not own a transcript
    # and must not manufacture a permanent read-only session.
    if len(args) > 1 and args[1] in _BACKGROUND_ROLES:
        return False
    for raw in args[:3]:
        value = os.fsdecode(raw)
        name = os.path.basename(value).lower()
        if name in _CLAUDE_COMMANDS:
            return True
        normalized = value.replace("\\", "/").lower()
        if "/claude/versions/" in normalized:
            return True
        if "claude-code" in normalized and name in {
            "cli.js", "cli.mjs", "index.js", "index.mjs",
        }:
            return True
    return False


def _explicit_session_ids(
    args: tuple[bytes, ...], sid_by_arg: Mapping[bytes, str],
) -> set[str]:
    result: set[str] = set()
    for index, arg in enumerate(args):
        if arg in _SESSION_FLAGS and index + 1 < len(args):
            sid = sid_by_arg.get(args[index + 1])
            if sid is not None:
                result.add(sid)
            continue
        for prefix in (b"--resume=", b"--session-id="):
            if arg.startswith(prefix):
                sid = sid_by_arg.get(arg[len(prefix):])
                if sid is not None:
                    result.add(sid)
    return result


def claude_session_holders(
    paths: Mapping[str, str],
    cwds: Mapping[str, str],
    *,
    wrapper_pid: int,
    proc_root: str = "/proc",
) -> HolderScan:
    """Return stable external Claude process identities for watched sessions.

    Direct children of ``wrapper_pid`` are the SDK processes owned by this
    wrapper and are excluded.  A foreign process with an explicit session flag
    owns only that session.  A foreign Claude process without a session id owns
    every watched session sharing its cwd; this deliberate false-positive is
    safer than allowing two Claude processes to append to one transcript.
    """
    holders = {sid: set() for sid in paths}
    root = Path(proc_root)
    sid_by_arg = {sid.encode(): sid for sid in paths}
    cwd_sids: dict[str, set[str]] = {}
    for sid in paths:
        cwd = cwds.get(sid)
        if not cwd:
            continue
        cwd_sids.setdefault(os.path.realpath(cwd), set()).add(sid)
    missing_cwds = set(paths).difference(
        sid for sids in cwd_sids.values() for sid in sids)

    complete = True
    try:
        processes = (entry for entry in root.iterdir() if entry.name.isdigit())
        for index, proc_dir in enumerate(processes):
            if index >= MAX_PROC_SCAN:
                complete = False
                break
            process_stat = _process_stat(proc_dir)
            if process_stat is None:
                continue
            parent_pid, start_ticks, _tty_nr = process_stat
            args = _process_cmdline(proc_dir)
            if args is None:
                # A disappearing process is harmless. A stable process whose
                # command line is unreadable makes the ownership scan incomplete.
                if _process_start_ticks(proc_dir) == start_ticks:
                    complete = False
                continue
            if not _is_claude_cli(args):
                continue
            if parent_pid == wrapper_pid:
                continue

            matched = _explicit_session_ids(args, sid_by_arg)
            if not matched:
                try:
                    process_cwd = os.path.realpath(os.readlink(proc_dir / "cwd"))
                except OSError:
                    if _process_start_ticks(proc_dir) == start_ticks:
                        complete = False
                    continue
                matched.update(cwd_sids.get(process_cwd, ()))
            if not matched:
                if missing_cwds:
                    complete = False
                continue
            if _process_start_ticks(proc_dir) != start_ticks:
                continue
            identity = ProcessIdentity(int(proc_dir.name), start_ticks)
            for sid in matched:
                holders[sid].add(identity)
    except OSError:
        return HolderScan(holders, False)
    return HolderScan(holders, complete)
