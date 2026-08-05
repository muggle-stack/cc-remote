"""Shared Codex app-server daemon discovery and lifecycle helpers.

The official daemon is process-global while each client connection is a short
``codex app-server proxy`` process.  This module owns only the former.  A
``CodexHandle`` continues to own (and terminate) its proxy or legacy stdio
process, so disconnecting one remote session cannot stop other Codex clients.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
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
from cc_remote.wrapper.os_compat import current_uid
from cc_remote.wrapper.process_scan import (
    _darwin_process_info,
    process_owner_uid,
)

log = logger("cc_remote.wrapper.codex_daemon")

_DAEMON_ENV = "CC_REMOTE_CODEX_DAEMON"
_DAEMON_MODES = frozenset({"auto", "off"})
_COMMAND_TIMEOUT = 30.0
_OUTPUT_MAX = 64 * 1024
_PID_RECORD_MAX = 4096
_STALE_UPDATER_EXIT_TIMEOUT = 3.0


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


class CodexDaemonUpgradeRequired(RuntimeError):
    """The managed shared daemon could not be aligned with the selected CLI."""


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_command(
    argv: tuple[str, ...], env: Mapping[str, str], timeout: float,
) -> _CommandResult:
    """Blocking subprocess boundary, kept separate for deterministic tests."""
    try:
        result = subprocess.run(
            argv,
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
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
        )
        file_stat = os.fstat(descriptor)
        if (not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != current_uid()
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
    if (process_owner_uid(updater_pid) != current_uid()
            or process_owner_uid(app_server_pid) != current_uid()
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
    ):
        self.mode = codex_daemon_mode(mode)
        self.socket_path = socket_path
        self.command_timeout = max(1.0, float(command_timeout))
        self._lock = asyncio.Lock()
        self._capability_identity: Optional[tuple[object, ...]] = None
        self._capable = False
        self._ready_identity: Optional[tuple[object, ...]] = None
        self._ready: Optional[CodexDaemonInfo] = None

    @property
    def info(self) -> Optional[CodexDaemonInfo]:
        return self._ready

    @property
    def strict_shared_affinity(self) -> bool:
        """Whether a verified managed daemon must not degrade to stdio."""
        return bool(
            self._ready is not None
            and self._ready.verified_remote_control
        )

    def invalidate(self) -> None:
        """Forget liveness after unexpected proxy EOF; keep help capability."""
        self._ready_identity = None
        self._ready = None

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
        if not await self.restart(codex_bin, env):
            self.invalidate()
            raise CodexDaemonUpgradeRequired(
                "Codex shared daemon is older than the selected CLI and "
                "could not be restarted"
            )
        verified = await self.version(codex_bin, env)
        if verified is None or _managed_daemon_lags_cli(verified):
            self.invalidate()
            raise CodexDaemonUpgradeRequired(
                "Codex shared daemon did not upgrade to the selected CLI"
            )
        return verified

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
        identity = _daemon_identity(codex_bin, env, self.socket_path)
        if identity == self._ready_identity and self._ready is not None:
            return self._ready
        async with self._lock:
            identity = _daemon_identity(codex_bin, env, self.socket_path)
            if identity == self._ready_identity and self._ready is not None:
                return self._ready
            if not await self.capability(codex_bin, env):
                return None

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
                log.warning("Codex daemon start unavailable; using stdio")
                self.invalidate()
                return None
            remote = await self.enable_remote_control(codex_bin, env)
            if remote is None:
                existing = _existing_proxy_candidate(lifecycle)
                if existing is None:
                    log.warning(
                        "Codex daemon remote control unavailable; using stdio")
                    self.invalidate()
                    return None
                # An official client already owns this app-server generation.
                # proxy_args() exposes it tentatively; CodexHandle validates the
                # actual WebSocket and initialize exchange before advertising a
                # shared writable session.
                log.info("using existing official Codex app-server candidate")
                self._ready_identity = identity
                self._ready = existing
                return existing

            # Only a successful enable proves that this is the official managed
            # daemon rather than a discoverable private Codex App process.  From
            # this point it is safe to align an older daemon generation.
            verified = await self.version(codex_bin, env)
            if verified is None:
                log.warning("Codex daemon version probe failed; using stdio")
                self.invalidate()
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
                log.warning(
                    "Codex daemon did not confirm remote control; using stdio")
                self.invalidate()
                return None
            self._ready_identity = identity
            self._ready = info
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

_DEFAULT_MANAGERS: dict[tuple[str, Optional[str]], CodexDaemonManager] = {}


def default_codex_daemon_manager(
    mode: Optional[str] = None, *, socket_path: Optional[str] = None,
) -> CodexDaemonManager:
    normalized = codex_daemon_mode(mode)
    key = (normalized, socket_path)
    manager = _DEFAULT_MANAGERS.get(key)
    if manager is None:
        manager = CodexDaemonManager(normalized, socket_path=socket_path)
        _DEFAULT_MANAGERS[key] = manager
    return manager
