"""Shared Codex app-server daemon discovery and lifecycle helpers.

The official daemon is process-global while each client connection is a short
``codex app-server proxy`` process.  This module owns only the former.  A
``CodexHandle`` continues to own (and terminate) its proxy or legacy stdio
process, so disconnecting one remote session cannot stop other Codex clients.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import resource
import signal
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from cc_remote.log import logger
from cc_remote.wrapper.process_scan import (
    _darwin_process_info,
    ProcessIdentity,
    process_identity,
    process_owner_uid,
)

log = logger("cc_remote.wrapper.codex_daemon")

_DAEMON_ENV = "CC_REMOTE_CODEX_DAEMON"
_DAEMON_MODES = frozenset({"auto", "off"})
_COMMAND_TIMEOUT = 30.0
_OUTPUT_MAX = 64 * 1024
_PID_RECORD_MAX = 4096
_STALE_UPDATER_EXIT_TIMEOUT = 3.0
_DAEMON_UPGRADE_SETTLE_TIMEOUT = 5.0
_DAEMON_UPGRADE_POLL_INTERVAL = 0.1
_DAEMON_NOFILE_SOFT_LIMIT = 4096
_RLIMIT_EXEC = str(Path(__file__).with_name("rlimit_exec.py"))
_PROC_ROOT = Path("/proc")
_HIGH_NOFILE_DAEMON_OPERATIONS = frozenset({
    "bootstrap", "enable-remote-control", "restart", "start",
})


def codex_daemon_mode(value: Optional[str] = None) -> str:
    """Return ``auto`` or ``off``; invalid configuration preserves stdio.

    Falling back to ``off`` for an invalid value is intentional.  A typo must
    not make the wrapper claim that it is attached to the shared daemon.
    """
    raw = value if value is not None else os.environ.get(_DAEMON_ENV, "auto")
    mode = raw.strip().lower() if isinstance(raw, str) else ""
    if mode in _DAEMON_MODES:
        return mode
    log.warning("invalid Codex daemon mode; using stdio", value=str(raw)[:64])
    return "off"


@dataclass(frozen=True)
class CodexDaemonInfo:
    socket_path: Optional[str]
    verified_remote_control: bool = False
    nofile_verified: bool = False


@dataclass(frozen=True)
class CodexSocketIdentity:
    """A profile-scoped standalone listener generation, not a daemon PID."""

    codex_home: str
    socket_path: str
    device: int
    inode: int
    created_ns: int


CodexServerIdentity = ProcessIdentity | CodexSocketIdentity


class CodexDaemonUpgradeRequired(RuntimeError):
    """The managed shared daemon could not be aligned with the selected CLI."""


class CodexProfileDaemonUnavailable(CodexDaemonUpgradeRequired):
    """A configured account profile has no usable shared control plane."""


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_command(
    argv: tuple[str, ...], env: Mapping[str, str], timeout: float,
) -> _CommandResult:
    """Blocking subprocess boundary, kept separate for deterministic tests."""
    command_argv = argv
    if os.name == "posix" and any(
        argv[index:index + 2] == ("app-server", "daemon")
        and argv[index + 2] in _HIGH_NOFILE_DAEMON_OPERATIONS
        for index in range(max(0, len(argv) - 2))
    ):
        # The official lifecycle command launches the durable daemon.  Its
        # resource limits are inherited at that boundary, so raising only this
        # short-lived child also raises the managed daemon without changing the
        # wrapper or unrelated user processes.
        command_argv = (
            sys.executable,
            _RLIMIT_EXEC,
            str(_DAEMON_NOFILE_SOFT_LIMIT),
            *argv,
        )
    try:
        result = subprocess.run(
            command_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Exception text can contain paths or command details.  The manager's
        # caller needs only a bounded failure class to select stdio fallback.
        return _CommandResult(127, b"", type(exc).__name__.encode("ascii"))
    return _CommandResult(
        result.returncode,
        bytes(result.stdout or b"")[:_OUTPUT_MAX],
        bytes(result.stderr or b"")[:_OUTPUT_MAX],
    )


def _prepare_profile_standalone(
    codex_bin: str,
    env: Mapping[str, str],
) -> Optional[bool]:
    """Expose one verified managed CLI payload inside a sibling CODEX_HOME.

    Official daemon bootstrap looks for
    ``$CODEX_HOME/packages/standalone/current/codex``. A second account made
    with ``CODEX_HOME=... codex login`` owns valid auth and session state, but
    normally still executes the primary home's global ``~/.local/bin/codex``;
    it therefore has no profile-local managed path for bootstrap.

    Share only the managed CLI's ``current`` entry. Daemon sockets, auth,
    config, rollouts, and all other state stay rooted in the sibling
    ``CODEX_HOME``. Refuse ambiguous layouts instead of replacing anything the
    user already installed.
    """
    raw_home = env.get("CODEX_HOME")
    if not isinstance(raw_home, str) or not raw_home:
        return None
    profile_home = Path(os.path.realpath(os.path.expanduser(raw_home)))
    if not profile_home.is_absolute():
        return None
    try:
        binary = Path(codex_bin).expanduser().resolve(strict=True)
    except OSError:
        return None
    # Accept only the official standalone release layout. A package-manager
    # binary or arbitrary executable must not become a trusted daemon payload.
    if (
        binary.parent.name != "bin"
        or binary.parent.parent.parent.name != "releases"
        or binary.parent.parent.parent.parent.name != "standalone"
    ):
        return None
    standalone_root = binary.parent.parent.parent.parent
    source_current = standalone_root / "current"
    try:
        source_binary = (source_current / "codex").resolve(strict=True)
        source_stat = standalone_root.stat()
        home_stat = profile_home.stat()
    except OSError:
        return None
    if (
        source_binary != binary
        or not stat.S_ISDIR(source_stat.st_mode)
        or source_stat.st_uid != os.getuid()
        or not stat.S_ISDIR(home_stat.st_mode)
        or home_stat.st_uid != os.getuid()
    ):
        return None

    destination = profile_home / "packages" / "standalone" / "current"
    if destination.is_file() or destination.is_dir():
        try:
            existing = (destination / "codex").resolve(strict=True)
        except OSError:
            return False
        if existing == binary:
            return os.access(existing, os.X_OK)
        # A configured account may already own an older official standalone
        # ``current`` symlink.  Merely restarting that daemon cannot upgrade it:
        # the official lifecycle command launches the stale path again.  Only
        # replace the pointer when every part of the old target is the same
        # user's canonical standalone release layout.  Directories, ordinary
        # files, broken links, and third-party layouts remain user-owned.
        profile_standalone = profile_home / "packages" / "standalone"
        try:
            destination_stat = destination.lstat()
            existing_stat = existing.stat()
            existing_release = existing.parent.parent
            safely_replaceable = bool(
                stat.S_ISLNK(destination_stat.st_mode)
                and destination_stat.st_uid == os.getuid()
                and stat.S_ISREG(existing_stat.st_mode)
                and existing_stat.st_uid == os.getuid()
                and os.access(existing, os.X_OK)
                and existing.parent.name == "bin"
                and existing_release.parent.name == "releases"
                and existing_release.parent.parent == profile_standalone
            )
        except OSError:
            return False
        if not safely_replaceable:
            return False
        replacement = destination.with_name(
            f".{destination.name}.cc-remote-{os.getpid()}-"
            f"{time.monotonic_ns()}"
        )
        try:
            os.symlink(source_current, replacement, target_is_directory=True)
            # The official updater may advance ``current`` concurrently.  A
            # symlink's target is immutable, so the same inode proves the path
            # still names the exact pointer validated above.  If it changed,
            # preserve the updater's result instead of overwriting it.
            if not os.path.samestat(destination_stat, destination.lstat()):
                replacement.unlink()
                try:
                    concurrent = (destination / "codex").resolve(strict=True)
                except OSError:
                    return False
                return concurrent == binary and os.access(concurrent, os.X_OK)
            os.replace(replacement, destination)
        except OSError:
            try:
                replacement.unlink()
            except OSError:
                pass
            return False
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            # The atomic replacement is already visible. A directory-fsync
            # failure weakens crash durability but must not make callers retry
            # or report that the old pointer is still active.
            log.warning(
                "Codex profile standalone directory fsync failed",
                error_type=type(exc).__name__,
            )
        try:
            prepared = (destination / "codex").resolve(strict=True)
        except OSError:
            return False
        return prepared == binary and os.access(prepared, os.X_OK)
    # ``Path.exists`` is false for a broken symlink. Never replace one.
    if os.path.lexists(destination):
        return False

    try:
        for directory in (
            profile_home / "packages",
            profile_home / "packages" / "standalone",
        ):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            directory_stat = directory.lstat()
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.getuid()
            ):
                return False
        os.symlink(source_current, destination, target_is_directory=True)
    except FileExistsError:
        # A concurrent profile bootstrap won the race. Validate its result.
        pass
    except OSError:
        return False
    try:
        prepared = (destination / "codex").resolve(strict=True)
    except OSError:
        return False
    return prepared == binary and os.access(prepared, os.X_OK)


def _json_object(data: bytes) -> Optional[dict[str, Any]]:
    """Parse the daemon's single JSON object without accepting log prose."""
    if not data or len(data) > _OUTPUT_MAX:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _text(value: Any, limit: int = 4096) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


def _managed_pid(path: Path) -> Optional[int]:
    """Read one bounded, same-user daemon PID record without following links."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        file_stat = os.fstat(descriptor)
        if (not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.getuid()
                or file_stat.st_size > _PID_RECORD_MAX):
            return None
        data = os.read(descriptor, _PID_RECORD_MAX + 1)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(data) > _PID_RECORD_MAX:
        return None
    payload = _json_object(data)
    pid = payload.get("pid") if payload is not None else None
    return pid if isinstance(pid, int) and pid > 1 else None


def _managed_daemon_process_identity(
    codex_home: str | Path,
) -> Optional[ProcessIdentity]:
    """Return the exact same-user app-server generation from official state.

    The durable updater keeps running while replacing its app-server child.
    A per-client proxy can therefore remain superficially alive after the
    socket generation it joined has gone away.  Pair the official bounded PID
    record with the kernel process start token so PID reuse cannot make an old
    proxy look current.
    """
    home = Path(os.path.realpath(os.path.expanduser(os.fspath(codex_home))))
    pid = _managed_pid(home / "app-server-daemon" / "app-server.pid")
    if pid is None or process_owner_uid(pid) != os.getuid():
        return None
    return process_identity(pid)


def _protected_socket_directory(uid: int) -> Path:
    # Match codex-uds: independent of HOME, TMPDIR, and account settings.
    return Path("/tmp").resolve() / f"codex-daemon-{uid}"


def socket_identity(path: str, *, owner_uid: int | None = None) -> tuple[int, int, int]:
    """Validate a native listener, including Codex 0.156's protected alias.

    Native Unix aliases point to /tmp/codex-daemon-UID/SHA256(canonical address).
    Accept only that exact mapping, never an arbitrary or cross-account link.
    The physical listener's identity fences replacement behind an unchanged alias.
    """
    uid = os.getuid() if owner_uid is None else owner_uid
    address = Path(path)
    parent = address.parent
    parent_info = parent.lstat()
    if (not address.is_absolute() or str(parent.resolve()) != str(parent)
            or not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != uid
            or parent_info.st_mode & 0o022):
        raise ValueError("Codex socket parent is not private to its owner")
    info = address.lstat()
    if stat.S_ISLNK(info.st_mode):
        directory = _protected_socket_directory(uid)
        target = directory / hashlib.sha256(os.fsencode(address)).hexdigest()
        if info.st_uid != uid or os.readlink(address) != str(target):
            raise ValueError("Codex socket alias does not match its account address")
        directory_info = directory.lstat()
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != uid
                or stat.S_IMODE(directory_info.st_mode) != 0o700):
            raise ValueError("Codex protected socket directory is not private")
        info = target.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid
            or info.st_mode & 0o077):
        raise ValueError("Codex socket is not private to its owner")
    return info.st_dev, info.st_ino, info.st_ctime_ns


def _standalone_socket_identity(
    codex_home: str, socket_path: str,
) -> Optional[CodexSocketIdentity]:
    """Observe a private native listener without connecting or scanning PIDs.

    A standalone app-server has no managed PID record. Its Unix listener is
    replaced on restart, so inode/ctime changes fence old proxies just as a
    PID/start-token change does for a managed daemon. Never substitute another
    account's socket, follow an arbitrary symlink, or accept a shared-writable path.
    The proxy handshake still proves that the observed listener speaks Codex.
    """
    try:
        home = Path(codex_home).resolve()
        path = Path(socket_path)
        if not path.is_absolute():
            return None
        parent = path.parent.resolve()
        parent.relative_to(home)
        device, inode, created_ns = socket_identity(str(parent / path.name))
        return CodexSocketIdentity(
            str(home), str(parent / path.name),
            device, inode, created_ns,
        )
    except (OSError, ValueError, RuntimeError):
        return None


def _linux_process_start_ticks(pid: int) -> Optional[int]:
    """Return one stable Linux process-generation token."""
    try:
        raw = (_PROC_ROOT / str(pid) / "stat").read_bytes()
        end = raw.rfind(b") ")
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _ensure_managed_daemon_nofile(
    codex_bin: str,
    env: Mapping[str, str],
    lifecycle: Mapping[str, Any],
    *,
    allow_local_listener: bool = False,
) -> Optional[bool]:
    """Raise and verify the actual managed Linux daemon's file limit.

    The official lifecycle client can hand the detached process to the user's
    systemd manager, which may replace the caller's inherited soft limit. A
    high-limit launcher alone is therefore not evidence on Linux. Resolve the
    exact same-user PID record, validate its executable/argv and process
    generation, apply ``prlimit`` to that PID, then read it back.

    ``None`` means this platform has no supported cross-process verification;
    lifecycle commands still run through ``rlimit_exec`` so Darwin descendants
    inherit the requested limit. ``False`` is a Linux verification failure and
    callers must not advertise the managed daemon as ready.
    """
    if not sys.platform.startswith("linux"):
        return None
    prlimit = getattr(resource, "prlimit", None)
    if prlimit is None:
        return False
    codex_home = env.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    daemon_root = Path(os.path.realpath(os.path.expanduser(codex_home))) / (
        "app-server-daemon"
    )
    pid = _managed_pid(daemon_root / "app-server.pid")
    if pid is None or process_owner_uid(pid) != os.getuid():
        return False
    proc_root = _PROC_ROOT / str(pid)
    before = _linux_process_start_ticks(pid)
    if before is None:
        return False
    expected_path = _text(lifecycle.get("managedCodexPath")) or codex_bin
    try:
        executable = os.path.realpath(os.readlink(proc_root / "exe"))
        expected_executable = os.path.realpath(expected_path)
        raw_cmdline = (proc_root / "cmdline").read_bytes()
    except OSError:
        return False
    argv = tuple(value for value in raw_cmdline.split(b"\0") if value)
    listeners = [
        argv[index + 1] for index, value in enumerate(argv[:-1])
        if value == b"--listen"
    ] + [value.split(b"=", 1)[1] for value in argv if value.startswith(b"--listen=")]
    expected_socket = os.fsencode(str(daemon_root.parent / "app-server-control/app-server-control.sock"))
    local_listener = allow_local_listener and listeners in (
        [b"unix://" + expected_socket],
        [b"unix://"],
    )
    if (
        executable != expected_executable
        or b"app-server" not in argv
        or (b"--remote-control" not in argv and not local_listener)
    ):
        return False
    try:
        soft, hard = prlimit(pid, resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < _DAEMON_NOFILE_SOFT_LIMIT:
            return False
        if soft != resource.RLIM_INFINITY and soft < _DAEMON_NOFILE_SOFT_LIMIT:
            prlimit(
                pid,
                resource.RLIMIT_NOFILE,
                (_DAEMON_NOFILE_SOFT_LIMIT, hard),
            )
        verified_soft, _verified_hard = prlimit(pid, resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return False
    after = _linux_process_start_ticks(pid)
    return bool(
        after == before
        and (
            verified_soft == resource.RLIM_INFINITY
            or verified_soft >= _DAEMON_NOFILE_SOFT_LIMIT
        )
    )


def _darwin_process_state(pid: int) -> Optional[str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if value else None


def _terminate_stale_darwin_daemon_updater(
    codex_bin: str,
    env: Mapping[str, str],
) -> bool:
    """SIGTERM one exact updater whose sole managed app-server is a zombie.

    Current official macOS daemon builds can leave ``pid-update-loop`` alive
    after its child becomes defunct.  Every official lifecycle command then
    blocks on the stale parent and the control socket refuses connections.  Do
    not generalize this into process killing: all persisted/process identities,
    ownership, ancestry, zombie state, and argv must agree before one SIGTERM.
    """
    if sys.platform != "darwin":
        return False
    codex_home = env.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    daemon_root = Path(codex_home) / "app-server-daemon"
    app_server_pid = _managed_pid(daemon_root / "app-server.pid")
    updater_pid = _managed_pid(daemon_root / "app-server-updater.pid")
    if (app_server_pid is None or updater_pid is None
            or app_server_pid == updater_pid):
        return False
    updater = _darwin_process_info(updater_pid)
    app_server = _darwin_process_info(app_server_pid)
    if updater is None or app_server is None:
        return False
    updater_identity, _updater_parent, updater_tty, updater_args = updater
    app_server_identity, app_server_parent, _server_tty, _server_args = app_server
    expected_args = (b"app-server", b"daemon", b"pid-update-loop")
    if (updater_tty != 0 or app_server_parent != updater_pid
            or len(updater_args) != 4
            or updater_args[1:] != expected_args):
        return False
    try:
        updater_bin = os.path.realpath(os.fsdecode(updater_args[0]))
    except (TypeError, ValueError):
        return False
    if updater_bin != os.path.realpath(codex_bin):
        return False
    if (process_owner_uid(updater_pid) != os.getuid()
            or process_owner_uid(app_server_pid) != os.getuid()
            or not (_darwin_process_state(app_server_pid) or "").startswith("Z")):
        return False
    # Close the PID-reuse window immediately before signalling both identities.
    current_updater = _darwin_process_info(updater_pid)
    current_app_server = _darwin_process_info(app_server_pid)
    if (current_updater is None or current_updater[0] != updater_identity
            or current_app_server is None
            or current_app_server[0] != app_server_identity
            or current_app_server[1] != updater_pid):
        return False
    try:
        os.kill(updater_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    deadline = time.monotonic() + _STALE_UPDATER_EXIT_TIMEOUT
    while time.monotonic() < deadline:
        current = _darwin_process_info(updater_pid)
        if current is None or current[0] != updater_identity:
            return True
        time.sleep(0.05)
    return False


def _release_version(value: Any) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _managed_daemon_lags_cli(lifecycle: dict[str, Any]) -> bool:
    """Whether the wrapper-owned daemon is older than the selected CLI.

    A private Codex App app-server can also be discoverable through ``version``.
    Callers must therefore use this only after ``enable-remote-control`` has
    confirmed official daemon ownership.  Do not depend on ``backend``: the
    current macOS daemon reports ``pid`` while the Linux daemon omits it.
    """
    if lifecycle.get("status") != "running":
        return False
    if not all((
        _text(lifecycle.get("managedCodexPath")),
        _text(lifecycle.get("managedCodexVersion"), 128),
    )):
        return False
    cli_version = _release_version(lifecycle.get("cliVersion"))
    app_server_version = _release_version(lifecycle.get("appServerVersion"))
    return bool(
        cli_version is not None
        and app_server_version is not None
        and app_server_version < cli_version
    )


def _daemon_info(
    lifecycle: dict[str, Any], remote_control: dict[str, Any],
) -> Optional[CodexDaemonInfo]:
    socket_path = _text(
        remote_control.get("socketPath") or lifecycle.get("socketPath"))
    remote_enabled = remote_control.get("remoteControlEnabled") is True
    # The official enable command returns this field.  Requiring it prevents a
    # zero-exit shim or incompatible older CLI from being advertised as a
    # remotely writable shared daemon.
    if not remote_enabled:
        return None
    return CodexDaemonInfo(
        socket_path=socket_path,
        verified_remote_control=True,
    )


def _existing_proxy_candidate(
    lifecycle: dict[str, Any],
) -> Optional[CodexDaemonInfo]:
    """Return an official existing app-server candidate for proxy validation.

    Codex Desktop and other official clients can start the standalone
    app-server before ``codex app-server daemon`` owns its lifecycle.  Current
    CLIs then report the complete managed package/socket identity from
    ``daemon version`` but reject ``enable-remote-control`` with "not managed".
    The official proxy can still attach to that socket.  Keep this path narrow:
    require the full standalone identity and let CodexHandle's WebSocket
    handshake + initialize request be the authoritative liveness check.  A
    rejected proxy still falls back to private stdio without claiming shared
    ownership.
    """
    if lifecycle.get("status") != "running":
        return None
    socket_path = _text(lifecycle.get("socketPath"))
    managed_path = _text(lifecycle.get("managedCodexPath"))
    managed_version = _text(lifecycle.get("managedCodexVersion"), 128)
    cli_version = _text(lifecycle.get("cliVersion"), 128)
    app_server_version = _text(lifecycle.get("appServerVersion"), 128)
    if not all((socket_path, managed_path, managed_version,
                cli_version, app_server_version)):
        return None
    if (not os.path.isabs(socket_path) or "\x00" in socket_path
            or len(os.fsencode(socket_path)) > 4096):
        return None
    return CodexDaemonInfo(socket_path=socket_path)


def _binary_identity(path: str) -> tuple[object, ...]:
    """Fingerprint the executable so an in-place CLI upgrade re-probes help."""
    resolved = path
    if os.sep not in path:
        resolved = shutil.which(path) or path
    real = os.path.realpath(resolved)
    try:
        stat = os.stat(resolved)
    except OSError:
        return (path, real, None, None, None, None)
    return (
        path,
        real,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


def _daemon_identity(
    path: str, env: Mapping[str, str], socket_path: Optional[str],
) -> tuple[object, ...]:
    return (
        *_binary_identity(path),
        env.get("CODEX_HOME"),
        socket_path,
    )


class CodexDaemonManager:
    """Serialize idempotent daemon setup across resident Code sessions."""

    def __init__(
        self,
        mode: Optional[str] = None,
        *,
        socket_path: Optional[str] = None,
        command_timeout: float = _COMMAND_TIMEOUT,
        require_shared: bool = False,
        allow_restart: bool = True,
    ):
        self.mode = codex_daemon_mode(mode)
        self.socket_path = socket_path
        self.command_timeout = max(1.0, float(command_timeout))
        self.require_shared = bool(require_shared)
        self.allow_restart = bool(allow_restart)
        self._lock = asyncio.Lock()
        self._capability_identity: Optional[tuple[object, ...]] = None
        self._capable = False
        self._ready_identity: Optional[tuple[object, ...]] = None
        self._ready: Optional[CodexDaemonInfo] = None
        self._ready_codex_home: Optional[str] = None
        self._standalone_socket_path: Optional[str] = None
        self._managed_identity_required = False

    @property
    def info(self) -> Optional[CodexDaemonInfo]:
        return self._ready

    @property
    def strict_shared_affinity(self) -> bool:
        """Whether a verified managed daemon must not degrade to stdio."""
        return bool(
            self.require_shared
            or (not self.allow_restart and self._ready is not None)
            or (
                self._ready is not None
                and self._ready.verified_remote_control
            )
        )

    def invalidate(self) -> None:
        """Forget liveness after unexpected proxy EOF; keep help capability."""
        self._ready_identity = None
        self._ready = None
        # Keep the profile-scoped home after the first verified connection so
        # other resident handles can still compare their process generation.
        # Clearing it here would make one proxy EOF look like a daemon swap to
        # every healthy sibling handle and cause a reconnect cascade.

    def current_process_identity(self) -> Optional[CodexServerIdentity]:
        """Observe the discovered server generation (PID or native listener).

        Keep this historical method name for handle/test manager compatibility.
        Once a managed PID is observed, missing/unreadable PID state is an
        outage, never permission to downgrade to standalone socket tracking.
        invalidate() must preserve this barrier for healthy sibling handles.
        """
        home = self._ready_codex_home
        if home is None:
            return None
        identity = _managed_daemon_process_identity(home)
        if identity is not None:
            self._managed_identity_required = True
        if self._managed_identity_required or self._standalone_socket_path is None:
            return identity
        # Only a genuinely absent PID file permits the standalone identity.
        # Broken symlinks, corrupt records and permission errors fail closed.
        try:
            (Path(home) / "app-server-daemon" / "app-server.pid").lstat()
        except FileNotFoundError:
            return _standalone_socket_identity(home, self._standalone_socket_path)
        except OSError:
            pass
        return None

    async def _run(
        self, codex_bin: str, env: Mapping[str, str], *args: str,
    ) -> _CommandResult:
        argv = (codex_bin, *args)
        return await asyncio.to_thread(
            _run_command, argv, env, self.command_timeout)

    async def capability(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> bool:
        """Check both official commands, caching by executable identity."""
        if self.mode == "off" or os.name != "posix":
            return False
        identity = _daemon_identity(codex_bin, env, self.socket_path)
        if identity == self._capability_identity:
            return self._capable
        daemon_help = await self._run(
            codex_bin, env, "app-server", "daemon", "--help")
        proxy_help = await self._run(
            codex_bin, env, "app-server", "proxy", "--help")
        capable = daemon_help.returncode == 0 and proxy_help.returncode == 0
        self._capability_identity = identity
        self._capable = capable
        if not capable:
            self.invalidate()
        return capable

    async def version(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> Optional[dict[str, Any]]:
        result = await self._run(
            codex_bin, env, "app-server", "daemon", "version")
        return _json_object(result.stdout) if result.returncode == 0 else None

    async def start(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> Optional[dict[str, Any]]:
        result = await self._run(
            codex_bin, env, "app-server", "daemon", "start")
        return _json_object(result.stdout) if result.returncode == 0 else None

    async def bootstrap(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> bool:
        """Install one profile-local durable daemon with remote control.

        Official daemon state is rooted in ``CODEX_HOME``.  This command is
        therefore safe for explicitly configured sibling profiles and is the
        supported first-start path on headless Linux hosts.
        """
        prepared = await asyncio.to_thread(
            _prepare_profile_standalone, codex_bin, env)
        if prepared is False:
            log.warning(
                "Codex profile managed standalone path could not be prepared")
        result = await self._run(
            codex_bin,
            env,
            "app-server",
            "daemon",
            "bootstrap",
            "--remote-control",
        )
        if result.returncode != 0:
            log.warning(
                "Codex profile daemon bootstrap failed",
                returncode=result.returncode,
            )
            return False
        return True

    async def restart(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> bool:
        result = await self._run(
            codex_bin, env, "app-server", "daemon", "restart")
        return result.returncode == 0

    async def _align_managed_daemon(
        self,
        codex_bin: str,
        env: Mapping[str, str],
        lifecycle: dict[str, Any],
    ) -> dict[str, Any]:
        if not _managed_daemon_lags_cli(lifecycle):
            return lifecycle
        log.info(
            "restarting lagging managed Codex daemon",
            cli_version=_text(lifecycle.get("cliVersion"), 128),
            app_server_version=_text(
                lifecycle.get("appServerVersion"), 128),
        )
        prepared = await asyncio.to_thread(
            _prepare_profile_standalone, codex_bin, env,
        )
        if prepared is False:
            log.warning(
                "Codex profile managed standalone could not be aligned"
            )
        if not await self.restart(codex_bin, env):
            self.invalidate()
            raise CodexDaemonUpgradeRequired(
                "Codex shared daemon is older than the selected CLI and "
                "could not be restarted"
            )
        deadline = (
            asyncio.get_running_loop().time()
            + _DAEMON_UPGRADE_SETTLE_TIMEOUT
        )
        while True:
            verified = await self.version(codex_bin, env)
            if verified is not None and not _managed_daemon_lags_cli(verified):
                return verified
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.invalidate()
                raise CodexDaemonUpgradeRequired(
                    "Codex shared daemon did not upgrade to the selected CLI"
                )
            await asyncio.sleep(min(
                _DAEMON_UPGRADE_POLL_INTERVAL,
                remaining,
            ))

    async def enable_remote_control(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> Optional[dict[str, Any]]:
        result = await self._run(
            codex_bin, env,
            "app-server", "daemon", "enable-remote-control",
        )
        return _json_object(result.stdout) if result.returncode == 0 else None

    async def ensure_started(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> Optional[CodexDaemonInfo]:
        """Start and remotely enable the daemon, or return ``None`` for stdio."""
        if self.mode == "off":
            return None
        codex_home = os.path.realpath(os.path.expanduser(
            env.get("CODEX_HOME") or "~/.codex"
        ))
        identity = _daemon_identity(codex_bin, env, self.socket_path)
        if identity == self._ready_identity and self._ready is not None:
            return self._ready
        async with self._lock:
            identity = _daemon_identity(codex_bin, env, self.socket_path)
            if identity == self._ready_identity and self._ready is not None:
                return self._ready
            if not await self.capability(codex_bin, env):
                if self.require_shared:
                    raise CodexProfileDaemonUnavailable(
                        "Codex profile shared daemon commands are unavailable"
                    )
                return None

            if not self.allow_restart:
                return await self._prepare_without_restart(codex_bin, env, identity)

            lifecycle = await self.version(codex_bin, env)
            if lifecycle is None and self.require_shared:
                log.info("bootstrapping required Codex profile daemon")
                if await self.bootstrap(codex_bin, env):
                    lifecycle = await self.version(codex_bin, env)
            if lifecycle is None:
                await self.start(codex_bin, env)
                lifecycle = await self.version(codex_bin, env)
            if lifecycle is None:
                recovered = await asyncio.to_thread(
                    _terminate_stale_darwin_daemon_updater, codex_bin, env,
                )
                if recovered:
                    log.warning(
                        "terminated exact stale Codex daemon updater; "
                        "starting replacement"
                    )
                    await self.start(codex_bin, env)
                    lifecycle = await self.version(codex_bin, env)
            if lifecycle is None:
                # Non-Darwin failures, and stale states that did not satisfy
                # every identity check above, get only the official lifecycle
                # command.  Never signal an ambiguous process.
                log.warning(
                    "Codex daemon start unavailable; attempting restart")
                if await self.restart(codex_bin, env):
                    lifecycle = await self.version(codex_bin, env)
            if lifecycle is None:
                self.invalidate()
                if self.require_shared:
                    raise CodexProfileDaemonUnavailable(
                        "Codex profile shared daemon could not be started"
                    )
                log.warning("Codex daemon start unavailable; using stdio")
                return None
            remote = await self.enable_remote_control(codex_bin, env)
            if remote is None:
                existing = _existing_proxy_candidate(lifecycle)
                if existing is None:
                    self.invalidate()
                    if self.require_shared:
                        raise CodexProfileDaemonUnavailable(
                            "Codex profile shared daemon remote control is "
                            "unavailable"
                        )
                    log.warning(
                        "Codex daemon remote control unavailable; using stdio")
                    return None
                # An official client already owns this app-server generation.
                # proxy_args() exposes it tentatively; CodexHandle validates the
                # actual WebSocket and initialize exchange before advertising a
                # shared writable session.
                log.info("using existing official Codex app-server candidate")
                self._ready_identity = identity
                self._ready = existing
                self._ready_codex_home = codex_home
                self._standalone_socket_path = (
                    existing.socket_path
                    if self.socket_path in {None, existing.socket_path}
                    else None
                )
                return existing

            # Only a successful enable proves that this is the official managed
            # daemon rather than a discoverable private Codex App process.  From
            # this point it is safe to align an older daemon generation.
            verified = await self.version(codex_bin, env)
            if verified is None:
                self.invalidate()
                if self.require_shared:
                    raise CodexProfileDaemonUnavailable(
                        "Codex profile shared daemon could not be verified"
                    )
                log.warning("Codex daemon version probe failed; using stdio")
                return None
            before_alignment = verified
            verified = await self._align_managed_daemon(
                codex_bin, env, before_alignment)
            if verified is not before_alignment:
                # A race upgraded the daemon after enable.  Re-enable remote
                # control on the replacement generation before advertising it.
                remote = await self.enable_remote_control(codex_bin, env)
                if remote is None:
                    self.invalidate()
                    raise CodexDaemonUpgradeRequired(
                        "Codex shared daemon restarted but remote control "
                        "could not be re-enabled"
                    )
            info = _daemon_info(verified, remote)
            if info is None:
                self.invalidate()
                if self.require_shared:
                    raise CodexProfileDaemonUnavailable(
                        "Codex profile shared daemon did not confirm remote "
                        "control"
                    )
                log.warning(
                    "Codex daemon did not confirm remote control; using stdio")
                return None
            nofile_verified = await asyncio.to_thread(
                _ensure_managed_daemon_nofile,
                codex_bin,
                env,
                verified,
            )
            if nofile_verified is False:
                self.invalidate()
                if self.require_shared:
                    raise CodexProfileDaemonUnavailable(
                        "Codex profile shared daemon file limit could not be "
                        "verified"
                    )
                log.warning(
                    "Codex daemon file limit unavailable; using stdio")
                return None
            info = CodexDaemonInfo(
                socket_path=info.socket_path,
                verified_remote_control=info.verified_remote_control,
                nofile_verified=nofile_verified is True,
            )
            self._ready_identity = identity
            self._ready = info
            self._ready_codex_home = codex_home
            self._managed_identity_required = True
            self._standalone_socket_path = None
            return info

    async def _prepare_without_restart(
        self, codex_bin: str, env: Mapping[str, str], identity: tuple[object, ...],
    ) -> Optional[CodexDaemonInfo]:
        """Prepare local sharing without interrupting native clients.

        Official ``start`` reuses an existing listener. In contrast, bootstrap,
        restart and even enable-remote-control can stop a running generation.
        Local TUI/proxy sharing only needs the Unix listener; cloud remote
        control is a separate native setting and is not changed here.
        """
        lifecycle = await self.version(codex_bin, env)
        if lifecycle is None:
            await self.start(codex_bin, env)
            lifecycle = await self.version(codex_bin, env)
        if lifecycle is None and self.require_shared:
            prepared = await asyncio.to_thread(
                _prepare_profile_standalone, codex_bin, env)
            if prepared is True:
                await self.start(codex_bin, env)
                lifecycle = await self.version(codex_bin, env)
        info = _existing_proxy_candidate(lifecycle) if lifecycle else None
        if info is None:
            self.invalidate()
            if self.require_shared:
                raise CodexProfileDaemonUnavailable(
                    "Codex shared daemon unavailable; existing processes were preserved")
            return None
        codex_home = os.path.realpath(os.path.expanduser(
            env.get("CODEX_HOME") or "~/.codex"))
        if _managed_daemon_process_identity(codex_home) is not None:
            self._managed_identity_required = True
            nofile = await asyncio.to_thread(
                _ensure_managed_daemon_nofile, codex_bin, env, lifecycle,
                allow_local_listener=True)
            if nofile is False:
                self.invalidate()
                raise CodexProfileDaemonUnavailable(
                    "Codex shared daemon file limit could not be verified")
            info = CodexDaemonInfo(
                socket_path=info.socket_path, nofile_verified=nofile is True)
        self._ready_identity = identity
        self._ready = info
        self._ready_codex_home = codex_home
        self._standalone_socket_path = info.socket_path
        return info

    async def proxy_args(
        self, codex_bin: str, env: Mapping[str, str],
    ) -> Optional[list[str]]:
        info = await self.ensure_started(codex_bin, env)
        if info is None:
            return None
        argv = [codex_bin, "app-server", "proxy"]
        socket_path = self.socket_path or info.socket_path
        if socket_path:
            argv.extend(["--sock", socket_path])
        return argv

_DEFAULT_MANAGERS: dict[
    tuple[str, Optional[str], Optional[str]], CodexDaemonManager
] = {}


def default_codex_daemon_manager(
    mode: Optional[str] = None, *, socket_path: Optional[str] = None,
    codex_home: Optional[str] = None,
) -> CodexDaemonManager:
    normalized = codex_daemon_mode(mode)
    normalized_home = (
        os.path.realpath(os.path.expanduser(codex_home))
        if codex_home else None
    )
    key = (normalized, socket_path, normalized_home)
    manager = _DEFAULT_MANAGERS.get(key)
    if manager is None:
        manager = CodexDaemonManager(normalized, socket_path=socket_path)
        _DEFAULT_MANAGERS[key] = manager
    return manager
