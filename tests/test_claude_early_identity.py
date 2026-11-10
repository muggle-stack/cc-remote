"""A first question must already route under its native catalog identity."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import AssistantMessage, StreamEvent, TextBlock

from cc_remote.config import WrapperConfig
from cc_remote.protocol import AnswerQuestion, SwitchSession
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import extract_session_id
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_steering import NativeClient, result, until, user

SID = "540455a1-e9f7-4a9e-a558-126b4321e982"


@pytest.mark.parametrize("parent", [None, "child-tool"])
@pytest.mark.parametrize("kind", ["stream", "assistant"])
def test_native_parent_identity_is_available_before_result(kind, parent):
    message = (StreamEvent(uuid="event", session_id=SID, event={}, parent_tool_use_id=parent)
               if kind == "stream" else AssistantMessage(
                   content=[TextBlock(text="")], model="claude-test", session_id=SID,
                   parent_tool_use_id=parent))
    assert extract_session_id(message) == (SID if parent is None else None)


@pytest.mark.asyncio
async def test_tool_only_first_turn_can_be_opened_and_answered_from_native_catalog(monkeypatch):
    native = NativeClient()
    native.disconnect = AsyncMock()
    sdk = SdkHandle(WrapperConfig())
    sdk.client = native
    sdk.applied_auto_compact_mode = sdk.auto_compact_mode
    sdk.applied_effort = sdk.effort
    sdk.refresh_goal = AsyncMock()
    machine, transport, ctx = _machine_with_sdk(sdk)
    machine.sessions.clear()
    ctx.session_id = None
    ctx.key = "tmp-first-question"
    machine.sessions[ctx.key] = ctx
    machine.focused_sid = "another-session"
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    monkeypatch.setattr(machine, "_spawn", AsyncMock(side_effect=AssertionError("must reuse live question owner")))
    monkeypatch.setattr(machine, "_load_history", AsyncMock())
    sdk._start_message_pump()
    ctx.active_msg_id = "human"
    ctx.state = "running"
    runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "ask once"))
    question = None
    try:
        await until(lambda: bool(native.inputs))
        await native.queue.put(user("native-human", "ask once"))
        # No init message: the startup control read already consumed it.
        await native.queue.put({"type": "stream_event", "uuid": "first-tool", "session_id": SID,
                                "event": {"type": "message_start", "message": {"id": "answer", "role": "assistant", "content": []}}})
        await until(lambda: ctx.key == SID)
        assert machine.focused_sid == "another-session"
        assert list(machine.sessions) == [SID]
        question = asyncio.create_task(machine._on_ask(ctx, "Continue?", [{"label": "Continue"}, {"label": "Stop"}]))
        await until(lambda: bool(ctx.pending_asks))
        ask_id = next(iter(ctx.pending_asks))
        assert any(event.type == "ask_user" and event.sid == SID for event in transport.sent)
        await machine._handle_switch_session(SwitchSession(session_id=SID, engine="claude", client_id="catalog-view"))
        assert machine.sessions[SID] is ctx
        assert ctx.sdk is sdk and not runner.done()
        await machine._handle_answer_question(AnswerQuestion(sid=SID, ask_id=ask_id, answer="Continue"))
        assert await question == "Continue"
        await native.queue.put({**result(), "session_id": SID})
        await asyncio.wait_for(runner, 3)
        assert not ctx.pending_asks
        assert len(native.inputs) == 1
    finally:
        for task in [runner, question]:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await sdk.detach_for_shutdown()
