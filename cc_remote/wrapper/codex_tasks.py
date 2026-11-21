"""Read-only presentation of the optional account-local async task MCP."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

from cc_remote.log import redact_text
from cc_remote.protocol import (
    MAX_BACKGROUND_PROCESS_COMMAND_CHARS, MAX_BACKGROUND_PROCESS_CWD_CHARS,
    BackgroundProcessItem, ProcessEvent,
)
from cc_remote.task_store import MAX_TASK_ARGV_CHARS, TERMINAL, TaskStore, thread_id
from cc_remote.wrapper.sanitize import bounded_text

TASK_SERVER = "cc_remote_tasks"
TASK_RECEIPT_TOOL = "task_result"
TASK_ITEM_PREFIX = "async-task:"
_SECRET_OPTION = r"--[a-z0-9_-]*(?:token|password|passwd|secret|api[_-]?key|authorization|cookie|credential)"
_TASK_SECRET_FLAG = re.compile(_SECRET_OPTION, re.IGNORECASE)
_INLINE_SECRET_OPTION = re.compile(
    rf'''(?i)(?<![\w-])({_SECRET_OPTION}(?:[ \t]+|=))(?:(?:"[^"\r\n]*"|'[^'\r\n]*')|[^\s;&]+)''',
)
_SECRET_HEADER_NAME = r"(?:proxy-)?authorization|(?:set-)?cookie|x-api-key"
_TASK_SECRET_HEADER = re.compile(
    rf"""
    # Keep following arguments when a simple quoted header ends at a shell word boundary.
    (?P<quote>["'])(?P<quoted_name>{_SECRET_HEADER_NAME})[ \t]*:
        (?:(?!(?P=quote))[^\\$`])*(?P=quote)(?=\Z|[ \t\n;&|<>()])
    |
    # A direct argv header or an ambiguous shell word is masked to the end of the element.
    (?:(?<![\w-])|(?<=-H))(?P<name>{_SECRET_HEADER_NAME})\s*:[\s\S]*\Z
    """,
    re.IGNORECASE | re.VERBOSE,
)
TASK_STATES = {
    "completed": "succeeded", "failed": "failed", "timed_out": "failed",
    "interrupted": "interrupted", "cancelled": "cancelled",
}
TASK_LABELS = {
    "completed": "任务已完成", "failed": "任务失败", "timed_out": "任务超时",
    "interrupted": "任务已中断", "cancelled": "任务已取消",
}


def read_task_callbacks(home: Path, sid: str) -> tuple[str, ...]:
    """Exact accepted callback IDs, scoped to this account and native thread."""
    return tuple(dict.fromkeys(
        thread_id(row["delivery_turn_id"])
        for row in TaskStore(home).activity_snapshot()
        if row["sid"] == sid and row["state"] in TERMINAL
        and row["delivery"] == "accepted" and row.get("delivery_turn_id")))


def read_task_activity(home: Path) -> dict[str, list[BackgroundProcessItem]]:
    result: dict[str, list[BackgroundProcessItem]] = {}
    for row in TaskStore(home).activity_snapshot():
        sid, key = thread_id(row["sid"]), thread_id(row["id"])
        state = row["state"]
        if state not in TERMINAL | {"queued", "running"}:
            raise ValueError("Unknown async task state")
        terminal = state in TERMINAL
        delivery = row["delivery"]
        status = "running" if state == "running" else "pending"
        summary = (f"{TASK_LABELS[state]}，等待通知 Codex" if terminal
                   else "等待执行" if state == "queued" else "后台执行中")
        if terminal and delivery == "accepted":
            summary = "Codex 已接收结果，等待处理完成"
        elif terminal and delivery in {"failed", "rejected", "unknown"}:
            status = "failed"
            summary = ("Codex 处理结果未确认，任务结果已保留" if delivery == "unknown"
                       else "Codex 未能处理通知，任务结果已保留")
        callback_turn = None
        if terminal and delivery in {"accepted", "failed", "unknown"} and row.get("delivery_turn_id"):
            try:
                callback_turn = thread_id(row["delivery_turn_id"])
            except ValueError:
                pass  # Legacy unverified IDs still use the worker's reconciliation.
        result.setdefault(sid, []).append(BackgroundProcessItem(
            item_id=f"{TASK_ITEM_PREFIX}{key}", kind="task",
            title=row["title"], status=status, summary=summary, turn_id=callback_turn,
            started_at=row["created"], updated_at=row["updated"],
        ))
    return result


def _sanitized_command(argv: list[str]) -> str:
    parts = []
    redact_next = False
    for arg in argv:
        if redact_next:
            parts.append("***")
            redact_next = False
            continue
        option, equals, _value = arg.partition("=")
        if _TASK_SECRET_FLAG.fullmatch(option):
            parts.append(f"{option}=***" if equals else option)
            redact_next = not equals
            continue
        arg = _TASK_SECRET_HEADER.sub(_mask_task_secret_header, arg)
        arg = _INLINE_SECRET_OPTION.sub(r"\1***", arg)
        # The store bounds the entire argv, so redact before clipping the preview.
        parts.append(redact_text(arg, limit=MAX_TASK_ARGV_CHARS))
    return shlex.join(parts)


def _mask_task_secret_header(match: re.Match[str]) -> str:
    quote = match["quote"] or ""
    name = match["quoted_name"] or match["name"]
    return f"{quote}{name}: ***{quote}"


def read_task_parameters(home: Path, sid: str, item_id: str) -> ProcessEvent | None:
    """One owner-scoped detail page, separate from replayed activity or output."""
    spec = TaskStore(home).read_parameters(item_id.removeprefix(TASK_ITEM_PREFIX), sid)
    if spec is None:
        return None
    command, shortened = bounded_text(
        _sanitized_command(spec["argv"]), MAX_BACKGROUND_PROCESS_COMMAND_CHARS,
    )
    cwd, _ = bounded_text(redact_text(spec["cwd"]), MAX_BACKGROUND_PROCESS_CWD_CHARS)
    return ProcessEvent(
        item_id=item_id, kind="task", phase="snapshot", title=spec["title"],
        command=command, cwd=cwd, input={"timeout_seconds": spec["timeout_seconds"]},
        truncated=shortened or None,
    )


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
