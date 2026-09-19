"""SDK-shaped controller for the private persistent service."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import os
from uuid import uuid4

from cc_remote.claude_steering import ClaudeSteerRejected
from cc_remote.log import logger

from .wire import ControllerLeaseConflict, decode_sdk, encode_sdk, read_frame, write_frame

log = logger("cc_remote.claude_service.client")

CONTROLLER_LEASE_WAIT_SECONDS = 5.0
CONTROLLER_LEASE_RETRY_DELAY = 0.1

callback_identity: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "claude_service_callback", default=None)


class Connection:
    def __init__(self, socket_path: str):
        self.socket_path = os.path.expanduser(socket_path)
        self.reader = None
        self.writer = None
        self.task = None
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                reply = await read_frame(self.reader)
                future = self.pending.get(reply.get("id"))
                if future is not None and not future.done():
                    if "error" in reply:
                        error_type = TimeoutError if reply.get("timeout") else RuntimeError
                        if reply["error"] == "ControllerLeaseConflict":
                            error_type = ControllerLeaseConflict
                        future.set_exception(error_type("Claude service: " + reply["error"]))
                    else:
                        future.set_result(reply.get("result"))
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Claude service connection closed"))

    async def call(self, method: str, params: dict | None = None, *, request_id=None, timeout=90):
        if self.task is None or self.task.done():
            raise ConnectionError("Claude service is not connected")
        key = request_id or uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            async with self.lock:
                await write_frame(self.writer, {"id": key, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(key, None)

    async def disconnect(self) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(ConnectionError):
                await self.writer.wait_closed()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def options_payload(options) -> dict:
    # Callables and live MCP objects stay in their owning processes. Provider
    # environment goes over the same-user socket only, never to disk/relay/log.
    omitted = {"can_use_tool", "stderr", "debug_stderr", "mcp_servers"}
    if options.hooks or options.session_store:
        raise ValueError("custom SDK hooks/session stores require a service adapter")
    result = {
        field.name: encode_sdk(getattr(options, field.name))
        for field in dataclasses.fields(options) if field.name not in omitted
    }
    servers = options.mcp_servers
    if isinstance(servers, dict):
        result["mcp_servers"] = {
            name: encode_sdk({key: value for key, value in config.items() if key != "instance"})
            for name, config in servers.items()
        }
    else:
        result["mcp_servers"] = encode_sdk(servers)
    return result


class RemoteClient:
    def __init__(self, socket_path, *, options, metadata, isolated=False):
        self.connection = Connection(socket_path)
        self.options = options
        self.metadata = metadata
        self.isolated = isolated
        self.description: dict = {}
        self.id = None
        self._query = self
        self.callback_task = None
        self.callback_tasks: dict[str, asyncio.Task] = {}
        self.bridges = {}
        self.bridge_ready: set[str] = set()
        self.bridge_locks: dict[str, asyncio.Lock] = {}
        self.next_turn: dict | None = None
        self.recovery: dict | None = None
        self.last_seq = 0
        self.ready = asyncio.Event()
        self.owner_identity = None

    async def connect(self) -> None:
        await self.connection.connect()
        try:
            from importlib.metadata import version

            hello = await self.connection.call("hello")
            if hello["sdk_version"] != version("claude-agent-sdk"):
                raise RuntimeError("Claude service SDK differs from this Wrapper; drain before upgrading it")
            from cc_remote.wrapper.child_env import sanitized_child_env, claude_sdk_process_env

            payload = options_payload(self.options)
            environment = sanitized_child_env()
            payload["env"] = (
                claude_sdk_process_env(self.options.env, environment)
                if self.isolated else {**environment, **self.options.env}
            )
            self.description = await self._open({
                "options": payload,
                "metadata": self.metadata,
                "isolated": self.isolated,
                "fork": self.options.fork_session,
                "session": self.metadata.get("service_id"),
                "strict_session": bool(hello.get("strict_controller_leases")),
            }, legacy=not hello.get("strict_controller_leases"))
        except BaseException:
            await self.connection.disconnect()
            raise
        self.id = self.description["id"]
        from cc_remote.wrapper.process_scan import process_identity

        self.owner_identity = process_identity(self.description["pid"])
        self.recovery = self.description["turn"]
        self.last_seq = self.description["after"]
        self.callback_task = asyncio.create_task(self._callbacks())

    async def _open(self, params, *, legacy):
        if params["session"] is None:
            return await self.connection.call("open", params)
        # Only recovery of a listed worker may wait for the previous socket's
        # finally block. Never retry an unknown open response or resubmit Query.
        async with asyncio.timeout(None) as wait:
            while True:
                try:
                    return await self.connection.call("open", params)
                except RuntimeError as exc:
                    conflict = isinstance(exc, ControllerLeaseConflict)
                    if not conflict and (not legacy or str(exc) != "Claude service: RuntimeError"):
                        raise
                    if wait.when() is None:
                        wait.reschedule(asyncio.get_running_loop().time() + CONTROLLER_LEASE_WAIT_SECONDS)
                    # Older immutable services report only exception names.
                    # Their existing-worker open has exactly one RuntimeError:
                    # an occupied lease. Recheck its full identity before retry.
                    if not conflict:
                        sessions = await self.connection.call("list")
                        if not any(item["id"] == params["session"] and all(
                            item["metadata"].get(key) == self.metadata.get(key)
                            for key in ("profile_root", "session_id", "space", "work_id", "btw", "cwd")
                        ) for item in sessions):
                            raise
                await asyncio.sleep(CONTROLLER_LEASE_RETRY_DELAY)

    async def call(self, method, params=None, **kwargs):
        return await self.connection.call(method, {"session": self.id, **(params or {})}, **kwargs)

    async def query(self, prompt) -> None:
        if not isinstance(prompt, str):
            prompt = [item async for item in prompt]
        turn = self.next_turn
        if not turn or not turn.get("id"):
            raise ValueError("persistent Claude query requires its original turn identity")
        self.next_turn = None
        await self.call("query", {"prompt": prompt, "turn": turn}, request_id="query-" + turn["id"])

    async def receive_messages(self):
        await self.ready.wait()
        for seed in self.description.get("task_seeds", []):
            if seed["seq"] <= self.last_seq:
                yield {**seed["data"], "__cc_service_origin": seed["origin_id"]}
        while True:
            result = await self.call("events", {"after": self.last_seq})
            for event in result["events"]:
                self.last_seq = event["seq"]
                yield {**event["data"], "__cc_service_seq": event["seq"],
                       "__cc_service_ts": event["ts"]}
                if event["seq"] == self.description["head"]:
                    # Even an ignored rate-limit/extension frame can be the
                    # last backlog row. Flush reconstruction without waiting
                    # for the model to produce another visible event.
                    yield {"type": "system", "subtype": "cc_remote_service_replay_end",
                           "__cc_service_seq": event["seq"]}
            if result["failure"] and not result["events"]:
                raise RuntimeError("Claude SDK stream ended: " + result["failure"])

    async def steer(self, prompt, *, native_id, metadata, turn_id, background_id=None):
        if not self.description.get("native_steering"):
            raise ClaudeSteerRejected("Claude service requires a steering upgrade")
        if background_id is not None and not self.description.get("background_steering"):
            raise ClaudeSteerRejected("Claude service requires a background steering upgrade")
        accepted = await self.call("steer", {
            "prompt": prompt, "native_id": native_id,
            "metadata": metadata, "turn_id": turn_id,
            **({"background_id": background_id} if background_id is not None else {}),
        }, request_id="steer-" + native_id)
        if not accepted:
            raise ClaudeSteerRejected("Claude response has already ended")

    async def _callbacks(self) -> None:
        await self.ready.wait()
        while True:
            result = await self.call("callbacks", {"known": list(self.callback_tasks)})
            for key in result["closed"]:
                task = self.callback_tasks.pop(key, None)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            for call in result["pending"]:
                key = call["id"]
                if key not in self.callback_tasks:
                    self.callback_tasks[key] = asyncio.create_task(self._callback(call))

    async def _callback(self, call) -> None:
        token = callback_identity.set(call["id"])
        handled = False
        value = None
        delay = 0.25
        try:
            while True:
                try:
                    if not handled:
                        if call["kind"] == "permission":
                            value = encode_sdk(await self.options.can_use_tool(
                                call["name"], call["input"], decode_sdk(call["context"])))
                        elif call["kind"] == "mcp":
                            value = await self._mcp(call["name"], call["message"])
                        else:
                            raise ValueError("unknown Claude callback")
                        handled = True
                    # An unknown answer delivery must reuse both the value and
                    # request identity, never execute the tool a second time.
                    await self.call("answer", {"callback_id": call["id"], "value": value},
                                    request_id="answer-" + call["id"])
                    return
                except Exception as exc:
                    if self.connection.task is None or self.connection.task.done():
                        raise
                    # Keep this known callback alive until the service closes
                    # it or the controller detaches. Cancellation must escape.
                    log.warning("Claude service callback will retry", callback_id=call["id"],
                                kind=call["kind"], stage="answer" if handled else "handler",
                                error=type(exc).__name__)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5)
        finally:
            callback_identity.reset(token)

    async def _mcp(self, name: str, message: dict):
        from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge

        if name not in self.bridges:
            self.bridges[name] = SdkMcpBridge(name, self.options.mcp_servers[name]["instance"])
            self.bridge_locks[name] = asyncio.Lock()
        bridge = self.bridges[name]
        async with self.bridge_locks[name]:
            if name not in self.bridge_ready:
                initialize = self.description.get("initializers", {}).get(name)
                if message.get("method") != "initialize" and initialize:
                    await bridge.handle(initialize)
                    await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.bridge_ready.add(name)
        return await bridge.handle(message)

    async def _send_control_request(self, request, timeout=60):
        return await self.call("control", {"request": request, "timeout": timeout}, timeout=timeout + 10)

    async def interrupt(self):
        return await self.call("interrupt")

    async def set_model(self, model):
        return await self._send_control_request({"subtype": "set_model", "model": model})

    async def set_permission_mode(self, mode):
        return await self._send_control_request({"subtype": "set_permission_mode", "mode": mode})

    async def rewind_files(self, user_message_id):
        return await self._send_control_request({"subtype": "rewind_files", "user_message_id": user_message_id})

    async def detach(self) -> None:
        # Close the socket first: cancelled Wrapper callbacks must not become
        # negative native answers. The service retains their original Futures.
        await self.connection.disconnect()
        tasks = [*self.callback_tasks.values()]
        if self.callback_task is not None:
            tasks.append(self.callback_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for bridge in self.bridges.values():
            await bridge.aclose()

    async def disconnect(self) -> None:
        try:
            await self.call("close")
        finally:
            await self.detach()
