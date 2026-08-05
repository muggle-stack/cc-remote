"""Coordinate an intentional Codex daemon restart with cc-remote.

The restart marker is a local generation barrier, not an authentication store.
It lets resident cc-remote sessions interrupt an already-running turn on the
old daemon and continue the same logical task on the replacement without
unlocking queued browser messages.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Literal, Optional
from uuid import uuid4

from cc_remote.wrapper.file_lock_compat import LOCK_EX, LOCK_UN, flock
from cc_remote.wrapper.os_compat import fchmod


_SCHEMA_VERSION = 2
_FILENAME = "codex-daemon-restart.json"
_MAX_STATE_BYTES = 4096
_LOG_FILENAME = "codex-daemon-restart.log"
_DEFAULT_SYNC_TIMEOUT = 60.0
_DEFAULT_WORKER_TIMEOUT = 60.0
_DEFAULT_DRAIN_GRACE = 2.0
_OUTCOME_GRACE = 5.0
_FAILED_STATE_RETENTION = 15.0
RestartPhase = Literal["restarting", "ready", "failed"]


@dataclass(frozen=True)
class CodexDaemonRestartState:
    epoch: str
    phase: RestartPhase
    updated_at: float
    deadline_at: float


def restart_outcome_timeout(
    timeout: Optional[float] = _DEFAULT_WORKER_TIMEOUT,
    *,
    drain_grace: float = _DEFAULT_DRAIN_GRACE,
) -> float:
    """Return the bounded marker lifetime shared by worker and wrapper."""
    worker_timeout = (
        _DEFAULT_WORKER_TIMEOUT
        if timeout is None or timeout <= 0
        else max(1.0, float(timeout))
    )
    return max(0.0, float(drain_grace)) + worker_timeout + _OUTCOME_GRACE


def restart_state_is_stale(
    state: CodexDaemonRestartState,
    *,
    now: Optional[float] = None,
) -> bool:
    """Return whether a non-ready outcome has exceeded its explicit deadline."""
    if state.phase == "ready":
        return False
    current = time.time() if now is None else float(now)
    return current >= state.deadline_at


def restart_state_path(state_dir: str | Path | None = None) -> Path:
    root = (
        Path(state_dir)
        if state_dir is not None
        else Path(os.environ.get(
            "CC_REMOTE_STATE_DIR", str(Path.home() / ".cc-remote")))
    )
    return root.expanduser() / _FILENAME


def read_restart_state(path: str | Path) -> Optional[CodexDaemonRestartState]:
    target = Path(path)
    try:
        if target.stat().st_size > _MAX_STATE_BYTES:
            return None
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") not in {1, _SCHEMA_VERSION}:
        return None
    epoch = raw.get("epoch")
    phase = raw.get("phase")
    updated_at = raw.get("updated_at")
    deadline_at = raw.get("deadline_at")
    if (
        not isinstance(epoch, str)
        or len(epoch) != 32
        or any(ch not in "0123456789abcdef" for ch in epoch)
        or phase not in {"restarting", "ready", "failed"}
        or not isinstance(updated_at, (int, float))
        or isinstance(updated_at, bool)
    ):
        return None
    if raw.get("version") == 1:
        deadline_at = float(updated_at) + restart_outcome_timeout()
    if (
        not isinstance(deadline_at, (int, float))
        or isinstance(deadline_at, bool)
        or float(deadline_at) < float(updated_at)
    ):
        return None
    return CodexDaemonRestartState(
        epoch=epoch,
        phase=phase,
        updated_at=float(updated_at),
        deadline_at=float(deadline_at),
    )


def write_restart_state(
    path: str | Path,
    *,
    epoch: str,
    phase: RestartPhase,
    deadline_at: Optional[float] = None,
) -> CodexDaemonRestartState:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    updated_at = time.time()
    if deadline_at is None:
        deadline_at = (
            updated_at + _FAILED_STATE_RETENTION
            if phase == "failed"
            else updated_at + restart_outcome_timeout()
        )
    state = CodexDaemonRestartState(
        epoch=epoch,
        phase=phase,
        updated_at=updated_at,
        deadline_at=max(updated_at, float(deadline_at)),
    )
    payload = json.dumps(
        {
            "version": _SCHEMA_VERSION,
            "epoch": state.epoch,
            "phase": state.phase,
            "updated_at": state.updated_at,
            "deadline_at": state.deadline_at,
        },
        separators=(",", ":"),
    ) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent)
    try:
        fchmod(fd, temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return state


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fchmod(fd, path, 0o600)
        flock(fd, LOCK_EX)
        yield
    finally:
        flock(fd, LOCK_UN)
        os.close(fd)


def _state_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _worker_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.worker.lock")


def _publish_if_current(
    path: Path,
    *,
    epoch: str,
    phase: RestartPhase,
) -> bool:
    """Publish a worker outcome without overwriting a newer switch request."""
    with _exclusive_lock(_state_lock_path(path)):
        current = read_restart_state(path)
        if current is None or current.epoch != epoch:
            return False
        write_restart_state(
            path,
            epoch=epoch,
            phase=phase,
            deadline_at=(
                time.time() + _FAILED_STATE_RETENTION
                if phase == "failed"
                else current.deadline_at
            ),
        )
        return True


def _codex_binary(explicit: Optional[str]) -> str:
    def resolve(value: str) -> str:
        expanded = os.path.expanduser(value)
        if os.path.isabs(expanded) or os.sep in expanded:
            return os.path.realpath(expanded)
        return shutil.which(expanded) or expanded

    if explicit:
        return resolve(explicit)
    configured = os.environ.get("CODEX_BIN", "").strip()
    if configured:
        return resolve(configured)
    standalone = Path.home() / ".local" / "bin" / "codex"
    if standalone.exists():
        return str(standalone)
    return shutil.which("codex") or "codex"


def _run_official_restart(
    codex_bin: str,
    *,
    timeout: Optional[float],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [codex_bin, "app-server", "daemon", "restart"],
        text=True,
        capture_output=True,
        timeout=(
            None if timeout is None or timeout <= 0
            else max(1.0, float(timeout))
        ),
        check=False,
    )


def restart_managed_daemon(
    *,
    codex_bin: Optional[str] = None,
    state_dir: str | Path | None = None,
    timeout: Optional[float] = _DEFAULT_SYNC_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Publish a barrier, run the official restart, then publish its outcome."""
    restart_timeout = (
        _DEFAULT_SYNC_TIMEOUT
        if timeout is None or timeout <= 0
        else max(1.0, float(timeout))
    )
    epoch = uuid4().hex
    path = restart_state_path(state_dir)
    deadline_at = time.time() + restart_outcome_timeout(
        restart_timeout, drain_grace=0.0)
    with _exclusive_lock(_state_lock_path(path)):
        write_restart_state(
            path,
            epoch=epoch,
            phase="restarting",
            deadline_at=deadline_at,
        )
    try:
        with _exclusive_lock(_worker_lock_path(path)):
            result = _run_official_restart(
                _codex_binary(codex_bin),
                timeout=restart_timeout,
            )
    except BaseException:
        _publish_if_current(path, epoch=epoch, phase="failed")
        raise
    _publish_if_current(
        path,
        epoch=epoch,
        phase="ready" if result.returncode == 0 else "failed",
    )
    return result


def _scheduled_worker(
    *,
    epoch: str,
    codex_bin: Optional[str],
    state_dir: str | Path | None,
    timeout: Optional[float],
    drain_grace: float = _DEFAULT_DRAIN_GRACE,
) -> int:
    """Run a queued restart after Remote turns have entered their drain.

    Publishing the generation marker and starting the official graceful restart
    back-to-back races cc-remote's 50 ms watchers: the daemon can enter restart
    drain before every resident proxy has submitted ``turn/interrupt``.  Those
    rejected interrupts then keep the same graceful restart alive forever.
    Leave a small bounded quiesce window after the marker, and revalidate the
    epoch afterward so a superseded switch never restarts the daemon.
    """
    restart_timeout = (
        _DEFAULT_WORKER_TIMEOUT
        if timeout is None or timeout <= 0
        else max(1.0, float(timeout))
    )
    path = restart_state_path(state_dir)
    with _exclusive_lock(_worker_lock_path(path)):
        current = read_restart_state(path)
        if (
            current is None
            or current.epoch != epoch
            or current.phase != "restarting"
        ):
            return 0
        if restart_state_is_stale(current):
            _publish_if_current(path, epoch=epoch, phase="failed")
            return 124
        if drain_grace > 0:
            time.sleep(drain_grace)
        current = read_restart_state(path)
        if (
            current is None
            or current.epoch != epoch
            or current.phase != "restarting"
        ):
            return 0
        if restart_state_is_stale(current):
            _publish_if_current(path, epoch=epoch, phase="failed")
            return 124
        try:
            result = _run_official_restart(
                _codex_binary(codex_bin),
                timeout=restart_timeout,
            )
        except BaseException:
            _publish_if_current(path, epoch=epoch, phase="failed")
            raise
        _publish_if_current(
            path,
            epoch=epoch,
            phase="ready" if result.returncode == 0 else "failed",
        )
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return result.returncode


def schedule_managed_daemon_restart(
    *,
    codex_bin: Optional[str] = None,
    state_dir: str | Path | None = None,
    timeout: Optional[float] = _DEFAULT_WORKER_TIMEOUT,
) -> str:
    """Publish the barrier and detach the potentially long graceful restart.

    The official daemon waits for accepted turns to finish. cc-remote interrupts
    its own managed turns after observing the marker, but native clients may
    still be active; a post-switch hook must therefore return after scheduling
    rather than treating graceful drain time as a failed account switch.
    """
    worker_timeout = (
        _DEFAULT_WORKER_TIMEOUT
        if timeout is None or timeout <= 0
        else max(1.0, float(timeout))
    )
    epoch = uuid4().hex
    path = restart_state_path(state_dir)
    deadline_at = time.time() + restart_outcome_timeout(
        worker_timeout,
        drain_grace=_DEFAULT_DRAIN_GRACE,
    )
    with _exclusive_lock(_state_lock_path(path)):
        write_restart_state(
            path,
            epoch=epoch,
            phase="restarting",
            deadline_at=deadline_at,
        )

    argv = [
        sys.executable,
        "-m",
        "cc_remote.codex_daemon_restart",
        "--worker",
        epoch,
        "--timeout",
        str(worker_timeout),
        "--drain-grace",
        str(_DEFAULT_DRAIN_GRACE),
    ]
    if codex_bin:
        argv.extend(("--codex-bin", codex_bin))
    if state_dir is not None:
        argv.extend(("--state-dir", str(state_dir)))

    log_path = path.with_name(_LOG_FILENAME)
    log_fd = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        fchmod(log_fd, log_path, 0o600)
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        _publish_if_current(path, epoch=epoch, phase="failed")
        raise
    finally:
        os.close(log_fd)
    return epoch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cc_remote.codex_daemon_restart",
        description=(
            "Restart the official Codex daemon and notify cc-remote resident "
            "sessions so active turns can move to the new generation."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--detach",
        action="store_true",
        help="schedule the graceful restart and return immediately",
    )
    mode.add_argument("--worker", metavar="EPOCH", help=argparse.SUPPRESS)
    parser.add_argument("--codex-bin")
    parser.add_argument("--state-dir")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--drain-grace", type=float)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        try:
            return _scheduled_worker(
                epoch=args.worker,
                codex_bin=args.codex_bin,
                state_dir=args.state_dir,
                timeout=(
                    _DEFAULT_WORKER_TIMEOUT
                    if args.timeout is None or args.timeout <= 0
                    else args.timeout
                ),
                drain_grace=(
                    _DEFAULT_DRAIN_GRACE
                    if args.drain_grace is None
                    else max(0.0, args.drain_grace)
                ),
            )
        except Exception as exc:
            print(
                f"Codex daemon restart worker failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
    if args.detach:
        try:
            schedule_managed_daemon_restart(
                codex_bin=args.codex_bin,
                state_dir=args.state_dir,
                timeout=(
                    _DEFAULT_WORKER_TIMEOUT
                    if args.timeout is None or args.timeout <= 0
                    else args.timeout
                ),
            )
        except Exception as exc:
            print(
                f"Codex daemon restart scheduling failed: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1
        return 0
    try:
        result = restart_managed_daemon(
            codex_bin=args.codex_bin,
            state_dir=args.state_dir,
            timeout=(
                _DEFAULT_SYNC_TIMEOUT
                if args.timeout is None
                else None if args.timeout <= 0
                else args.timeout
            ),
        )
    except subprocess.TimeoutExpired:
        print("Codex daemon restart timed out", file=sys.stderr)
        return 124
    except Exception as exc:
        print(
            f"Codex daemon restart failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
