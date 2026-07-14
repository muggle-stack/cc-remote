"""CLI test client for Phase 1.

Connects to the relay as a client, streams events, and lets you type prompts.
Commands (each on its own line):
  <text>      send as a query
  /stop       interrupt the running turn
  /reconnect  drop and reconnect (tests replay from per-session cursors)
  /ping       liveness check
  /quit       exit

Env: RELAY_URL (default ws://127.0.0.1:8765/ws), LOGIN_PASSWORD.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import cc_remote.config  # noqa: F401  (triggers .env load)
from cc_remote.log import logger, setup
from cc_remote.protocol import (
    Hello, Interrupt, Ping, Query, ProtocolError,
    deserialize, serialize,
)
from tests.e2e_auth import client_connection

setup("cc_remote.cli", os.environ.get("LOG_LEVEL", "WARNING"))
log = logger("cc_remote.cli")

RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")


class CliClient:
    def __init__(self, url: str, password: str):
        self.url = url
        self.password = password
        self.client_id = uuid.uuid4().hex
        self.cursors: dict[str, int] = {}
        self.generations: dict[str, str] = {}
        self.ws = None
        self._quitting = False
        self._cmd_q: asyncio.Queue = asyncio.Queue()
        self._stdin_task: asyncio.Task | None = None

    async def run(self) -> None:
        self._stdin_task = asyncio.create_task(self._stdin_reader())
        print(f"cli client ready (client_id={self.client_id[:8]})  commands: /stop /reconnect /ping /quit")
        try:
            await self._connection_loop()
        finally:
            if self._stdin_task:
                self._stdin_task.cancel()

    # ---- stdin -> command queue (cancellable) ----

    async def _stdin_reader(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                await self._cmd_q.put("/quit")
                return
            await self._cmd_q.put(line.decode().rstrip("\n"))

    # ---- connection lifecycle with auto-reconnect ----

    async def _connection_loop(self) -> None:
        backoff = 1.0
        while not self._quitting:
            try:
                async with await client_connection(self.url, self.password) as ws:
                    self.ws = ws
                    await self._send(ws, Hello(role="client", client_id=self.client_id,
                                               cursors=(self.cursors or None),
                                               generations=(self.generations or None)))
                    print(f"\n[connected cursors={self.cursors}]")
                    backoff = 1.0
                    await self._session(ws)
            except Exception as e:
                if not self._quitting:
                    print(f"\n[conn error: {e}, retry in {backoff:.0f}s]")
            if not self._quitting:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _session(self, ws) -> None:
        recv = asyncio.create_task(self._receiver(ws))
        cmd = asyncio.create_task(self._cmd_consumer(ws))
        done, pending = await asyncio.wait({recv, cmd}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    async def _cmd_consumer(self, ws) -> None:
        while True:
            line = await self._cmd_q.get()
            if line == "/quit":
                self._quitting = True
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            if line == "/stop":
                await self._send(ws, Interrupt())
                print("[interrupt sent]")
            elif line == "/reconnect":
                print("[reconnecting...]")
                try:
                    await ws.close()
                except Exception:
                    pass
                return  # ends session; _connection_loop reconnects
            elif line == "/ping":
                await self._send(ws, Ping(n=1))
            elif line.startswith("/"):
                print(f"[unknown command: {line}]")
            elif line:
                await self._send(ws, Query(prompt=line, msg_id=uuid.uuid4().hex))
                print(f"[query] {line}")

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                msg = deserialize(raw)
            except ProtocolError as e:
                print(f"\n[bad frame: {e}]")
                continue
            self._handle_event(msg)

    # ---- event rendering + seq tracking ----

    def _handle_event(self, msg) -> None:
        t = msg.type
        sid = getattr(msg, "sid", None)
        generation = getattr(msg, "generation", None)
        if sid and generation:
            self.generations[sid] = generation
        if t == "session_rekey":
            old, new = msg.old_key, msg.session_id
            if old in self.cursors and new not in self.cursors:
                self.cursors[new] = self.cursors[old]
            self.cursors.pop(old, None)
            if old in self.generations and new not in self.generations:
                self.generations[new] = self.generations[old]
            self.generations.pop(old, None)
        if t == "delta":
            sys.stdout.write(msg.text)
            sys.stdout.flush()
        elif t == "user_msg":
            print(f"\n» {msg.prompt}")
        elif t == "assistant_msg_start":
            print(f"\n[assistant {msg.message_id[:8]}] ", end="", flush=True)
        elif t == "assistant_msg_end":
            print()  # newline after a finished assistant message
        elif t == "tool_use":
            inp = json.dumps(msg.input, ensure_ascii=False)
            print(f"\n  [tool_use {msg.tool} id={msg.tool_use_id[:8]} input={inp[:140]}]")
        elif t == "tool_result":
            tag = "ERR" if msg.is_error else "ok"
            trunc = " (truncated)" if msg.truncated else ""
            print(f"\n  [tool_result {tag}{trunc}] {msg.content[:200]}")
        elif t == "state":
            print(f"\n=== state: {msg.state} ===")
        elif t == "turn_end":
            r = msg.result
            print(f"\n--- turn_end subtype={r.subtype} "
                  f"duration={r.duration_ms}ms error={r.is_error} ---")
        elif t == "error":
            print(f"\n!!! error {msg.code}: {msg.message}")
        elif t == "snapshot":
            print(f"\n[snapshot state={msg.state} tail={msg.tail_text[:80]!r}]")
        elif t == "replay_start":
            print(f"\n>>replay {msg.from_seq}->{msg.to_seq} truncated={msg.truncated}")
        elif t == "replay_end":
            print(f"\n<<replay_end to={msg.to_seq} truncated={msg.truncated}")
        elif t == "wrapper_disconnected":
            print("\n[wrapper_disconnected — waiting for reconnect]")
        elif t == "wrapper_reconnected":
            print(f"\n[wrapper_reconnected state={msg.state}]")
        elif t == "pong":
            print(f"[pong {msg.n}]")
        else:
            print(f"\n[{t}]")

        seq = getattr(msg, "seq", None)
        if sid and seq is not None and seq > self.cursors.get(sid, 0):
            self.cursors[sid] = seq

    async def _send(self, ws, msg) -> None:
        try:
            await ws.send(serialize(msg))
        except Exception as e:
            print(f"\n[send failed: {e}]")


def main() -> None:
    client = CliClient(RELAY_URL, LOGIN_PASSWORD)
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print("\n[bye]")


if __name__ == "__main__":
    main()
