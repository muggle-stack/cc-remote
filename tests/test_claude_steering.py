"""Non-interrupting Claude inputs through one native reader and durable owner."""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage

from cc_remote.claude_steering import ClaudeSteerRejected
from cc_remote.protocol import Steer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator, replayed_user_message_id
from cc_remote.wrapper import claude_steer
from tests.test_claude_service import environment, released
from tests.test_multisession import _mk_ctx, _mk_machine


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
