"""No-model Codex connection checks, published by the actual Wrapper user.

The release installer cannot safely guess the service's PATH/account environment
or run an account's Codex as root. It instead waits for this startup receipt.
This verifies transport readiness, not an operator's shell aliases or an already
running TUI's automatic discovery.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from typing import Any

from websockets.client import ClientProtocol
from websockets.frames import Frame, Opcode
from websockets.http11 import Response
from websockets.uri import parse_uri

from cc_remote import __version__
from cc_remote.wrapper.codex_daemon import CodexDaemonManager, socket_identity
from cc_remote.wrapper.process_scan import process_identity

REPORT_NAME = "codex-readiness.json"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT = 8.0


async def probe_proxy(binary: str, env: dict[str, str], socket_path: str) -> None:
    """Initialize through the official raw WebSocket proxy, without a thread."""
    process = await asyncio.create_subprocess_exec(
        binary, "app-server", "proxy", "--sock", socket_path,
        env=env, cwd=env.get("HOME") or str(Path.home()),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=256 * 1024,
        start_new_session=True,
    )
    protocol = ClientProtocol(parse_uri("ws://localhost/"), max_size=256 * 1024)

    async def flush() -> None:
        assert process.stdin is not None
        for chunk in protocol.data_to_send():
            if chunk:
                process.stdin.write(chunk)
        await process.stdin.drain()

    try:
        async with asyncio.timeout(_TIMEOUT):
            assert process.stdin is not None and process.stdout is not None
            initialize = json.dumps({
                "id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "cc-remote-readiness", "version": __version__},
                },
            }).encode()
            protocol.send_request(protocol.connect())
            await flush()
            fragments = bytearray()
            messages = 0
            while messages < 32:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    raise RuntimeError("Codex proxy closed before initialization")
                protocol.receive_data(chunk)
                if protocol.handshake_exc is not None:
                    raise RuntimeError("Codex proxy rejected WebSocket handshake")
                for event in protocol.events_received():
                    if isinstance(event, Response):
                        protocol.send_text(initialize)
                    elif isinstance(event, Frame):
                        if event.opcode in {Opcode.BINARY, Opcode.CLOSE}:
                            raise RuntimeError("Codex proxy closed or returned binary data")
                        if event.opcode not in {Opcode.TEXT, Opcode.CONT}:
                            continue
                        fragments.extend(event.data)
                        if len(fragments) > 256 * 1024:
                            raise RuntimeError("Codex initialization exceeds limit")
                        if not event.fin:
                            continue
                        messages += 1
                        response = json.loads(fragments)
                        fragments.clear()
                        if not isinstance(response, dict) or response.get("id") != 1:
                            continue
                        if "error" in response or not isinstance(response.get("result"), dict):
                            raise RuntimeError("Codex proxy rejected initialization")
                        protocol.send_text(b'{"method":"initialized"}')
                        protocol.send_close()
                        await flush()
                        return
                await flush()
            raise RuntimeError("Codex proxy did not return initialization")
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            # Only this short-lived, thread-free probe is terminated.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


async def check_profile(
    profile_id: str, home: str, binary: str, daily_cli: str | None,
    env: dict[str, str], manager: CodexDaemonManager,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "profile": profile_id, "home": home, "status": "unavailable",
        "wrapper_cli": binary, "daily_cli": daily_cli,
        "terminal_connection": "unverified",
    }
    try:
        # Setup is owned by Wrapper startup, serialized by the same manager
        # that subsequent Code sessions use. Work never uses this manager.
        info = await manager.ensure_started(binary, env)
        if info is None or not info.socket_path:
            row["reason"] = "daemon_unavailable"
            return row
        expected = os.path.join(os.path.realpath(home),
                                "app-server-control", "app-server-control.sock")
        if os.path.abspath(info.socket_path) != expected:
            row["reason"] = "account_socket_mismatch"
            return row
        before = socket_identity(expected)
        wrapper = await manager.version(binary, env)
        if not wrapper or wrapper.get("status") != "running":
            row["reason"] = "daemon_unavailable"
            return row
        await probe_proxy(binary, env, expected)
        if daily_cli is None:
            row["reason"] = "daily_cli_missing"
            return row
        daily = await manager.version(daily_cli, env)
        if (
            not daily or daily.get("status") != "running"
            or os.path.abspath(str(daily.get("socketPath", ""))) != expected
            or os.path.abspath(str(wrapper.get("socketPath", ""))) != expected
        ):
            row["reason"] = "daily_cli_mismatch"
            return row
        versions = [wrapper.get("cliVersion"), daily.get("cliVersion"),
                    wrapper.get("appServerVersion"), daily.get("appServerVersion")]
        if not all(isinstance(value, str) and value for value in versions) or len(set(versions)) != 1:
            row["reason"] = "version_mismatch"
            return row
        if os.path.realpath(daily_cli) != os.path.realpath(binary):
            await probe_proxy(daily_cli, env, expected)
        if socket_identity(expected) != before:
            row["reason"] = "daemon_changed"
            return row
        row.update(status="ready", socket=expected, socket_identity=list(before), version=versions[0])
    except Exception as exc:
        # A CLI may include credentials or prompts in stderr/errors. Publish
        # only a closed reason and exception class, never its raw message.
        row.update(reason="connection_failed", error_type=type(exc).__name__)
    return row


def write_report(state_dir: Path, rows: list[dict[str, Any]]) -> None:
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("cannot identify Wrapper process")
    payload = {
        "schema": 1, "source": str(SOURCE_ROOT), "version": __version__,
        "wrapper": asdict(identity), "created_at": time.time(), "profiles": rows,
    }
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".codex-readiness-", dir=state_dir)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(state_dir / REPORT_NAME)
    finally:
        temporary.unlink(missing_ok=True)
