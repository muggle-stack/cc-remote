"""Normalize native Claude compact metadata from live and persisted events."""
from __future__ import annotations

from typing import Any
import hashlib
import re

from cc_remote.protocol import (
    MAX_SAFE_WIRE_INTEGER, AssistantMsgEnd, AssistantMsgStart, Delta,
)


def manual_compact_prompt(content: object) -> str | None:
    """Recognize only the native /compact command, never other slash commands."""
    if isinstance(content, list):
        if any((block.get("type") != "text" if isinstance(block, dict)
                else not isinstance(getattr(block, "text", None), str)) for block in content):
            return None
        texts = (
            block.get("text", "") if isinstance(block, dict)
            else getattr(block, "text", "")
            for block in content
        )
        content = "".join(text for text in texts if isinstance(text, str))
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text == "/compact":
        return text
    if not text.startswith(("<command-name>", "<command-message>")):
        return None
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if name is None or name[1].strip() != "/compact":
        return None
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    return "/compact" + (" " + args[1].strip() if args and args[1].strip() else "")


def compact_completion_events(boundary_id: str, turn_id: str | None) -> list:
    """A stable UI receipt for a proven manual compact, shared by live/history."""
    message_id = "compact-result-" + hashlib.sha256(boundary_id.encode()).hexdigest()[:24]
    return [
        AssistantMsgStart(message_id=message_id, turn_id=turn_id, channel="final"),
        Delta(message_id=message_id, turn_id=turn_id, channel="final",
              text="上下文已压缩，可以继续当前会话。"),
        AssistantMsgEnd(message_id=message_id, turn_id=turn_id, channel="final"),
    ]


def compact_metadata(row: object) -> dict[str, Any]:
    """Use the SDK's snake case while accepting the transcript's camel case."""
    if not isinstance(row, dict):
        return {}
    metadata = row.get("compact_metadata", row.get("compactMetadata"))
    if not isinstance(metadata, dict):
        return {}
    result: dict[str, Any] = {}
    trigger = metadata.get("trigger")
    if isinstance(trigger, str) and trigger in {"auto", "manual"}:
        result["trigger"] = trigger
    for name, alias in (
        ("pre_tokens", "preTokens"),
        ("post_tokens", "postTokens"),
        ("duration_ms", "durationMs"),
    ):
        value = metadata.get(name, metadata.get(alias))
        if (isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= MAX_SAFE_WIRE_INTEGER):
            result[name] = value
    return result


def compact_context_usage(row: object) -> dict[str, int] | None:
    """Read a main-session boundary without counting the summarizer's input."""
    if (not isinstance(row, dict)
            or row.get("type") != "system"
            or row.get("subtype") != "compact_boundary"
            or row.get("isSidechain") is True
            or row.get("parentToolUseID") is not None
            or row.get("parent_tool_use_id") is not None):
        return None
    post_tokens = compact_metadata(row).get("post_tokens")
    return {"totalTokens": post_tokens} if post_tokens is not None else None
