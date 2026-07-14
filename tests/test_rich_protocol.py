"""Protocol-v6 contracts for engine-neutral process timeline events."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    DOWNSTREAM_TYPES,
    AssistantMsgEnd,
    AssistantMsgStart,
    Delta,
    ProcessEvent,
    ToolDelta,
    ToolResult,
    ToolUse,
    TurnDiff,
    TurnPlan,
    deserialize,
    serialize,
)


@pytest.mark.parametrize("event", [
    AssistantMsgStart(message_id="comment-1", channel="commentary"),
    Delta(message_id="comment-1", channel="commentary", text="正在检查。"),
    AssistantMsgEnd(message_id="comment-1", channel="commentary"),
    ToolUse(
        message_id="comment-1",
        tool_use_id="command-1",
        tool="commandExecution",
        input={"command": "pytest -q"},
        category="command",
        title="运行测试",
    ),
    ToolDelta(tool_use_id="command-1", stream="output", delta="ok\n"),
    ToolResult(
        tool_use_id="command-1",
        content="ok\n",
        is_error=False,
        status="succeeded",
        exit_code=0,
        duration_ms=125,
    ),
    ProcessEvent(
        item_id="hook-1",
        kind="hook",
        phase="end",
        status="succeeded",
        title="Hook 完成",
        duration_ms=20,
    ),
    TurnPlan(
        item_id="plan-1",
        explanation="执行计划",
        plan=[{"step": "检查代码", "status": "completed"}],
    ),
    TurnDiff(item_id="diff-1", diff="diff --git a/a b/a\n"),
])
def test_rich_event_roundtrip(event):
    assert deserialize(serialize(event)) == event
    assert event.type in DOWNSTREAM_TYPES


def test_rich_event_payloads_are_bounded_and_strict():
    with pytest.raises(ValidationError):
        ToolDelta(tool_use_id="tool-1", stream="output", delta="x" * (512 * 1024 + 1))
    with pytest.raises(ValidationError):
        ProcessEvent(
            item_id="hook-1",
            kind="hook",
            phase="end",
            status="succeeded",
            title="Hook",
            env={"TOKEN": "must never be accepted as an extra wire field"},
        )
