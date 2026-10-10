"""Read-only presentation of the optional account-local async task MCP."""
from __future__ import annotations

import json
from pathlib import Path

from cc_remote.protocol import BackgroundProcessItem, ProcessEvent
from cc_remote.task_store import TERMINAL, TaskStore, thread_id
from cc_remote.wrapper.sanitize import bounded_text

TASK_SERVER = "cc_remote_tasks"
TASK_RECEIPT_TOOL = "task_result"
TASK_STATES = {
    "completed": "succeeded", "failed": "failed", "timed_out": "failed",
    "interrupted": "interrupted", "cancelled": "cancelled",
}
TASK_LABELS = {
    "completed": "任务已完成", "failed": "任务失败", "timed_out": "任务超时",
    "interrupted": "任务已中断", "cancelled": "任务已取消",
}


def read_task_activity(home: Path) -> dict[str, list[BackgroundProcessItem]]:
    result: dict[str, list[BackgroundProcessItem]] = {}
    for row in TaskStore(home).activity_snapshot():
        sid, key = thread_id(row["sid"]), thread_id(row["id"])
        state = row["state"]
        if state not in TERMINAL | {"queued", "running"}:
            raise ValueError("Unknown async task state")
        terminal = state in TERMINAL
        result.setdefault(sid, []).append(BackgroundProcessItem(
            item_id=f"async-task:{key}", kind="task",
            title=row["title"], status="running" if state == "running" else "pending",
            summary=(f"{TASK_LABELS[state]}，等待通知 Codex" if terminal
                     else "等待执行" if state == "queued" else "后台执行中"),
            started_at=row["created"], updated_at=row["updated"],
        ))
    return result


def task_receipt_event(item: dict, *, item_id: str, turn_id: str | None,
                       detail_limit: int) -> ProcessEvent | None:
    """Only native tool-output receipts establish a continuation boundary."""
    if item.get("namespace") != TASK_SERVER or item.get("name") != TASK_RECEIPT_TOOL:
        return None
    raw = item.get("output")
    if not isinstance(raw, str) or len(raw) > 256 * 1024:
        return None
    try:
        receipt = json.loads(raw)
        if not isinstance(receipt, dict):
            return None
        key = thread_id(receipt.get("task_id"))
        state = receipt.get("state")
        if not isinstance(state, str) or state not in TASK_STATES:
            return None
    except (ValueError, TypeError):
        return None
    title, _ = bounded_text(receipt.get("title"), 120)
    detail, truncated = bounded_text(receipt.get("output"), detail_limit)
    return ProcessEvent(
        item_id=item_id, kind="task", phase="end", status=TASK_STATES[state],
        turn_id=turn_id, title=title or "后台任务",
        server=TASK_SERVER, tool=TASK_RECEIPT_TOOL,
        summary=TASK_LABELS[state], detail=detail or None,
        input={"task_id": key},
        truncated=bool(truncated or receipt.get("output_truncated")),
    )
