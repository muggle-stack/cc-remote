"""Non-interrupting Claude inputs through one native reader and durable owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ResultMessage

from cc_remote.claude_steering import ClaudeSteerRejected, PendingSteers
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


def requesting(uid="request"):
    return {"type": "system", "subtype": "status", "status": "requesting", "uuid": uid}


def test_result_receipt_requires_latest_consumed_input_and_preserves_pending_steers():
    from cc_remote.claude_steering import is_human_result

    steers = PendingSteers()
    steers.annotate(user("first"), managed_active=True)
    steers.add("second", {"id": "second-remote"})
    value = steers.annotate({**result(), "origin": ORIGIN,
                             "user_message_uuids": ["first"]}, managed_active=True)
    assert is_human_result(value) and value["__cc_steer_intermediate"]
    assert "second" in steers.pending
    steers.annotate(user("second"), managed_active=True)
    for fields in ({"user_message_uuid": "first"},
                   {"user_message_uuids": ["foreign"]},
                   {"user_message_uuid": "second", "parent_tool_use_id": "child"}):
        value = steers.annotate({**result(), "origin": ORIGIN, **fields}, managed_active=True)
        assert not is_human_result(value)

    value = steers.annotate({**result(), "origin": ORIGIN,
                             "user_message_uuids": ["first", "second"]}, managed_active=True)
    assert is_human_result(value) and not value.get("__cc_steer_intermediate")
    # A duplicate receipt or the next task-only Result cannot claim a new query.
    steers.begin_turn()
    for origin in (None, ORIGIN):
        value = steers.annotate({**result(), "origin": origin, "user_message_uuid": "second"})
        assert not is_human_result(value)


def test_receipts_do_not_change_older_service_terminal_ledger():
    from cc_remote.claude_steering import is_human_result

    steers = PendingSteers()
    steers.annotate(user("human"), managed_active=True)
    value = steers.annotate({**result(), "origin": ORIGIN, "user_message_uuid": "human"},
                            managed_active=True, result_receipts=False)
    assert "__cc_managed_result" not in value and not is_human_result(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_task_origin_result_consumes_exact_steer_and_commits_original_root(persistent):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
        sdk.client = client
        sdk._start_message_pump()
        consumer = None
        try:
            sdk.next_turn_id = "root"
            await sdk.query("first")

            async def read():
                return [m async for m in sdk.receive_response()]

            consumer = asyncio.create_task(read())
            await native.queue.put(user("human", "first"))
            await until(lambda: sdk._managed_input_seen)
            await sdk.steer("guide", native_id="native-guide", metadata={"id": "guide"})
            await native.queue.put({**result(), "origin": ORIGIN, "user_message_uuid": "human"})
            await native.queue.put(user("native-guide", "guide"))
            await until(lambda: not sdk._steers.pending)
            assert not consumer.done()
            if worker:
                assert worker.terminal_seq is None and worker.turn["id"] == "root"
            await native.queue.put({**result(), "origin": ORIGIN,
                                    "user_message_uuids": ["native-guide"], "queued_turn_count": 0})
            messages = await asyncio.wait_for(consumer, 3)
            assert isinstance(messages[-1], ResultMessage)
            await sdk.ack_service_message(messages[-1], turn_id="root")
            sdk.release_background_messages()
            if worker:
                assert worker.turn is None
            sdk.next_turn_id = "next"
            await sdk.query("next")
            await native.queue.put(user("next-native", "next"))
            await native.queue.put(result())
            messages = await asyncio.wait_for(read(), 3)
            await sdk.ack_service_message(messages[-1], turn_id="next")
            assert native.interrupts == 0
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await sdk._stop_message_pump()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("finish", ["result", "absorbed_result", "steer", "stop"])
@pytest.mark.parametrize("start", [
    requesting(),
    assistant("unannounced", [{"type": "text", "text": "continue"}]),
    {"type": "stream_event", "uuid": "unannounced", "session_id": "native-session",
     "event": {"type": "message_start", "message": {"id": "continuation", "content": []}}},
], ids=["request", "assistant", "stream"])
async def test_native_continuation_without_replayed_user_is_visible_and_interactive(persistent, finish, start):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client

        async def interrupt():
            native.interrupts += 1

        native.interrupt = interrupt
        sdk = SdkHandle(WrapperConfig())
        sdk.applied_auto_compact_mode = sdk.auto_compact_mode
        sdk.applied_auto_compact_threshold_tokens = sdk.auto_compact_threshold_tokens
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk.refresh_goal = AsyncMock(return_value=None)
        sdk.client = client
        sdk._start_message_pump()
        try:
            sdk.next_turn_id = "parent"
            await sdk.query("start background work")
            await native.queue.put(user("human", "start background work"))
            await native.queue.put(result())
            messages = [m async for m in sdk.receive_response()]
            await sdk.ack_service_message(messages[-1], turn_id="parent")
            sdk.release_background_messages()

            for task in ("task-a", "task-b"):
                await native.queue.put({"type": "system", "subtype": "task_notification",
                                        "task_id": task, "status": "completed", "output_file": "",
                                        "summary": "finished", "uuid": task, "session_id": ctx.session_id})
            await until(lambda: native.queue.empty() and sdk._background_callbacks_pending == 0)
            assert ctx.state == "idle" and not ctx.claude_background_followups

            # Claude Code 2.1.276 does not replay ordinary internal task prompts.
            # Its next request and output arrive without any UserMessage first.
            await native.queue.put(start.copy())
            await until(lambda: ctx.state == "running")
            running = [e for e in transport.sent if e.type == "state" and e.state == "running"]
            assert running[-1].msg_id == "parent" and running[-1].continuation
            assert machine._own_write(ctx.key)
            await native.queue.put(assistant("background-tool", [{"type": "tool_use", "id": "read",
                                                                 "name": "Read", "input": {"file_path": "README.md"}}]))
            await until(lambda: any(e.type == "tool_use" for e in transport.sent))
            tools = [e for e in transport.sent if e.type == "tool_use"]
            assert tools[-1].turn_id == "parent" and tools[-1].background
            if worker:
                assert worker.background_start is not None
                assert worker.journal.after(0)[0]["data"]["uuid"] == start["uuid"]

            if finish == "steer":
                reply = await machine._handle_steer(Steer(sid=ctx.key, cmd_id="guide", client_id="browser",
                                                        msg_id="guide-ui", prompt="continue here"))
                assert reply is None
                inputs = native.prompts if persistent else native.inputs
                written = [item async for item in inputs[-1]] if persistent else inputs[-1]
                uid = written[0]["uuid"]
                await native.queue.put(user(uid, "continue here"))
                await native.queue.put(assistant("guided", [{"type": "text", "text": "guided answer"}]))
                await native.queue.put(result())
                await until(lambda: ctx.turn_task is None and ctx.state == "idle")
                assert len(inputs) == 2
                assert sum(e.type == "turn_steered" for e in transport.sent) == 1
                assert sum(e.type == "turn_end" for e in transport.sent) == 1
            else:
                if finish == "stop":
                    await machine._handle_interrupt(SimpleNamespace(sid=ctx.key))
                    assert native.interrupts == 1
                if finish == "absorbed_result":
                    # The originating internal prompt was not replayed. Other
                    # task inputs arrive during its response, but its Result
                    # still names the original run, not the absorbed inputs.
                    for run in ("absorbed-a", "absorbed-b"):
                        await native.queue.put({**user(run), "isReplay": True, "origin": {
                            "kind": "task-notification", "producer": "session-task", "runId": run,
                        }})
                await native.queue.put({**result(), "origin": {
                    "kind": "task-notification", "producer": "session-task", "runId": "original",
                }})
                await until(lambda: ctx.state == "idle")
                assert not any(e.type == "turn_end" for e in transport.sent)
            await until(lambda: sdk._background_callbacks_pending == 0)
            assert not ctx.claude_background_followups and not sdk._steers.background_id
            if worker:
                await until(lambda: worker.turn is None and worker.background_start is None)
                assert not native.closed
            if finish == "absorbed_result":
                sdk.next_turn_id = "next-human"
                await sdk.query("next question")
                await native.queue.put(user("next-native", "next question"))
                await native.queue.put(result())
                messages = [m async for m in sdk.receive_response()]
                await sdk.ack_service_message(messages[-1], turn_id="next-human")
                inputs = native.prompts if persistent else native.inputs
                assert len(inputs) == 2 and native.interrupts == 0
        finally:
            for task in (ctx.turn_task, ctx.claude_autonomous_interrupt_task):
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await sdk._stop_message_pump()
            if persistent:
                await client.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [{"kind": "task-notification"}, ORIGIN])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("background_result", ["before", "before_request", "after", "none"])
async def test_autonomous_steer_waits_for_exact_echo_then_uses_normal_turn(uncertain, background_result, origin):
    sdk = SdkHandle(WrapperConfig())
    machine, transport, ctx = _machine_with_sdk(sdk)
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk.refresh_goal = AsyncMock(return_value=None)
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    try:
        await native.queue.put({**user("injected", "background task finished"), "origin": origin})
        await until(lambda: ctx.state == "running")
        assert ctx.active_msg_id is None and ctx.translator is None
        native.fail_write = uncertain
        command = Steer(sid=ctx.key, cmd_id="steer", client_id="browser", msg_id="guide-ui", prompt="guide")
        response = await machine._handle_steer(command)
        assert response is None if not uncertain else response.code == "steer_outcome_unknown"
        assert native.inputs[0][0]["priority"] == "next"
        uid = native.inputs[0][0]["uuid"]
        await native.queue.put(user("old-tool", [{"type": "tool_result", "tool_use_id": "old", "content": "done"}]))
        if background_result in {"before", "before_request"}:
            await native.queue.put({**result(), "origin": origin})
            await until(lambda: sdk._background_callbacks_pending == 0 and not sdk._steers.background_id)
        if background_result == "before_request":
            # The queued human request starts before Code replays its UUID.
            await native.queue.put(requesting("human-request"))
            await until(lambda: sdk._steers.background_id is not None)
        assert ctx.state == "running" and ctx.turn_task is None
        assert not any(e.type in {"turn_end", "turn_steered"} for e in transport.sent)
        with pytest.raises(RuntimeError, match="active response"):
            await sdk.query("must not overtake accepted guidance")
        await native.queue.put(user(uid))
        if background_result == "after":
            await native.queue.put({**result(), "origin": origin})
        await native.queue.put(assistant("guided-reply", [{"type": "text", "text": "guided answer"}]))
        await native.queue.put({**result(), "user_message_uuid": uid})
        await until(lambda: any(e.type == "turn_end" for e in transport.sent))
        await until(lambda: ctx.turn_task is None)
        assert [e.msg_id for e in transport.sent if e.type == "turn_steered"] == ["guide-ui"]
        assert sum(e.type == "turn_end" for e in transport.sent) == 1
        assert ctx.state == "idle" and not sdk._autonomous_steer_root
        assert not ctx.claude_background_followups and not sdk._steers.background_id
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
    pending = PendingSteers()
    pending.annotate({**user("injected"), "origin": ORIGIN})
    identity = pending.background_id
    pending.annotate({**result(), "origin": {"kind": "task-notification", "taskId": "unrelated"}})
    assert pending.background_id == identity
    pending.annotate({**result(), "origin": {"kind": "task-notification", "task_id": "background-task"}})
    assert pending.background_id is None


def test_only_main_activity_starts_an_implicit_continuation():
    pending = PendingSteers()
    frames = [requesting(), assistant("output", [])]
    for frame in frames:
        assert "__cc_background_start" not in pending.annotate(frame, managed_active=True)
        for field in ("parent_tool_use_id", "parentToolUseID"):
            assert "__cc_background_start" not in pending.annotate({**frame, field: "child"})
    for subtype in ("task_started", "task_progress", "task_notification", "task_updated"):
        pending.annotate({"type": "system", "subtype": subtype, "task_id": "child", "status": "completed"})
    assert pending.background_id is None

    started = pending.annotate(requesting())
    identity = started["__cc_background_start"]["id"]
    assert "__cc_background_start" not in pending.annotate(assistant("next", []))
    pending.annotate({**result(), "origin": {"kind": "human"}})
    assert pending.background_id == identity
    enriched = pending.annotate({**user("injected"), "origin": ORIGIN})
    assert enriched["__cc_background_start"]["id"] == identity
    ended = pending.annotate({**result(), "origin": ORIGIN})
    assert ended["__cc_background_end"] == identity and pending.background_id is None


@pytest.mark.parametrize("legacy_journal", [False, True])
def test_implicit_response_retains_ownership_across_absorbed_task_origins(legacy_journal):
    from cc_remote.claude_steering import background_end_ids, is_human_result

    owner, replay = PendingSteers(), PendingSteers()

    def emit(frame):
        value = owner.annotate(frame)
        if legacy_journal and value.get("__cc_background_start"):
            value = {**value, "__cc_background_start": {
                key: data for key, data in value["__cc_background_start"].items() if key != "implicit"
            }}
        return value, replay.annotate(value)

    start, _ = emit(requesting())
    identity = start["__cc_background_start"]["id"]
    for run in ("absorbed-a", "absorbed-b", "absorbed-a"):
        value, projected = emit({**user(run), "isReplay": True, "origin": {
            "kind": "task-notification", "producer": "session-task", "runId": run,
        }})
        assert value["__cc_background_start"]["id"] == identity
        assert projected["__cc_background_start"]["id"] == identity

    for pending in (owner, replay):
        pending.add("next-human", {"id": "next-ui"})
    terminal = {**result(), "origin": {
        "kind": "task-notification", "producer": "session-task", "runId": "original",
    }}
    for field in ("parent_tool_use_id", "parentToolUseID"):
        for value in emit({**terminal, field: "child"}):
            assert not background_end_ids(value)
        assert owner.background_id == replay.background_id == identity

    for value in emit(terminal):
        assert background_end_ids(value) == (identity,)
        assert not is_human_result(value)
    for pending in (owner, replay):
        assert pending.background_id is None
        assert "next-human" in pending.pending


def test_implicit_response_terminal_preserves_unrelated_explicit_origin():
    from cc_remote.claude_steering import background_end_ids

    pending = PendingSteers()
    explicit = pending.annotate({**user("older-task"), "origin": ORIGIN})["__cc_background_start"]
    pending.annotate({**requesting(), "__cc_background_start": {
        "id": "implicit-request", "origin_key": None, "origin": None, "managed": False,
    }})
    pending.annotate({**user("absorbed"), "origin": {
        "kind": "task-notification", "runId": "absorbed",
    }})
    terminal = pending.annotate({**result(), "origin": {
        "kind": "task-notification", "runId": "original",
    }})
    assert background_end_ids(terminal) == ("implicit-request",)
    assert pending.background_id == explicit["id"]
    pending.annotate({**result(), "origin": ORIGIN})
    assert pending.background_id is None


@pytest.mark.parametrize("absorbed_before_echo", [False, True])
def test_human_echo_adopts_only_implicit_activity_with_legacy_terminal_annotations(absorbed_before_echo):
    pending = PendingSteers()
    explicit = pending.annotate({**user("older-task"), "origin": ORIGIN})["__cc_background_start"]
    # A journal may restore an explicit older continuation beside a controller's
    # implicit request claim; neither may erase the other's provenance.
    implicit = {"id": "implicit-request", "origin_key": None, "origin": None, "managed": False}
    pending.annotate({**requesting(), "__cc_background_start": implicit})
    if absorbed_before_echo:
        pending.annotate({**user("in-turn-task"), "origin": {
            "kind": "task-notification", "runId": "absorbed-before-human-echo",
        }})
    pending.annotate(user("native-human"), managed_active=True)
    absorbed = pending.annotate({**user("absorbed"), "origin": {
        "kind": "task-notification", "taskId": "current-task",
    }, "__cc_background_start": {
        "id": "service-task", "origin_key": '["task-notification", "task", "current-task"]',
        "origin": {"kind": "task-notification", "taskId": "current-task"}, "managed": True,
    }}, managed_active=True)["__cc_background_start"]
    ended = pending.annotate({**result(), "__cc_background_ends": [absorbed["id"]]}, managed_active=True)
    assert set(ended["__cc_background_ends"]) == {implicit["id"], absorbed["id"]}
    assert pending.background_id == explicit["id"]
    pending.annotate({**result(), "origin": ORIGIN})
    assert pending.background_id is None


@pytest.mark.parametrize("journaled", [False, True])
def test_exact_steer_echo_adopts_request_before_managed_route_activates(journaled):
    pending = PendingSteers()
    explicit = pending.annotate({**user("unrelated-task"), "origin": ORIGIN})["__cc_background_start"]
    implicit = {"id": "human-request", "origin_key": None, "origin": None,
                "managed": False, "implicit": True}
    pending.annotate({**requesting(), "__cc_background_start": implicit})
    metadata = {"id": "human-ui"}
    echo = user("native-human")
    if journaled:
        echo["__cc_steer"] = metadata
    else:
        pending.add("native-human", metadata)
    # Both owners activate their managed route only AFTER annotating this echo.
    value = pending.annotate(echo, managed_active=False)
    assert value["__cc_steer"] == metadata
    terminal = pending.annotate({**result(), "user_message_uuid": "native-human"}, managed_active=True)
    assert terminal["__cc_background_ends"] == [implicit["id"]]
    assert pending.background_id == explicit["id"]


@pytest.mark.parametrize("echo", [
    user("foreign"),
    {**user("expected"), "isSynthetic": True},
    {**user("expected"), "isCompactSummary": True},
    {**user("expected"), "parent_tool_use_id": "child"},
    {**user("expected"), "origin": ORIGIN},
    user("expected", [{"type": "tool_result", "tool_use_id": "tool", "content": "done"}]),
], ids=["foreign", "synthetic", "compact", "child", "task", "tool-result"])
def test_unmatched_input_cannot_adopt_an_implicit_request(echo):
    pending = PendingSteers()
    identity = pending.annotate(requesting())["__cc_background_start"]["id"]
    pending.add("expected", {"id": "human-ui"})
    pending.annotate(echo, managed_active=False)
    terminal = pending.annotate({**result(), "user_message_uuid": "expected"}, managed_active=True)
    assert not terminal["__cc_managed_result"]
    assert not terminal.get("__cc_background_ends")
    assert pending.background_id == identity and "expected" in pending.pending


@pytest.mark.asyncio
async def test_implicit_response_absorbs_tasks_while_detached_then_accepts_next_query():
    from cc_remote.claude_steering import background_end_ids

    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        await native.queue.put(requesting())
        await until(lambda: worker.journal.seq == 1)
        await first.detach()
        await released(worker)
        for run in ("absorbed-a", "absorbed-b"):
            await native.queue.put({**user(run), "isReplay": True, "origin": {
                "kind": "task-notification", "producer": "session-task", "runId": run,
            }})
        await native.queue.put({**result(), "origin": {
            "kind": "task-notification", "producer": "session-task", "runId": "original",
        }})
        await until(lambda: worker.journal.seq == 4)
        # Native completion alone must not discard unprojected output. The new
        # controller replays the entire response before acknowledging its end.
        assert worker.background_start == 0 and worker.terminal_seq is None
        second = await attach()
        replay = PendingSteers()
        stream = second.receive_messages()
        for _ in range(3):
            value = replay.annotate(await asyncio.wait_for(anext(stream), 3))
            await second.call("ack", {"seq": value["__cc_service_seq"]})
        assert worker.background_start == 0 and len(worker.journal.after(0)) == 4
        terminal = replay.annotate(await asyncio.wait_for(anext(stream), 3))
        assert background_end_ids(terminal) and replay.background_id is None
        await second.call("ack", {"seq": terminal["__cc_service_seq"]})
        assert worker.background_start is None and worker.journal.after(0) == []
        second.next_turn = {"id": "next-human"}
        await second.query("next question")
        assert native.prompts == ["next question"]
        assert worker.client is native and not native.closed and native.interrupts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("replayed_user", [False, True])
@pytest.mark.parametrize("echo_before_detach", [False, True])
@pytest.mark.parametrize("background_result", ["pending", "finished", "finished_before_request"])
async def test_service_background_steer_recovery_does_not_resubmit(echo_before_detach, background_result, replayed_user):
    guide_id = "22222222-2222-4222-8222-222222222222"
    metadata = {"id": "guide-ui", "prompt": "guide", "fingerprint": "a" * 64}
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        await native.queue.put({**user("injected"), "origin": ORIGIN} if replayed_user else requesting())
        await until(lambda: worker.steers.background_id)
        target = worker.steers.background_id
        await first.steer("guide", native_id=guide_id, metadata=metadata,
                          turn_id="guide-ui", background_id=target)
        assert worker.turn["awaiting_steer"]
        if background_result != "pending":
            await native.queue.put({**result(), "origin": ORIGIN})
            await until(lambda: worker.steers.background_id is None)
        if background_result == "finished_before_request":
            await native.queue.put({**requesting("human-request"), "user_message_uuids": [guide_id]})
            await until(lambda: worker.steers.background_id is not None)
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
            await native.queue.put({**result(), "user_message_uuid": guide_id})
            await until(lambda: worker.turn is None)
            await until(lambda: ctx.turn_task is None)
            assert len(native.prompts) == 1 and native.interrupts == 0
            assert sum(e.type == "turn_steered" for e in transport.sent) == 1
            assert sum(e.type == "turn_end" for e in transport.sent) == 1
            assert worker.background_start is None
            assert ctx.state == "idle"
            assert not ctx.claude_background_followups and not sdk._steers.background_id
        finally:
            if ctx.turn_task:
                ctx.turn_task.cancel()
                await asyncio.gather(ctx.turn_task, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
async def test_steered_request_finishing_offline_retires_its_durable_background_claim():
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        await native.queue.put({**user("old-task"), "origin": ORIGIN})
        await until(lambda: worker.steers.background_id is not None)
        await first.steer("guide", native_id="human", metadata={"id": "human-ui"},
                          turn_id="human-ui", background_id=worker.steers.background_id)
        await first.detach()
        await released(worker)
        await native.queue.put({**result(), "origin": ORIGIN})
        await native.queue.put(requesting("human-request"))
        await native.queue.put(user("human"))
        await native.queue.put({**result(), "user_message_uuid": "human"})
        await until(lambda: worker.terminal_seq is not None)
        terminal_seq = worker.terminal_seq
        assert worker.background_start is not None  # Unacknowledged output stays durable.

        second = await attach()
        stream = second.receive_messages()
        terminal = None
        async with asyncio.timeout(3):
            while second.last_seq < terminal_seq:
                terminal = await anext(stream)
        assert terminal["__cc_managed_result"]
        assert terminal["__cc_background_ends"]
        await second.call("commit", {"turn_id": "human-ui", "seq": terminal_seq})
        assert worker.turn is None and worker.background_start is None
        assert not worker.steers.background_id and not worker.journal.after(0)
        assert len(native.prompts) == 1 and native.interrupts == 0
        # A normal next query must be accepted without another steering workaround.
        second.next_turn = {"id": "next-human"}
        await second.query("next question")
        assert len(native.prompts) == 2 and not native.closed


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


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_consecutive_steers_wait_for_each_exact_echo(persistent):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=8))
        sdk.client = client
        sdk._start_message_pump()
        sdk.next_turn_id = "root"
        await sdk.query("start")

        async def read():
            return [message async for message in sdk.receive_response()]

        reader = asyncio.create_task(read())
        try:
            await native.queue.put(user("root-native", "start"))
            await until(lambda: sdk._managed_input_seen)
            for uid in ("a", "b", "c"):
                await sdk.steer("same prompt", native_id=uid, metadata={"id": "ui-" + uid})
            assert list(sdk._steers.pending) == ["a", "b", "c"]
            if worker:
                assert list(worker.steers.pending) == ["a", "b", "c"]
            for uid in ("a", "b", "c"):
                await native.queue.put(result())  # An in-flight Result cannot retire unconsumed input.
                await native.queue.put(user(uid, "same prompt"))
                await until(lambda: uid not in sdk._steers.pending)
                assert not reader.done()
            await native.queue.put(result())
            messages = await asyncio.wait_for(reader, 3)
            echoes = [message._cc_steer["id"] for message in messages
                      if getattr(message, "_cc_steer", None)]
            assert echoes == ["ui-a", "ui-b", "ui-c"]
            assert sum(isinstance(message, ResultMessage) for message in messages) == 1
            assert native.interrupts == 0
            if not worker:
                assert native.consumers == 1
                assert [row[0]["priority"] for row in native.inputs[1:]] == ["next"] * 3
            await sdk.ack_service_message(messages[-1], turn_id="root")
            if worker:
                assert worker.turn is None
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            sdk.release_background_messages()
            await sdk._stop_message_pump()
