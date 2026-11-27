"""Serve existing Code tool handlers over Antigravity's stdio MCP transport."""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import anyio
from mcp import types
from mcp.shared.message import SessionMessage
from pydantic import ValidationError

from cc_remote.session_messages import make_server, reference
from cc_remote.wrapper.artifact_inspection import make_artifact_server
from cc_remote.wrapper.ask import ASK_USER_TOOL, make_ask_server

_STDIO = Path(__file__).resolve().with_name("antigravity_mcp_stdio.py")
_MAX_FRAME = 4 * 1024 * 1024
_MODES = ("default", "accept-edits", "plan")


class AntigravityTools:
    def __init__(self, runtime, cwd: str):
        self.runtime = runtime
        self.machine = runtime.machine
        self.cwd = cwd
        info = os.stat(cwd)
        self.workspace_id = info.st_dev, info.st_ino
        self.ctx = None
        self.handle = None
        # The permission exception must point to this mount, even when the
        # local agy settings define a server with the standard name.
        self.ask_server_name = f"cc-remote-ask-{uuid4().hex[:16]}"
        # A short path fits the Unix socket address limit on macOS.
        self.directory = TemporaryDirectory(dir="/tmp", prefix="ccr-agy-")
        self.listeners: list[asyncio.Server] = []
        self.connections: set[asyncio.Task] = set()
        self.closed = False

    def workspace_matches(self) -> bool:
        if self.closed:
            return False
        try:
            info = os.stat(self.cwd)
        except OSError:
            return False
        return self.workspace_id == (info.st_dev, info.st_ino)

    def bind(self, ctx) -> None:
        self.ctx, self.handle = ctx, ctx.sdk

    async def start(self) -> list[dict]:
        servers = [
            make_ask_server(self.ask, self.set_mode, ask_batch=self.ask_batch, modes=_MODES),
            make_artifact_server(self.cwd, before_inspect=self.active_context),
        ]
        if self.machine._session_messages is not None and self.machine._session_messages.enabled:
            servers.append(make_server(self.message))
        commands = []
        for server in servers:
            path = Path(self.directory.name) / f"{server.name}.sock"

            async def connect(reader, writer, server=server):
                await self.serve(server, reader, writer)

            listener = await asyncio.start_unix_server(
                connect, path=str(path), limit=_MAX_FRAME)
            self.listeners.append(listener)
            path.chmod(0o600)
            commands.append({
                "name": self.ask_server_name if server.name == "cc-remote-ask" else server.name,
                "command": sys.executable,
                "args": ["-I", "-B", str(_STDIO), str(path)], "env": [],
            })
        return commands

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        connections = tuple(self.connections)
        for listener in self.listeners:
            listener.close()
        for connection in connections:
            connection.cancel()
        try:
            await asyncio.gather(*connections, return_exceptions=True)
            await asyncio.gather(*(listener.wait_closed() for listener in self.listeners))
        finally:
            self.directory.cleanup()

    async def serve(self, server, reader, writer) -> None:
        task = asyncio.current_task()
        try:
            if self.closed or len(self.connections) >= 8:
                return
            self.connections.add(task)
            receive, incoming = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            outgoing, send = anyio.create_memory_object_stream[SessionMessage](0)

            async def write_message(message):
                payload = message.model_dump_json(by_alias=True, exclude_none=True)
                writer.write(payload.encode() + b"\n")
                await writer.drain()

            async def read():
                try:
                    async with receive:
                        while line := await reader.readline():
                            try:
                                message = types.JSONRPCMessage.model_validate_json(line)
                            except ValidationError as exc:
                                await receive.send(exc)
                            else:
                                request = message.root
                                if isinstance(request, types.JSONRPCRequest) and request.method == "server/discover":
                                    # agy probes the modern MCP handshake first.
                                    # The pinned server SDK uses initialize.
                                    await write_message(types.JSONRPCError(
                                        jsonrpc="2.0", id=request.id,
                                        error=types.ErrorData(code=types.METHOD_NOT_FOUND, message="Method not found"),
                                    ))
                                else:
                                    await receive.send(SessionMessage(message))
                except (OSError, ValueError):
                    pass
                finally:
                    group.cancel_scope.cancel()

            async def write():
                try:
                    async with send:
                        async for reply in send:
                            await write_message(reply.message)
                except OSError:
                    group.cancel_scope.cancel()

            async with incoming, outgoing, anyio.create_task_group() as group:
                group.start_soon(read)
                group.start_soon(write)
                try:
                    await server.run(incoming, outgoing, server.create_initialization_options())
                finally:
                    group.cancel_scope.cancel()
        finally:
            self.connections.discard(task)
            writer.transport.abort()
            with suppress(OSError):
                await writer.wait_closed()

    def require_active(self, turn) -> None:
        ctx, handle = self.ctx, self.handle
        if (self.closed or ctx is None or ctx.sdk is not handle
                or not self.machine._is_resident_context(ctx) or ctx.btw or ctx.space != "code"
                or not handle.connected or not self.workspace_matches()
                or ctx.write_state != "writable" or ctx.state != "running"
                or turn is None or handle.turn is not turn or turn.stopping
                or (not turn.autonomous and all(response.done() for _, response in turn.responses))):
            raise ValueError("Tool caller is no longer an active Gemini Code turn.")

    async def active_context(self):
        if self.handle is None:
            raise ValueError("Gemini Code session is not ready.")
        await self.handle.input_ready.wait()
        turn = self.handle.turn
        self.require_active(turn)
        return self.ctx, turn

    def owns_question(self, tool: object) -> bool:
        if self.handle is None or tool != f"mcp_{self.ask_server_name}_{ASK_USER_TOOL}":
            return False
        try:
            self.require_active(self.handle.turn)
        except ValueError:
            return False
        return True

    async def ask(self, question, options):
        ctx, turn = await self.active_context()
        with turn.interaction():
            return await self.machine._on_mcp_ask(
                ctx, question, options, timeout=float("inf"), dismiss_actions=["cancel"],
                guard=lambda: self.require_active(turn))

    async def ask_batch(self, questions):
        ctx, turn = await self.active_context()
        with turn.interaction():
            return await self.machine._on_mcp_ask_batch(
                ctx, questions, timeout=float("inf"), dismiss_actions=["cancel"],
                guard=lambda: self.require_active(turn))

    async def set_mode(self, mode):
        ctx, turn = await self.active_context()
        async with ctx.query_lock:
            self.require_active(turn)
            await self.runtime.apply_option(ctx, "mode", mode)

    async def message(self, name, args, _meta):
        ctx, _ = await self.active_context()
        bus = self.machine._session_messages
        if bus is None:
            raise ValueError("Session messaging is disabled.")
        return await bus.call(reference("antigravity", ctx.key), name, args)
