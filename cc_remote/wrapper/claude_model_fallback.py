"""Translate only Claude's authoritative model-fallback system envelope."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from cc_remote.protocol import ProcessEvent


_MODEL = re.compile(r"claude-[a-zA-Z0-9._-]{1,100}(?:\[1m\])?\Z")
FALLBACK_TOOL = "model_refusal_fallback"


def model_fallback_event(data: Any, *, turn_id: str | None = None) -> ProcessEvent | None:
    if not isinstance(data, dict) or data.get("subtype") != FALLBACK_TOOL:
        return None
    if (data.get("parent_tool_use_id") or data.get("parentToolUseId")
            or data.get("parentToolUseID") or data.get("isSidechain") is True):
        return None
    original = data.get("originalModel")
    fallback = data.get("fallbackModel")
    if not all(isinstance(model, str) and _MODEL.fullmatch(model)
               for model in (original, fallback)) or original == fallback:
        return None
    # Do not relay arbitrary upstream content, refusal explanations, or URLs.
    # The structured event proves the switch; the displayed reason is bounded.
    reason = ("原模型拒绝响应，Claude 自动回退"
              if data.get("trigger") == "refusal" else "Claude 切换了备用模型")
    # UUID/request identity survives SDK projection and native history reloads.
    # Never retain arbitrary provider payloads (including credentials) here.
    identity = next((value for key in ("uuid", "requestId")
                     if isinstance(value := data.get(key), str)
                     and 0 < len(value) <= 128), None)
    identity = identity or f"{turn_id}:{original}:{fallback}"
    item_id = "model-fallback:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
    return ProcessEvent(
        item_id=item_id, kind="model", phase="snapshot", status="succeeded",
        turn_id=turn_id, title="模型已回退", tool=FALLBACK_TOOL,
        summary=f"{original} → {fallback}。{reason}。",
        input={"original_model": original, "fallback_model": fallback,
               "scope": "session" if data.get("scope") == "session" else "turn"},
    )
