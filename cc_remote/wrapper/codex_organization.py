"""Native section/read receipts projected through existing Remote controls."""
from __future__ import annotations

import hashlib
import re
from typing import Any

# Built-in identity from the official 0.167 schema/docs and pristine-server
# threadSection/list. Names are editable; never identify this section by title.
PINNED_SECTION_ID = "01984de2-8f74-7c91-a3b2-5c5e937cf318"
READ_RECEIPT_PREFIX = "codex-read-"


def native_pinned(thread: dict) -> bool | None:
    if "section" not in thread:
        return None  # Older server: keep the local pin projection.
    section = thread["section"]
    if section is None:
        return False
    if isinstance(section, dict) and isinstance(section.get("id"), str):
        return section["id"] == PINNED_SECTION_ID
    return None


def native_read_state(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    revision = value.get("revision")
    if not isinstance(revision, str) or not 0 < len(revision) <= 4096:
        return None
    position = value.get("firstUnread")
    if position is not None:
        if not isinstance(position, dict):
            return None
        if position.get("type") == "turn":
            if not isinstance(position.get("turnId"), str) or not position["turnId"]:
                return None
        elif position.get("type") != "threadStart":
            return None
    return {"revision": revision, "firstUnread": position}


def read_receipt_id(state: dict) -> str:
    # The opaque revision is not a wire ID. Hash it without changing its native
    # meaning; acknowledgement must revalidate this exact identity before RPC.
    receipt = READ_RECEIPT_PREFIX + hashlib.sha256(state["revision"].encode()).hexdigest()
    position = state.get("firstUnread")
    turn = position.get("turnId") if isinstance(position, dict) else None
    if isinstance(turn, str) and re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", turn):
        receipt += ":" + turn
    return receipt
