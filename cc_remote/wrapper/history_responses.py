"""Payload-free response boundaries for lazy Claude history disclosures."""
from __future__ import annotations

from typing import Any


def response_process_summaries(
    events: list[dict[str, Any]], turn_id: str, answer_ids: set[str],
) -> list[dict[str, Any]] | None:
    # Match the native block order used by assistantContinuations. Updates to
    # an existing tool/child never create a new response, even if they arrive
    # after its answer. Keep only identities, clocks and presentation metadata.
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        kind = event.get("type")
        if kind in {"assistant_msg_start", "delta", "assistant_msg_end"}:
            category, identity = "text", event.get("message_id")
        elif kind in {"tool_use", "tool_result"}:
            category, identity = "tool", event.get("tool_use_id")
            if kind == "tool_result" and (category, identity) not in items:
                continue
        elif kind == "process":
            category, identity = "process", event.get("item_id")
            if (event.get("server") == "cc_remote"
                    and event.get("tool") in {"agent_return", "task_return"}
                    and str(identity).startswith(("return-", "dsh-return-"))):
                continue
            if event.get("tool") == "model_refusal_fallback":
                continue
        else:
            continue
        if not isinstance(identity, str):
            continue
        item = items.setdefault((category, identity), {
            "id": identity, "category": category, "has_text": False,
        })
        for key in ("channel", "delivery", "parent_id", "tool"):
            if event.get(key) is not None:
                item[key] = event[key]
        if kind == "process":
            item["process_kind"] = event.get("kind")
        if event.get("background") is True:
            item["background"] = True
        if kind == "delta" and isinstance(event.get("text"), str):
            item["has_text"] = bool(event["text"]) or (
                not event.get("replace") and item["has_text"])
        stamp = event.get("ts")
        if isinstance(stamp, (int, float)) and stamp >= 0:
            stamp = round(stamp * 1000)
            item.setdefault("startedTs", stamp)
            if kind in {"assistant_msg_end", "tool_result"} or (
                kind == "process" and event.get("phase") == "end"
            ):
                item["doneTs"] = stamp

    dedicated_agents = {i.get("parent_id") for i in items.values()
                        if i.get("process_kind") == "agent"}
    original: dict[str, Any] = {"id": turn_id, "background": False, "itemCount": 0}
    responses = [original]
    current = None
    answered = False
    for item in items.values():
        category = item["category"]
        is_answer = (category == "text" and item["has_text"]
                     and item.get("channel") in {None, "unknown", "final"})
        process_kind = item.get("process_kind")
        child = category == "process" and process_kind in {"agent", "task"}
        bookkeeping = category == "process" and process_kind not in {
            "command", "file_change", "mcp", "web_search", "server_tool", "reasoning",
        }
        if (not item.get("background") or child
                or (bookkeeping and (current is None or answered))
                or item.get("delivery") == "async"):
            target = original
        else:
            if current is None or answered:
                current = {"id": item["id"], "background": True, "itemCount": 0}
                responses.append(current)
                answered = False
            target = current
        if "startedTs" in item:
            target.setdefault("startedTs", item["startedTs"])
        # Child bookkeeping can finish long after the main response. Its
        # completion must not extend that response's clock into waiting time.
        if not child and "doneTs" in item and "answerId" not in target:
            target["doneTs"] = max(target.get("doneTs", 0), item["doneTs"])
        if is_answer and item.get("delivery") != "async":
            target.setdefault("answerId", item["id"])
            if target is current:
                answered = True
        elif (item.get("delivery") != "async"
              and (category != "text" or (item["has_text"]
                  and item.get("channel") in {"thinking", "commentary"}))
              and not (category == "tool" and item["id"] in dedicated_agents)):
            target["itemCount"] += 1
    if len(responses) == 1:
        return None
    # Only closed response boundaries are authoritative. The live tail keeps
    # its own process state. At most one envelope per retained final answer,
    # plus the original response; never ship heavy tool/reasoning payloads.
    return [r for r in responses if r is original or r.get("answerId") in answer_ids]
