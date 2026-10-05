"""Codex App cross-thread input envelopes (not sub-agent collaboration).

Only decode the native envelope. A source id is display/navigation metadata,
never authority to change accounts, machines or execute an action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NATIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENVELOPE = re.compile(
    r"<codex_delegation>\s*<source_thread_id>([^<>]+)</source_thread_id>"
    r"\s*<input>([^<>]*)</input>\s*</codex_delegation>",
    re.DOTALL,
)
_TOOLS = frozenset({"create_thread", "send_message_to_thread", "handoff_thread"})


@dataclass(frozen=True)
class CodexDelegation:
    source_thread_id: str
    prompt: str


def parse_codex_delegation(text: object) -> CodexDelegation | None:
    if not isinstance(text, str) or len(text) > 1024 * 1024:
        return None
    match = _ENVELOPE.fullmatch(text.strip())
    if match is None or not _NATIVE_ID.fullmatch(match[1].strip()):
        return None
    # Match the official App's escaping exactly; do not parse XML/entities.
    prompt = match[2].strip().replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    if not prompt:
        return None
    return CodexDelegation(match[1].strip(), prompt)


def is_codex_delegation_output(item: object) -> bool:
    return (isinstance(item, dict)
            and str(item.get("type") or "").lower() == "functioncalloutput"
            and item.get("namespace") == "codex_app"
            and isinstance(item.get("name"), str)
            and item.get("name") in _TOOLS)


def normalize_codex_delegation_item(item: dict) -> dict:
    """Project native turnToolOutput as the same user item used by the App.

    Other function outputs must stay tools. Keep the exact item id for live /
    persisted-history reconciliation; never infer identity from equal text.
    """
    if (not is_codex_delegation_output(item)
            or parse_codex_delegation(item.get("output")) is None):
        return item
    return {"type": "userMessage", "id": item.get("id"), "clientId": None,
            "content": [{"type": "text", "text": item["output"]}]}


def codex_message_target(tool: object, arguments: object, server: object = None) -> str | None:
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return None
    native_tool = tool in {"send_message_to_thread", "codex_app.send_message_to_thread"}
    native_server = server == "codex_app" or arguments.get("namespace") == "codex_app"
    if not ((native_tool and native_server) or tool == "mcp__codex_app__send_message_to_thread"):
        return None
    target = arguments.get("threadId")
    return target if isinstance(target, str) and _NATIVE_ID.fullmatch(target) else None
