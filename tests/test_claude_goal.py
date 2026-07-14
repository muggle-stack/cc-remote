"""Zero-token coverage for Claude Code's native /goal bridge."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    UserMessage,
)

from cc_remote.protocol import Error, GoalState
from cc_remote.wrapper import claude_goal as goal_module
from cc_remote.wrapper.claude_goal import (
    active_goal_from_message,
    goal_message_update,
    make_claude_goal,
    read_claude_goal,
)
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


def _record(kind, **extra):
    return {"type": kind, **extra}


def test_transcript_goal_status_recovers_progress_and_clear(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    records = [
        _record("attachment", timestamp="2026-07-12T00:00:00Z", attachment={
            "type": "goal_status", "met": False, "sentinel": True,
            "condition": "ship it",
        }),
        _record("assistant", message={
            "id": "same-message", "usage": {"input_tokens": 10},
        }),
        # Claude transcripts can contain several content records for one API
        # message. Only the greatest observed usage for that id counts.
        _record("assistant", message={
            "id": "same-message", "usage": {
                "input_tokens": 10, "output_tokens": 5,
            },
        }),
        _record("attachment", timestamp="2026-07-12T00:00:03Z", attachment={
            "type": "goal_status", "met": False, "condition": "ship it",
            "reason": "tests still running",
        }),
    ]
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(goal_module, "transcript_path", lambda _sid: str(transcript))

    exists, goal = read_claude_goal("session-1", now=1_783_814_410)
    assert exists is True
    assert goal == {
            "threadId": "session-1",
            "objective": "ship it",
            "status": "active",
            "engine": "claude",
        "tokenBudget": None,
        "tokensUsed": 15,
        "timeUsedSeconds": 10,
        "createdAt": 1_783_814_400.0,
        "updatedAt": 1_783_814_403.0,
        "iterations": 1,
        "setAt": 1_783_814_400.0,
        "lastReason": "tests still running",
    }

    with transcript.open("a", encoding="utf-8") as output:
        output.write(json.dumps(_record(
            "attachment", timestamp="2026-07-12T00:00:04Z", attachment={
                "type": "goal_status", "met": True, "sentinel": True,
                "condition": "ship it",
            })) + "\n")
    assert read_claude_goal("session-1", now=1_783_814_410) == (True, None)


def test_raw_active_goal_schema_maps_to_common_contract():
    message = SystemMessage(subtype="active_goal", data={
        "type": "active_goal",
        "value": {
            "condition": "finish review",
            "iterations": 2,
            "set_at": 1_783_814_400_000,
            "tokens_at_start": 900,
            "last_reason": "one test remains",
        },
    })
    goal = active_goal_from_message(
        message, "session-2", now=1_783_814_405)
    assert goal["threadId"] == "session-2"
    assert goal["objective"] == "finish review"
    assert goal["iterations"] == 2
    assert goal["tokensAtStart"] == 900
    assert goal["timeUsedSeconds"] == 5
    assert goal["lastReason"] == "one test remains"
    assert active_goal_from_message(
        SystemMessage(subtype="active_goal", data={"value": None}),
        "session-2",
    ) is None


def test_live_goal_feedback_updates_iterations_and_deduplicates_usage():
    goal = make_claude_goal("session-3", "wait for green", now=100)
    token_totals = {}
    first = AssistantMessage(
        content=[], model="claude", message_id="m1",
        usage={"input_tokens": 7, "output_tokens": 3},
    )
    assert goal_message_update(first, goal, token_totals, now=101) is True
    assert goal_message_update(first, goal, token_totals, now=102) is False
    feedback = UserMessage(content=(
        "Stop hook feedback:\n[wait for green]: CI is still running"))
    assert goal_message_update(feedback, goal, token_totals, now=103) is True
    assert goal["tokensUsed"] == 10
    assert goal["iterations"] == 1
    assert goal["lastReason"] == "CI is still running"


def test_sdk_compat_reader_preserves_active_goal_before_result():
    async def run():
        raw = [
            {
                "type": "active_goal", "value": {
                    "condition": "done", "iterations": 0,
                    "set_at": 1, "tokens_at_start": 2,
                },
                "session_id": "s", "uuid": "u",
            },
            {
                "type": "result", "subtype": "success",
                "duration_ms": 1, "duration_api_ms": 1,
                "is_error": False, "num_turns": 0,
                "session_id": "s",
            },
        ]

        class Query:
            async def receive_messages(self):
                for item in raw:
                    yield item

        handle = SdkHandle(SimpleNamespace())
        handle.client = SimpleNamespace(_query=Query())
        messages = [message async for message in handle.receive_response()]
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].subtype == "active_goal"
        assert isinstance(messages[1], ResultMessage)

    asyncio.run(run())


def test_machine_routes_claude_goal_commands_through_normal_turn():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-goal", "claude-goal")
        ctx.engine = "claude"
        handle = SdkHandle(SimpleNamespace())

        async def context_usage():
            return {"totalTokens": 123}

        handle.get_context_usage = context_usage
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        queries = []

        async def handle_query(query):
            queries.append(query)
            return None

        machine._handle_query = handle_query
        result = await machine._handle_set_goal(SimpleNamespace(
            sid=ctx.key, objective="finish tests", status="active",
            token_budget=None,
        ))
        assert isinstance(result, GoalState)
        assert queries[-1].prompt == "/goal finish tests"
        assert result.goal.tokensAtStart == 123
        assert ctx.goal_visible is True

        result = await machine._handle_clear_goal(SimpleNamespace(sid=ctx.key))
        assert isinstance(result, GoalState) and result.goal is None
        assert queries[-1].prompt == "/goal clear"
        assert ctx.goal_visible is False

        count = len(queries)
        error = await machine._handle_set_goal(SimpleNamespace(
            sid=ctx.key, objective="x", status="paused", token_budget=None,
        ))
        assert isinstance(error, Error)
        assert len(queries) == count
        states = [message for message in transport.sent
                  if isinstance(message, GoalState)]
        assert states[-2].goal.objective == "finish tests"
        assert states[-1].goal is None

    asyncio.run(run())
