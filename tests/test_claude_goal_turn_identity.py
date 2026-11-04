"""Native Claude /goal turns keep one identity across live and History."""
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage, UserMessage

from cc_remote.protocol import AssistantMsgEnd, Delta, ToolUse, TurnEnd, UserMsg
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import (
    StreamTranslator, replayed_user_message_id, translate_history,
)

PROMPT = "33333333-3333-4333-8333-333333333331"
ANSWER = "33333333-3333-4333-8333-333333333332"
GOAL = "33333333-3333-4333-8333-333333333333"
HOOK = "33333333-3333-4333-8333-333333333334"
ACK = "33333333-3333-4333-8333-333333333335"
WORK = "33333333-3333-4333-8333-333333333336"
RESULT = "33333333-3333-4333-8333-333333333337"
DONE = "33333333-3333-4333-8333-333333333338"
BROWSER_GOAL = "goal-9059fc03e36c4db3887edc24dd5404a9"


def _goal_envelope(args: str) -> str:
    return (
        "<command-name>/goal</command-name>\n"
        "            <command-message>goal</command-message>\n"
        f"            <command-args>{args}</command-args>"
    )


def _row(uid, role, content, **fields):
    return SimpleNamespace(
        uuid=uid, type=role,
        parent_tool_use_id=fields.pop("parent_tool_use_id", None),
        is_meta=fields.pop("is_meta", False),
        message={"role": role, "content": content, **fields},
    )


def _goal_transcript():
    return [
        _row(PROMPT, "user", "where does the preview image go?"),
        _row(ANSWER, "assistant", [{"type": "text", "text": "use preview.png"}],
             stop_reason="end_turn"),
        _row(GOAL, "user", _goal_envelope("ship steps 1-10")),
        _row(HOOK, "user", "A session-scoped Stop hook is now active.",
             is_meta=True),
        _row(ACK, "assistant", [{"type": "text", "text": "Goal received."}],
             stop_reason=None),
        _row(WORK, "assistant", [{"type": "tool_use", "id": "build",
             "name": "Bash", "input": {"command": "make"}}],
             stop_reason="tool_use"),
        _row(RESULT, "user", [{"type": "tool_result", "tool_use_id": "build",
             "content": "ok"}]),
        _row(DONE, "assistant", [{"type": "text", "text": "All steps done."}],
             stop_reason="end_turn"),
    ]


def test_history_projects_native_goal_as_its_own_bound_turn():
    events = translate_history(
        _goal_transcript(), 10_000,
        client_message_ids={GOAL: BROWSER_GOAL},
    )

    users = [event for event in events if isinstance(event, UserMsg)]
    assert [(user.msg_id, user.client_msg_id, user.prompt) for user in users] == [
        (PROMPT, None, "where does the preview image go?"),
        (GOAL, BROWSER_GOAL, "/goal ship steps 1-10"),
    ]
    ends = [event for event in events if isinstance(event, TurnEnd)]
    assert [end.checkpoint_id for end in ends] == [PROMPT, GOAL]
    goal_start = events.index(users[1])
    assert all(
        events.index(event) > goal_start
        for event in events
        if isinstance(event, ToolUse) and event.tool_use_id == "build"
    )

    turns = materialize_history_turns([event.model_dump() for event in events])
    assert [turn["id"] for turn in turns] == [PROMPT, GOAL]
    assert turns[1]["clientMsgId"] == BROWSER_GOAL
    answers = [block["text"] for block in turns[0]["blocks"]
               if block["kind"] == "text"]
    assert answers == ["use preview.png"]


@pytest.mark.parametrize("args, prompt", [
    ("clear", "/goal clear"),
    ("", "/goal"),
])
def test_history_keeps_other_native_goal_commands_visible(args, prompt):
    events = translate_history([
        _row(PROMPT, "user", "first"),
        _row(GOAL, "user", _goal_envelope(args)),
    ], 10_000)

    assert [event.prompt for event in events if isinstance(event, UserMsg)] == [
        "first", prompt,
    ]


def test_live_goal_echo_binds_the_remote_turn_but_other_commands_do_not():
    goal = UserMessage(content=_goal_envelope("ship steps 1-10"), uuid=GOAL)
    status = UserMessage(content="<command-name>/status</command-name>",
                         uuid=PROMPT)

    assert replayed_user_message_id(goal) == GOAL
    assert replayed_user_message_id(status) is None


def test_live_and_history_goal_turns_share_checkpoint_identity():
    """A live TurnEnd names the same native user row History uses as its id."""
    translator = StreamTranslator(10_000, turn_id=BROWSER_GOAL)
    translator.feed(UserMessage(content=_goal_envelope("ship"), uuid=GOAL))
    terminal = translator.feed(ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="session-1",
    ))

    end = next(event for event in terminal if isinstance(event, TurnEnd))
    assert end.checkpoint_id == GOAL
    assert not any(isinstance(event, (Delta, AssistantMsgEnd))
                   for event in terminal)
