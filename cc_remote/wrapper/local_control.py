"""Device-local control requests from other processes of this user.

A local tool that runs the same Claude Code session itself, for example a task
runner resuming it with ``claude --resume``, asks the Wrapper to let go of the
idle subprocess it keeps for a viewer. Requests carry no prompt and cannot
start model work; the socket is private to the Wrapper's user.

One JSON line in, one JSON line out::

    {"op": "release_claude_session", "config_dir": "/home/me/.claude", "session_id": "<uuid>"}
    {"status": "released" | "busy" | "not_resident" | "unknown"}
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from cc_remote.log import logger

log = logger("cc_remote.wrapper.local_control")
MAX_REQUEST = 4096
MAX_CLIENTS = 8
REQUEST_TIMEOUT = 30


def _session_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value.lower():
        raise ValueError("session_id must be a Claude session UUID")
    return value.lower()


def _config_dir(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or not os.path.isabs(value):
        raise ValueError("config_dir must be an absolute path")
    return value


class LocalControl:
    def __init__(self, machine):
        self.machine = machine
        self.root = Path(machine.cfg.state_dir) / "control"
        self.socket = self.root / "control.sock"
        self.server = None
        self.lock_fd = None
        self.clients = set()

    async def start(self):
        """Serve the socket; a failure only disables this optional entry point."""
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Control state must be a private directory owned by this user")
            if len(os.fsencode(self.socket)) > 103:
                raise ValueError("Control socket path exceeds the platform limit")
            self.lock_fd = os.open(self.root / "server.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock_info = os.fstat(self.lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
                raise ValueError("Invalid control server lock")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.socket.exists() or self.socket.is_symlink():
                info = self.socket.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("Refusing to replace an unrelated control socket")
                self.socket.unlink()
            self.server = await asyncio.start_unix_server(self._client, str(self.socket), limit=MAX_REQUEST)
            os.chmod(self.socket, 0o600)
        except (OSError, ValueError) as exc:
            log.warning("Local control socket unavailable", error=str(exc))
            self._release_lock()

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.socket.unlink(missing_ok=True)
            self.server = None
        for client in list(self.clients):
            client.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        self._release_lock()

    def _release_lock(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    async def handle(self, request: object) -> dict:
        if (not isinstance(request, dict) or set(request) != {"op", "config_dir", "session_id"}
                or request["op"] != "release_claude_session"):
            raise ValueError("Unknown control request")
        status = await self.machine.release_claude_session(
            _config_dir(request["config_dir"]), _session_id(request["session_id"]))
        if status == "unknown":
            return {"status": status, "error": "Native process exit could not be confirmed; do not take over the session"}
        return {"status": status}

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            if len(self.clients) > MAX_CLIENTS:
                result = {"status": "rejected", "error": "Too many control requests"}
            else:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    result = await self.handle(json.loads(await reader.readline()))
        except ValueError as exc:
            result = {"status": "rejected", "error": str(exc) or "Invalid control request"}
        except Exception:
            log.warning("Local control request failed")
            result = {"status": "unknown", "error": "Control request failed; the session may or may not be released"}
        finally:
            self.clients.discard(task)
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            writer.close()
