"""A provider error Claude Code continues past is progress, not the answer."""
from types import SimpleNamespace

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
    UserMessage,
)

from cc_remote.protocol import AssistantMsgEnd, AssistantMsgStart, Delta, TurnEnd
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import StreamTranslator, translate_history

LOST = "API Error: Connection lost mid-response. The response above may be incomplete."
HUMAN, ERROR, WORK, RESULT, DONE = (
    f"{n:08d}-5555-4555-8555-555555555555" for n in range(1, 6))


def _row(uid, role, content, **fields):
    return SimpleNamespace(
        uuid=uid, type=role,
        parent_tool_use_id=fields.pop("parent_tool_use_id", None),
        message={"role": role, "content": content, **fields})


def _error_row():
    return _row(ERROR, "assistant", [{"type": "text", "text": LOST}],
                model="<synthetic>", stop_reason="stop_sequence")


def _channels(events, message_id):
    return {event.channel for event in events
            if isinstance(event, (AssistantMsgStart, Delta, AssistantMsgEnd))
            and event.message_id == message_id}


def test_history_demotes_a_provider_error_the_same_response_continued_past():
    events = translate_history([
        _row(HUMAN, "user", "implement it"),
        _error_row(),
        _row(WORK, "assistant", [{"type": "tool_use", "id": "build",
             "name": "Bash", "input": {"command": "make"}}],
             stop_reason="tool_use"),
        _row(RESULT, "user", [{"type": "tool_result", "tool_use_id": "build",
             "content": "ok"}]),
        _row(DONE, "assistant", [{"type": "text", "text": "implemented"}],
             stop_reason="end_turn"),
    ], 10_000)

    assert _channels(events, ERROR) == {"commentary"}
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.result.subtype == "success"
    assert terminal.result.is_error is False
    turn = materialize_history_turns([event.model_dump() for event in events])[0]
    assert "error" not in turn
    finals = [block["text"] for block in turn["blocks"]
              if block["kind"] == "text" and block["channel"] == "final"]
    assert finals == ["implemented"]


def test_history_keeps_a_terminal_provider_error_as_the_failed_answer():
    events = translate_history([
        _row(HUMAN, "user", "implement it"),
        _error_row(),
    ], 10_000)

    assert _channels(events, ERROR) == {"final"}
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.result.is_error is True


def test_history_recovery_does_not_clear_an_earlier_failure():
    events = translate_history([
        _row(HUMAN, "user", "/compact"),
        _error_row(),
        _row(DONE, "assistant", [{"type": "text", "text": "summary"}],
             stop_reason="end_turn"),
    ], 10_000)

    assert _channels(events, ERROR) == {"commentary"}
    terminal = next(event for event in events if isinstance(event, TurnEnd))
    # A /compact command row alone never proves compaction succeeded.
    assert terminal.result.is_error is True


def _assistant(content, *, model="claude-test", stop_reason=None, uuid=None):
    return AssistantMessage(content=content, model=model,
                            stop_reason=stop_reason, uuid=uuid)


def _result(is_error=False):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=is_error, num_turns=1, session_id="session-1")


def test_live_provider_error_stays_progress_when_claude_continues():
    translator = StreamTranslator(10_000, turn_id="browser-turn")
    error = translator.feed(_assistant(
        [TextBlock(text=LOST)], model="<synthetic>",
        stop_reason="stop_sequence", uuid=ERROR))
    error_mid = next(event.message_id for event in error
                     if isinstance(event, Delta))
    assert _channels(error, error_mid) == {"commentary"}
    translator.feed(_assistant([ToolUseBlock(
        id="build", name="Bash", input={"command": "make"})],
        stop_reason="tool_use", uuid=WORK))
    translator.feed(UserMessage(content=[ToolResultBlock(
        tool_use_id="build", content="ok", is_error=False)]))
    translator.feed(_assistant([TextBlock(text="implemented")],
                               stop_reason="end_turn", uuid=DONE))
    terminal = translator.feed(_result())

    assert not any(isinstance(event, AssistantMsgEnd)
                   and event.message_id == error_mid for event in terminal)
    assert isinstance(terminal[-1], TurnEnd)


def test_live_terminal_provider_error_is_promoted_to_the_answer():
    translator = StreamTranslator(10_000, turn_id="browser-turn")
    error = translator.feed(_assistant(
        [TextBlock(text=LOST)], model="<synthetic>",
        stop_reason="stop_sequence", uuid=ERROR))
    error_mid = next(event.message_id for event in error
                     if isinstance(event, Delta))
    terminal = translator.feed(_result(is_error=True))

    correction = [event for event in terminal
                  if isinstance(event, AssistantMsgEnd)]
    assert [(event.message_id, event.channel) for event in correction] == [
        (error_mid, "final")]
    assert not any(isinstance(event, Delta) for event in terminal)
