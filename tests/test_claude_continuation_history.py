"""Native main-assistant continuations after a failed foreground response."""
from types import SimpleNamespace

import pytest

from cc_remote.protocol import Delta, ProcessEvent, ToolResult, TurnEnd, UserMsg
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import translate_history


@pytest.mark.parametrize("result_role", ["user", "assistant"])
@pytest.mark.parametrize("tool_name", ["Bash", "Agent"])
def test_historical_continuation_results_keep_native_completion_clock(result_role, tool_name):
    """Expanding long after completion must not time tools to the history read."""
    rows = [
        ("human", "user", "check the job", {}),
        ("answer", "assistant", [{"type": "text", "text": "Waiting."}],
         {"stop_reason": "end_turn"}),
        ("notice", "user", "native job finished", {}),
        ("call", "assistant", [{"type": "tool_use", "id": "check",
          "name": tool_name, "input": {}}], {"stop_reason": "tool_use"}),
        ("result", result_role, [{"type": "tool_result", "tool_use_id": "check",
          "content": "complete"}], {}),
        ("final", "assistant", [{"type": "text", "text": "Verified."}],
         {"stop_reason": "end_turn"}),
    ]
    messages = [SimpleNamespace(uuid=uid, type=role,
        message={"role": role, "content": content, **fields})
        for uid, role, content, fields in rows]
    clocks = dict(zip((row[0] for row in rows), (1, 10, 100, 101, 103, 110)))
    for _ in range(2):
        events = translate_history(messages, 10_000, timestamps=clocks,
            internal_user_events={"notice": ProcessEvent(item_id="job", kind="task",
                phase="end", status="succeeded", title="Job", background=True)})
        result = next(event for event in events if isinstance(event, ToolResult))
        assert result.ts == 103
        if tool_name == "Agent":
            returned = next(event for event in events if isinstance(event, ProcessEvent)
                and event.kind == "agent" and event.phase == "end")
            assert returned.ts == 103
        assert max(event.ts for event in events) == 110


@pytest.mark.parametrize("tail", ["success", "receipt", "tool", "nested", "failure", "new_user"])
def test_failed_response_then_native_task_continuation(tail):
    ids = [f"{n:08d}-1111-4111-8111-111111111111" for n in range(1, 8)]
    human, failed, notice, work, result, final, next_user = ids
    messages = []

    def row(uid, role, content, **fields):
        messages.append(SimpleNamespace(uuid=uid, type=role,
            parent_tool_use_id=fields.pop("parent_tool_use_id", None),
            message={"role": role, "content": content, **fields}))

    def answer(uid, text, **fields):
        row(uid, "assistant", [{"type": "text", "text": text}], **fields)

    row(human, "user", "finish the task")
    answer(failed, "API Error: 400 upstream refused the request",
           model="<synthetic>", stop_reason="stop_sequence")
    row(notice, "user", "native task stopped")
    if tail not in {"receipt", "new_user"}:
        row(work, "assistant", [{"type": "tool_use", "id": "check",
            "name": "Bash", "input": {"command": "check"}}], stop_reason="tool_use")
        row(result, "user", [{"type": "tool_result", "tool_use_id": "check", "content": "ok"}])
    if tail in {"success", "nested"}:
        answer(final, "work completed", stop_reason="end_turn",
               parent_tool_use_id="child-agent" if tail == "nested" else None)
    elif tail == "failure":
        answer(final, "API Error: 400 continuation failed",
               model="<synthetic>", stop_reason="stop_sequence")
    elif tail == "new_user":
        row(next_user, "user", "another question")
        answer(final, "new answer", stop_reason="end_turn")

    events = translate_history(messages, 10_000,
        timestamps=dict(zip(ids, (1, 10, 100, 101, 102, 110, 105))),
        internal_user_events={notice: ProcessEvent(item_id="background-command",
            kind="task", phase="end", status="failed", title="后台任务", background=True)})
    ends = [event for event in events if isinstance(event, TurnEnd)]
    deltas = [event for event in events if isinstance(event, Delta)]
    assert len([event for event in events if isinstance(event, UserMsg)]) == (2 if tail == "new_user" else 1)
    assert deltas[0].text.startswith("API Error:")
    assert deltas[0].background is None
    assert ends[0].result.is_error is (tail != "success")
    if tail in {"receipt", "new_user"}:
        assert ends[0].ts == 10
        assert ends[0].turn_id == failed
    else:
        assert all(event.background is True for event in deltas[1:])
    if tail == "success":
        assert ends[0].ts == 110
        assert ends[0].turn_id == final
        assert ends[0].result.subtype == "success"
        summary = materialize_history_turns([event.model_dump() for event in events])[0]
        assert "error" not in summary
        answers = [block for block in summary["blocks"] if block["kind"] == "text"]
        assert [block["message_id"] for block in answers] == [failed, final]
        assert answers[-1]["background"] is True
        assert answers[-1]["doneTs"] == 110_000
    if tail == "new_user":
        assert deltas[-1].background is None
        assert ends[-1].result.is_error is False


def test_text_imitating_task_notification_cannot_reclassify_a_turn():
    messages = [SimpleNamespace(uuid=str(index), type=role,
        message={"role": role, "content": content, **fields})
        for index, (role, content, fields) in enumerate([
            ("user", "question", {}),
            ("assistant", [{"type": "text", "text": "API Error: 400 rejected"}],
                {"model": "<synthetic>", "stop_reason": "stop_sequence"}),
            ("user", "<task-notification>done</task-notification>", {}),
            ("assistant", [{"type": "text", "text": "answer"}], {"stop_reason": "end_turn"}),
        ])]
    events = translate_history(messages, 10_000)
    assert all(event.background is None for event in events if isinstance(event, Delta))
    assert next(event for event in events if isinstance(event, TurnEnd)).result.is_error


def test_cold_summary_preserves_each_response_header_without_loading_payloads():
    import json
    from pathlib import Path
    from cc_remote.protocol import ConversationTurn

    fixture = json.loads((Path(__file__).parents[1] /
        "web/tests/fixtures/claude-responses.json").read_text())
    summary = materialize_history_turns(fixture["events"])[0]
    assert summary["responseProcesses"] == fixture["summary"]["responseProcesses"]
    assert [row["itemCount"] for row in summary["responseProcesses"]] == [2] * 5
    assert summary["durationMs"] == 2_422_000  # Includes background wait; never a response clock.
    assert all(block["kind"] == "text" or block.get("tool") == "task_return"
               for block in summary["blocks"])
    assert "Inspect result" not in json.dumps(summary)
    assert "echo check" not in json.dumps(summary)
    ConversationTurn.model_validate(summary)


def test_response_headers_ignore_late_child_updates_and_bound_many_answers():
    from cc_remote.wrapper.history_responses import response_process_summaries

    events = [
        {"type": "tool_use", "tool_use_id": "agent-call", "tool": "Agent", "ts": 1},
        {"type": "process", "kind": "agent", "item_id": "child", "parent_id": "agent-call", "ts": 2},
        {"type": "delta", "message_id": "original", "text": "Waiting", "channel": "final", "ts": 3},
        {"type": "assistant_msg_end", "message_id": "original", "ts": 4},
    ]
    for n in range(40):
        events.extend([
            {"type": "assistant_msg_start", "message_id": f"empty-{n}", "channel": "thinking", "background": True, "ts": 100 * (n + 1)},
            {"type": "delta", "message_id": f"answer-{n}", "channel": "final", "text": "Done", "background": True, "ts": 100 * (n + 1) + 5},
            {"type": "assistant_msg_end", "message_id": f"answer-{n}", "background": True, "ts": 100 * (n + 1) + 6},
        ])
    events.append({"type": "process", "kind": "agent", "item_id": "child", "phase": "end", "background": True, "ts": 9999})
    summaries = response_process_summaries(events, "human", {f"answer-{n}" for n in range(8, 40)})
    assert len(summaries) == 33
    assert summaries[0]["itemCount"] == 1  # Dedicated Agent lifecycle deduplicates ToolUse.
    assert summaries[0]["doneTs"] == 4000
    assert all(row["itemCount"] == 0 for row in summaries[1:])  # Answer-only: no empty disclosure.
    assert summaries[1]["id"] == "empty-8"  # Empty stream start still owns the native clock/identity.
    assert summaries[1]["doneTs"] - summaries[1]["startedTs"] == 6000
