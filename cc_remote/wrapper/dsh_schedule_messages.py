"""Visible inputs from the pinned native scheduler's durable delivery records."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from cc_remote.protocol import TimedMessage


def schedule_message(data: dict) -> tuple[str, TimedMessage | None] | None:
    # Model/user prose resembling a reminder is not evidence of a delivery.
    if data.get("source", {}).get("kind") != "schedule":
        return None
    text = "\n".join(block["text"] for block in data.get("content", [])
                     if isinstance(block, dict) and block.get("type") == "text"
                     and isinstance(block.get("text"), str))
    lines = text.split("\n")
    if not lines or lines[0] not in {"[SCHEDULE REMINDER]", "[SCHEDULE REMINDER BATCH]"}:
        # SCHEDULE CHANGE notices are configuration bookkeeping, not queries.
        return None
    try:
        if lines[1] != "This is a scheduled message from the user":
            raise ValueError
        if lines[0] == "[SCHEDULE REMINDER]":
            if len(lines) != 5:
                raise ValueError
            fields = dict(line.split(": ", 1) for line in lines[2:])
            reminders = [{"schedule_id": json.loads(fields["schedule_id_json"]),
                          "occurrence_at": fields["occurrence_at"],
                          "reminder_prompt": json.loads(fields["reminder_prompt_json"])}]
        else:
            if len(lines) != 3 or not lines[2].startswith("reminders_json: "):
                raise ValueError
            reminders = json.loads(lines[2][len("reminders_json: "):])
        if not isinstance(reminders, list) or not 1 <= len(reminders) <= 1000:
            raise ValueError
        deliveries = []
        for item in reminders:
            if not isinstance(item, dict) or not isinstance(item.get("reminder_prompt"), str):
                raise ValueError
            when = datetime.fromisoformat(item["occurrence_at"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError
            receipt = TimedMessage(task_id=item["schedule_id"], title="自动化任务",
                                   scheduled_at=when.timestamp())
            deliveries.append((item["reminder_prompt"], receipt))
        if len(deliveries) == 1:
            return deliveries[0]
        # One native batch is one model input/answer, not several human turns.
        ids = json.dumps([receipt.task_id for _, receipt in deliveries]).encode()
        receipt = TimedMessage(task_id="dsh-schedule-" + hashlib.sha256(ids).hexdigest()[:32],
            title=f"批量自动化任务（{len(deliveries)} 项，最早计划时间）",
            scheduled_at=min(receipt.scheduled_at for _, receipt in deliveries))
        return "\n\n".join(f"{i}. {prompt}" for i, (prompt, _) in enumerate(deliveries, 1)), receipt
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
        # Preserve a delivered input if a future native framing cannot be decoded;
        # never hide its content or fabricate an execution time from local clocks.
        return text, None
