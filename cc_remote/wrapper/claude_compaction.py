"""Normalize native Claude compact metadata from live and persisted events."""
from __future__ import annotations

from typing import Any

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER


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
