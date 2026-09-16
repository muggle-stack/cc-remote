"""Own SDK/CLI lifetimes and pending callbacks outside the Wrapper process.

Disconnecting a controller never interrupts a query. Only explicit ``close`` or
``interrupt`` operations do that. The private journal retains an unacknowledged
human turn from its beginning, including its terminal result, for reconstruction.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from cc_remote.claude_steering import PendingSteers, steer_message

from .wire import decode_sdk, encode_sdk, private_directory, read_frame, same_user, write_frame

MAX_SESSIONS = 64
MAX_CALLS = 64


async def _prompt_messages(messages: list[dict]):
    # Bind the materialized list as an argument. Closing over a variable that
    # is then replaced by this iterator makes the iterator try to iterate itself.
    for message in messages:
        yield message


class Journal:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL)")
        self.seq = 0
        self.size = 0

    def append(self, value: dict) -> int:
        payload = json.dumps(value, ensure_ascii=False)
        self.seq += 1
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (self.seq, time.time(), payload))
        self.db.commit()
        self.size += len(payload.encode())
        return self.seq

    def after(self, seq: int) -> list[dict]:
        # Bound the batch by bytes as well as count; a single SDK frame can be
        # much larger than a normal text delta.
        result = []
        size = 0
        for key, ts, payload in self.db.execute(
            "SELECT seq, ts, payload FROM events WHERE seq > ? ORDER BY seq LIMIT 32", (seq,)
        ):
            if result and size + len(payload.encode()) > 16 * 1024 * 1024:
                break
            size += len(payload.encode())
            result.append({"seq": key, "ts": ts, "data": json.loads(payload)})
        return result

    def prune(self, seq: int) -> None:
        self.db.execute("DELETE FROM events WHERE seq <= ?", (seq,))
        self.db.commit()
        self.size = self.db.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0) FROM events"
        ).fetchone()[0]

    def close(self) -> None:
        self.db.close()


def _human_result(data: dict) -> bool:
    origin = data.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return data.get("type") == "result" and kind in (None, "human")


class Session:
    def __init__(self, directory: Path, metadata: dict, factory=None):
        self.id = uuid4().hex
        self.metadata = dict(metadata)
        self.journal_path = directory / f"{self.id}.sqlite3"
        self.journal = Journal(self.journal_path)
        self.factory = factory
        self.client = None
        self.controller = None
        self.reader = None
        self.changed = asyncio.Condition()
        self.callbacks: dict[str, tuple[dict, asyncio.Future]] = {}
        self.callback_answers: dict[str, object] = {}
        self.initializers: dict[str, dict] = {}
        self.turn: dict | None = None
        self.steers = PendingSteers()
        self.origin_id: str | None = None
        self.ack = 0
        self.terminal_seq: int | None = None
        self.failure: str | None = None
        self.controls: dict = {}
        self.task_seeds: dict[str, dict] = {}
        self.background_turns: list[dict] = []
        self.submitted_turns: set[str] = set()
        self.question_answers: dict[str, object] = {}
        self.closed = False
        self.mutations: dict[str, asyncio.Task] = {}
        self.mutation_fingerprints: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    @property
    def background_start(self) -> int | None:
        return self.background_turns[0]["start_seq"] if self.background_turns else None

    async def callback(self, kind: str, payload: dict):
        if len(self.callbacks) >= MAX_CALLS:
            raise RuntimeError("Claude callback capacity reached")
        key = "claude-call-" + uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.callbacks[key] = ({"id": key, "kind": kind, **payload}, future)
        await self.notify()
        try:
            return await future
        finally:
            self.callbacks.pop(key, None)
            await self.notify()

    def mcp_proxy(self, name: str):
        import anyio
        from mcp import types
        from mcp.server import Server
        from mcp.shared.message import SessionMessage

        session = self

        class Proxy(Server):
            async def run(self, read_stream, write_stream, initialization_options, **kwargs):
                async def forward(item):
                    value = item.message.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if value.get("method") == "initialize":
                        session.initializers[name] = value
                    reply = await session.callback("mcp", {"name": name, "message": value})
                    if reply is not None:
                        await write_stream.send(SessionMessage(types.JSONRPCMessage.model_validate(reply)))

                async with anyio.create_task_group() as group:
                    async for item in read_stream:
                        group.start_soon(forward, item)

        return Proxy(name)

    async def start(self, options: dict, isolated: bool) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from cc_remote.wrapper.claude_transport import account_isolated_transport

        async def permission(name, tool_input, context):
            # The SDK's signal is process-local; pending native requests remain
            # owned here. All public context fields retain their SDK types.
            context = dataclasses.replace(context, signal=None)
            answer = await self.callback("permission", {
                "name": name, "input": tool_input, "context": encode_sdk(context),
            })
            return decode_sdk(answer)

        values = decode_sdk(options)
        servers = values.get("mcp_servers", {})
        if isinstance(servers, dict):
            for name, config in servers.items():
                if config.get("type") == "sdk":
                    config["instance"] = self.mcp_proxy(name)
        opts = ClaudeAgentOptions(**values, can_use_tool=permission, stderr=lambda _: None)
        self.controls = {"model": opts.model, "permission_mode": opts.permission_mode,
                         "effort": opts.effort}
        factory = self.factory or ClaudeSDKClient
        kwargs = {"options": opts}
        if isolated:
            kwargs["transport"] = account_isolated_transport(opts)
        self.client = factory(**kwargs)
        await self.client.connect()
        self.reader = asyncio.create_task(self.read_messages())

    async def read_messages(self) -> None:
        try:
            async for value in self.client._query.receive_messages():
                if self.closed:
                    return
                value = self.steers.annotate(value)
                if "__cc_steer" in value:
                    self.origin_id = value["__cc_steer"]["id"]
                # The journal is on disk, like Claude's own transcript. Do not
                # stop the sole native reader at a per-turn byte cap: an offline
                # long turn could then never deliver the Result that frees it.
                seq = self.journal.append(value)
                origin = value.get("origin")
                kind = origin.get("kind") if isinstance(origin, dict) else None
                if value.get("type") == "user" and kind not in (None, "human"):
                    self.background_turns.append({"start_seq": seq - 1, "terminal_seq": None})
                if value.get("type") == "result" and kind not in (None, "human"):
                    for turn in reversed(self.background_turns):
                        if turn["terminal_seq"] is None:
                            turn["terminal_seq"] = seq
                            break
                if value.get("type") == "system":
                    subtype = value.get("subtype")
                    task_id = value.get("task_id")
                    if subtype in {"task_started", "task_notification"} and task_id:
                        self.task_seeds[str(task_id)] = {
                            "data": value, "origin_id": self.origin_id, "seq": seq,
                        }
                sid = value.get("session_id")
                if isinstance(sid, str) and sid:
                    self.metadata["session_id"] = sid
                if (self.turn is not None and _human_result(value)
                        and not value.get("__cc_steer_intermediate")):
                    self.terminal_seq = seq
                await self.notify()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = type(exc).__name__
        else:
            self.failure = "SDKStreamClosed"
        finally:
            await self.notify()

    def description(self) -> dict:
        return {
            "id": self.id, "metadata": self.metadata, "turn": self.turn,
            "origin_id": self.origin_id, "terminal_seq": self.terminal_seq,
            "after": self.turn["start_seq"] if self.turn else (
                min(self.ack, self.background_start)
                if self.background_start is not None else self.ack),
            "initializers": self.initializers, "failure": self.failure,
            "head": self.journal.seq, "controls": self.controls, "pid": os.getpid(),
            "task_seeds": list(self.task_seeds.values()),
            "native_steering": True,
        }

    async def events(self, after: int) -> dict:
        async with self.changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait_for(
                    lambda: self.closed or self.failure or self.journal.seq > after
                ), 20)
        return {"events": self.journal.after(after), "failure": self.failure}

    async def pending(self, known: list[str]) -> dict:
        async with self.changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait_for(
                    lambda: self.closed or set(known) != {
                        key for key, (_, future) in self.callbacks.items() if not future.done()
                    }
                ), 20)
        pending = {key: value for key, (value, future) in self.callbacks.items() if not future.done()}
        return {"pending": [value for key, value in pending.items() if key not in known],
                "closed": [key for key in known if key not in pending]}

    async def mutate(self, request_id: str, method: str, params: dict):
        # Accepted writes outlive their connection. A lost acknowledgement may
        # be retried with the same ID but must never submit a second model turn.
        identity = [method, params]
        if method == "steer" and params.get("metadata", {}).get("fingerprint"):
            # A controller replacement may stage identical attachment bytes at
            # another private path. Compare the original browser payload digest,
            # not those incidental paths; the first mutation keeps its payload.
            identity = [method, params["turn_id"], params["native_id"],
                        params["metadata"]["fingerprint"]]
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        previous = self.mutation_fingerprints.get(request_id)
        if previous is not None and previous != fingerprint:
            raise ValueError("Claude request identity was reused with different data")
        task = self.mutations.get(request_id)
        if task is None:
            if len(self.mutations) >= 1024:
                done = [key for key, value in self.mutations.items() if value.done()]
                for key in done[:512]:
                    self.mutations.pop(key)
                    self.mutation_fingerprints.pop(key, None)
            if len(self.mutations) >= 1024:
                raise RuntimeError("Claude service request capacity reached")
            task = asyncio.create_task(self._mutate(method, params))
            self.mutations[request_id] = task
            self.mutation_fingerprints[request_id] = fingerprint
        return await asyncio.shield(task)

    async def _mutate(self, method: str, params: dict):
        if method == "remember_answer":
            key = params["ask_id"]
            if key in self.question_answers and self.question_answers[key] != params["answer"]:
                raise ValueError("Claude question was already answered differently")
            self.question_answers[key] = params["answer"]
            return None
        if method == "answer":
            key = params["callback_id"]
            pending = self.callbacks.get(key)
            if pending is not None and not pending[1].done():
                pending[1].set_result(params.get("value"))
                self.callback_answers[key] = True
            elif key not in self.callback_answers:
                raise ValueError("Claude callback is no longer pending")
            return None
        if method == "interrupt":
            return await self.steers.interrupt(self.client)
        if method == "control":
            result = await self.client._query._send_control_request(
                params["request"], timeout=params.get("timeout", 60))
            request = params["request"]
            if request.get("subtype") == "set_model":
                self.controls["model"] = request.get("model")
            elif request.get("subtype") == "set_permission_mode":
                self.controls["permission_mode"] = request.get("mode")
            return result
        async with self.lock:
            if method == "steer":
                if (self.turn is None or self.terminal_seq is not None
                        or self.failure or params["turn_id"] != self.turn["id"]):
                    return False
                self.steers.add(params["native_id"], params["metadata"])

                async def stream():
                    yield steer_message(params["prompt"], params["native_id"])

                await self.client.query(stream())
                return True
            if method == "query":
                if params["turn"]["id"] in self.submitted_turns:
                    raise ValueError("Claude turn was already submitted")
                if self.turn is not None or self.background_start is not None or self.failure:
                    raise RuntimeError("Claude session is busy or unavailable")
                prompt = params["prompt"]
                if isinstance(prompt, list):
                    if not prompt or not all(isinstance(item, dict) for item in prompt):
                        raise ValueError("Claude prompt must contain message objects")
                    # Validate before claiming the turn. Once a transport write
                    # starts, an exception cannot prove that delivery failed.
                    json.dumps(prompt)
                    prompt = _prompt_messages(prompt)
                elif not isinstance(prompt, str):
                    raise ValueError("Claude prompt must be text or message objects")
                self.turn = {**params["turn"], "start_seq": self.journal.seq, "started_at": time.time()}
                self.origin_id = self.turn["id"]
                self.terminal_seq = None
                self.submitted_turns.add(self.turn["id"])
                self.callback_answers.clear()
                self.task_seeds = {
                    key: seed for key, seed in self.task_seeds.items()
                    if seed["data"].get("subtype") != "task_notification"
                }
                # Retain the accepted operation even on a transport exception:
                # acceptance may be unknown, so automatic resubmission is unsafe.
                await self.client.query(prompt)
                return None
            if method == "commit":
                if self.turn and params["turn_id"] == self.turn["id"]:
                    if self.terminal_seq is None or params["seq"] != self.terminal_seq:
                        raise ValueError("Claude terminal was not acknowledged exactly")
                    self.ack = max(self.ack, self.terminal_seq)
                    self.turn = None
                    self.journal.prune(
                        min(self.ack, self.background_start)
                        if self.background_start is not None else self.ack)
                    await self.notify()
                return None
            if method == "ack":
                seq = params["seq"]
                if not 0 <= seq <= self.journal.seq:
                    raise ValueError("invalid Claude journal acknowledgement")
                self.ack = max(self.ack, seq)
                retired = [turn["terminal_seq"] for turn in self.background_turns
                           if turn["terminal_seq"] is not None and seq >= turn["terminal_seq"]]
                if retired:
                    self.background_turns = [turn for turn in self.background_turns
                                             if turn["terminal_seq"] not in retired]
                    self.task_seeds = {
                        key: seed for key, seed in self.task_seeds.items()
                        if seed["data"].get("subtype") != "task_notification"
                        or seed["seq"] > max(retired)
                    }
                boundary = min(self.ack, self.turn["start_seq"]) if self.turn else self.ack
                if self.background_start is not None:
                    boundary = min(boundary, self.background_start)
                self.journal.prune(boundary)
                await self.notify()
                return None
            if method == "metadata":
                self.metadata.update(params["value"])
                return None
        raise ValueError("unknown Claude service operation")

    async def close(self) -> None:
        self.closed = True
        await self.notify()
        if self.reader is not None:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.client is not None:
            await self.client.disconnect()
        for _, future in self.callbacks.values():
            future.cancel()
        self.journal.close()
        self.journal_path.unlink(missing_ok=True)


class Service:
    def __init__(self, directory: Path, *, factory=None):
        private_directory(directory)
        self.directory = directory
        self.factory = factory
        self.sessions: dict[str, Session] = {}
        self.open_lock = asyncio.Lock()

    async def connection(self, reader, writer) -> None:
        if not same_user(writer):
            writer.close()
            return
        owner = object()
        send_lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        async def request(frame):
            key = frame.get("id")
            try:
                value = await self.dispatch(owner, key, frame["method"], frame.get("params", {}))
                reply = {"id": key, "result": value}
            except Exception as exc:
                # SDK errors may contain account/provider data; the local client
                # gets a classification, never a credential-bearing traceback.
                from cc_remote.wrapper.sdk import _is_control_request_timeout

                subtype = frame.get("params", {}).get("request", {}).get("subtype", "")
                reply = {"id": key, "error": type(exc).__name__, "timeout": bool(
                    subtype and _is_control_request_timeout(exc, subtype=subtype))}
            async with send_lock:
                await write_frame(writer, reply)

        try:
            while True:
                frame = await read_frame(reader)
                if len(tasks) >= MAX_CALLS:
                    raise ValueError("too many Claude service requests")
                task = asyncio.create_task(request(frame))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            # Cancel connection waiters, never the shielded mutations or SDK.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for session in self.sessions.values():
                if session.controller is owner:
                    session.controller = None
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def dispatch(self, owner, request_id, method, params):
        if method == "hello":
            from importlib.metadata import version

            return {"sdk_version": version("claude-agent-sdk"), "pid": os.getpid(),
                    "source": str(Path(__file__).resolve().parents[2])}
        if method == "list":
            return [session.description() for session in self.sessions.values() if not session.closed]
        if method == "open":
            async with self.open_lock:
                identity = params["metadata"]
                session = self.sessions.get(params.get("session"))
                if session is None and identity.get("session_id") and not params.get("fork"):
                    session = next((item for item in self.sessions.values() if all(
                        item.metadata.get(key) == identity.get(key)
                        for key in ("profile_root", "session_id", "space", "work_id", "btw")
                    )), None)
                attached = session is not None
                if session is not None:
                    if any(session.metadata.get(key) != identity.get(key) for key in (
                        "profile_root", "session_id", "space", "work_id", "btw", "cwd",
                    )):
                        raise PermissionError("Claude session identity mismatch")
                    if session.controller is not None and session.controller is not owner:
                        raise RuntimeError("Claude service already has a controller")
                else:
                    if len(self.sessions) >= MAX_SESSIONS:
                        raise RuntimeError("Claude service session capacity reached")
                    session = Session(self.directory, identity, self.factory)
                    self.sessions[session.id] = session
                    try:
                        await session.start(params["options"], params.get("isolated", False))
                    except BaseException:
                        self.sessions.pop(session.id, None)
                        await session.close()
                        raise
                session.controller = owner
                return {**session.description(), "attached": attached}
        session = self.sessions[params["session"]]
        if session.controller is not owner:
            raise PermissionError("Claude service controller lease is required")
        if method == "events":
            return await session.events(params["after"])
        if method == "callbacks":
            return await session.pending(params.get("known", []))
        if method == "question_answer":
            key = params["ask_id"]
            return {"found": key in session.question_answers, "answer": session.question_answers.get(key)}
        if method == "close":
            await session.close()
            self.sessions.pop(session.id, None)
            return None
        return await session.mutate(request_id, method, params)
