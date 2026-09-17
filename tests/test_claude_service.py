"""Zero-model regression for SDK lifetime across controller replacement."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import sys
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, ToolPermissionContext

from cc_remote.claude_service.client import RemoteClient
from cc_remote.claude_service.server import Service


def test_private_socket_registration_and_explicit_override(tmp_path, monkeypatch):
    from cc_remote.config import WrapperConfig, _claude_service_socket, wrapper_config

    monkeypatch.setenv("CC_REMOTE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CC_REMOTE_CLAUDE_SERVICE_SOCKET", raising=False)
    assert _claude_service_socket() == ""
    registration = tmp_path / "claude-service.json"
    registration.write_text('{"socket": "/private/service.sock"}')
    registration.chmod(0o600)
    assert _claude_service_socket() == "/private/service.sock"
    assert wrapper_config().claude_service_socket == "/private/service.sock"
    assert WrapperConfig().claude_service_socket == ""
    monkeypatch.setenv("CC_REMOTE_CLAUDE_SERVICE_SOCKET", "/override.sock")
    assert _claude_service_socket() == "/override.sock"
    monkeypatch.setenv("CC_REMOTE_CLAUDE_SERVICE_SOCKET", "")
    assert _claude_service_socket() == ""
    monkeypatch.delenv("CC_REMOTE_CLAUDE_SERVICE_SOCKET")
    registration.chmod(0o644)
    with pytest.raises(ValueError):
        _claude_service_socket()
    registration.unlink()
    registration.symlink_to(tmp_path / "missing.json")
    with pytest.raises(OSError):
        _claude_service_socket()


class FakeClient:
    def __init__(self, *, options, **kwargs):
        self.options = options
        self._query = self
        self.queue = asyncio.Queue()
        self.prompts = []
        self.closed = False
        self.interrupts = 0

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def interrupt(self):
        self.interrupts += 1
        await self.queue.put({"type": "result", "subtype": "error_during_execution"})

    async def receive_messages(self):
        while True:
            yield await self.queue.get()


@contextlib.asynccontextmanager
async def environment():
    # Darwin sockaddr_un is short; pytest's normal temp hierarchy exceeds it.
    with tempfile.TemporaryDirectory(prefix="cc-sdk-", dir="/tmp") as root:
        directory = Path(root)
        service = Service(directory, factory=FakeClient)
        path = directory / "service.sock"
        server = await asyncio.start_unix_server(service.connection, path)
        clients = []

        async def attach(*, profile="primary", permission=None, mcp_server=None,
                         session_id="native-session"):
            options = ClaudeAgentOptions(can_use_tool=permission, mcp_servers=(
                {"ask": {"type": "sdk", "name": "ask", "instance": mcp_server}}
                if mcp_server is not None else {}))
            client = RemoteClient(str(path), options=options, metadata={
                "profile_root": profile, "session_id": session_id, "space": "code",
                "applied_auto_compact": ["inherit", None], "applied_effort": "max",
            })
            clients.append(client)
            await client.connect()
            client.ready.set()
            return client

        try:
            yield service, attach
        finally:
            for client in clients:
                await client.detach()
            server.close()
            await server.wait_closed()
            for session in service.sessions.values():
                await session.close()


async def released(session):
    async with asyncio.timeout(2):
        while session.controller is not None:
            await asyncio.sleep(0.001)


def test_materialized_image_prompt_reaches_real_sdk_transport(tmp_path):
    import json
    from types import SimpleNamespace
    from claude_agent_sdk import ClaudeSDKClient
    from cc_remote.claude_service.server import Session

    async def run():
        writes = []

        class Transport:
            async def write(self, payload):
                writes.append(json.loads(payload))

        session = Session(tmp_path, {})
        client = ClaudeSDKClient()
        client._query = SimpleNamespace()
        client._transport = Transport()
        session.client = client
        message = {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "inspect attachment"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
        ]}}
        try:
            await session.mutate("request", "query", {"turn": {"id": "image"}, "prompt": [message]})
            assert len(writes) == 1
            assert writes[0]["message"] == message["message"]
            assert session.turn["id"] == "image"
            session.terminal_seq = session.journal.append({"type": "result"})
            await session.mutate("commit", "commit", {"turn_id": "image", "seq": session.terminal_seq})
            await session.mutate("next", "query", {"turn": {"id": "text"}, "prompt": "next"})
            assert len(writes) == 2
        finally:
            session.journal.close()

    asyncio.run(run())


def test_invalid_prompt_does_not_claim_turn_but_unknown_delivery_does(tmp_path):
    from types import SimpleNamespace
    from cc_remote.claude_service.server import Session

    async def run():
        async def uncertain_delivery(prompt):
            raise ConnectionError("write may have reached native CLI")

        session = Session(tmp_path, {})
        session.client = SimpleNamespace(query=uncertain_delivery)
        try:
            with pytest.raises(ValueError):
                await session.mutate("bad", "query", {"turn": {"id": "bad"}, "prompt": [42]})
            assert session.turn is None
            assert not session.submitted_turns
            with pytest.raises(ConnectionError):
                await session.mutate("unknown", "query", {"turn": {"id": "unknown"}, "prompt": "hi"})
            assert session.turn["id"] == "unknown"
            with pytest.raises(RuntimeError):
                await session.mutate("new", "query", {"turn": {"id": "new"}, "prompt": "next"})
        finally:
            session.journal.close()

    asyncio.run(run())


@pytest.mark.parametrize("control_error", [None, "unsupported", "timeout"])
def test_attached_summary_opt_in_keeps_running_turn_and_native_child(
    monkeypatch, control_error,
):
    from cc_remote.config import WrapperConfig
    from cc_remote.protocol import Delta, TurnEnd
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk

    calls = []

    async def control(client, request, timeout):
        calls.append((request, timeout))
        if control_error == "unsupported":
            raise RuntimeError("unknown control subtype")
        if control_error == "timeout":
            raise TimeoutError("control request timeout")
        return {}

    monkeypatch.setattr(FakeClient, "_send_control_request", control, raising=False)

    async def go():
        async with environment() as (service, attach):
            first = await attach(session_id=SESSION_ID)
            first.next_turn = {"id": "browser-msg", "prompt": "hello"}
            await first.query("hello")
            worker = service.sessions[first.id]
            native = worker.client
            original_controls = worker.controls.copy()
            await first.detach()
            await released(worker)

            handle = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
            handle.service_metadata = worker.metadata.copy()
            handle.service_defer_events = True
            try:
                await handle.connect(resume_id=SESSION_ID, cwd="/tmp")
                assert handle.client.description["attached"] is True
                assert calls == [({
                    "subtype": "set_max_thinking_tokens",
                    "thinking_display": "summarized",
                }, 2.0)]
                assert worker.client is native
                assert native.closed is False and native.interrupts == 0
                assert native.prompts == ["hello"]
                assert worker.controls == original_controls
                assert handle.service_recovery["id"] == "browser-msg"

                # A display rejection/timeout must still let the original
                # stream and terminal cross the service and Wrapper boundary.
                # This injected delta verifies transport, not native timing:
                # an active CLI loop may defer summaries to the next query.
                channel = "text" if control_error else "thinking"
                await native.queue.put({
                    "type": "stream_event", "uuid": "summary-1", "session_id": SESSION_ID,
                    "event": {"type": "content_block_delta", "index": 0, "delta": {
                        "type": f"{channel}_delta", channel: "Inspecting the build configuration.",
                    }},
                })
                await native.queue.put({
                    "type": "result", "subtype": "success", "duration_ms": 20,
                    "duration_api_ms": 19, "is_error": False, "num_turns": 1,
                    "session_id": SESSION_ID,
                })
                machine, transport, ctx = _machine_with_sdk(handle)
                ctx.active_msg_id = "browser-msg"
                ctx.state = "running"
                handle.start_service_events()
                await asyncio.wait_for(machine._run_turn(ctx, "hello", _recover_service=True), 3)
                deltas = [item for item in transport.sent if isinstance(item, Delta)]
                assert any(item.text == "Inspecting the build configuration." for item in deltas)
                if not control_error:
                    assert any(item.channel == "thinking" for item in deltas)
                assert len([item for item in transport.sent if isinstance(item, TurnEnd)]) == 1
                assert native.prompts == ["hello"]
                assert native.interrupts == 0 and native.closed is False
                assert worker.turn is None
            finally:
                await handle.detach_for_shutdown()

    asyncio.run(go())


def test_running_query_and_offline_terminal_survive_wrapper_disconnect():
    async def go():
        async with environment() as (service, attach):
            first = await attach()
            first.next_turn = {"id": "browser-msg", "prompt": "hello"}
            await first.query("hello")
            worker = service.sessions[first.id]
            native = worker.client
            await native.queue.put({"type": "assistant", "message": {"content": "part one"}})
            stream = first.receive_messages()
            assert (await anext(stream))["message"]["content"] == "part one"
            await first.detach()
            await released(worker)
            assert not native.closed
            assert native.interrupts == 0
            await native.queue.put({"type": "assistant", "message": {"content": "part two"}})
            await native.queue.put({"type": "result", "subtype": "success"})
            second = await attach()
            assert second.id == first.id
            assert second.recovery["id"] == "browser-msg"
            replay = second.receive_messages()
            assert (await anext(replay))["message"]["content"] == "part one"
            assert (await anext(replay))["message"]["content"] == "part two"
            terminal = await anext(replay)
            await second.call("commit", {"turn_id": "browser-msg", "seq": terminal["__cc_service_seq"]})
            assert native.prompts == ["hello"]
            assert worker.turn is None
            await second.disconnect()
            assert native.closed
    asyncio.run(go())


def test_mcp_tool_call_survives_controller_and_server_replacement():
    from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge
    from mcp import types
    from mcp.server import Server

    async def go():
        asked = asyncio.Event()

        def ask_server(answer=None):
            server = Server("ask")

            @server.call_tool()
            async def call_tool(name, arguments):
                asked.set()
                if answer is None:
                    await asyncio.Event().wait()
                return [types.TextContent(type="text", text=answer)]

            return server

        async with environment() as (service, attach):
            first = await attach(mcp_server=ask_server())
            worker = service.sessions[first.id]
            native = SdkMcpBridge("ask", worker.client.options.mcp_servers["ask"]["instance"])
            try:
                initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "native-test", "version": "1"},
                }}
                await asyncio.wait_for(native.handle(initialize), 3)
                await native.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
                request = asyncio.create_task(native.handle({
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "ask_user", "arguments": {}},
                }))
                await asyncio.wait_for(asked.wait(), 3)
                await first.detach()
                await released(worker)
                assert not request.done()
                await attach(mcp_server=ask_server("answered after deployment"))
                response = await asyncio.wait_for(request, 3)
                assert response["result"]["content"][0]["text"] == "answered after deployment"
                assert not worker.client.closed
            finally:
                await native.aclose()
    asyncio.run(go())


def test_pending_permission_keeps_its_future_until_replacement_answers():
    async def go():
        first_asked = asyncio.Event()

        async def unavailable(*args):
            first_asked.set()
            await asyncio.Event().wait()

        async def allow(name, arguments, context):
            assert name == "Bash"
            assert context.tool_use_id == "tool-1"
            return PermissionResultAllow()

        async with environment() as (service, attach):
            first = await attach(permission=unavailable)
            worker = service.sessions[first.id]
            permission = asyncio.create_task(worker.client.options.can_use_tool(
                "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id="tool-1")))
            await asyncio.wait_for(first_asked.wait(), 2)
            keys = list(worker.callbacks)
            await first.detach()
            await released(worker)
            assert not permission.done()
            assert list(worker.callbacks) == keys
            await attach(permission=allow)
            result = await asyncio.wait_for(permission, 2)
            assert isinstance(result, PermissionResultAllow)
            assert not worker.client.closed
    asyncio.run(go())


def test_duplicate_query_request_and_competing_controller_do_not_write_twice():
    async def go():
        async with environment() as (service, attach):
            first = await attach()
            params = {"prompt": "one", "turn": {"id": "turn-1"}}
            await first.call("query", params, request_id="stable-request")
            await first.call("query", params, request_id="stable-request")
            with pytest.raises(RuntimeError):
                await first.call("query", {**params, "prompt": "changed"}, request_id="stable-request")
            with pytest.raises(RuntimeError):
                await attach()
            assert service.sessions[first.id].client.prompts == ["one"]
            other = await attach(profile="another-account")
            assert other.id != first.id
            assert len(service.sessions) == 2
    asyncio.run(go())


def test_answered_question_page_is_not_asked_again_after_restart():
    from types import SimpleNamespace

    from cc_remote.claude_service.client import callback_identity
    from cc_remote.protocol import AnswerQuestion, AskUser
    from tests.test_claude_autocompact import _machine_with_sdk

    async def question(machine, ctx, text):
        token = callback_identity.set("same-native-tool-call")
        try:
            return await machine._on_ask(ctx, text, [
                {"label": "Yes"}, {"label": "No"},
            ])
        finally:
            callback_identity.reset(token)

    async def go():
        async with environment() as (service, attach):
            first = await attach()
            machine, transport, ctx = _machine_with_sdk(SimpleNamespace(client=first, ask_server=None))
            task = asyncio.create_task(question(machine, ctx, "First page?"))
            async with asyncio.timeout(2):
                while not any(isinstance(event, AskUser) for event in transport.sent):
                    await asyncio.sleep(0.001)
            event = next(event for event in transport.sent if isinstance(event, AskUser))
            result = await machine._handle_answer_question(AnswerQuestion(
                sid=ctx.key, ask_id=event.ask_id, answer="Yes"))
            assert result is None
            assert await task == "Yes"
            assert service.sessions[first.id].question_answers[event.ask_id] == "Yes"
            await first.detach()
            await released(service.sessions[first.id])
            second = await attach()
            machine2, transport2, ctx2 = _machine_with_sdk(SimpleNamespace(client=second, ask_server=None))
            assert await asyncio.wait_for(question(machine2, ctx2, "First page?"), 2) == "Yes"
            assert not any(isinstance(event, AskUser) for event in transport2.sent)
    asyncio.run(go())


def test_interrupt_requires_terminal_ack_before_next_query():
    async def go():
        async with environment() as (service, attach):
            first = await attach()
            first.next_turn = {"id": "one"}
            await first.query("one")
            await first.interrupt()
            with pytest.raises(RuntimeError):
                await first.call("query", {"prompt": "too early", "turn": {"id": "two"}})
            terminal = await anext(first.receive_messages())
            await first.call("commit", {"turn_id": "one", "seq": terminal["__cc_service_seq"]})
            first.next_turn = {"id": "two"}
            await first.query("two")
            assert service.sessions[first.id].client.prompts == ["one", "two"]
    asyncio.run(go())


def test_machine_reconstructs_offline_completion_without_resubmitting_or_duplicating_text():
    from cc_remote.config import WrapperConfig
    from cc_remote.protocol import Delta, TurnEnd
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk

    async def go():
        async with environment() as (service, attach):
            first = await attach()
            first.next_turn = {"id": "browser-msg", "prompt": "hello"}
            await first.query("hello")
            worker = service.sessions[first.id]
            await first.detach()
            await released(worker)
            native = worker.client
            rows = [
                {"type": "stream_event", "uuid": "e1", "session_id": SESSION_ID, "event": {
                    "type": "message_start", "message": {"id": "native-assistant"},
                }},
                {"type": "stream_event", "uuid": "e2", "session_id": SESSION_ID, "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello "},
                }},
                {"type": "stream_event", "uuid": "e3", "session_id": SESSION_ID, "event": {
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": "world"},
                }},
                {"type": "result", "subtype": "success", "duration_ms": 20,
                 "duration_api_ms": 19, "is_error": False, "num_turns": 1, "session_id": SESSION_ID},
            ]
            for row in rows:
                await native.queue.put(row)
            async with asyncio.timeout(2):
                while worker.journal.seq < len(rows):
                    await asyncio.sleep(0.001)
            worker.journal.db.execute("UPDATE events SET ts = 1700000000.0")
            worker.journal.db.commit()
            handle = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
            handle.service_metadata = worker.metadata.copy()
            handle.service_defer_events = True
            await handle.connect(resume_id=SESSION_ID, cwd="/tmp")
            machine, transport, ctx = _machine_with_sdk(handle)
            ctx.active_msg_id = "browser-msg"
            ctx.state = "running"
            try:
                handle.start_service_events()
                await asyncio.wait_for(machine._run_turn(ctx, "hello", _recover_service=True), 3)
                deltas = [item for item in transport.sent if isinstance(item, Delta)]
                assert len(deltas) == 1
                assert deltas[0].text == "Hello world"
                assert deltas[0].replace
                assert deltas[0].ts == 1700000000.0
                assert len([item for item in transport.sent if isinstance(item, TurnEnd)]) == 1
                assert native.prompts == ["hello"]
                assert ctx.state == "idle"
                assert worker.turn is None
            finally:
                await handle.detach_for_shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("field", ["profile_root", "session_id", "cwd", "space", "work_id", "btw"])
def test_explicit_worker_attach_checks_complete_identity(field):
    async def go():
        async with environment() as (service, attach):
            first = await attach()
            worker = service.sessions[first.id]
            with pytest.raises(RuntimeError):
                await first.connection.call("open", {
                    "session": first.id, "options": {},
                    "metadata": {**worker.metadata, field: "another-value"},
                })
            assert len(service.sessions) == 1
            assert worker.client.prompts == []
    asyncio.run(go())


def test_overlapping_buffered_background_turns_keep_each_unacknowledged_prefix():
    async def go():
        async with environment() as (service, attach):
            first = await attach()
            worker = service.sessions[first.id]
            rows = [
                {"type": "user", "origin": {"kind": "task-notification"}, "uuid": "task-a"},
                {"type": "assistant", "message": {"content": "first task"}},
                {"type": "result", "origin": {"kind": "task-notification"}},
                {"type": "user", "origin": {"kind": "task-notification"}, "uuid": "task-b"},
                {"type": "assistant", "message": {"content": "second task"}},
                {"type": "result", "origin": {"kind": "task-notification"}},
            ]
            for row in rows:
                await worker.client.queue.put(row)
            async with asyncio.timeout(2):
                while worker.journal.seq < len(rows):
                    await asyncio.sleep(0.001)
            await first.call("ack", {"seq": 2})
            assert worker.description()["after"] == 0
            assert len(worker.journal.after(0)) == 6
            await first.call("ack", {"seq": 3})
            assert worker.description()["after"] == 3
            await first.call("ack", {"seq": 5})
            assert worker.description()["after"] == 3
            await first.detach()
            await released(worker)
            second = await attach()
            stream = second.receive_messages()
            assert (await anext(stream))["uuid"] == "task-b"
            assert (await anext(stream))["message"]["content"] == "second task"
            terminal = await anext(stream)
            await second.call("ack", {"seq": terminal["__cc_service_seq"]})
            assert worker.background_start is None
            second.next_turn = {"id": "next-human"}
            await second.query("continue")
            assert worker.client.prompts == ["continue"]
    asyncio.run(go())


def test_machine_recovers_background_followup_without_a_new_human_completion():
    from cc_remote.config import WrapperConfig
    from cc_remote.protocol import Delta, TurnEnd
    from cc_remote.wrapper import claude_service
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk

    async def go():
        async with environment() as (service, attach):
            first = await attach()
            first.next_turn = {"id": "origin-human", "prompt": "hello"}
            await first.query("hello")
            worker = service.sessions[first.id]
            native = worker.client
            result = {"type": "result", "subtype": "success", "duration_ms": 20,
                      "duration_api_ms": 19, "is_error": False, "num_turns": 1,
                      "session_id": SESSION_ID}
            await native.queue.put({
                "type": "system", "subtype": "task_started", "task_id": "task-1",
                "description": "Background review", "uuid": "start", "session_id": SESSION_ID,
                "tool_use_id": "agent-tool", "task_type": "agent",
            })
            await native.queue.put(result)
            stream = first.receive_messages()
            await anext(stream)
            terminal = await anext(stream)
            await first.call("commit", {"turn_id": "origin-human", "seq": terminal["__cc_service_seq"]})
            await first.detach()
            await released(worker)
            rows = [
                {"type": "system", "subtype": "task_notification", "task_id": "task-1",
                 "status": "completed", "output_file": "/tmp/test-output", "summary": "done",
                 "uuid": "notification", "session_id": SESSION_ID, "tool_use_id": "agent-tool"},
                {"type": "user", "message": {"role": "user", "content": "<task-notification>done</task-notification>"},
                 "parent_tool_use_id": None, "uuid": "autonomous-user-1",
                 "origin": {"kind": "task-notification"}},
                {"type": "assistant", "message": {"id": "followup", "role": "assistant",
                 "model": "test", "content": [{"type": "text", "text": "Background findings"}]},
                 "parent_tool_use_id": None},
                {**result, "origin": {"kind": "task-notification"}},
            ]
            for row in rows:
                await native.queue.put(row)
            async with asyncio.timeout(2):
                while worker.journal.seq < 6:
                    await asyncio.sleep(0.001)
            handle = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
            handle.service_metadata = worker.metadata.copy()
            handle.service_defer_events = True
            await handle.connect(resume_id=SESSION_ID, cwd="/tmp")
            machine, transport, ctx = _machine_with_sdk(handle)
            ctx.state = "idle"
            handle.background_message_callback = lambda message, turn: machine._on_claude_background_message(ctx, message, turn)
            try:
                await claude_service.activate(machine, ctx)
                async with asyncio.timeout(3):
                    while worker.ack < 6:
                        await asyncio.sleep(0.001)
                deltas = [item for item in transport.sent if isinstance(item, Delta)]
                assert len(deltas) == 1
                assert deltas[0].text == "Background findings"
                assert deltas[0].background
                assert deltas[0].turn_id == "origin-human"
                assert deltas[0].replace
                assert not any(isinstance(item, TurnEnd) for item in transport.sent)
                assert native.prompts == ["hello"]
                assert ctx.state == "idle"
            finally:
                await handle.detach_for_shutdown()
            await released(worker)
            # A later deployment replays only the completed task seed. It must
            # not reserve another autonomous Result which will never arrive.
            # Older persistent services retain these acknowledged seeds even
            # after a newer controller has committed the autonomous turn.
            worker.task_seeds["task-1"] = {
                "data": rows[0], "origin_id": "origin-human", "seq": 3,
            }
            restored = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
            restored.service_metadata = worker.metadata.copy()
            restored.service_defer_events = True
            await restored.connect(resume_id=SESSION_ID, cwd="/tmp")
            machine2, _transport2, ctx2 = _machine_with_sdk(restored)
            observed_seed = asyncio.Event()

            async def background(message, turn):
                await machine2._on_claude_background_message(ctx2, message, turn)
                if getattr(message, "_cc_service_seed", False):
                    observed_seed.set()

            restored.background_message_callback = background
            try:
                await claude_service.activate(machine2, ctx2)
                await asyncio.wait_for(observed_seed.wait(), timeout=2)
                assert ctx2.claude_background_followups == {}
                assert ctx2.state == "idle"
                assert native.prompts == ["hello"]
            finally:
                await restored.detach_for_shutdown()
    asyncio.run(go())


def test_real_controller_process_exit_leaves_service_and_accepted_work_alive():
    from cc_remote.claude_service.client import Connection

    service_code = """
import asyncio, sys
from pathlib import Path
from cc_remote.claude_service.server import Service
from tests.test_claude_service import FakeClient
class RunningClient(FakeClient):
    async def query(self, prompt):
        await super().query(prompt)
        async def finish():
            await asyncio.sleep(0.2)
            await self.queue.put({'type': 'result', 'subtype': 'success', 'result': 'finished'})
        asyncio.create_task(finish())
async def run():
    service = Service(Path(sys.argv[1]), factory=RunningClient)
    server = await asyncio.start_unix_server(service.connection, sys.argv[2])
    print('ready', flush=True)
    async with server:
        await server.serve_forever()
asyncio.run(run())
"""
    controller_code = """
import asyncio, sys
from claude_agent_sdk import ClaudeAgentOptions
from cc_remote.claude_service.client import RemoteClient
async def run():
    client = RemoteClient(sys.argv[1], options=ClaudeAgentOptions(), metadata={
        'profile_root': 'test', 'session_id': 'test-session', 'space': 'code'})
    await client.connect()
    client.next_turn = {'id': 'only-query', 'prompt': 'one'}
    await client.query('one')
    print('accepted', flush=True)
    await asyncio.Event().wait()
asyncio.run(run())
"""

    async def go():
        with tempfile.TemporaryDirectory(prefix="cc-sdk-process-", dir="/tmp") as root:
            path = str(Path(root) / "service.sock")
            service = await asyncio.create_subprocess_exec(
                sys.executable, "-c", service_code, root, path, stdout=asyncio.subprocess.PIPE)
            controller = None
            connection = Connection(path)
            try:
                assert await asyncio.wait_for(service.stdout.readline(), 3) == b"ready\n"
                controller = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", controller_code, path, stdout=asyncio.subprocess.PIPE)
                assert await asyncio.wait_for(controller.stdout.readline(), 3) == b"accepted\n"
                controller.kill()  # Only the process created by this test.
                await controller.wait()
                await connection.connect()
                descriptions = await connection.call("list")
                assert len(descriptions) == 1
                description = descriptions[0]
                assert description["pid"] == service.pid
                assert service.returncode is None
                assert description["turn"]["id"] == "only-query"
                await connection.call("open", {
                    "session": description["id"], "metadata": description["metadata"], "options": {},
                })
                events = await connection.call("events", {"session": description["id"], "after": 0})
                assert events["events"][-1]["data"]["result"] == "finished"
            finally:
                await connection.disconnect()
                if controller is not None and controller.returncode is None:
                    controller.kill()
                    await controller.wait()
                if service.returncode is None:
                    service.terminate()
                    await service.wait()
    asyncio.run(go())
