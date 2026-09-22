"""Exercise native compact ordering through the real SDK pump and projection."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage, SystemMessage

from cc_remote.protocol import GetContext, ProcessEvent
from cc_remote.wrapper.claude_compaction import compact_metadata
from cc_remote.wrapper.sdk import ClaudeBackgroundBoundary, SdkHandle
from cc_remote.wrapper.stream import StreamTranslator
from cc_remote.wrapper.work_context import recover_claude_context_usage
from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk


def boundary():
    return {
        "type": "system", "subtype": "compact_boundary", "uuid": "native-boundary",
        "compact_metadata": {
            "trigger": "manual", "pre_tokens": 600_000,
            "post_tokens": 8_000, "duration_ms": 20_000,
        },
    }


def result():
    return {
        "type": "result", "subtype": "success", "duration_ms": 20_000,
        "duration_api_ms": 19_000, "is_error": False, "num_turns": 1,
        "session_id": SESSION_ID,
    }


def status():
    return {"type": "system", "subtype": "status", "status": "compacting",
            "uuid": "compact-start"}


class CompactClient:
    def __init__(self, rows):
        self._query = self
        self.queue = asyncio.Queue()
        self.rows = rows
        self.prompts = []

    async def query(self, prompt):
        self.prompts.append(prompt)
        for row in self.rows:
            await self.queue.put(row)

    async def receive_messages(self):
        while True:
            yield await self.queue.get()


@pytest.mark.parametrize("replay_user", [False, True])
@pytest.mark.parametrize("post_tokens", [8_000, 0])
def test_native_compact_boundary_before_user_replay_reaches_control(replay_user, post_tokens):
    async def run():
        compact = boundary()
        compact["compact_metadata"]["post_tokens"] = post_tokens
        rows = [status(), status(), compact]
        if replay_user:
            rows.append({"type": "user", "uuid": "native-user",
                         "message": {"role": "user", "content": "/compact"}})
        rows.append(result())
        handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
        handle.client = CompactClient(rows)
        handle._record_context_usage({
            "totalTokens": 600_000, "maxTokens": 800_000,
            "rawMaxTokens": 1_000_000, "autoCompactThreshold": 800_000,
        })
        background = []

        async def on_background(message, _turn):
            background.append(message)

        handle.background_message_callback = on_background
        handle._start_message_pump()
        machine, transport, ctx = _machine_with_sdk(handle)
        try:
            event = await asyncio.wait_for(machine._compact_managed_claude_context(
                ctx, reason="test native order"), timeout=1)
            assert event.item_id == "native-boundary"
            assert event.summary == f"手动压缩 · 600,000 → {post_tokens:,} tokens"
            assert event.duration_ms == 20_000
            assert event.input == {"compaction_started_id": "compact-start"}
            assert handle.client.prompts == ["/compact"]
            assert background == []
            processes = [e for e in transport.sent if isinstance(e, ProcessEvent)]
            assert [e.phase for e in processes] == ["start", "end"]
            assert ctx.claude_compaction_revision == 1
            report = await machine._handle_get_context(GetContext(sid=SESSION_ID))
            assert report.available is not False
            assert report.total_tokens == post_tokens
            assert report.max_tokens == 800_000
            assert report.auto_compact_threshold_tokens == 800_000
            assert report.categories == []
        finally:
            await handle._stop_message_pump()

    asyncio.run(run())


def test_malformed_context_read_preserves_count_and_capacity():
    handle = SdkHandle(SimpleNamespace())
    valid = {"totalTokens": 8_000, "maxTokens": 800_000,
             "rawMaxTokens": 1_000_000, "autoCompactThreshold": 800_000}
    handle._record_context_usage(valid)
    handle._record_context_usage({"model": "claude-sonnet-4-6"})
    assert handle.cached_context_usage() == valid
    assert handle.raw_context_max_tokens == 1_000_000
    assert handle.effective_auto_compact_threshold_tokens == 800_000


def test_regular_prompt_does_not_claim_an_earlier_compact_boundary():
    async def run():
        rows = [status(), boundary(),
                {"type": "user", "uuid": "new-user", "origin": {"kind": "human"},
                 "message": {"role": "user", "content": "continue"}}, result()]
        handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
        handle.client = CompactClient(rows)
        background = []

        async def on_background(message, _turn):
            background.append(message)

        handle.background_message_callback = on_background
        handle._start_message_pump()
        try:
            await handle.query("continue")
            messages = [message async for message in handle.receive_response()]
            handle.release_background_messages()
            await asyncio.wait_for(handle._background_callbacks_drained.wait(), 1)
            assert not any(isinstance(m, SystemMessage) for m in messages)
            assert [m.subtype for m in background if isinstance(m, SystemMessage)] == [
                "status", "compact_boundary"]
            # The human terminal also retires the anonymous pre-input activity.
            # Its boundary is internal lifecycle bookkeeping, not a second
            # compact event or a duplicate native Result in the history.
            assert len(background) == 3
            assert isinstance(background[-1], ClaudeBackgroundBoundary)
            assert background[-1].identities == (background[0]._cc_background_start["id"],)
            assert not any(isinstance(m, ResultMessage) for m in background)
            assert isinstance(messages[-1], ResultMessage)
        finally:
            await handle._stop_message_pump()

    asyncio.run(run())


@pytest.mark.parametrize("is_error", [False, True])
def test_terminal_without_boundary_stops_animation_without_success(is_error):
    translator = StreamTranslator(1024, turn_id="turn")
    translator.feed(SystemMessage(subtype="status", data=status()))
    terminal = result()
    terminal.pop("type")
    terminal["is_error"] = is_error
    events = translator.feed(ResultMessage(**terminal))
    process = next(e for e in events if isinstance(e, ProcessEvent))
    assert process.item_id == "compact-start"
    assert process.phase == "end"
    assert process.status == ("interrupted" if is_error else "failed")
    assert process.summary is None


@pytest.mark.parametrize("post", [8_000, 0, None])
def test_cold_context_read_stops_at_latest_compact_boundary(tmp_path, post):
    path = tmp_path / "session.jsonl"
    end = boundary()
    end["compact_metadata"]["post_tokens"] = post
    old = {"type": "assistant", "message": {"usage": {"input_tokens": 600_000}}}
    path.write_text("\n".join(map(json.dumps, [old, end])))
    expected = {"totalTokens": post} if post is not None else None
    assert recover_claude_context_usage(SESSION_ID, path=str(path)) == expected
    with path.open("a") as stream:
        stream.write("\n" + json.dumps({"type": "assistant", "message": {
            "usage": {"input_tokens": 8_000, "output_tokens": 500}}}))
    assert recover_claude_context_usage(SESSION_ID, path=str(path)) == {
        "totalTokens": 8_500}


@pytest.mark.parametrize("value", [True, -1, 2**54, [], "8000"])
def test_compact_metadata_does_not_publish_invalid_counters(value):
    assert compact_metadata({"compact_metadata": {
        "trigger": [], "pre_tokens": value, "post_tokens": value,
        "duration_ms": value}}) == {}
