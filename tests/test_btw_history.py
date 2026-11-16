"""Ephemeral presentation recovery without a native history API/model call."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import (
    AssistantMsgEnd, Delta, Error, ProcessEvent, StateEvent, SyncBtw, ToolDelta, TurnBinding,
    TurnDiff, TurnEnd, TurnPlan, TurnResult,
    TurnSteered, UserMsg,
)
from cc_remote.wrapper.btw_history import BtwHistory, CodexBtwCompletions
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.ringbuffer import RingBuffer
from tests.test_multisession import _mk_ctx, _mk_machine


def replay(history, *, max_bytes=1_000_000, max_events=1000):
    return history.replay(tail_seq=100, generation="g", max_bytes=max_bytes,
                          max_events=max_events, turn_usage=[])


def seed(history, user="human", native="native"):
    history.observe(UserMsg(msg_id=user, prompt="read only"))
    history.observe(TurnBinding(msg_id=user, turn_id=native))


def completed(index, text=None):
    return {"method": "item/completed", "params": {
        "threadId": "fork", "turnId": "native", "item": {
            "id": f"item-{index}", "type": "agentMessage",
            "phase": "commentary", "text": text or f"message {index}",
        },
    }}


def test_compaction_replay_retains_one_completed_item_in_source_order():
    history = BtwHistory(10000, 100)
    history.observe(UserMsg(msg_id="human", prompt="inspect"))
    start = ProcessEvent(item_id="compact-start", kind="compaction", phase="start",
                         status="running", turn_id="human", title="压缩上下文")
    history.observe(start)
    history.observe(Delta(message_id="reply", text="continued", turn_id="human"))
    end = start.model_copy(update={
        "item_id": "native-boundary", "phase": "end", "status": "succeeded",
        "input": {"compaction_started_id": "compact-start"},
    })
    history.observe(end)
    history.observe(start)  # A delayed heartbeat must not revive the old item.
    history.observe(end)
    restored = replay(history)
    assert [(e.type, getattr(e, "item_id", None)) for e in restored[2:-1]] == [
        ("process", "native-boundary"), ("delta", None)]
    assert next(e for e in restored if isinstance(e, ProcessEvent)).status == "succeeded"
    assert history._items == 2
    assert history._bytes == sum(RingBuffer._size(e) for e in restored[1:-1])


def test_compaction_replay_does_not_merge_unrelated_turns():
    history = BtwHistory(10000, 100)
    history.observe(UserMsg(msg_id="older", prompt="previous"))
    start = ProcessEvent(item_id="compact-start", kind="compaction", phase="start",
                         status="running", turn_id="older", title="压缩上下文")
    history.observe(start)
    history.observe(UserMsg(msg_id="newer", prompt="current"))
    history.observe(start.model_copy(update={
        "item_id": "native-boundary", "phase": "end", "status": "succeeded",
        "turn_id": "newer", "input": {"compaction_started_id": "compact-start"},
    }))
    restored = [e for e in replay(history) if isinstance(e, ProcessEvent)]
    assert [(e.item_id, e.turn_id, e.status) for e in restored] == [
        ("compact-start", "older", "running"), ("native-boundary", "newer", "succeeded")]


def test_token_ring_eviction_does_not_cut_message_or_lose_user_boundary():
    history = BtwHistory(1_000_000, 100)
    ring = RingBuffer(4, 4096)
    frames = [UserMsg(msg_id="human", prompt="inspect"),
              TurnBinding(msg_id="human", turn_id="native")]
    frames += [Delta(message_id="reply", text=str(i) + ",", channel="commentary")
               for i in range(100)]
    frames += [AssistantMsgEnd(message_id="reply", channel="commentary"),
               TurnEnd(turn_id="native", result=TurnResult(
                   subtype="success", duration_ms=123, is_error=False))]
    for seq, event in enumerate(frames, 1):
        event.seq = seq
        ring.append(event)
        history.observe(event)
    assert ring.head_seq > 100
    restored = replay(history)
    assert restored[0].rebuild and not restored[0].truncated
    assert [e.msg_id for e in restored if e.type == "user_msg"] == ["human"]
    assert [e.text for e in restored if e.type == "delta"] == [
        "".join(str(i) + "," for i in range(100))]
    assert all(e.seq is None for e in restored)
    assert len(restored) == 7


@pytest.mark.parametrize("identity", ["checkpoint", "binding", "live-owner"])
def test_claude_terminal_replays_with_exact_owner_not_assistant_uuid(identity):
    history = BtwHistory(10000, 100)
    human = "native-user" if identity == "checkpoint" else "browser-input"
    history.observe(UserMsg(msg_id=human, prompt="first question"))
    if identity == "binding":
        history.observe(TurnBinding(msg_id=human, turn_id="native-user"))
    history.observe(Delta(message_id="assistant-reply", text="finished", channel="final"))
    history.observe(AssistantMsgEnd(message_id="assistant-reply", channel="final"))
    # A late terminal must still close its own input, never the newest one.
    history.observe(UserMsg(msg_id="next-input", prompt="second question"))
    terminal = TurnEnd(turn_id="assistant-reply", checkpoint_id="native-user",
                       result=TurnResult(subtype="success", duration_ms=42, is_error=False))
    if identity == "live-owner":
        # Real BTW deliberately skips persistent browser/native alias binding.
        terminal._changes_turn_id = human
    history.observe(terminal)
    history.observe(terminal.model_copy(deep=True))
    frames = replay(history)
    restored = [e for e in frames if isinstance(e, TurnEnd)]
    assert len(restored) == 1
    assert restored[0].turn_id == "assistant-reply"
    assert restored[0].checkpoint_id == "native-user"
    assert history.turns[human].end is not None
    assert history.turns["next-input"].end is None
    assert frames.index(restored[0]) < next(
        i for i, e in enumerate(frames) if isinstance(e, UserMsg) and e.msg_id == "next-input")
    assert history._bytes == sum(RingBuffer._size(e) for e in frames[1:-1])


def test_unknown_claude_terminal_does_not_close_newest_btw_turn():
    history = BtwHistory(10000, 100)
    seed(history)
    terminal = TurnEnd(turn_id="unknown-assistant", checkpoint_id="evicted-user",
                       result=TurnResult(subtype="success", duration_ms=42, is_error=False))
    terminal._changes_turn_id = "evicted-browser-input"
    history.observe(terminal)
    assert not any(isinstance(e, TurnEnd) for e in replay(history))


def test_replay_budget_keeps_whole_items_and_their_exact_user():
    history = BtwHistory(20_000, 100)
    seed(history)
    history.observe(Delta(message_id="old", text="x" * 2000))
    history.observe(Delta(message_id="new", text="whole newest message"))
    history.observe(AssistantMsgEnd(message_id="new"))
    restored = replay(history, max_bytes=1300)
    assert restored[0].truncated
    assert restored[1].type == "user_msg" and restored[2].type == "turn_binding"
    assert [e.text for e in restored if e.type == "delta"] == ["whole newest message"]
    tiny = BtwHistory(1500, 2)
    seed(tiny)
    for index in range(20):
        tiny.observe(Delta(message_id=str(index), text="safe"))
    assert tiny.truncated and tiny._bytes <= 1500 and tiny._items <= 2
    assert replay(tiny)[1].type == "user_msg"


def test_native_completions_repair_pre_gap_text_and_missing_items():
    cfg = SimpleNamespace(cc_cwd="/tmp", tool_result_max=1000,
                          turn_reader_queue_cap=1)
    handle = CodexHandle(cfg)
    handle.btw_completions = CodexBtwCompletions(1_000_000, 100, 1000)
    handle.btw_completions.initial_owner = "human"
    handle._open_managed_stream()
    handle.turn_id = "native"
    history = BtwHistory(1_000_000, 100)
    seed(history)
    history.observe(Delta(message_id="item-0", text="，only the tail"))
    history.observe(AssistantMsgEnd(message_id="item-0"))
    for index in range(90):
        handle._queue_managed_notification(completed(index))
    assert handle._managed_overflow
    history.repair("native", handle.btw_completions.snapshots("native"))
    values = [e.text for e in replay(history) if isinstance(e, Delta)]
    assert values == [f"message {index}" for index in range(90)]
    # A delayed pre-terminal queue delta cannot append to a complete snapshot.
    history.observe(Delta(message_id="item-0", text="late suffix"))
    assert [e.text for e in replay(history) if isinstance(e, Delta)] == values


def test_repair_never_assigns_an_ambiguous_steer_to_another_input():
    history = BtwHistory(1_000_000, 100)
    seed(history, "first")
    history.observe(TurnSteered(msg_id="second", turn_id="native", prompt="next"))
    native = CodexBtwCompletions(1_000_000, 100, 1000)
    native.initial_owner = "first"
    native.observe(completed(1, "belongs to first"))
    native.initial_owner = "second"
    native.owners["native"] = "second"
    native.observe(completed(2, "belongs to second"))
    native.owners["native"] = None
    native.observe(completed(3, "ambiguous"))
    history.repair("native", native.snapshots("native"))
    assert list(history.turns["first"].items) == ["message:item-1"]
    assert list(history.turns["second"].items) == ["message:item-2"]
    assert "ambiguous" not in str(replay(history))


def test_duplicate_native_user_lifecycle_keeps_exact_completed_item_ownership():
    native = CodexBtwCompletions(10000, 10, 1000)
    native.initial_owner = "human"
    user = {"method": "item/started", "params": {"turnId": "native", "item": {
        "type": "userMessage", "id": "native-user", "content": [{"type": "text", "text": "inspect"}],
    }}}
    native.observe(user)
    native.observe({**user, "method": "item/completed"})
    native.observe(completed(1))
    assert native.snapshots("native")[0][0] == "human"
    user["params"]["item"]["id"] = "another-user"
    native.observe(user)
    native.observe(completed(2))
    assert native.snapshots("native")[1][0] is None


def test_native_recovery_cache_is_bounded_and_excludes_private_reasoning():
    native = CodexBtwCompletions(5000, 3, 1000)
    for index in range(50):
        native.observe(completed(index, "x" * 300))
    assert native._bytes <= 5000
    assert sum(len(rows) for rows in native.turns.values()) <= 3
    event = completed(99)
    event["params"]["item"] = {
        "id": "private", "type": "reasoning", "summary": [],
        "content": [{"text": "PRIVATE SECRET"}], "encryptedContent": "PRIVATE SECRET",
    }
    native.observe(event)
    assert "PRIVATE SECRET" not in str(native.snapshots("native"))


def test_partial_completion_cache_does_not_reorder_the_live_prefix():
    history = BtwHistory(1_000_000, 100)
    seed(history)
    for index in [0, 1, 3]:
        history.observe(Delta(message_id=f"item-{index}", text=f"message {index}"))
    native = CodexBtwCompletions(10000, 3, 1000)
    native.initial_owner = "human"
    for index in range(5):
        native.observe(completed(index))
    history.repair("native", native.snapshots("native"))
    assert [e.text for e in replay(history) if e.type == "delta"] == [
        f"message {index}" for index in range(5)]


def test_repair_uses_running_items_as_order_anchors_without_replacing_their_text():
    history = BtwHistory(1_000_000, 100)
    seed(history)
    history.observe(Delta(message_id="item-0", text="first"))
    history.observe(Delta(message_id="item-3", text="still streaming"))
    native = CodexBtwCompletions(10000, 10, 1000)
    native.initial_owner = "human"
    for index in range(5):
        item = completed(index)
        if index == 3:
            item["method"] = "item/started"
        native.observe(item)
    history.repair("native", native.snapshots("native"), item_order=native.item_order("native"))
    assert [e.text for e in replay(history) if e.type == "delta"] == [
        "message 0", "message 1", "message 2", "still streaming", "message 4"]


def test_repair_retains_translator_normalized_ids_outside_native_order_map():
    history = BtwHistory(10000, 10)
    seed(history)
    history.repair("native", [("human", [Delta(message_id="normalized", text="whole")])],
                   item_order=["native-id"])
    assert [e.text for e in replay(history) if e.type == "delta"] == ["whole"]


def test_process_and_large_tool_streams_replay_the_same_content():
    history = BtwHistory(3_000_000, 100)
    seed(history)
    for delta in ["first ", "second"]:
        history.observe(ProcessEvent(item_id="process", kind="command", phase="update",
                                     title="Command", append_to="output", delta=delta))
    chunks = ["a" * 400_000, "b" * 200_000, "c" * 10_000]
    for delta in chunks:
        history.observe(ToolDelta(tool_use_id="tool", stream="output", delta=delta))
    restored = replay(history, max_bytes=3_000_000)
    process = next(e for e in restored if e.type == "process")
    assert process.output == "first second" and process.append_to is None
    assert "".join(e.delta for e in restored if e.type == "tool_delta") == "".join(chunks)
    assert all(len(e.delta) <= 512 * 1024 for e in restored if e.type == "tool_delta")


def test_item_snapshot_retains_plan_diff_and_exact_turn_error():
    history = BtwHistory(10000, 100)
    seed(history)
    history.observe(TurnPlan(item_id="plan", turn_id="native", plan=[]))
    history.observe(TurnDiff(item_id="diff", turn_id="native", diff="a diff"))
    history.observe(Error(msg_id="human", code="internal", message="failed"))
    history.observe(Error(msg_id="unaccepted-input", code="internal", message="unrelated"))
    restored = replay(history)
    assert any(e.type == "turn_plan" for e in restored)
    assert any(e.type == "turn_diff" and e.diff == "a diff" for e in restored)
    assert [e.message for e in restored if e.type == "error"] == ["failed"]


def test_evicted_item_does_not_reappear_as_a_mid_sentence_fragment():
    history = BtwHistory(1500, 10)
    seed(history)
    history.observe(Delta(message_id="big", text="prefix" * 1000))
    history.observe(Delta(message_id="big", text="，only a suffix"))
    history.observe(AssistantMsgEnd(message_id="big"))
    history.observe(Delta(message_id="new", text="A complete new item."))
    restored = replay(history)
    assert restored[0].truncated
    assert [e.text for e in restored if e.type == "delta"] == ["A complete new item."]


def test_long_stream_coalesces_bounded_chunks_and_replace_discards_old_text():
    history = BtwHistory(1_000_000, 10)
    seed(history)
    for _ in range(5000):
        history.observe(Delta(message_id="stream", text="x" * 100))
    text = [e.text for e in replay(history) if e.type == "delta"]
    assert "".join(text) == "x" * 500_000
    assert len(text) < 20 and max(map(len, text)) <= 32 * 1024
    history.observe(Delta(message_id="stream", text="replacement", replace=True))
    history.observe(Delta(message_id="stream", text=" complete"))
    assert [e.text for e in replay(history) if e.type == "delta"] == ["replacement complete"]
    assert history._bytes < 2000


def test_btw_overflow_and_sync_use_private_snapshots_without_native_history():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("btw-private")
        ctx.engine = "codex"
        ctx.btw = ctx.btw_announced = True
        ctx.owner_client_id = "owner"
        ctx.sdk = SimpleNamespace(btw_completions=CodexBtwCompletions(50000, 100, 1000))
        ctx.sdk.btw_completions.initial_owner = "human"
        machine.sessions[ctx.key] = ctx
        machine._push_mirrored_history = AsyncMock(side_effect=AssertionError("durable read"))
        for frame in [UserMsg(msg_id="human", prompt="inspect"),
                      TurnBinding(msg_id="human", turn_id="native"),
                      Delta(message_id="item-1", text="tail")]:
            await machine._emit(ctx, frame)
        ctx.sdk.btw_completions.observe(completed(1, "full reply"))
        transport.sent.clear()
        await machine._repair_codex_projection_after_overflow(ctx, "native")
        assert all(e.owner_id == "owner" and e.sid == ctx.key for e in transport.sent)
        assert any(e.type == "delta" and e.text == "full reply" for e in transport.sent)
        transport.sent.clear()
        await machine._handle_sync_btw(SyncBtw(
            sid=ctx.key, client_id="new-tab", owner_id="owner", cursor=999999,
            generation=machine.instance_id))
        assert any(e.type == "user_msg" and e.msg_id == "human" for e in transport.sent)
        assert all(e.to == "new-tab" and e.owner_id == "owner" for e in transport.sent)
        machine._push_mirrored_history.assert_not_called()
        transport.sent.clear()
        ctx.owner_client_id = None
        await machine._repair_codex_projection_after_overflow(ctx, "native")
        assert not transport.sent
    asyncio.run(run())


@pytest.mark.parametrize("state", ["running", "interrupting", "draining", "idle"])
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("unwinding", [False, True])
def test_claude_btw_sync_restores_exact_activity_before_replay_end(state, continuation, unwinding):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("btw-claude-running")
        ctx.engine = "claude"
        ctx.btw = ctx.btw_announced = True
        ctx.owner_client_id = "owner"
        ctx.sdk = SimpleNamespace()
        machine.sessions[ctx.key] = ctx
        for frame in [UserMsg(msg_id="old-input", prompt="finished question"),
                      Delta(message_id="old-answer", text="finished reply"),
                      TurnEnd(turn_id="old-answer", checkpoint_id="old-input",
                              result=TurnResult(subtype="success", duration_ms=1,
                                                is_error=False))]:
            await machine._emit(ctx, frame)
        ctx.active_msg_id = "current-input"
        await machine._emit(ctx, UserMsg(msg_id="current-input", prompt="still working"))
        await machine._emit(ctx, Delta(message_id="current-answer", text="checking"))
        if continuation:
            ctx.claude_background_followups = {"notification": "active"}
            ctx.claude_background_translator = SimpleNamespace(turn_id="old-input")
        if unwinding:
            # A real terminal hands activity back to the autonomous owner even
            # before the managed task finishes its asynchronous cleanup.
            ctx.turn_task = asyncio.current_task()
            await machine._emit(ctx, TurnEnd(turn_id="current-input", result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)))
        ctx.state = state
        await machine._emit(ctx, StateEvent(state=state))
        tail = ctx.buffer.tail_seq
        transport.sent.clear()
        await machine._handle_sync_btw(SyncBtw(
            sid=ctx.key, client_id="new-tab", owner_id="owner",
            generation=machine.instance_id))
        frames = transport.sent
        end_index = next(i for i, e in enumerate(frames) if e.type == "replay_end")
        activity = frames[end_index - 1]
        assert isinstance(activity, StateEvent)
        assert activity.state == state
        assert activity.msg_id == (None if state == "idle" else
                                   "old-input" if continuation else "current-input")
        assert activity.continuation is continuation
        assert activity.seq is None
        assert ctx.buffer.tail_seq == tail, "a read must not append a live event"
        assert all(e.to == "new-tab" and e.owner_id == "owner" for e in frames)
        assert not any(isinstance(e, TurnBinding) for e in frames)
        assert next(e for e in frames if e.type == "snapshot").state == state
    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_owner", [None, "previous-input"])
async def test_claude_btw_managed_replay_ignores_pre_echo_background_owner(previous_owner):
    """A real SDK request-before-echo leaves an absorbed background claim alive."""
    from cc_remote.wrapper.sdk import SdkHandle
    from cc_remote.wrapper.stream import StreamTranslator
    from tests.test_claude_steering import NativeClient, assistant, requesting, result, until, user

    machine, transport = _mk_machine()
    ctx = _mk_ctx("btw-pre-echo")
    ctx.btw = ctx.btw_announced = True
    ctx.owner_client_id = "owner"
    sdk = ctx.sdk = SdkHandle(machine.cfg)
    sdk.client = native = NativeClient()
    machine.sessions[ctx.key] = ctx
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk._start_message_pump()
    consumer = None
    try:
        if previous_owner:
            await machine._emit(ctx, UserMsg(msg_id=previous_owner, prompt="previous"))
            await machine._emit(ctx, TurnEnd(turn_id=previous_owner, result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)))
        sdk._turn_origin_id = previous_owner
        ctx.active_msg_id = sdk.next_turn_id = "current-input"
        ctx.translator = StreamTranslator(1024, turn_id="current-input")
        await sdk.query("current")
        await machine._set_state(ctx, "running")
        await machine._emit(ctx, UserMsg(msg_id="current-input", prompt="current"))

        async def consume():
            async for message in sdk.receive_response():
                for event in ctx.translator.feed(message):
                    await machine._emit(ctx, event)

        consumer = ctx.turn_task = asyncio.create_task(consume())
        await native.queue.put(requesting())
        await until(lambda: bool(ctx.claude_background_followups))
        await native.queue.put(user("native-human", "current"))
        await native.queue.put(assistant("current-answer", [{"type": "text", "text": "working"}]))
        await until(lambda: any(e.type == "delta" for e in transport.sent))
        assert ctx.claude_background_translator.turn_id == previous_owner
        assert machine._claude_autonomous_followup_pending(ctx)

        for _ in range(2):
            transport.sent.clear()
            await machine._handle_sync_btw(SyncBtw(
                sid=ctx.key, client_id="new-tab", owner_id="owner"))
            activity = next(e for e in transport.sent if isinstance(e, StateEvent))
            assert activity.msg_id == "current-input"
            assert activity.state == "running" and not activity.continuation
            assert activity.seq is None

        await native.queue.put(result())
        await asyncio.wait_for(consumer, 3)
        sdk.release_background_messages()
        await until(lambda: not machine._claude_autonomous_followup_pending(ctx))
        assert native.inputs == ["current"] and native.interrupts == 0
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await sdk._stop_message_pump()


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_owner", [None, "previous-input"])
@pytest.mark.parametrize("before_echo", [False, True])
@pytest.mark.parametrize("unrelated_result", [False, True])
async def test_claude_btw_compaction_spans_native_input_echo(previous_owner, before_echo, unrelated_result):
    from cc_remote.wrapper.sdk import SdkHandle
    from cc_remote.wrapper.stream import StreamTranslator
    from tests.test_claude_compaction_flow import boundary, status
    from tests.test_claude_steering import NativeClient, assistant, result, until, user

    machine, transport = _mk_machine()
    ctx = _mk_ctx("btw-compaction")
    ctx.btw = ctx.btw_announced = True
    ctx.owner_client_id = "owner"
    sdk = ctx.sdk = SdkHandle(machine.cfg)
    sdk.client = native = NativeClient()
    machine.sessions[ctx.key] = ctx
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk._start_message_pump()
    consumer = None
    try:
        if previous_owner:
            await machine._emit(ctx, UserMsg(msg_id=previous_owner, prompt="previous"))
            await machine._emit(ctx, TurnEnd(turn_id=previous_owner, result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)))
        sdk._turn_origin_id = previous_owner
        ctx.active_msg_id = sdk.next_turn_id = "current-input"
        ctx.translator = StreamTranslator(1024, turn_id="current-input")
        await sdk.query("current")
        await machine._set_state(ctx, "running")
        await machine._emit(ctx, UserMsg(msg_id="current-input", prompt="current"))

        async def consume():
            async for message in sdk.receive_response():
                for event in ctx.translator.feed(message):
                    await machine._emit(ctx, event)

        consumer = ctx.turn_task = asyncio.create_task(consume())
        if not before_echo:
            await native.queue.put(user("native-human", "current"))
        await native.queue.put(status())
        await until(lambda: any(isinstance(e, ProcessEvent) for e in transport.sent))
        if before_echo:
            await native.queue.put(user("native-human", "current"))
        if unrelated_result:
            await native.queue.put({**result(), "origin": {
                "kind": "task-notification", "taskId": "unrelated-task"}})
        await native.queue.put(boundary())
        await native.queue.put(assistant("continued", [{"type": "text", "text": "working"}]))
        await until(lambda: any(e.type == "delta" for e in transport.sent))
        processes = [e for e in transport.sent if isinstance(e, ProcessEvent)]
        assert len(processes) == 2
        start, end = processes
        assert end.input == {"compaction_started_id": start.item_id}
        assert end.turn_id == start.turn_id
        assert end.status == "succeeded"
        assert end.background is (True if previous_owner and before_echo else None)

        for _ in range(2):
            transport.sent.clear()
            await machine._handle_sync_btw(SyncBtw(
                sid=ctx.key, client_id="new-tab", owner_id="owner"))
            restored = [e for e in transport.sent if isinstance(e, ProcessEvent)]
            assert len(restored) == 1
            assert restored[0].item_id == end.item_id
            assert restored[0].status == "succeeded"
            assert any(isinstance(e, StateEvent) and e.state == "running"
                       and e.msg_id == "current-input" for e in transport.sent)

        await native.queue.put(result())
        await asyncio.wait_for(consumer, 3)
        sdk.release_background_messages()
        await until(lambda: not machine._claude_autonomous_followup_pending(ctx))
        assert native.inputs == ["current"] and native.interrupts == 0
    finally:
        if consumer:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await sdk._stop_message_pump()
