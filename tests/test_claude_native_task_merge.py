"""Match Code's queued_command -> replayed user projection inside a response."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.history_store import HistoryIndexStore, materialize_history_turns
from cc_remote.wrapper.stream import transcript_compact_history_page, translate_history
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_background_completion import task_input
from tests.test_claude_service import environment, released
from tests.test_claude_steering import NativeClient, assistant, result, until, user


HUMAN = "11111111-1111-4111-8111-111111111111"
ANSWER = "22222222-2222-4222-8222-222222222222"


def absorbed_task(uid):
    # Native Code 2.1.276 Y$n projects a queued_command attachment with
    # uuid=source_uuid and isReplay=true, including when absorbed_mid_turn.
    return {**task_input(uid, {"kind": "task-notification"}), "isReplay": True}


def response_frames(stop_reason):
    final = assistant(ANSWER, [{"type": "text", "text": "Both checks passed."}])
    final["message"]["stop_reason"] = stop_reason
    return [
        user(HUMAN, "check both"),
        assistant("before", [{"type": "tool_use", "id": "before-tool",
                              "name": "Read", "input": {"file_path": "README.md"}}]),
        absorbed_task("notification-a"),
        assistant("after-a", [{"type": "tool_use", "id": "after-a-tool",
                               "name": "Read", "input": {"file_path": "a.txt"}}]),
        absorbed_task("notification-b"),
        assistant("after-b", [{"type": "tool_use", "id": "after-b-tool",
                               "name": "Read", "input": {"file_path": "b.txt"}}]),
        final,
        result(),
    ]


def controller(sdk):
    sdk.applied_auto_compact_mode = sdk.auto_compact_mode
    sdk.applied_auto_compact_threshold_tokens = sdk.auto_compact_threshold_tokens
    sdk.applied_effort = sdk.effort
    sdk.refresh_goal = AsyncMock(return_value=None)
    machine, transport, ctx = _machine_with_sdk(sdk)
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    return machine, transport, ctx


def assert_one_response(events):
    tools = [event for event in events if event.type == "tool_use"]
    assert [event.tool_use_id for event in tools] == ["before-tool", "after-a-tool", "after-b-tool"]
    assert all(not event.background for event in tools)
    final = [event for event in events if event.type == "assistant_msg_end"
             and event.message_id == ANSWER and event.channel == "final"]
    assert len(final) == 1 and not final[0].background
    terminal = [event for event in events if event.type == "turn_end"]
    assert len(terminal) == 1
    assert terminal[0].turn_id == ANSWER
    assert terminal[0].checkpoint_id == HUMAN
    assert not any(event.type == "state" and event.continuation for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("stop_reason", ["end_turn", None])
async def test_absorbed_notifications_keep_one_main_response_and_accept_next_prompt(persistent, stop_reason):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
        machine, transport, ctx = controller(sdk)
        sdk.connect = AsyncMock(side_effect=AssertionError("do not replace the native process"))
        sdk.force_reconnect = sdk.connect
        sdk.client = client
        sdk._start_message_pump()
        runner = None
        try:
            ctx.state, ctx.active_msg_id = "running", "browser-human"
            runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "check both"))
            inputs = native.prompts if persistent else native.inputs
            await until(lambda: len(inputs) == 1)
            for frame in response_frames(stop_reason):
                await native.queue.put(frame)
            await asyncio.wait_for(runner, 3)
            await until(lambda: sdk._background_callbacks_pending == 0 and ctx.state == "idle")
            assert_one_response(transport.sent)
            assert not ctx.claude_background_followups
            assert not sdk._steers.background_id
            if worker:
                assert worker.turn is None and worker.background_start is None

            # The parent's real Result permits normal input independently of
            # other children; it must not wait for an invented second Result.
            ctx.claude_active_tasks.add("another-child")
            ctx.state, ctx.active_msg_id = "running", "next-human"
            runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "next question"))
            await until(lambda: len(inputs) == 2)
            await native.queue.put(user("next-human", "next question"))
            await native.queue.put(result())
            await asyncio.wait_for(runner, 3)
            assert ctx.state == "idle"
            assert ctx.claude_active_tasks == {"another-child"}
            assert native.interrupts == 0
        finally:
            if runner:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await sdk._stop_message_pump()
            if persistent:
                await client.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 4])
async def test_old_replayed_task_and_tool_result_cannot_claim_new_query_before_human_echo(cap):
    native = NativeClient()
    sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=cap))
    sdk.client = native
    background = []

    async def project(message, _turn):
        background.append(message)

    sdk.background_message_callback = project
    sdk._start_message_pump()
    try:
        await sdk.query("new question")
        old = user("old-tool-result")
        old["message"]["content"] = [{"type": "tool_result", "tool_use_id": "old-tool", "content": "done"}]
        await native.queue.put(old)
        await native.queue.put(absorbed_task("old-notification"))
        await native.queue.put(assistant("old-answer", [{"type": "text", "text": "old report"}]))
        await native.queue.put({**result(), "origin": {"kind": "task-notification"}})
        await native.queue.put(user(HUMAN, "new question"))
        await native.queue.put(absorbed_task("new-notification"))
        await native.queue.put(assistant(ANSWER, [{"type": "text", "text": "new report"}]))
        await native.queue.put(result())
        managed = [message async for message in sdk.receive_response()]
        await until(lambda: sdk._background_callbacks_pending == 0)
        assert [message.uuid for message in managed if getattr(message, "uuid", None)] == [HUMAN, "new-notification", ANSWER]
        assert [message.uuid for message in background if getattr(message, "uuid", None)] == [
            "old-tool-result", "old-notification", "old-answer"]
        assert not sdk._steers.background_id
    finally:
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_absorbed_task_after_foreign_terminal_retains_the_open_human_response():
    native = NativeClient()
    sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
    sdk.client = native
    background = []

    async def project(message, _turn):
        background.append(message)

    sdk.background_message_callback = project
    sdk._start_message_pump()
    try:
        await sdk.query("check both")
        frames = response_frames("end_turn")
        frames[2:2] = [
            task_input("channel-input", {"kind": "channel"}),
            assistant("channel-answer", [{"type": "text", "text": "channel report"}]),
            {**result(), "origin": {"kind": "channel"}},
        ]
        for frame in frames:
            await native.queue.put(frame)
        managed = [message async for message in sdk.receive_response()]
        await until(lambda: sdk._background_callbacks_pending == 0)
        assert [message.uuid for message in managed if getattr(message, "uuid", None)] == [
            HUMAN, "before", "notification-a", "after-a", "notification-b", "after-b", ANSWER]
        assert [message.uuid for message in background if getattr(message, "uuid", None)] == [
            "channel-input", "channel-answer"]
    finally:
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_task_return_between_responses_does_not_merge_into_pending_steer():
    native = NativeClient()
    sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
    sdk.client = native
    background = []

    async def project(message, _turn):
        background.append(message)

    sdk.background_message_callback = project
    sdk._start_message_pump()
    consumer = None
    try:
        await sdk.query("first question")

        async def read():
            return [message async for message in sdk.receive_response()]

        consumer = asyncio.create_task(read())
        await native.queue.put(user(HUMAN, "first question"))
        await until(lambda: sdk._pending_turn_background_release is None)
        await sdk.steer("next instruction", native_id="guide-echo", metadata={"id": "guide"})
        await native.queue.put(result())  # Intermediate: the guide is not consumed yet.
        await native.queue.put(absorbed_task("between-responses"))
        await native.queue.put(assistant("earlier-report", [{"type": "text", "text": "earlier report"}]))
        await native.queue.put({**result(), "origin": {"kind": "task-notification"}})
        await native.queue.put(user("guide-echo", "next instruction"))
        await native.queue.put(absorbed_task("absorbed-by-guide"))
        await native.queue.put(assistant(ANSWER, [{"type": "text", "text": "guided answer"}]))
        await native.queue.put(result())
        managed = await asyncio.wait_for(consumer, 3)
        await until(lambda: sdk._background_callbacks_pending == 0)
        assert [message.uuid for message in managed if getattr(message, "uuid", None)] == [
            HUMAN, "guide-echo", "absorbed-by-guide", ANSWER]
        assert [message.uuid for message in background if getattr(message, "uuid", None)] == [
            "between-responses", "earlier-report"]
        assert native.interrupts == 0 and native.consumers == 1
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_absorbed_notifications_recover_in_order_without_resubmitting():
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        first.next_turn = {"id": "browser-human", "prompt": "check both"}
        await first.query("check both")
        frames = response_frames("end_turn")
        for frame in frames[:3]:
            await native.queue.put(frame)
        await until(lambda: worker.journal.seq == 3)
        await first.detach()
        await released(worker)
        for frame in frames[3:]:
            await native.queue.put(frame)
        await until(lambda: worker.journal.seq == len(frames))

        sdk = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
        sdk.service_defer_events = True
        machine, transport, ctx = controller(sdk)
        sdk.service_metadata = worker.metadata.copy()
        try:
            await sdk.connect(resume_id="native-session", cwd="/tmp")
            from cc_remote.wrapper.claude_service import activate

            await activate(machine, ctx)
            await until(lambda: worker.turn is None and ctx.turn_task is None
                        and ctx.state == "idle" and sdk._background_callbacks_pending == 0)
            assert_one_response(transport.sent)
            assert not ctx.claude_background_followups
            assert worker.background_start is None
            assert native.prompts == ["check both"]
            assert native.interrupts == 0 and not native.closed
        finally:
            if ctx.turn_task:
                ctx.turn_task.cancel()
                await asyncio.gather(ctx.turn_task, return_exceptions=True)
            await sdk.detach_for_shutdown()


def test_native_attachment_history_matches_managed_live_response(tmp_path):
    rows = [
        {**user("older-human", "earlier question"), "parentUuid": None,
         "timestamp": "2026-09-19T13:08:00Z"},
        {**assistant("older-answer", [{"type": "text", "text": "earlier answer"}]),
         "parentUuid": "older-human", "timestamp": "2026-09-19T13:08:01Z"},
        {"type": "system", "subtype": "compact_boundary", "uuid": "boundary",
         "parentUuid": None, "logicalParentUuid": "older-answer",
         "timestamp": "2026-09-19T13:08:02Z",
         "compactMetadata": {"trigger": "auto", "preTokens": 500_000}},
    ]
    parent = "boundary"
    for index, frame in enumerate(response_frames("end_turn")[:-1]):
        timestamp = f"2026-09-19T13:09:{index:02d}Z"
        uid = frame["uuid"]
        if frame.get("origin"):
            # The same input is an attachment in native JSONL, not a new
            # top-level human row. source_uuid is its SDK replay identity.
            row = {"type": "attachment", "uuid": f"attachment-{uid}",
                   "attachment": {"type": "queued_command", "source_uuid": uid,
                                  "commandMode": "task-notification", "origin": frame["origin"],
                                  "prompt": "background task finished", "timestamp": timestamp}}
        else:
            row = frame.copy()
        row.update(parentUuid=parent, timestamp=timestamp)
        rows.append(row)
        parent = row["uuid"]
        if frame.get("origin"):
            rows.append({"type": "queue-operation", "operation": "remove",
                         "reason": "absorbed_mid_turn", "timestamp": timestamp})
    path = tmp_path / "native.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store = HistoryIndexStore(tmp_path / "index")
    for _ in range(2):  # Cold ancestry read, then the indexed summary/detail page.
        page = transcript_compact_history_page(HUMAN, path=str(path), limit=1, index_store=store)
        assert page is not None
        events = translate_history(page.messages, 4096, page.timestamps, page.internal_events)
        assert [event.msg_id for event in events if event.type == "user_msg"] == [HUMAN]
        assert_one_response(events)
        turn, = materialize_history_turns([event.model_dump() for event in events])
        assert turn["done"] and turn["processDetailState"] == "present"
        assert turn["detailEventCount"] > 0
        answers = [block for block in turn["blocks"] if block["kind"] == "text" and block["channel"] == "final"]
        assert len(answers) == 1 and answers[0]["message_id"] == ANSWER
        assert not answers[0].get("background")
