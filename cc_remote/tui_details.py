"""Readable, bounded descriptions of control state, not protocol dumps."""

import re
from datetime import datetime

from cc_remote.tui_presentation import bounded, tokens


LABELS = {
    "cwd": "Working directory",
    "perm": "Approval policy",
    "permission_profile": "Filesystem permissions",
    "codex_context": "Context limits",
    "max_context_tokens": "Context limit",
    "model_context_window": "Model context window",
    "model_auto_compact_token_limit": "Automatic compaction threshold",
    "used_percent": "Consumed",
    "percentage": "Context used",
    "resets_at": "Resets at",
    "window_duration_mins": "Window duration",
    "primary": "Primary window",
    "secondary": "Secondary window",
    "effort": "Reasoning effort",
    "fast": "Fast mode",
    "web_search": "Web search",
    "ds": "Description",
    "sid": "Session",
    "cmd_id": "Command ID",
    "request_id": "Request ID",
}


def field_label(name):
    return LABELS.get(
        name,
        re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
        .replace("_", " ")
        .capitalize(),
    )


def scalar(value, key=""):
    if value is None:
        return "Not available / default"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if key in {"percentage", "used_percent"}:
            return f"{value:.0f}%"
        if key == "resets_at":
            try:
                return (
                    datetime.fromtimestamp(value)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S %Z")
                )
            except (ValueError, OSError, OverflowError):
                return "Unknown reset time"
        if key == "window_duration_mins":
            return (
                f"{value / 60:g} hours"
                if value % 60 == 0
                else f"{value} minutes"
            )
        if "token" in key.lower():
            return f"{tokens(value)} tokens ({value:g})"
    return str(value)


def details(value):
    """Keep unknown fields visible without exposing JSON as the default UI."""

    def walk(item, depth=0, key=""):
        indent = "  " * depth
        if isinstance(item, dict):
            for name, child in item.items():
                label = field_label(name)
                if isinstance(child, (dict, list)) and child:
                    yield f"{indent}{label}"
                    yield from walk(child, depth + 1, name)
                else:
                    yield f"{indent}{label}: {scalar(child, name) if child not in ([], {}) else 'None'}"
        elif isinstance(item, list):
            for index, child in enumerate(item, 1):
                if isinstance(child, (dict, list)):
                    yield f"{indent}• Item {index}"
                    yield from walk(child, depth + 1)
                else:
                    yield f"{indent}• {scalar(child, key)}"
        else:
            yield indent + scalar(item, key)

    if value is None or value == {} or value == []:
        return "No data available."
    return "\n".join(walk(bounded(value)))
