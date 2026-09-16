"""Native usage accounting, ownership and reconnect regressions (no model calls)."""
import asyncio

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent
from pydantic import ValidationError

from cc_remote.protocol import TokenUsage, TurnUsage, deserialize, serialize
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import CodexStreamTranslator
from cc_remote.wrapper.dsh_stream import DshProjection
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.stream import StreamTranslator
from cc_remote.wrapper.token_usage import CodexUsageTracker, native_usage


def stream(event, parent=None):
    return StreamEvent(uuid="event-id", session_id="session", event=event,
                       parent_tool_use_id=parent)


def start(mid, tokens=10):
    return stream({"type": "message_start", "message": {"id": mid, "usage": {
        "input_tokens": tokens, "output_tokens": 1,
        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20,
    }}})


def test_claude_usage_is_cumulative_per_response_not_per_block_or_replay():
    tr = StreamTranslator(8000, turn_id="user-a")
    first = tr.feed(start("m1"))[0]
    assert first.usage.input_tokens == 130
    assert first.usage.output_tokens is None  # SDK message-start placeholder
    delta = stream({"type": "message_delta", "usage": {"output_tokens": 40}})
    assert tr.feed(delta)[0].usage.output_tokens == 40
    assert tr.feed(delta) == []
    assert tr.feed(AssistantMessage(content=[], model="test", message_id="m1",
        usage={"input_tokens": 10, "output_tokens": 1})) == []
    second = tr.feed(start("m2", 15))[0]
    assert second.usage.input_tokens == 265
    assert second.usage.cache_read_tokens == 200
    assert tr.feed(delta)[0].usage.output_tokens == 80
    assert tr.feed(stream({"type": "message_delta", "usage": {"output_tokens": 999}},
                          parent="subagent")) == []
    tr.rebind_turn("user-b")
    late = tr.feed(stream({"type": "message_delta", "usage": {"output_tokens": 50}}))[0]
    assert late.turn_id == "user-a"
    assert late.usage.output_tokens == 90
    assert tr.feed(start("m3"))[0].turn_id == "user-b"
    assert tr.feed(delta)[0].usage.output_tokens == 40


def test_claude_result_supplies_usage_when_partial_usage_is_unavailable():
    tr = StreamTranslator(8000, turn_id="user")
    events = tr.feed(ResultMessage(subtype="success", duration_ms=12,
        duration_api_ms=10, is_error=False, num_turns=1, session_id="session",
        usage={"input_tokens": 50, "output_tokens": 75, "cache_read_input_tokens": 100}))
    usage = next(event for event in events if isinstance(event, TurnUsage))
    assert usage.usage.input_tokens == 150
    assert usage.usage.output_tokens == 75
    assert events[-1].type == "turn_end"


def codex_sample(turn, inputs, outputs, *, last_inputs=20, last_outputs=10, cached=10):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": "thread", "turnId": turn, "tokenUsage": {
            "total": {"inputTokens": inputs, "outputTokens": outputs, "cachedInputTokens": cached},
            "last": {"inputTokens": last_inputs, "outputTokens": last_outputs, "cachedInputTokens": 10},
        }}}


def test_codex_totals_do_not_recount_duplicate_or_identical_requests():
    tracker = CodexUsageTracker()
    assert tracker.feed(codex_sample("old", 1000, 200)).usage.input_tokens == 20
    tracker.feed({"method": "turn/started", "params": {"turn": {"id": "new"}}})
    update = codex_sample("new", 1020, 210, cached=20)
    first = tracker.feed(update)
    assert first.usage == TokenUsage(input_tokens=20, output_tokens=10, cache_read_tokens=10)
    assert tracker.feed(update) is None
    assert tracker.feed(codex_sample("old", 1000, 200)) is None
    second = tracker.feed(codex_sample("new", 1040, 220, cached=30))
    assert second.usage.input_tokens == 40
    assert second.usage.output_tokens == 20
    tracker.feed({"method": "thread/compacted", "params": {}})
    reset = tracker.feed(codex_sample("new", 20, 10))
    assert reset.usage.input_tokens == 60
    assert tracker.feed(codex_sample("new", 20, 10)) is None


def test_codex_usage_reaches_managed_and_spontaneous_readers_without_foreign_updates():
    class Cfg:
        tool_result_max = 8000
        cc_cwd = "/tmp"
        turn_reader_queue_cap = 32

    async def run():
        handle = CodexHandle(Cfg())
        handle.thread_id, handle.turn_id, handle.turn_active = "thread", "turn", True
        handle._turn_q = asyncio.Queue()
        foreign = codex_sample("turn", 1000, 200)
        foreign["params"]["threadId"] = "other-thread"
        await handle._dispatch(foreign)
        assert handle._turn_q.empty()
        await handle._dispatch(codex_sample("turn", 1000, 200))
        native = handle._turn_q.get_nowait()
        event = CodexStreamTranslator(8000).feed(native)[0]
        assert event.turn_id == "turn"
        assert event.usage.output_tokens == 10
        # A duplicate can remain in the queue, but carries no new usage reading.
        await handle._dispatch(codex_sample("turn", 1000, 200))
        assert CodexStreamTranslator(8000).feed(handle._turn_q.get_nowait()) == []

        handle._turn_q = None
        handle._spontaneous_turn_id = "turn"
        captured = []
        handle._queue_spontaneous_notification = lambda message, size: captured.append(message)
        await handle._dispatch(codex_sample("turn", 1020, 210, cached=20))
        assert CodexStreamTranslator(8000).feed(captured[0])[0].usage.output_tokens == 20

    asyncio.run(run())


def test_dsh_live_settlement_reconnect_and_retry_usage_count_once():
    p = DshProjection()
    raw = {"inputTokens": 10, "outputTokens": 20, "cacheReadTokens": 100, "cacheWriteTokens": 5}
    def record(seq, kind, data):
        return p.record({"seq": seq, "type": kind, "time": 1, "data": data})
    record(0, "turn/start", {"turn": 1})
    p.frame({"type": "start", "attemptId": "a", "turn": 1, "step": 1, "revision": 1})
    live = p.frame({"type": "chunk", "attemptId": "a", "revision": 2, "index": 0,
                   "chunk": {"type": "usage", "usage": raw}})[0]
    assert live.usage.input_tokens == 115
    settled = {"turn": 1, "step": 1, "message": {"content": []}, "usage": raw}
    assert not any(isinstance(e, TurnUsage) for e in record(1, "assistant/message", settled))
    assert record(1, "assistant/message", settled) == []
    next_events = record(2, "assistant/message", {**settled, "step": 2})
    assert next(e for e in next_events if isinstance(e, TurnUsage)).usage.output_tokens == 40
    cold = DshProjection()
    cold.record({"seq": 0, "type": "turn/start", "time": 1, "data": {"turn": 1}})
    cold.record({"seq": 1, "type": "assistant/message", "time": 1, "data": settled})
    replay = cold.record({"seq": 2, "type": "assistant/message", "time": 1,
                         "data": {**settled, "step": 2}})
    assert next(e for e in replay if isinstance(e, TurnUsage)).usage.output_tokens == 40
    retry = record(3, "assistant/attempt", {"turn": 1, "step": 3, "stream": [
        {"type": "chunk", "chunk": {"type": "usage", "usage": raw}},
    ]})
    assert next(e for e in retry if isinstance(e, TurnUsage)).usage.output_tokens == 60
    p.frame({"type": "start", "attemptId": "retry", "turn": 1, "step": 3, "revision": 10})
    retry_live = p.frame({"type": "chunk", "attemptId": "retry", "revision": 11, "index": 0,
                         "chunk": {"type": "usage", "usage": raw}})
    assert retry_live[0].usage.output_tokens == 80
    assert not any(isinstance(e, TurnUsage) for e in record(4, "assistant/message", {**settled, "step": 3}))


def test_usage_snapshot_survives_evicted_replay_tail_and_protocol_rejects_bad_counts():
    ring = RingBuffer(max_events=1, max_bytes=1024)
    for i in range(10):
        event = TurnUsage(turn_id=f"turn-{i}", usage=TokenUsage(input_tokens=i), seq=i+1)
        assert deserialize(serialize(event)) == event
        ring.append(event)
    snapshot = ring.replay_from(None, cc_session_id="session", state="running")[0]
    assert len(snapshot.turn_usage) == 8
    assert snapshot.turn_usage[-1].usage.input_tokens == 9
    caught_up = ring.replay_from(10, cc_session_id="session", state="running")[-1]
    assert caught_up.turn_usage[-1].usage.input_tokens == 9
    for invalid in (-1, True, 1.5, float("inf"), 2**53):
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=invalid)
        assert native_usage({"inputTokens": invalid}, "codex") is None
