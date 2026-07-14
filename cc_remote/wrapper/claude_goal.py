"""Claude Code ``/goal`` state recovered from its authoritative transcript.

Claude Code 2.1.205 implements goals as a session-scoped Stop hook.  Unlike
Codex's app-server, it has no goal control RPC.  The CLI persists every state
transition as a ``goal_status`` transcript attachment, which makes the
transcript the only stable source of truth across wrapper restarts.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from claude_agent_sdk.types import AssistantMessage, SystemMessage, UserMessage

from cc_remote.wrapper.sanitize import bounded_text
from cc_remote.wrapper.stream import _bounded_jsonl_lines, transcript_path

_MAX_GOAL_RECORDS = 200_000
_GOAL_TEXT_MAX = 16 * 1024
NO_GOAL_EVENT = object()


def _epoch_seconds(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        # Claude's in-memory activeGoal uses Date.now() milliseconds.  The
        # transcript uses ISO timestamps; normalize both to protocol seconds.
        return seconds / 1000.0 if seconds > 100_000_000_000 else seconds
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00") if value.endswith("Z") else value
            ).timestamp()
        except (TypeError, ValueError):
            pass
    return default


def _safe_text(value: Any) -> str:
    text, _ = bounded_text(value, _GOAL_TEXT_MAX)
    return text


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += max(0, int(value))
    return total


def _new_goal(
    thread_id: str,
    objective: str,
    *,
    set_at: float,
    tokens_at_start: int | None = None,
) -> dict[str, Any]:
    goal: dict[str, Any] = {
        "threadId": thread_id,
        "objective": objective,
        "status": "active",
        "engine": "claude",
        "tokenBudget": None,
        "tokensUsed": 0,
        "timeUsedSeconds": 0,
        "createdAt": set_at,
        "updatedAt": set_at,
        "iterations": 0,
        "setAt": set_at,
    }
    if tokens_at_start is not None:
        goal["tokensAtStart"] = max(0, int(tokens_at_start))
    return goal


def make_claude_goal(
    thread_id: str,
    objective: str,
    *,
    tokens_at_start: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Create the immediate state that native ``/goal <condition>`` installs."""
    objective = _safe_text(objective).strip()
    if not objective:
        raise ValueError("Claude goal objective cannot be empty")
    return _new_goal(
        thread_id, objective, set_at=time.time() if now is None else now,
        tokens_at_start=tokens_at_start,
    )


def current_goal(goal: dict[str, Any] | None, now: float | None = None):
    """Return a copy with live elapsed time, without mutating cached state."""
    if goal is None:
        return None
    out = dict(goal)
    current = time.time() if now is None else now
    set_at = _epoch_seconds(out.get("setAt"), current)
    out["timeUsedSeconds"] = max(0, int(current - set_at))
    return out


def read_claude_goal(
    session_id: str, *, now: float | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Return ``(transcript_exists, active_goal)`` for ``session_id``.

    A false ``goal_status`` sentinel starts/replaces the goal, each later false
    status is one evaluator iteration, and any true status clears it (natural
    completion and ``/goal clear`` use the same persisted transition).
    """
    path = transcript_path(session_id)
    if not path:
        return False, None

    goal: dict[str, Any] | None = None
    message_tokens: dict[str, int] = {}
    records = 0
    try:
        with open(path, encoding="utf-8") as transcript:
            for line in _bounded_jsonl_lines(transcript):
                records += 1
                if records > _MAX_GOAL_RECORDS:
                    break
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue

                attachment = record.get("attachment")
                if (record.get("type") == "attachment"
                        and isinstance(attachment, dict)
                        and attachment.get("type") == "goal_status"):
                    met = attachment.get("met") is True
                    condition = _safe_text(attachment.get("condition")).strip()
                    stamp = _epoch_seconds(
                        record.get("timestamp"), time.time() if now is None else now)
                    if met:
                        goal = None
                        message_tokens.clear()
                        continue
                    if attachment.get("sentinel") is True or goal is None:
                        if condition:
                            goal = _new_goal(
                                session_id, condition, set_at=stamp)
                            message_tokens.clear()
                        continue
                    if condition and condition != goal.get("objective"):
                        # Be defensive if a future CLI omits the replacement
                        # sentinel: a changed condition still starts a new goal.
                        goal = _new_goal(session_id, condition, set_at=stamp)
                        message_tokens.clear()
                        continue
                    goal["iterations"] = int(goal.get("iterations", 0)) + 1
                    reason = _safe_text(attachment.get("reason")).strip()
                    if reason:
                        goal["lastReason"] = reason
                    goal["updatedAt"] = stamp
                    continue

                if goal is None or record.get("type") != "assistant":
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    continue
                message_tokens[message_id] = max(
                    message_tokens.get(message_id, 0),
                    _usage_tokens(message.get("usage")),
                )
    except (OSError, UnicodeError):
        # Preserve an in-memory state when a concurrent transcript replace or
        # transient I/O failure prevents an authoritative refresh.
        return False, None

    if goal is not None:
        goal["tokensUsed"] = sum(message_tokens.values())
    return True, current_goal(goal, now)


def active_goal_from_message(
    msg: Any, thread_id: str, *, now: float | None = None,
):
    """Map a raw ``active_goal`` QueryEvent wrapped as ``SystemMessage``.

    Python SDK 0.2.110 through 0.2.116 drops this top-level message.  The
    wrapper's compatibility reader preserves it as a SystemMessage so a future
    CLI that emits it can update the UI without waiting for transcript refresh.
    ``NO_GOAL_EVENT`` distinguishes an unrelated message from ``value=null``.
    """
    if not isinstance(msg, SystemMessage) or msg.subtype != "active_goal":
        return NO_GOAL_EVENT
    data = msg.data if isinstance(msg.data, dict) else {}
    value = data.get("value")
    if value is None:
        return None
    if not isinstance(value, dict):
        return NO_GOAL_EVENT
    condition = _safe_text(value.get("condition")).strip()
    if not condition:
        return NO_GOAL_EVENT
    current = time.time() if now is None else now
    set_at = _epoch_seconds(value.get("set_at"), current)
    tokens_at_start = value.get("tokens_at_start")
    if not isinstance(tokens_at_start, int) or isinstance(tokens_at_start, bool):
        tokens_at_start = None
    goal = _new_goal(
        thread_id, condition, set_at=set_at,
        tokens_at_start=tokens_at_start,
    )
    iterations = value.get("iterations")
    if isinstance(iterations, int) and not isinstance(iterations, bool):
        goal["iterations"] = max(0, iterations)
    reason = _safe_text(value.get("last_reason")).strip()
    if reason:
        goal["lastReason"] = reason
    goal["updatedAt"] = current
    return current_goal(goal, now)


def goal_message_update(
    msg: Any,
    goal: dict[str, Any] | None,
    token_totals: dict[str, int],
    *,
    now: float | None = None,
) -> bool:
    """Apply live evaluator feedback/token usage to a cached active goal."""
    if goal is None:
        return False
    current = time.time() if now is None else now
    if isinstance(msg, AssistantMessage):
        message_id = msg.message_id
        if not message_id:
            return False
        total = _usage_tokens(msg.usage)
        previous = token_totals.get(message_id, 0)
        if total <= previous:
            return False
        token_totals[message_id] = total
        goal["tokensUsed"] = int(goal.get("tokensUsed", 0)) + total - previous
        goal["updatedAt"] = current
        return True
    if not isinstance(msg, UserMessage) or not isinstance(msg.content, str):
        return False
    prefix = f"Stop hook feedback:\n[{goal.get('objective', '')}]:"
    if not msg.content.startswith(prefix):
        return False
    reason = _safe_text(msg.content[len(prefix):]).strip()
    goal["iterations"] = int(goal.get("iterations", 0)) + 1
    if reason:
        goal["lastReason"] = reason
    goal["updatedAt"] = current
    return True
