"""Translate only Claude's authoritative model-fallback system envelope."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from cc_remote.protocol import ProcessEvent


_MODEL = re.compile(r"claude-[a-zA-Z0-9._-]{1,100}(?:\[1m\])?\Z")
FALLBACK_TOOL = "model_refusal_fallback"


def model_fallback_timestamp(data: dict) -> str | None:
    value = data.get("timestamp")
    if isinstance(value, str) and len(value) <= 64:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return None


def model_fallback_event(data: Any, *, turn_id: str | None = None) -> ProcessEvent | None:
    if not isinstance(data, dict) or data.get("subtype") != FALLBACK_TOOL:
        return None
    if (data.get("parent_tool_use_id") or data.get("parentToolUseId")
            or data.get("parentToolUseID") or data.get("isSidechain") is True):
        return None
    # Live stream-json uses snake_case; the native transcript serializer uses
    # camelCase. The SDK passes the live envelope through unchanged.
    original = data.get("original_model", data.get("originalModel"))
    fallback = data.get("fallback_model", data.get("fallbackModel"))
    if (data.get("originalModel", original) != original
            or data.get("fallbackModel", fallback) != fallback):
        return None
    if not all(isinstance(model, str) and _MODEL.fullmatch(model)
               for model in (original, fallback)) or original == fallback:
        return None
    # Do not relay arbitrary upstream content, refusal explanations, or URLs.
    # The structured event proves the switch; the displayed reason is bounded.
    reason = ("原模型拒绝响应，已切换备用模型"
              if data.get("trigger") == "refusal" else "Claude 切换了备用模型")
    # UUID/request identity survives SDK projection and native history reloads.
    # Never retain arbitrary provider payloads (including credentials) here.
    identity = next((value for key in ("uuid", "request_id", "requestId")
                     if isinstance(value := data.get(key), str)
                     and 0 < len(value) <= 128), None)
    identity = identity or f"{turn_id}:{original}:{fallback}"
    item_id = "model-fallback:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    return ProcessEvent(
        item_id=item_id, kind="model", phase="snapshot", status="succeeded",
        turn_id=turn_id, title="模型已回退", tool=FALLBACK_TOOL,
        summary=f"{original} → {fallback}。{reason}。",
        input={"original_model": original, "fallback_model": fallback,
               # Older native CLIs omitted scope for main-session fallback.
               "scope": "session" if data.get("scope", "session") == "session" else "turn"},
    )
