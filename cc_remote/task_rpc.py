"""Async tasks use only the selected account's existing official daemon."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path

from websockets.asyncio.client import unix_connect

from cc_remote.wrapper.codex_daemon import socket_identity


class Rejected(RuntimeError):
    """An explicit native rejection; no automatic replay."""


class NativeTasks:
    def __init__(self, ws, on_output=None):
        self.ws = ws
        self.on_output = on_output
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:
                    if "id" in msg:
                        # This client never grants approvals on behalf of a user.
                        await self.ws.send(json.dumps({"id": msg["id"], "error": {
                            "code": -32601, "message": "Unsupported task client request"}}))
                    elif msg["method"] == "command/exec/outputDelta" and self.on_output:
                        self.on_output(msg.get("params", {}))
                    continue
                future = self.pending.get(msg.get("id"))
                if future is not None and not future.done():
                    if "error" in msg:
                        future.set_exception(Rejected("Official daemon rejected the operation"))
                    else:
                        future.set_result(msg.get("result"))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Official daemon connection lost"))

    async def rpc(self, method, params, *, timeout=20, before_send=None):
        if self.reader.done():
            raise ConnectionError("Official daemon connection closed")
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            if before_send:
                before_send()
            await self.ws.send(json.dumps({"id": key, "method": method, "params": params}))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)

    async def thread(self, sid):
        result = await self.rpc("thread/read", {"threadId": sid, "includeTurns": False})
        thread = result.get("thread", {})
        if thread.get("id") != sid:
            raise Rejected("Original thread identity was not confirmed")
        return thread

    async def turn_status(self, sid, turn_id):
        """Read the exact callback turn without loading items or resuming it.

        A later successful turn is not evidence for this receipt. Bound both
        pages and the caller's total read time; missing history stays unknown.
        """
        await self.thread(sid)
        cursor = None
        seen = set()
        for _ in range(8):
            result = await self.rpc("thread/turns/list", {
                "threadId": sid, "cursor": cursor, "limit": 50,
                "sortDirection": "desc", "itemsView": "notLoaded",
            })
            rows = result.get("data")
            if not isinstance(rows, list):
                raise ValueError("Invalid native turn page")
            for turn in rows:
                if isinstance(turn, dict) and turn.get("id") == turn_id:
                    status = turn.get("status")
                    return status if isinstance(status, str) else None
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
        return None

    async def close(self):
        self.reader.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await self.reader


@asynccontextmanager
async def connect(home: Path, on_output=None):
    path = home / "app-server-control/app-server-control.sock"
    identity = socket_identity(str(path))
    async with unix_connect(str(path), uri="ws://localhost/rpc", compression=None,
                            max_size=4 * 1024 * 1024, open_timeout=5, close_timeout=1) as ws:
        if socket_identity(str(path)) != identity:
            raise ConnectionError("Selected account's daemon socket changed while connecting")
        client = NativeTasks(ws, on_output)
        try:
            await client.rpc("initialize", {
                "clientInfo": {"name": "cc-remote-async-tasks", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await ws.send(json.dumps({"method": "initialized"}))
            yield client
        finally:
            await client.close()
