"""A process-local bridge for agy's managed lifecycle hooks."""
from __future__ import annotations

import asyncio
import json
import shlex
import socket
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

_MAX_EVENT = 4096
_MAX_INPUT = 32 * 1024 * 1024

# agy sorts its hook groups by name and skips later Stop groups when a
# preceding hook requests continuation. Observe the final decision point.
_GROUP = "\U0010ffff" * 4 + "-cc-remote"


class AntigravityHooks:
    def __init__(self, on_event):
        self.on_event = on_event
        self.directory = None
        self.listener = None
        self.connections = set()

    async def start(self):
        self.directory = TemporaryDirectory(prefix="ccr-agy-hooks-", dir="/tmp")
        root = Path(self.directory.name)
        try:
            path = root / "events.sock"
            self.listener = await asyncio.start_unix_server(self.receive, str(path), limit=_MAX_EVENT)
            path.chmod(0o600)
            hooks = root / ".agents"
            hooks.mkdir(mode=0o700)
            command = shlex.join([sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(path)])
            handler = {"type": "command", "command": command, "timeout": 3}
            (hooks / "hooks.json").write_text(json.dumps({
                _GROUP: {"PreInvocation": [handler], "Stop": [handler]},
            }), encoding="utf-8")
            return ("--add-dir", str(root))
        except BaseException:
            await self.close()
            raise

    async def receive(self, reader, writer):
        from cc_remote.claude_service.wire import same_user

        task = asyncio.current_task()
        self.connections.add(task)
        try:
            if not same_user(writer):
                return
            async with asyncio.timeout(2):
                event = json.loads(await reader.readline())
                if not isinstance(event, dict):
                    return
                native = event.get("session")
                kind = event.get("kind")
                reason = event.get("reason", "")
                if (not isinstance(native, str) or not 1 <= len(native) <= 512
                        or kind not in ("invoke", "stop") or not isinstance(reason, str) or len(reason) > 128):
                    return
                await self.on_event(native, kind, reason)
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            self.connections.discard(task)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
        tasks = tuple(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


def report(path):
    raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
    if len(raw) > _MAX_INPUT:
        return
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return
    native = payload.get("conversationId")
    if not isinstance(native, str) or not 1 <= len(native) <= 512:
        return
    event = payload.get("hookEventName")
    if event == "PreInvocation":
        notice = {"session": native, "kind": "invoke"}
    elif event == "Stop" and payload.get("fullyIdle") is True:
        reason = payload.get("terminationReason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 128:
            return
        notice = {"session": native, "kind": "stop", "reason": "error" if payload.get("error") else reason}
    else:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(path)
        connection.sendall(json.dumps(notice).encode() + b"\n")
        connection.shutdown(socket.SHUT_WR)
        # Keep the native hook synchronous until the Wrapper records the
        # observation, even if agy is about to start another invocation.
        connection.recv(1)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        with suppress(OSError, ValueError):
            report(sys.argv[1])
    print("{}")
