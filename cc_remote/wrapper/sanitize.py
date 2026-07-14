"""Bound model-originated UI payloads before they enter rings or WebSockets."""
from __future__ import annotations

import json
from itertools import islice
from typing import Any

_PRIORITY_KEYS = (
    "file_path", "path", "command", "cmd", "cwd", "workdir", "description",
    "query", "pattern", "name", "tool", "changes", "patch", "content",
)
_MAX_STRUCT_DEPTH = 4


def _json_bytes(value: Any) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, default=str, separators=(",", ":"),
    ).encode("utf-8"))


def _shallow(
    value: Any,
    string_cap: int = 8192,
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= string_cap:
            return value, False
        return value[:string_cap] + "…", True
    if isinstance(value, (dict, list, tuple)) and depth >= _MAX_STRUCT_DEPTH:
        return f"<{type(value).__name__} omitted>", True
    if isinstance(value, dict):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>", True
        ancestors.add(identity)
        result: dict[str, Any] = {}
        truncated = len(value) > 64
        try:
            for key, item in islice(value.items(), 64):
                key_text = key if isinstance(key, str) else f"<{type(key).__name__}>"
                normalized_key = key_text[:128]
                normalized_item, item_truncated = _shallow(
                    item, string_cap, depth + 1, ancestors)
                result[normalized_key] = normalized_item
                truncated = (truncated or item_truncated
                             or not isinstance(key, str) or len(key_text) > 128)
            return result, truncated
        finally:
            ancestors.discard(identity)
    if isinstance(value, (list, tuple)):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>", True
        ancestors.add(identity)
        result = []
        truncated = len(value) > 32
        try:
            for item in islice(value, 32):
                normalized_item, item_truncated = _shallow(
                    item, string_cap, depth + 1, ancestors)
                result.append(normalized_item)
                truncated = truncated or item_truncated
            return result, truncated
        finally:
            ancestors.discard(identity)
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    return f"<{type(value).__name__}>", True


def bounded_tool_input(value: Any, max_bytes: int) -> dict:
    """Return a useful dict whose serialized representation is safely bounded."""
    budget = max(1024, max_bytes)
    normalized, was_truncated = _shallow(value)
    if not isinstance(normalized, dict):
        normalized = {"args": normalized}
    if not was_truncated and _json_bytes(normalized) <= budget:
        return normalized

    summary: dict[str, Any] = {"_truncated": True}
    original = value if isinstance(value, dict) else {"args": value}
    for key in _PRIORITY_KEYS:
        if key not in original:
            continue
        summary[key] = _shallow(original[key], min(4096, budget // 4))[0]
        if _json_bytes(summary) > budget:
            summary.pop(key, None)
            break
    if len(summary) == 1:
        summary["summary"] = "tool input omitted: payload exceeded safety limit"
    # Defensive final squeeze for unusually long keys/metadata.
    while _json_bytes(summary) > budget and len(summary) > 1:
        summary.pop(next(reversed(summary)))
    return summary


def bounded_text(value: Any, max_chars: int) -> tuple[str, bool]:
    """Render model/tool output without first materializing an unbounded repr."""
    budget = max(1, max_chars)
    parts: list[str] = []
    used = 0
    truncated = False
    ancestors: set[int] = set()

    def append(text: str) -> None:
        nonlocal used, truncated
        remaining = budget - used
        if remaining <= 0:
            truncated = True
            return
        if len(text) > remaining:
            parts.append(text[:remaining])
            used += remaining
            truncated = True
        else:
            parts.append(text)
            used += len(text)

    def walk(item: Any, depth: int = 0) -> None:
        nonlocal truncated
        if used >= budget:
            truncated = True
            return
        if isinstance(item, str):
            append(item)
        elif item is None or isinstance(item, (bool, int, float)):
            append(str(item) if item is not None else "")
        elif isinstance(item, (list, tuple, dict)):
            if depth >= _MAX_STRUCT_DEPTH:
                append(f"<{type(item).__name__} omitted>")
                truncated = True
                return
            identity = id(item)
            if identity in ancestors:
                append("<cycle omitted>")
                truncated = True
                return
            ancestors.add(identity)
            try:
                if isinstance(item, (list, tuple)):
                    for index, child in enumerate(islice(item, 64)):
                        if index:
                            append("\n")
                        walk(child, depth + 1)
                    if len(item) > 64:
                        truncated = True
                else:
                    append("{")
                    for index, (key, child) in enumerate(islice(item.items(), 64)):
                        if index:
                            append(", ")
                        # Parsed JSON object keys are strings. Treat any SDK-specific
                        # object key as opaque instead of invoking arbitrary __str__.
                        key_text = (key if isinstance(key, str)
                                    else f"<{type(key).__name__}>")
                        append(key_text[:128] + ": ")
                        walk(child, depth + 1)
                    if len(item) > 64:
                        truncated = True
                    append("}")
            finally:
                ancestors.discard(identity)
        else:
            # Avoid invoking an arbitrary object's potentially enormous __str__.
            append(f"<{type(item).__name__}>")

    walk(value)
    return "".join(parts), truncated
