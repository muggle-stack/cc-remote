"""Detect Claude sessions owned by another local Claude Code process.

An idle Claude TUI does not keep its transcript open, so transcript growth is
not a stable ownership signal.  Prefer an explicit session id from the process
command line.  Fall back to the working directory only when it identifies one
watched session unambiguously.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Collection, Literal, Mapping

from cc_remote.wrapper.codex_external import (
    HolderScan,
)
from cc_remote.wrapper.process_scan import (
    MAX_PROC_SCAN,
    ProcessIdentity,
    _process_cmdline,
    _process_start_ticks,
    _process_stat,
    darwin_process_snapshot,
    process_command_environment_value,
    process_identity,
)


_CLAUDE_COMMANDS = frozenset({"claude", "claude.exe"})
_SESSION_FLAGS = frozenset({b"--resume", b"-r", b"--session-id"})
_CONTINUE_FLAGS = frozenset({b"--continue", b"-c"})
_BACKGROUND_ROLES = frozenset({
    b"daemon", b"bg-pty-host", b"bg-spare", b"--bg-pty-host", b"--bg-spare",
})

_SDK_ENTRYPOINTS = frozenset({"sdk-py"})
_SDK_PROMPT_SOURCES = frozenset({"sdk"})
_NEUTRAL_METADATA_TYPES = frozenset({
    "ai-title",
    "atis-latch",
    "mode",
    "permission-mode",
    "queue-operation",
})
_MAX_DARWIN_CLAUDE_CANDIDATES = 256


def _profile_scoped_process(
    identity: ProcessIdentity,
    args: tuple[bytes, ...],
    paths: Mapping[str, str],
    *,
    config_dirs: Mapping[str, str] | None,
    default_config_dir: str | None,
    process_cwd: str | None,
    proc_root: str = "/proc",
) -> tuple[bool, tuple[bytes, ...], set[str], str | None]:
    """Bind one exact Claude CLI process to one config-root namespace.

    The legacy single-account scanner deliberately skips environment reads.
    Once explicit profiles are configured, an unreadable or malformed
    ``CLAUDE_CONFIG_DIR`` is an incomplete ownership observation: assigning an
    identical native UUID to the wrong account would weaken the read-only
    boundary.
    """
    if config_dirs is None:
        return True, args, set(paths), None
    complete, exact_args, configured = process_command_environment_value(
        identity, "CLAUDE_CONFIG_DIR", proc_root=proc_root)
    if not complete or exact_args is None:
        return False, args, set(), None
    if not _is_claude_cli(exact_args):
        return True, exact_args, set(), None
    raw_root = configured if configured is not None else default_config_dir
    if (
        not isinstance(raw_root, str)
        or not raw_root
        or "\x00" in raw_root
        or len(os.fsencode(raw_root)) > 4096
    ):
        return False, exact_args, set(), None
    if not os.path.isabs(raw_root):
        if not process_cwd:
            return False, exact_args, set(), None
        raw_root = os.path.join(process_cwd, raw_root)
    root = os.path.realpath(raw_root)
    scoped = {
        sid for sid in paths
        if os.path.realpath(config_dirs[sid]) == root
    }
    return True, exact_args, scoped, root


def _scoped_sid_by_arg(
    sids: Collection[str],
    native_session_ids: Mapping[str, str] | None,
) -> dict[bytes, str]:
    return {
        (native_session_ids[sid] if native_session_ids is not None else sid).encode(): sid
        for sid in sids
    }


def _resolve_continue_target(
    resolver: Callable[..., str | None],
    cwd: str,
    config_root: str | None,
    *,
    profile_scoped: bool,
) -> str | None:
    if profile_scoped:
        return resolver(cwd, config_root)
    return resolver(cwd)


def classify_claude_growth(
    data: bytes,
    owned_message_ids: Collection[str] = (),
) -> tuple[Literal["sdk", "external", "unknown"], tuple[str, ...]]:
    """Attribute complete Claude JSONL growth without a time heuristic.

    Agent SDK transcript rows carry ``entrypoint=sdk-py`` (and user rows also
    carry ``promptSource=sdk``), while native TUI rows carry ``entrypoint=cli``.
    A few metadata rows have no direct origin; ``last-prompt`` and file-history
    rows can still be attributed through the message UUID they reference.

    Unknown or partial data deliberately remains unknown so the machine can
    fail closed unless an SDK operation is actively writing. An explicit
    foreign entrypoint always wins, even during an SDK operation.
    """
    if not data or not data.endswith(b"\n"):
        return "unknown", ()

    rows: list[dict] = []
    try:
        for raw_line in data.splitlines():
            if not raw_line.strip():
                continue
            value = json.loads(raw_line)
            if not isinstance(value, dict):
                return "unknown", ()
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unknown", ()
    if not rows:
        return "unknown", ()

    known_owned = {
        value for value in owned_message_ids
        if isinstance(value, str) and value
    }
    new_owned: list[str] = []
    sdk_evidence = False
    external_evidence = False
    unresolved_rows: list[dict] = []

    for row in rows:
        entrypoint = row.get("entrypoint")
        prompt_source = row.get("promptSource")
        if (isinstance(entrypoint, str) and entrypoint
                and entrypoint not in _SDK_ENTRYPOINTS):
            external_evidence = True
            continue
        if (isinstance(prompt_source, str) and prompt_source
                and prompt_source not in _SDK_PROMPT_SOURCES
                and not (entrypoint in _SDK_ENTRYPOINTS and prompt_source == "system")):
            external_evidence = True
            continue
        if entrypoint in _SDK_ENTRYPOINTS or prompt_source in _SDK_PROMPT_SOURCES:
            sdk_evidence = True
            message_id = row.get("uuid")
            if isinstance(message_id, str) and message_id:
                known_owned.add(message_id)
                new_owned.append(message_id)
            continue
        unresolved_rows.append(row)

    # Resolve origin-less metadata after collecting SDK ids from the entire
    # chunk: file snapshots can precede/follow their referenced user row.
    for row in unresolved_rows:
        row_type = str(row.get("type") or "")
        if row_type == "last-prompt":
            reference = row.get("leafUuid")
        elif row_type == "file-history-snapshot":
            reference = row.get("messageId")
        elif row_type in _NEUTRAL_METADATA_TYPES:
            continue
        else:
            return "external" if external_evidence else "unknown", ()
        if isinstance(reference, str) and reference in known_owned:
            sdk_evidence = True
        else:
            return "external" if external_evidence else "unknown", ()

    if external_evidence:
        return "external", ()
    if sdk_evidence:
        # Preserve insertion order while avoiding unbounded duplicate ids.
        return "sdk", tuple(dict.fromkeys(new_owned))
    return "unknown", ()


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
) -> tuple[set[str], bool]:
    result: set[str] = set()
    has_explicit_target = False
    for index, arg in enumerate(args):
        if arg in _SESSION_FLAGS:
            if index + 1 < len(args):
                target = args[index + 1]
                if target and not target.startswith(b"-"):
                    has_explicit_target = True
                    sid = sid_by_arg.get(target)
                    if sid is not None:
                        result.add(sid)
            continue
        for prefix in (b"--resume=", b"--session-id="):
            if arg.startswith(prefix):
                target = arg[len(prefix):]
                if not target:
                    continue
                has_explicit_target = True
                sid = sid_by_arg.get(target)
                if sid is not None:
                    result.add(sid)
    return result, has_explicit_target


def _is_continue(args: tuple[bytes, ...]) -> bool:
    """Return whether this CLI asks Claude to continue the cwd's latest chat."""
    return any(
        arg in _CONTINUE_FLAGS or arg.startswith(b"--continue=")
        for arg in args
    )


def _is_descendant(
    pid: int,
    parent_pid: int,
    wrapper_pid: int,
    parent_by_pid: Mapping[int, int] | None = None,
    proc_root: Path | None = None,
    owned_roots: Collection[int] = (),
) -> bool:
    """Return whether pid belongs to the wrapper's complete SDK process tree."""
    seen = {pid}
    current = parent_pid
    for _ in range(64):
        if current == wrapper_pid or current in owned_roots:
            return True
        if current <= 1 or current in seen:
            return False
        seen.add(current)
        if parent_by_pid is not None:
            next_parent = parent_by_pid.get(current)
        elif proc_root is not None:
            stat = _process_stat(proc_root / str(current))
            next_parent = stat[0] if stat is not None else None
        else:
            next_parent = None
        if next_parent is None:
            return False
        current = next_parent
    return False


def _darwin_process_cwds(
    pids: Collection[int],
) -> tuple[dict[int, str], bool]:
    if not pids:
        return {}, True
    try:
        completed = subprocess.run(
            [
                "/usr/sbin/lsof", "-n", "-P", "-w", "-a",
                "-p", ",".join(str(pid) for pid in sorted(pids)),
                "-d", "cwd", "-Fpn",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return {}, False
    if completed.returncode not in (0, 1):
        return {}, False
    result: dict[int, str] = {}
    pid: int | None = None
    for line in completed.stdout.splitlines():
        if not line:
            continue
        if line[0] == "p":
            try:
                pid = int(line[1:])
            except ValueError:
                return {}, False
        elif line[0] == "n" and pid is not None:
            result[pid] = os.path.realpath(line[1:])
    return result, True


def _darwin_claude_session_holders(
    paths: Mapping[str, str],
    cwds: Mapping[str, str],
    *,
    wrapper_pid: int,
    continue_bindings: dict[ProcessIdentity, str],
    continue_candidates: dict[ProcessIdentity, str],
    continue_resolver: Callable[..., str | None] | None,
    config_dirs: Mapping[str, str] | None,
    native_session_ids: Mapping[str, str] | None,
    default_config_dir: str | None,
    owner_identities: Collection[ProcessIdentity] = (),
) -> HolderScan:
    holders = {sid: set() for sid in paths}
    snapshot, complete = darwin_process_snapshot()
    if not complete:
        return HolderScan(holders, False)
    parent_by_pid = {info[0].pid: info[1] for info in snapshot}
    owned_roots = {info[0].pid for info in snapshot if info[0] in owner_identities}
    candidates = [
        info for info in snapshot
        if _is_claude_cli(info[3])
        and not _is_descendant(
            info[0].pid, info[1], wrapper_pid, parent_by_pid=parent_by_pid,
            owned_roots=owned_roots)
    ]
    if len(candidates) > _MAX_DARWIN_CLAUDE_CANDIDATES:
        return HolderScan(holders, False)

    process_cwds, cwd_scan_complete = _darwin_process_cwds(
        [info[0].pid for info in candidates])
    cwd_sids: dict[str, set[str]] = {}
    for sid in paths:
        cwd = cwds.get(sid)
        if cwd:
            cwd_sids.setdefault(os.path.realpath(cwd), set()).add(sid)
    missing_cwds = set(paths).difference(
        sid for sids in cwd_sids.values() for sid in sids)
    seen_continue: set[ProcessIdentity] = set()

    for identity, _parent_pid, _tty_nr, snapshot_args in candidates:
        process_cwd = process_cwds.get(identity.pid)
        account_complete, args, account_sids, config_root = (
            _profile_scoped_process(
                identity,
                snapshot_args,
                paths,
                config_dirs=config_dirs,
                default_config_dir=default_config_dir,
                process_cwd=process_cwd,
            )
        )
        if not account_complete:
            complete = False
            continue
        if not account_sids:
            continue
        sid_by_arg = _scoped_sid_by_arg(
            account_sids, native_session_ids)
        matched, has_explicit_session = _explicit_session_ids(
            args, sid_by_arg)
        continue_command = (
            not matched and not has_explicit_session and _is_continue(args))
        if continue_command:
            seen_continue.add(identity)
            bound_sid = continue_bindings.get(identity)
            if bound_sid is None:
                bound_sid = continue_candidates.get(identity)
            if bound_sid is None:
                if process_cwd is None or not cwd_scan_complete:
                    complete = False
                    continue
                if continue_resolver is None:
                    complete = False
                    continue
                try:
                    bound_sid = _resolve_continue_target(
                        continue_resolver,
                        process_cwd,
                        config_root,
                        profile_scoped=config_dirs is not None,
                    )
                except Exception:
                    complete = False
                    continue
                if bound_sid is None:
                    complete = False
                    continue
                continue_candidates[identity] = bound_sid
            if bound_sid in account_sids:
                continue_bindings[identity] = bound_sid
                matched.add(bound_sid)
        elif not matched and not has_explicit_session:
            if process_cwd is None:
                if not cwd_scan_complete or process_identity(identity.pid) == identity:
                    complete = False
                continue
            cwd_matches = set(cwd_sids.get(process_cwd, ())).intersection(
                account_sids)
            if len(cwd_matches) == 1:
                matched.update(cwd_matches)

        if not matched:
            if (missing_cwds.intersection(account_sids)
                    and not has_explicit_session):
                complete = False
            continue
        if process_identity(identity.pid) != identity:
            continue_bindings.pop(identity, None)
            continue_candidates.pop(identity, None)
            complete = False
            continue
        for sid in matched:
            holders[sid].add(identity)

    if complete:
        for identity in set(continue_bindings).difference(seen_continue):
            continue_bindings.pop(identity, None)
        for identity in set(continue_candidates).difference(seen_continue):
            continue_candidates.pop(identity, None)
    return HolderScan(holders, complete)


def claude_session_holders(
    paths: Mapping[str, str],
    cwds: Mapping[str, str],
    *,
    wrapper_pid: int,
    proc_root: str = "/proc",
    continue_bindings: dict[ProcessIdentity, str] | None = None,
    continue_candidates: dict[ProcessIdentity, str] | None = None,
    continue_resolver: Callable[..., str | None] | None = None,
    config_dirs: Mapping[str, str] | None = None,
    native_session_ids: Mapping[str, str] | None = None,
    default_config_dir: str | None = None,
    owner_identities: Collection[ProcessIdentity] = (),
) -> HolderScan:
    """Return stable external Claude process identities for watched sessions.

    Descendants of ``wrapper_pid`` are SDK processes owned by this wrapper and
    are excluded.  A foreign process with an explicit session flag
    owns only that session.  A foreign Claude process without a session id is
    associated by cwd only when exactly one watched session uses that cwd.
    Ambiguous same-cwd processes must not make every sibling session read-only.
    """
    holders = {sid: set() for sid in paths}
    if config_dirs is not None and set(config_dirs) != set(paths):
        return HolderScan(holders, False)
    if (
        native_session_ids is not None
        and set(native_session_ids) != set(paths)
    ):
        return HolderScan(holders, False)
    root = Path(proc_root)
    bindings = continue_bindings if continue_bindings is not None else {}
    candidates = (
        continue_candidates if continue_candidates is not None else {})
    if sys.platform == "darwin" and proc_root == "/proc":
        return _darwin_claude_session_holders(
            paths,
            cwds,
            wrapper_pid=wrapper_pid,
            continue_bindings=bindings,
            continue_candidates=candidates,
            continue_resolver=continue_resolver,
            config_dirs=config_dirs,
            native_session_ids=native_session_ids,
            default_config_dir=default_config_dir,
            owner_identities=owner_identities,
        )
    owned_roots = {
        identity.pid for identity in owner_identities
        if process_identity(identity.pid, proc_root=proc_root) == identity
    }
    cwd_sids: dict[str, set[str]] = {}
    for sid in paths:
        cwd = cwds.get(sid)
        if not cwd:
            continue
        cwd_sids.setdefault(os.path.realpath(cwd), set()).add(sid)
    missing_cwds = set(paths).difference(
        sid for sids in cwd_sids.values() for sid in sids)
    seen_continue: set[ProcessIdentity] = set()

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
            if _is_descendant(
                    int(proc_dir.name), parent_pid, wrapper_pid,
                    proc_root=root, owned_roots=owned_roots):
                continue
            identity = ProcessIdentity(int(proc_dir.name), start_ticks)
            process_cwd: str | None = None
            if config_dirs is not None:
                try:
                    process_cwd = os.path.realpath(
                        os.readlink(proc_dir / "cwd"))
                except OSError:
                    if _process_start_ticks(proc_dir) == start_ticks:
                        complete = False
                    continue
            account_complete, args, account_sids, config_root = (
                _profile_scoped_process(
                    identity,
                    args,
                    paths,
                    config_dirs=config_dirs,
                    default_config_dir=default_config_dir,
                    process_cwd=process_cwd,
                    proc_root=proc_root,
                )
            )
            if not account_complete:
                complete = False
                continue
            if not account_sids:
                continue
            sid_by_arg = _scoped_sid_by_arg(
                account_sids, native_session_ids)
            matched, has_explicit_session = _explicit_session_ids(
                args, sid_by_arg)
            continue_command = (
                not matched
                and not has_explicit_session
                and _is_continue(args)
            )
            if continue_command:
                seen_continue.add(identity)
                if identity in bindings:
                    bound_sid = bindings[identity]
                else:
                    if identity in candidates:
                        bound_sid = candidates[identity]
                    else:
                        if process_cwd is None:
                            try:
                                process_cwd = os.path.realpath(
                                    os.readlink(proc_dir / "cwd"))
                            except OSError:
                                if _process_start_ticks(proc_dir) == start_ticks:
                                    complete = False
                                continue
                        if continue_resolver is None:
                            # The watched subset cannot prove Claude's cwd-global
                            # "latest" target. Treat missing catalog authority as
                            # incomplete, never as the sole watched sid.
                            complete = False
                            continue
                        try:
                            bound_sid = _resolve_continue_target(
                                continue_resolver,
                                process_cwd,
                                config_root,
                                profile_scoped=config_dirs is not None,
                            )
                        except Exception:
                            complete = False
                            continue
                        if bound_sid is None:
                            # A live `-c` process should have selected a native
                            # session. An empty/racing catalog is not proof that
                            # it owns none of the watched sessions; retry on the
                            # next scan while remaining fail-closed now.
                            complete = False
                            continue
                        # Cache the native startup selection even before Remote
                        # watches it, but do not call that an ownership binding.
                        # When the exact sid enters `paths`, promote it below.
                        candidates[identity] = bound_sid
                if bound_sid in account_sids:
                    bindings[identity] = bound_sid
                    matched.add(bound_sid)
            if (not matched and not has_explicit_session
                    and not continue_command):
                if process_cwd is None:
                    try:
                        process_cwd = os.path.realpath(
                            os.readlink(proc_dir / "cwd"))
                    except OSError:
                        if _process_start_ticks(proc_dir) == start_ticks:
                            complete = False
                        continue
                cwd_matches = set(cwd_sids.get(
                    process_cwd, ())).intersection(account_sids)
                if len(cwd_matches) == 1:
                    matched.update(cwd_matches)
            if not matched:
                if missing_cwds.intersection(account_sids):
                    complete = False
                continue
            if _process_start_ticks(proc_dir) != start_ticks:
                bindings.pop(identity, None)
                candidates.pop(identity, None)
                continue
            for sid in matched:
                holders[sid].add(identity)
    except OSError:
        return HolderScan(holders, False)
    if complete:
        for identity in set(bindings).difference(seen_continue):
            bindings.pop(identity, None)
        for identity in set(candidates).difference(seen_continue):
            candidates.pop(identity, None)
    return HolderScan(holders, complete)
