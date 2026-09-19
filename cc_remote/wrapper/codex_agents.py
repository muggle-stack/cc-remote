"""Read-only, account-local Codex subagent details with proven ancestry."""
from __future__ import annotations

import hashlib
import json
import re

from cc_remote.wrapper.codex_sessions import codex_rollout_path, _read_meta
from cc_remote.wrapper.codex_stream import codex_translate_history, _live_id
from cc_remote.wrapper.history_store import HistorySourceFingerprint

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_MAX_BYTES = 64 * 1024 * 1024
_MAX_RECORD = 16 * 1024 * 1024
_PUBLIC_EVENTS = {"assistant_msg_start", "assistant_msg_end", "delta",
                  "tool_use", "tool_result", "tool_delta", "process"}


def _source(home, sid):
    if not isinstance(sid, str) or not _ID.fullmatch(sid):
        raise ValueError("协作代理标识无效")
    path = codex_rollout_path(sid, codex_home=home)
    if not path:
        raise ValueError("协作代理记录尚未生成，请稍后重试")
    meta = _read_meta(path)
    if not meta or meta.get("id") != sid:
        raise ValueError("协作代理记录身份不匹配")
    return path, meta


def _parent(meta):
    source = meta.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    return spawn.get("parent_thread_id") if isinstance(spawn, dict) else None


def _records(path):
    # Never allocate an unbounded transcript or record for a detail request.
    with open(path, "rb") as stream:
        while True:
            offset = stream.tell()
            if offset > _MAX_BYTES:
                raise ValueError("协作代理记录过大，暂时无法读取完整过程")
            line = stream.readline(_MAX_RECORD + 1)
            if not line:
                return
            if len(line) > _MAX_RECORD:
                raise ValueError("协作代理记录过大，暂时无法读取完整过程")
            if not line.endswith(b"\n"):
                return  # The native writer may still be appending this row.
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                yield offset, row["payload"], row.get("type")


def _legacy_targets(path, run_id):
    for _, payload, kind in _records(path):
        if kind != "event_msg":
            continue
        item = payload.get("item") if payload.get("type") == "item_completed" else payload
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type", "")).replace("_", "").lower()
        if typ == "subagentactivity":
            if _live_id(item.get("id") or item.get("event_id"), "sub-agent") == run_id:
                return [item.get("agentThreadId") or item.get("agent_thread_id")]
        elif typ == "collabagenttoolcall":
            if _live_id(item.get("id"), "collab-agent") == run_id:
                return item.get("receiverThreadIds") or item.get("receiver_thread_ids") or []
    return []


def _detail_events(events, root_sid, ancestors):
    # Correlate before paging: the tool call and its activity may land on
    # different pages. Only this child's translated (bounded) inputs qualify.
    messages = {e.tool_use_id: e.input.get("message") for e in events
                if e.type == "tool_use" and e.tool.rsplit(".", 1)[-1]
                in {"send_message", "send_input", "sendInput"}}
    public = [e.model_dump(exclude_none=True) for e in events if e.type in _PUBLIC_EVENTS]
    for event in public:
        if event["type"] != "process" or event["kind"] != "agent":
            continue
        inputs = event.get("input") or {}
        run_id = inputs.get("agent_run_id")
        if not isinstance(run_id, str) or not run_id.startswith("codex-agent:"):
            continue
        target = run_id.removeprefix("codex-agent:")
        if target not in ancestors:
            continue
        message = messages.get(event["item_id"])
        if not isinstance(message, str) and event.get("tool") == "sendInput":
            message = inputs.get("prompt")
        role = "主代理" if target == root_sid else "上级代理"
        has_message = isinstance(message, str) and bool(message)
        event["title"] = f"向{role}汇报" if has_message else f"{role}动态"
        if has_message:
            event["detail"] = message
        # An ancestor reference is an inline activity, not a descendant detail.
        # Explicit null keeps legacy cards without this field navigable.
        event["input"] = {"agent_run_id": None}
    return public


def load_detail(home, root_sid, run_id, tool_result_max):
    """Resolve a public card against native ancestry and return a bounded snapshot."""
    root_path, _ = _source(home, root_sid)
    targets = ([run_id.removeprefix("codex-agent:")] if run_id.startswith("codex-agent:")
               else _legacy_targets(root_path, run_id))
    if not targets or len(targets) > 32:
        raise ValueError("未找到这个协作代理")
    sources = []
    for target in dict.fromkeys(targets):
        path, meta = _source(home, target)
        parent = _parent(meta)
        seen = {target}
        while parent != root_sid:
            if not parent or parent in seen or len(seen) >= 32:
                raise ValueError("协作代理不属于当前会话")
            seen.add(parent)
            _, ancestor = _source(home, parent)
            parent = _parent(ancestor)
        sources.append((target, path, meta, (seen - {target}) | {root_sid}))
    if len(sources) > 1:
        # A native wait/send call can address several agents. Preserve each
        # identity as a separate drill-down, rather than mixing their streams.
        events = [{"type": "process", "item_id": f"codex-agent:{sid}",
                   "kind": "agent", "phase": "update", "status": "unknown",
                   "title": str(meta.get("agent_path") or "协作代理")[:1024]}
                  for sid, _, meta, _ in sources]
        revision = hashlib.sha256(json.dumps(targets).encode()).hexdigest()
        return events, "unknown", "协作代理", revision
    sid, path, meta, ancestors = sources[0]
    # Forked agents inherit parent context. Only their own native tasks belong
    # in this panel, never the parent's copied transcript prefix.
    offset = None
    status = "unknown"
    snapshot = HistorySourceFingerprint.capture(path)
    native_boundary = next((pos for pos, p, t in _records(path)
                            if t == "event_msg" and p.get("type") == "thread_settings_applied"
                            and p.get("thread_id") == sid), None)
    parent_turns = set()
    if native_boundary is None:
        parent_path, _ = _source(home, _parent(meta))
        parent_turns = {p.get("turn_id") for _, p, t in _records(parent_path)
                        if t == "event_msg" and p.get("type") == "task_started"}
    for pos, payload, kind in _records(path):
        if kind != "event_msg" or (native_boundary is not None and pos < native_boundary):
            continue
        typ = payload.get("type")
        if typ == "task_started" and payload.get("turn_id") not in parent_turns:
            if offset is None:
                offset = pos
            status = "running"
        elif offset is not None and typ in {"task_complete", "task_completed", "turn_aborted"}:
            status = ("interrupted" if typ == "turn_aborted"
                      else "failed" if payload.get("error") else "succeeded")
        elif offset is not None and typ in {"task_failed", "turn_failed", "task_error"}:
            status = "failed"
    title = str(meta.get("agent_path") or "协作代理")[:1024]
    if offset is None:
        return [], "pending", title, snapshot.token
    events, _ = codex_translate_history(path, tool_result_max, start_offset=offset,
                                        end_offset=snapshot.size, snapshot_in_progress=status == "running")
    if HistorySourceFingerprint.capture(path) != snapshot:
        raise ValueError("协作代理记录已更新，请重试读取")
    return (_detail_events(events, root_sid, ancestors),
            status, title, snapshot.token)
