"""Measured turn/step statistics from the pinned native event contract.

Like DSH's stats view, input includes cache reads/writes and concurrent tool
spans are unioned. Missing boundaries stay unknown; reads never activate agents.
"""
from __future__ import annotations

import math

from cc_remote.protocol import DshItem


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def elapsed(start, end):
    return max(0, end - start) if start is not None and end is not None else None


def total(values):
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def token_chunk(chunk):
    return ((chunk.get("type") in {"text-delta", "reasoning-delta"} and bool(chunk.get("text")))
            or (chunk.get("type") == "tool-call-delta" and (bool(chunk.get("argumentsDelta")) or "name" in chunk)))


def first_token(stream):
    for record in stream if isinstance(stream, list) else []:
        if record.get("type") == "chunk":
            if token_chunk(record.get("chunk", {})):
                return number(record.get("time"))
            continue
        time = number(record.get("time0"))
        if time is None:
            continue
        if record.get("type") == "tool-call-chunks" and "name" in record:
            return time
        for index, fragment in enumerate(record.get("args", record.get("texts", []))):
            if index:
                time += number((record.get("dt") or [])[index - 1]) or 0
            if fragment:
                return time
    return None


def union(spans):
    spans = sorted((start, end) for start, end in spans if start is not None and end is not None and end >= start)
    if not spans:
        return None
    result, left, right = 0, *spans[0]
    for start, end in spans[1:]:
        if start > right:
            result += right - left
            left = start
        right = max(right, end)
    return result + right - left


def project(events):
    turns, calls = {}, {}
    current = None
    for event in events:
        data, kind = event.get("data", {}), event["type"]
        if isinstance(event.get("surfaceOp"), dict):
            continue
        turn = data.get("turn", current)
        if type(turn) is not int or turn < 1:
            continue
        current = turn
        row = turns.setdefault(turn, {"turn": turn, "start": None, "end": None, "steps": {}, "state": "unknown"})
        time = number(event.get("time"))
        if kind == "turn/start":
            row.update(start=time, state="running")
        if kind == "turn/end":
            row.update(end=time, state=data.get("reason", {}).get("kind", "completed"))
        step_id = data.get("step")
        if type(step_id) is not int or step_id < 0:
            # Some result formats locate their originating step by call identity.
            message = data.get("message", {})
            source = message.get("source", {})
            call = calls.get(message.get("toolCallId") or source.get("callId") or data.get("subCallId")) or calls.get(data.get("rootCallId"))
            if call:
                turn, step_id = call["turn"], call["step"]
                row = turns[turn]
            else:
                continue
        step = row["steps"].setdefault(step_id, {"step": step_id, "start": None, "end": None,
            "message": None, "first": None, "usage": {}, "calls": {}, "retries": 0})
        if kind == "step/start":
            step["start"] = time
        elif kind == "step/end":
            step["end"] = time
        elif kind in {"assistant/attempt", "assistant/message"}:
            first = first_token(data.get("stream"))
            if step["first"] is None and first is not None:
                step["first"] = first
            if kind == "assistant/message":
                step["message"] = step["message"] if step["message"] is not None else time
                step["usage"] = data.get("usage") or step["usage"]
        elif kind == "assistant/live-chunk":
            chunk = data.get("chunk", {})
            if chunk.get("type") == "usage":
                step["usage"] = chunk.get("usage") or {}
            elif token_chunk(chunk) and step["first"] is None:
                step["first"] = time
        elif kind == "llm/retry":
            step["retries"] += 1
        elif kind in {"tool/call", "tool/ptc-dispatch-start"}:
            key = data.get("subCallId") if kind.endswith("dispatch-start") else data.get("callId")
            if isinstance(key, str):
                call = calls.setdefault(key, {"id": key, "turn": turn, "step": step_id,
                    "name": str(data.get("name") or "工具")[:128], "start": time, "end": None,
                    "inner": kind.endswith("dispatch-start"), "error": False})
                step["calls"][key] = call
        elif kind in {"tool/result", "tool/ptc-dispatch"}:
            message = data.get("message", {})
            key = data.get("subCallId") if kind.endswith("dispatch") else message.get("toolCallId") or message.get("source", {}).get("callId")
            call = calls.get(key)
            if call:
                call.update(end=time, error=bool(data.get("isError") or message.get("isError")))
                if data.get("name"):
                    call["name"] = str(data["name"])[:128]
    result = []
    for turn, row in sorted(turns.items()):
        steps = []
        tools = []
        for _, step in sorted(row["steps"].items()):
            usage = step["usage"]
            context = total(number(usage.get(key)) for key in ("inputTokens", "cacheReadTokens", "cacheWriteTokens"))
            cache = number(usage.get("cacheReadTokens"))
            entries = list(step["calls"].values())
            tools.extend({"name": call["name"], "step": call["step"], "duration": elapsed(call["start"], call["end"]),
                          "inner": call["inner"], "error": call["error"]} for call in entries)
            steps.append({"step": step["step"], "start": step["start"], "end": step["end"],
                "wall": elapsed(step["start"], step["end"]), "model": elapsed(step["start"], step["message"]),
                "ttft": elapsed(step["start"], step["first"]), "input": context,
                "output": number(usage.get("outputTokens")), "cacheRead": cache,
                "tool": union((call["start"], call["end"]) for call in entries),
                "pendingTools": sum(call["end"] is None for call in entries), "retries": step["retries"]})
        context, cached = total(s["input"] for s in steps), total(s["cacheRead"] for s in steps)
        result.append(DshItem(id=str(turn), title=f"第 {turn} 轮", state=row["state"], data={
            "turn": turn, "wall": elapsed(row["start"], row["end"]), "model": total(s["model"] for s in steps),
            "tool": union((call["start"], call["end"]) for call in calls.values() if call["turn"] == turn),
            "input": context, "output": total(s["output"] for s in steps),
            "cacheHit": cached / context if cached is not None and context else None,
            "partial": any(s["model"] is None or s["input"] is None for s in steps),
            "steps": steps[:128], "tools": tools[:256], "truncated": len(steps) > 128 or len(tools) > 256,
            "pendingTools": sum(s["pendingTools"] for s in steps)}))
    return result


async def read(client, cmd, response):
    snapshot = await client.history_snapshot(cmd.sid, before_seq=cmd.before_seq, max_messages=40)
    records = snapshot["records"]
    response.has_more = bool(snapshot.get("hasMore") and records)
    response.next_seq = records[0]["event"]["seq"] if response.has_more else None
    return project([record["event"] for record in records])[-64:]
