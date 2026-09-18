"""Non-interrupting Claude inputs through one native reader and durable owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ResultMessage

from cc_remote.claude_steering import ClaudeSteerRejected
from cc_remote.protocol import Steer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator, replayed_user_message_id
from cc_remote.wrapper import claude_steer
from tests.test_claude_service import environment, released
from tests.test_multisession import _mk_ctx, _mk_machine
from tests.test_claude_autocompact import _machine_with_sdk
from cc_remote.config import WrapperConfig


def user(uid, text="guide"):
    return {"type": "user", "uuid": uid, "parent_tool_use_id": None,
            "message": {"role": "user", "content": text}}


def result():
    return {"type": "result", "subtype": "success", "duration_ms": 42,
            "duration_api_ms": 12, "is_error": False, "num_turns": 1,
            "session_id": "native-session"}


def assistant(uid, content):
    return {"type": "assistant", "uuid": uid,
            "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                        "content": content}}


class NativeClient:
    def __init__(self):
        self._query = self
        self.queue = asyncio.Queue()
        self.inputs = []
        self.consumers = 0
        self.interrupts = 0
        self.fail_write = False

    async def query(self, prompt):
        self.inputs.append(prompt if isinstance(prompt, str)
                           else [item async for item in prompt])
        if self.fail_write:
            raise ConnectionError("write acknowledgement lost")

    async def receive_messages(self):
        self.consumers += 1
        while True:
            yield await self.queue.get()

    async def interrupt(self):
        self.interrupts += 1


ORIGIN = {"kind": "task-notification", "taskId": "background-task"}


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("background_result", ["before", "after", "none"])
async def test_autonomous_steer_waits_for_exact_echo_then_uses_normal_turn(uncertain, background_result):
    sdk = SdkHandle(WrapperConfig())
    machine, transport, ctx = _machine_with_sdk(sdk)
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk.refresh_goal = AsyncMock(return_value=None)
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    try:
        await native.queue.put({**user("injected", "background task finished"), "origin": ORIGIN})
        await until(lambda: ctx.state == "running")
        assert ctx.active_msg_id is None and ctx.translator is None
        native.fail_write = uncertain
        command = Steer(sid=ctx.key, cmd_id="steer", client_id="browser", msg_id="guide-ui", prompt="guide")
        response = await machine._handle_steer(command)
        assert response is None if not uncertain else response.code == "steer_outcome_unknown"
        assert native.inputs[0][0]["priority"] == "next"
        uid = native.inputs[0][0]["uuid"]
        await native.queue.put(user("old-tool", [{"type": "tool_result", "tool_use_id": "old", "content": "done"}]))
        if background_result == "before":
            await native.queue.put({**result(), "origin": ORIGIN})
            await until(lambda: sdk._background_callbacks_pending == 0 and not sdk._steers.background_id)
        assert ctx.state == "running" and ctx.turn_task is None
        assert not any(e.type in {"turn_end", "turn_steered"} for e in transport.sent)
        with pytest.raises(RuntimeError, match="active response"):
            await sdk.query("must not overtake accepted guidance")
        await native.queue.put(user(uid))
        if background_result == "after":
            await native.queue.put({**result(), "origin": ORIGIN})
        await native.queue.put(assistant("guided-reply", [{"type": "text", "text": "guided answer"}]))
        await native.queue.put(result())
        await until(lambda: any(e.type == "turn_end" for e in transport.sent))
        await until(lambda: ctx.turn_task is None)
        assert [e.msg_id for e in transport.sent if e.type == "turn_steered"] == ["guide-ui"]
        assert sum(e.type == "turn_end" for e in transport.sent) == 1
        assert ctx.state == "idle" and not sdk._autonomous_steer_root
        assert native.consumers == 1 and native.interrupts == 0 and len(native.inputs) == 1
    finally:
        if ctx.turn_task:
            ctx.turn_task.cancel()
            await asyncio.gather(ctx.turn_task, return_exceptions=True)
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_background_steering_cancellation_retires_only_pending_input():
    sdk = SdkHandle(WrapperConfig())
    machine, transport, ctx = _machine_with_sdk(sdk)
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    sdk._steers.capabilities.add("interrupt_cancel_queued_v1")
    try:
        await native.queue.put({**user("injected"), "origin": ORIGIN})
        await until(lambda: ctx.state == "running")
        await machine._handle_steer(Steer(sid=ctx.key, cmd_id="c", client_id="browser", msg_id="guide", prompt="guide"))
        uid = native.inputs[0][0]["uuid"]

        async def cancel(request):
            assert request == {"subtype": "interrupt", "cancel_queued": True}
            await native.queue.put({"type": "command_lifecycle", "state": "cancelled", "command_uuid": uid})
            await native.queue.put({**result(), "origin": ORIGIN})

        native._send_control_request = cancel
        await machine._handle_interrupt(SimpleNamespace(sid=ctx.key))
        await until(lambda: ctx.state == "idle")
        assert any(e.type == "error" and e.msg_id == "guide" for e in transport.sent)
        assert not any(e.type in {"turn_steered", "turn_end"} for e in transport.sent)
        assert not sdk._steers.pending and not sdk._autonomous_steer_root
    finally:
        if ctx.claude_autonomous_interrupt_task:
            ctx.claude_autonomous_interrupt_task.cancel()
            await asyncio.gather(ctx.claude_autonomous_interrupt_task, return_exceptions=True)
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_background_steer_rejects_after_human_terminal_before_consumer_drain():
    sdk = SdkHandle(WrapperConfig())
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    try:
        await native.queue.put({**user("injected"), "origin": ORIGIN})
        await until(lambda: sdk._steers.background_id)
        await sdk.steer("guide", native_id="guide", metadata={"id": "guide"})
        await native.queue.put(user("guide"))
        await native.queue.put(result())
        await until(lambda: sdk._autonomous_steer_root.get("adopted") and not sdk._turn_active)
        with pytest.raises(ClaudeSteerRejected):
            await sdk.steer("too late", native_id="late", metadata={"id": "late"})
        assert len(native.inputs) == 1
    finally:
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_service_multiple_background_inputs_cancel_one_and_keep_root_commit(tmp_path):
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        await worker.client.queue.put({**user("injected"), "origin": ORIGIN})
        await until(lambda: worker.steers.background_id)
        attachment = tmp_path / "staged"
        attachment.mkdir()
        await client.steer("first", native_id="first", metadata={"id": "first-ui", "attachment_dir": str(attachment)},
                           turn_id="first-ui", background_id=worker.steers.background_id)
        await client.steer("second", native_id="second", metadata={"id": "second-ui", "prompt": "second"},
                           turn_id="first-ui")
        await worker.client.queue.put({"type": "command_lifecycle", "state": "cancelled", "command_uuid": "first"})
        await until(lambda: "first" not in worker.steers.pending)
        assert worker.turn["id"] == "first-ui" and attachment.exists()
        await worker.client.queue.put(user("second"))
        await worker.client.queue.put(result())
        await until(lambda: worker.terminal_seq is not None)
        assert worker.turn["id"] == "first-ui" and not worker.background_turns
        await client.call("commit", {"turn_id": "first-ui", "seq": worker.terminal_seq})
        assert worker.turn is None and not attachment.exists()
        assert len(worker.client.prompts) == 2


def test_background_target_matches_exact_origin_across_native_field_spellings():
    from cc_remote.claude_steering import PendingSteers
    pending = PendingSteers()
    pending.annotate({**user("injected"), "origin": ORIGIN})
    identity = pending.background_id
    pending.annotate({**result(), "origin": {"kind": "task-notification", "taskId": "unrelated"}})
    assert pending.background_id == identity
    pending.annotate({**result(), "origin": {"kind": "task-notification", "task_id": "background-task"}})
    assert pending.background_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_before_detach", [False, True])
@pytest.mark.parametrize("background_result", [False, True])
async def test_service_background_steer_recovery_does_not_resubmit(echo_before_detach, background_result):
    guide_id = "22222222-2222-4222-8222-222222222222"
    metadata = {"id": "guide-ui", "prompt": "guide", "fingerprint": "a" * 64}
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        await native.queue.put({**user("injected"), "origin": ORIGIN})
        await until(lambda: worker.steers.background_id)
        target = worker.steers.background_id
        await first.steer("guide", native_id=guide_id, metadata=metadata,
                          turn_id="guide-ui", background_id=target)
        assert worker.turn["awaiting_steer"]
        if background_result:
            await native.queue.put({**result(), "origin": ORIGIN})
            await until(lambda: worker.steers.background_id is None)
        assert worker.terminal_seq is None
        if echo_before_detach:
            await native.queue.put(user(guide_id))
            await until(lambda: not worker.turn.get("awaiting_steer"))
        await first.detach()
        await released(worker)
        sdk = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
        sdk.service_metadata = worker.metadata.copy()
        sdk.service_defer_events = True
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk.refresh_goal = AsyncMock(return_value=None)
        # _configure reads this test machine's empty service setting; preserve
        # the existing exact owner identity from the original controller.
        sdk.service_metadata = worker.metadata.copy()
        try:
            await sdk.connect(resume_id="native-session", cwd="/tmp")
            from cc_remote.wrapper.claude_service import activate
            await activate(machine, ctx)
            if not echo_before_detach:
                assert not any(e.type in {"user_msg", "turn_steered"} for e in transport.sent)
                await sdk.steer("guide", native_id=guide_id, metadata=metadata)
                await native.queue.put(user(guide_id))
            await native.queue.put(assistant("answer", [{"type": "text", "text": "recovered"}]))
            await native.queue.put(result())
            await until(lambda: worker.turn is None)
            await until(lambda: ctx.turn_task is None)
            assert len(native.prompts) == 1 and native.interrupts == 0
            assert sum(e.type == "turn_steered" for e in transport.sent) == 1
            assert sum(e.type == "turn_end" for e in transport.sent) == 1
            assert worker.background_start is None
            assert ctx.state == "idle"
        finally:
            if ctx.turn_task:
                ctx.turn_task.cancel()
                await asyncio.gather(ctx.turn_task, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
async def test_service_rejects_stale_background_identity_and_old_capability():
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        await worker.client.queue.put({**user("injected"), "origin": ORIGIN})
        await until(lambda: worker.steers.background_id)
        target = worker.steers.background_id
        client.description.pop("background_steering")
        with pytest.raises(ClaudeSteerRejected):
            await client.steer("guide", native_id="g", metadata={"id": "g"}, turn_id="g", background_id=target)
        client.description["background_steering"] = True
        await worker.client.queue.put({**result(), "origin": ORIGIN})
        await until(lambda: worker.steers.background_id is None)
        with pytest.raises(ClaudeSteerRejected):
            await client.steer("guide", native_id="g", metadata={"id": "g"}, turn_id="g", background_id=target)
        assert not worker.client.prompts and worker.turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize("racing_terminal", [False, True])
@pytest.mark.parametrize("uncertain_write", [False, True])
async def test_steer_uses_next_without_interrupt_or_second_reader(racing_terminal, uncertain_write):
    sdk = SdkHandle(SimpleNamespace(turn_reader_queue_cap=8))
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    sdk.next_turn_id = "root"
    await sdk.query("start")
    received = []

    async def collect():
        async for message in sdk.receive_response():
            received.append(message)

    reader = asyncio.create_task(collect())
    try:
        await native.queue.put(user("root-native", "start"))
        native.fail_write = uncertain_write
        pending = sdk.steer("guide", native_id="guide-native", metadata={"id": "guide-ui", "prompt": "guide"})
        if uncertain_write:
            with pytest.raises(ConnectionError):
                await pending
        else:
            await pending
        payload = native.inputs[1][0]
        assert payload["priority"] == "next"
        assert payload["uuid"] == "guide-native"
        assert payload["message"]["content"] == "guide"
        with pytest.raises(RuntimeError, match="active response"):
            await sdk.query("must remain busy")
        if racing_terminal:
            await native.queue.put(result())
        await native.queue.put(user("guide-native"))
        await native.queue.put(assistant("reply", [{"type": "text", "text": "after guidance"}]))
        await native.queue.put(result())
        await asyncio.wait_for(reader, 2)
        assert native.consumers == 1 and native.interrupts == 0
        assert len(native.inputs) == 2
        assert sum(isinstance(m, ResultMessage) for m in received) == 1
        echoes = [m for m in received if getattr(m, "_cc_steer", None)]
        assert len(echoes) == 1 and echoes[0]._cc_steer["id"] == "guide-ui"
        with pytest.raises(ClaudeSteerRejected):
            await sdk.steer("late", native_id="late", metadata={"id": "late"})
        assert len(native.inputs) == 2
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        sdk.release_background_messages()
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_service_preserves_input_boundary_and_root_commit_across_detach():
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "root", "prompt": "start"}
        await first.query("start")
        worker = service.sessions[first.id]
        native = worker.client
        await first.steer("guide", native_id="guide-native",
                          metadata={"id": "guide-ui", "prompt": "guide"}, turn_id="root")
        envelope = [item async for item in native.prompts[1]]
        assert envelope[0]["priority"] == "next"
        await first.detach()
        await released(worker)
        await native.queue.put(result())
        await native.queue.put(user("guide-native"))
        await native.queue.put(result())
        second = await attach()
        replay = second.receive_messages()
        rows = []
        async with asyncio.timeout(2):
            while len(rows) < 3:
                row = await anext(replay)
                if row.get("type") != "system":
                    rows.append(row)
        assert rows[0]["__cc_steer_intermediate"]
        assert rows[1]["__cc_steer"]["id"] == "guide-ui"
        assert "__cc_steer_intermediate" not in rows[2]
        assert second.recovery["id"] == "root"
        assert worker.terminal_seq == rows[2]["__cc_service_seq"]
        with pytest.raises(ClaudeSteerRejected):
            await second.steer("late", native_id="late", metadata={"id": "late"}, turn_id="root")
        sdk = SdkHandle(SimpleNamespace())
        sdk.client = second
        sdk._turn_root_id = "root"
        terminal = SimpleNamespace(_cc_service_seq=rows[2]["__cc_service_seq"])
        await sdk.ack_service_message(terminal, turn_id="guide-ui")
        assert worker.turn is None
        assert native.interrupts == 0 and len(native.prompts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("btw", [False, True])
async def test_machine_waits_for_native_echo_and_retains_old_item_ownership(btw):
    machine, transport = _mk_machine()
    ctx = _mk_ctx("sid", "sid")
    ctx.btw = btw
    ctx.state = "running"
    ctx.active_msg_id = "root"
    ctx.translator = StreamTranslator(4000, turn_id="root")
    ctx.sdk = sdk = SdkHandle(SimpleNamespace(turn_reader_queue_cap=8))
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    machine.sessions[ctx.key] = ctx
    sdk.next_turn_id = "root"
    await sdk.query("start")
    try:
        tool = sdk._parse_compat_message(assistant("tool-msg", [{
            "type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pwd"}}]))
        before = ctx.translator.feed(tool)
        assert any(getattr(e, "turn_id", None) == "root" for e in before)
        command = Steer(sid="sid", cmd_id="command", client_id="browser", msg_id="guide-ui", prompt="guide")
        assert await machine._handle_steer(command) is None
        assert ctx.active_msg_id == "root"
        assert not any(e.type == "turn_steered" for e in transport.sent)
        native_id = native.inputs[1][0]["uuid"]
        raw = sdk._steers.annotate(user(native_id))
        echo = sdk._parse_compat_message(raw)
        echo._cc_steer = raw["__cc_steer"]
        event = await claude_steer.apply_echo(machine, ctx, echo, replayed_user_message_id(echo))
        assert event.msg_id == "guide-ui" and event.turn_id == native_id
        assert ctx.active_msg_id == "guide-ui"
        late_tool = sdk._parse_compat_message(user("tool-result", [{
            "type": "tool_result", "tool_use_id": "tool-1", "content": "old tool finished"}]))
        late = ctx.translator.feed(late_tool)
        assert any(e.type == "tool_result" and e.turn_id == "root" for e in late)
        new = ctx.translator.feed(sdk._parse_compat_message(assistant("reply", [{"type": "text", "text": "new"}])))
        assert all(e.turn_id == "guide-ui" for e in new if e.type in {"delta", "assistant_msg_start"})
        assert native.interrupts == 0
    finally:
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_service_without_native_steering_rejects_before_mutation():
    async with environment() as (service, attach):
        client = await attach()
        client.description.pop("native_steering")
        with pytest.raises(ClaudeSteerRejected):
            await client.steer("guide", native_id="uid", metadata={"id": "id"}, turn_id="root")
        assert service.sessions[client.id].client.prompts == []


@pytest.mark.asyncio
async def test_explicit_stop_cancels_pending_native_inputs_and_drains_actual_result():
    sdk = SdkHandle(SimpleNamespace(turn_reader_queue_cap=8))
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    sdk._steers.capabilities.add("interrupt_cancel_queued_v1")
    sdk.next_turn_id = "root"
    await sdk.query("start")
    received = []

    async def collect():
        async for message in sdk.receive_response():
            received.append(message)

    async def control(request):
        assert request == {"subtype": "interrupt", "cancel_queued": True}
        await native.queue.put({"type": "command_lifecycle", "state": "cancelled",
                                "command_uuid": "guide-native"})
        await native.queue.put({**result(), "subtype": "error_during_execution", "is_error": True})
        return {"still_queued": [], "cancelled": ["guide-native"]}

    native._send_control_request = control
    reader = asyncio.create_task(collect())
    try:
        await sdk.steer("guide", native_id="guide-native", metadata={"id": "guide-ui"})
        await sdk.interrupt()
        await asyncio.wait_for(reader, 2)
        assert received[0]._cc_steer_cancelled["id"] == "guide-ui"
        assert isinstance(received[-1], ResultMessage) and received[-1].is_error
        assert not sdk._steers.pending
        assert len(native.inputs) == 2 and native.interrupts == 0
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_service_retried_steer_after_detach_does_not_submit_again(tmp_path):
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "root", "prompt": "start"}
        await first.query("start")
        metadata = {"id": "guide-ui", "prompt": "read attachment", "fingerprint": "a" * 64,
                    "attachment_dir": str(tmp_path / "first")}
        await first.steer("read /first/file", native_id="stable-guidance", metadata=metadata, turn_id="root")
        worker = service.sessions[first.id]
        await first.detach()
        await released(worker)
        second = await attach()
        await second.steer("read /second/file", native_id="stable-guidance",
                           metadata={**metadata, "attachment_dir": str(tmp_path / "second")}, turn_id="root")
        assert len(worker.client.prompts) == 2
        assert worker.steers.pending["stable-guidance"] == metadata
        with pytest.raises(RuntimeError, match="ValueError"):
            await second.steer("changed input", native_id="stable-guidance",
                               metadata={**metadata, "fingerprint": "b" * 64}, turn_id="root")
        assert len(worker.client.prompts) == 2


@pytest.mark.asyncio
async def test_service_cancelled_guide_replays_before_terminal():
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "root", "prompt": "start"}
        await first.query("start")
        worker = service.sessions[first.id]
        worker.steers.capabilities.add("interrupt_cancel_queued_v1")
        await first.steer("guide", native_id="guide-native", metadata={"id": "guide-ui"}, turn_id="root")

        async def control(request):
            assert request["cancel_queued"]
            await worker.client.queue.put({"type": "command_lifecycle", "state": "cancelled",
                                           "command_uuid": "guide-native"})
            await worker.client.queue.put({**result(), "is_error": True})
            return {"cancelled": ["guide-native"]}

        worker.client._send_control_request = control
        await first.interrupt()
        replay = first.receive_messages()
        cancelled, terminal = await anext(replay), await anext(replay)
        assert cancelled["__cc_steer_cancelled"]["id"] == "guide-ui"
        assert "__cc_steer_intermediate" not in terminal
        assert worker.terminal_seq == terminal["__cc_service_seq"]
        assert not worker.steers.pending
