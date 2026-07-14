"""Translate ClaudeSDKClient messages into wire-protocol events.

Stateful per turn: tracks the current assistant message_id so streamed
content_block_delta text attaches to the right block; the assembled
AssistantMessage finalizes it. tool_use is emitted ONCE from the assembled
AssistantMessage (full input), never as JSON-fragment deltas — text deltas
still stream live via StreamEvent.
"""
from __future__ import annotations

import glob
import hashlib
import difflib
import json
import os
import re
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, ToolUseBlock, ToolResultBlock, TextBlock, ThinkingBlock,
    ServerToolUseBlock, ServerToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskUpdatedMessage,
    TaskNotificationMessage, HookEventMessage,
)

from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    ToolDelta, ProcessEvent, TurnPlan,
    TurnEnd, TurnResult, UserMsg,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CLAUDE_MESSAGE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_TRANSCRIPT_MATCHES = 1000
_MAX_TRANSCRIPT_RECORD_CHARS = 16 * 1024 * 1024
_MAX_TIMESTAMP_ENTRIES = 200_000
_MAX_SUBAGENT_FILES = 128
_MAX_SUBAGENT_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_SUBAGENT_EVENTS = 50_000
_MAX_TOOL_DELTA_CHARS = 512 * 1024
_TOOL_DELTA_FLUSH_SECONDS = 0.05
_MAX_REDACT_CONTAINER_ITEMS = 128
_MAX_REDACT_TOTAL_ITEMS = 512
_MAX_DIFF_SOURCE_CHARS = 512 * 1024
_MAX_DIFF_SOURCE_LINES = 4096
_MAX_LIVE_TOOL_ITEMS = 4096
_LIVE_TOOL_ITEMS_OMITTED_ID = "cc-remote-live-tools-omitted"
_DIFF_LINE_BREAK = re.compile(
    r"\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def _wire_id(value: Any, kind: str = "item", salt: str = "") -> str:
    """Return a stable protocol-safe id without leaking untrusted raw values."""
    if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
        return value
    raw = value[:1024] if isinstance(value, str) else type(value).__name__
    digest = hashlib.sha256(
        f"{kind}\0{salt}\0{raw}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"{kind}-{digest}"


def _short_text(value: Any, limit: int = 1024) -> str | None:
    text, _ = bounded_text(value, limit)
    text = " ".join(text.split())
    return text or None


_SENSITIVE_INPUT_MARKERS = (
    "token", "secret", "password", "passwd", "authorization", "cookie",
    "apikey", "privatekey", "credential", "environment",
)


def _sensitive_input_key(value: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    return (compact == "env" or compact.startswith("envvar")
            or any(marker in compact for marker in _SENSITIVE_INPUT_MARKERS))


def _redact_sensitive_input(
    value: Any,
    depth: int = 0,
    *,
    _remaining: list[int] | None = None,
    _ancestors: set[int] | None = None,
) -> Any:
    """Remove credential fields without walking an attacker-sized graph.

    Tool inputs originate outside the wrapper process.  Bound both each
    container and the complete traversal, and detect recursive containers
    before handing the result to the normal wire-size sanitizer.
    """
    if _remaining is None:
        _remaining = [_MAX_REDACT_TOTAL_ITEMS]
    if _ancestors is None:
        _ancestors = set()
    if depth >= 4:
        return "<nested value omitted>" if isinstance(value, (dict, list, tuple)) else value
    if isinstance(value, (dict, list, tuple)):
        identity = id(value)
        if identity in _ancestors:
            return "<circular reference omitted>"
        _ancestors.add(identity)
        try:
            if isinstance(value, dict):
                redacted = {}
                for index, (key, item) in enumerate(value.items()):
                    if (index >= _MAX_REDACT_CONTAINER_ITEMS
                            or _remaining[0] <= 0):
                        redacted["<items omitted>"] = (
                            f"{max(1, len(value) - index)} more")
                        break
                    _remaining[0] -= 1
                    key_text = (key if isinstance(key, str)
                                else f"<{type(key).__name__}>")
                    redacted[key_text] = (
                        "***" if _sensitive_input_key(key_text)
                        else _redact_sensitive_input(
                            item, depth + 1,
                            _remaining=_remaining, _ancestors=_ancestors)
                    )
                return redacted

            redacted_items = []
            for index, item in enumerate(value):
                if (index >= _MAX_REDACT_CONTAINER_ITEMS
                        or _remaining[0] <= 0):
                    redacted_items.append(
                        f"<{max(1, len(value) - index)} items omitted>")
                    break
                _remaining[0] -= 1
                redacted_items.append(_redact_sensitive_input(
                    item, depth + 1,
                    _remaining=_remaining, _ancestors=_ancestors))
            return (tuple(redacted_items) if isinstance(value, tuple)
                    else redacted_items)
        finally:
            _ancestors.remove(identity)
    return value


def _tool_meta(name: str, tool_input: dict[str, Any], *, server_tool: bool = False):
    """Map engine tool names to safe, compact presentation metadata."""
    raw_name = name or "Tool"
    lower = raw_name.lower()
    server = None
    if lower.startswith("mcp__"):
        parts = raw_name.split("__", 2)
        server = _short_text(parts[1], 1000) if len(parts) > 1 else None
        display = parts[2] if len(parts) > 2 else raw_name
        return "mcp", _short_text(tool_input.get("description")) or display, server
    if server_tool:
        category = "web_search" if lower in {"web_search", "web_fetch"} else "server_tool"
        target = (tool_input.get("query") or tool_input.get("url")
                  or tool_input.get("description"))
        verb = ("搜索" if lower == "web_search"
                else "读取网页" if lower == "web_fetch" else "服务端工具")
        return category, _short_text(target) and f"{verb} · {_short_text(target, 800)}" or verb, "anthropic"
    if lower in {"bash", "shell", "execute", "runcommand"}:
        description = _short_text(tool_input.get("description"), 800)
        command = _short_text(tool_input.get("command") or tool_input.get("cmd"), 160)
        return "command", description or (f"运行 · {command}" if command else "运行命令"), None
    if lower in {"read", "write", "edit", "multiedit", "notebookedit", "glob", "grep"}:
        path = _short_text(tool_input.get("file_path") or tool_input.get("path"), 800)
        pattern = _short_text(tool_input.get("pattern"), 800)
        verb = {
            "read": "读取", "write": "写入", "edit": "编辑", "multiedit": "编辑",
            "notebookedit": "编辑 Notebook", "glob": "查找文件", "grep": "搜索",
        }.get(lower, "文件操作")
        target = path or pattern
        return "file", f"{verb} · {target}" if target else verb, None
    if lower in {"websearch", "webfetch"}:
        target = _short_text(tool_input.get("query") or tool_input.get("url"), 800)
        verb = "搜索" if lower == "websearch" else "读取网页"
        return "web_search", f"{verb} · {target}" if target else verb, None
    if lower in {"agent", "task"}:
        title = (_short_text(tool_input.get("description"), 900)
                 or _short_text(tool_input.get("subagent_type"), 900)
                 or "协作代理")
        return "agent", title, None
    if lower == "enterplanmode":
        return "tool", "进入计划模式", None
    if lower == "exitplanmode":
        return "tool", "完成计划", None
    return "tool", _short_text(tool_input.get("description"), 900) or raw_name, None


def _tool_diff(name: str, tool_input: dict[str, Any], max_chars: int) -> tuple[str | None, bool]:
    """Build a bounded display diff only from the exact Edit/Write payload."""
    lower = (name or "").lower()
    path = _short_text(
        tool_input.get("file_path") or tool_input.get("path"), 800) or "file"

    # Bound sources before splitlines/SequenceMatcher. difflib otherwise builds
    # several full-size lists/maps and can consume quadratic CPU on a model-
    # supplied multi-megabyte Edit payload even though the wire result is tiny.
    source_char_limit = min(
        _MAX_DIFF_SOURCE_CHARS, max(16 * 1024, max_chars * 4))

    def source_lines(text: str) -> tuple[list[str], bool]:
        clipped = text[:source_char_limit]
        truncated = len(text) > len(clipped)
        lines: list[str] = []
        start = 0
        for match in _DIFF_LINE_BREAK.finditer(clipped):
            lines.append(clipped[start:match.end()])
            start = match.end()
            if len(lines) >= _MAX_DIFF_SOURCE_LINES:
                break
        if len(lines) < _MAX_DIFF_SOURCE_LINES and start < len(clipped):
            lines.append(clipped[start:])
            start = len(clipped)
        if start < len(clipped):
            truncated = True
        return lines, truncated

    if lower in {"edit", "multiedit"}:
        old = tool_input.get("old_string")
        new = tool_input.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None, False
        if (len(old) <= source_char_limit and len(new) <= source_char_limit
                and old == new):
            return None, False
        old_lines, old_truncated = source_lines(old)
        new_lines, new_truncated = source_lines(new)
        source_truncated = old_truncated or new_truncated
        lines = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=path, tofile=path, lineterm="",
        )
    elif lower == "write":
        new = tool_input.get("content")
        if not isinstance(new, str):
            return None, False
        new_lines, source_truncated = source_lines(new)
        lines = difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null", tofile=path, lineterm="",
        )
    else:
        return None, False

    # Consume the diff generator only up to the display budget. This prevents a
    # bounded-but-high-churn source from materializing a much larger diff before
    # the final truncation step.
    render_limit = max(1, min(max_chars, 2 * 1024 * 1024))
    parts: list[str] = []
    used = 0
    output_truncated = False
    for part in lines:
        normalized = part.rstrip("\n")
        prefix = "\n" if parts else ""
        remaining = render_limit - used
        if remaining <= 0:
            output_truncated = True
            break
        piece = prefix + normalized
        if len(piece) > remaining:
            parts.append(piece[:remaining])
            used += remaining
            output_truncated = True
            break
        parts.append(piece)
        used += len(piece)
    rendered = "".join(parts)
    if not rendered:
        if source_truncated:
            rendered = f"--- {path}\n+++ {path}\n@@ diff preview truncated @@"
            rendered = rendered[:render_limit]
        else:
            return None, False
    return rendered, source_truncated or output_truncated


def _safe_result_content(tool_name: str | None, content: Any) -> Any:
    """Keep MCP user-visible text while dropping opaque/private metadata."""
    if not (tool_name or "").lower().startswith("mcp__"):
        return content
    if isinstance(content, str) or content is None:
        return content
    blocks = content.get("content") if isinstance(content, dict) else content
    if not isinstance(blocks, list):
        return "MCP 调用已完成"
    texts = []
    for block in blocks[:64]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts) if texts else "MCP 调用已完成"


def _assistant_text_channel(stop_reason: str | None, has_tool: bool,
                            parent_tool_use_id: str | None = None) -> str:
    # Claude's intermediate narration commonly arrives in an AssistantMessage
    # with stop_reason=None immediately before a separate tool-use message.
    if parent_tool_use_id or has_tool or stop_reason in {None, "tool_use"}:
        return "commentary"
    return "final"


def _task_status(value: str | None) -> str:
    return {
        "pending": "pending", "running": "running", "paused": "pending",
        "completed": "succeeded", "success": "succeeded",
        "failed": "failed", "error": "failed",
        "killed": "cancelled", "stopped": "cancelled", "cancelled": "cancelled",
    }.get((value or "").lower(), "unknown")


def _task_progress(usage: Any, last_tool_name: str | None = None) -> str | None:
    bits = []
    if last_tool_name:
        bits.append(f"最近工具：{last_tool_name}")
    if isinstance(usage, dict):
        tool_uses = usage.get("tool_uses")
        total_tokens = usage.get("total_tokens")
        duration_ms = usage.get("duration_ms")
        if isinstance(tool_uses, int):
            bits.append(f"{tool_uses} 次工具调用")
        if isinstance(total_tokens, int):
            bits.append(f"{total_tokens} tokens")
        if isinstance(duration_ms, int):
            bits.append(f"{duration_ms / 1000:g}s")
    return " · ".join(bits) or None


class StreamTranslator:
    def __init__(self, tool_result_max: int, turn_id: str | None = None,
                 item_turns: dict[str, str] | None = None,
                 item_titles: dict[str, str] | None = None,
                 item_meta: dict[str, tuple[str, str | None]] | None = None):
        self.tool_result_max = tool_result_max
        self.turn_id = _wire_id(turn_id, "turn") if turn_id else None
        # These maps are optionally shared by every translator for one resident
        # session. Claude's queue is continuous across ResultMessage boundaries;
        # a background task update consumed at the start of the next query must
        # still update the turn that created it.
        self.item_turns = item_turns if item_turns is not None else {}
        self.item_titles = item_titles if item_titles is not None else {}
        self.item_meta = item_meta if item_meta is not None else {}
        self._message_ids: dict[str, str] = {}
        self._started_channels: set[str] = set()
        # Only the emitted prefix LENGTH is needed to deduplicate the assembled
        # AssistantMessage after streaming deltas.  Retaining and repeatedly
        # concatenating the complete text made long turns unbounded and O(n^2).
        self._emitted: dict[str, int] = {"thinking": 0, "text": 0}
        self._tool_diffs: dict[str, tuple[str, bool]] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_outputs: dict[str, str] = {}
        self._tool_delta_totals: dict[str, int] = {}
        self._tool_last_emit: dict[tuple[str, str], float] = {}
        self._tool_pending: dict[tuple[str, str], str] = {}
        self._tool_last_progress: dict[str, str] = {}
        # All per-tool maps below are gated by this fixed admission set. Once
        # full, unknown ids remain rejected for the rest of the turn; never
        # evicting ids is also a security tombstone for late MCP results, whose
        # private metadata may only be filtered while the original tool name is
        # still known.
        self._tool_items: set[str] = set()
        self._finished_tool_items: set[str] = set()
        self._tool_items_truncated = False
        self._plan_item_id: str | None = None
        # stop_reason can be null even for Claude's true final text. Keep the
        # last top-level no-tool candidate until the authoritative successful
        # Result boundary, where a second AssistantMsgEnd can reclassify the
        # existing UI block without repeating its content.
        self._ambiguous_final_mid: str | None = None
        self._has_final_text = False
        # Claude can emit several AssistantMessage records in one user turn.
        # fork_session(up_to_message_id=...) accepts the transcript UUID, not the
        # API message_id, so retain the last valid one until ResultMessage.
        self._last_assistant_uuid: str | None = None

    def _remember_turn(self, item_id: str, parent_id: str | None = None) -> str | None:
        turn = (self.item_turns.get(item_id)
                or (self.item_turns.get(parent_id) if parent_id else None)
                or self.turn_id)
        if turn:
            self.item_turns[item_id] = turn
        # Bound session-lifetime state even for very long-running wrappers.
        if len(self.item_turns) > 8192:
            for old in list(self.item_turns)[:1024]:
                self.item_turns.pop(old, None)
                self.item_titles.pop(old, None)
                self.item_meta.pop(old, None)
        return turn

    def _message_id(self, channel_key: str, suggested: str | None = None) -> str:
        current = self._message_ids.get(channel_key)
        if current:
            return current
        base = _wire_id(suggested or uuid.uuid4().hex, "msg")
        value = base if channel_key == "text" else f"{base}:thinking"
        current = _wire_id(value, "msg", channel_key)
        self._message_ids[channel_key] = current
        return current

    def _ensure_channel(self, events: list, channel_key: str, channel: str,
                        suggested: str | None = None) -> str:
        mid = self._message_id(channel_key, suggested)
        if channel_key not in self._started_channels:
            events.append(AssistantMsgStart(message_id=mid, channel=channel))
            self._started_channels.add(channel_key)
        return mid

    def _append_text(self, events: list, channel_key: str, channel: str,
                     text: Any, suggested: str | None = None) -> None:
        if not isinstance(text, str) or not text:
            return
        bounded, _ = bounded_text(text, self.tool_result_max)
        if not bounded:
            return
        mid = self._ensure_channel(events, channel_key, channel, suggested)
        events.append(Delta(message_id=mid, text=bounded, channel=channel))
        self._emitted[channel_key] += len(bounded)

    def _finish_message(self, events: list, text_channel: str) -> None:
        if "thinking" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["thinking"], channel="thinking"))
        if "text" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["text"], channel=text_channel))
        self._message_ids.clear()
        self._started_channels.clear()
        self._emitted = {"thinking": 0, "text": 0}

    def _emit_tool_use(self, events: list, block: ToolUseBlock | ServerToolUseBlock,
                       message_id: str, parent_id: str | None,
                       server_tool: bool = False) -> None:
        self._ambiguous_final_mid = None
        tool_id = _wire_id(block.id, "tool")
        if not self._admit_tool_item(tool_id, events):
            return
        parent = _wire_id(parent_id, "tool") if parent_id else None
        redacted_input = _redact_sensitive_input(block.input)
        safe_input = bounded_tool_input(redacted_input, self.tool_result_max)
        category, title, server = _tool_meta(
            block.name, redacted_input, server_tool=server_tool)
        events.append(ToolUse(
            message_id=message_id, tool_use_id=tool_id, tool=block.name,
            input=safe_input, category=category, title=title,
            parent_id=parent, server=server,
        ))
        self._tool_names[tool_id] = block.name
        self.item_titles[tool_id] = title
        self._remember_turn(tool_id, parent)
        diff, was_truncated = _tool_diff(
            block.name, block.input, self.tool_result_max)
        if diff:
            self._tool_diffs[tool_id] = (diff, was_truncated)

        lower = block.name.lower()
        if lower == "enterplanmode":
            self._plan_item_id = _wire_id(
                f"plan:{self.turn_id or tool_id}", "plan")
            events.append(ProcessEvent(
                item_id=self._plan_item_id, kind="plan", phase="start",
                status="running", turn_id=self.turn_id,
                title="计划模式", summary="正在制定计划",
            ))
        elif lower == "exitplanmode":
            plan_id = self._plan_item_id or _wire_id(
                f"plan:{self.turn_id or tool_id}", "plan")
            plan_text = block.input.get("plan")
            if isinstance(plan_text, str) and plan_text.strip():
                explanation, _ = bounded_text(plan_text, 64 * 1024)
                events.append(TurnPlan(
                    item_id=plan_id, turn_id=self.turn_id,
                    explanation=explanation, plan=[],
                ))
            events.append(ProcessEvent(
                item_id=plan_id, kind="plan", phase="end",
                status="succeeded", turn_id=self.turn_id,
                title="计划模式", summary="计划已完成",
            ))

    def _emit_tool_result(self, events: list, tool_use_id: Any, content: Any,
                          is_error: bool = False, summary: str | None = None) -> None:
        self._ambiguous_final_mid = None
        tool_id = _wire_id(tool_use_id, "tool")
        # Fail closed for a result whose ToolUse was omitted/never observed. In
        # particular, treating an unknown MCP result as a generic tool result
        # would bypass _safe_result_content and expose its opaque `_meta` fields.
        if (tool_id not in self._tool_items
                or tool_id not in self._tool_names
                or tool_id in self._finished_tool_items):
            return
        events.extend(self._flush_tool_deltas(tool_id))
        content = _safe_result_content(self._tool_names.get(tool_id), content)
        text, was_truncated = bounded_text(content, self.tool_result_max)
        diff_info = self._tool_diffs.pop(tool_id, None)
        diff = diff_info[0] if diff_info and not is_error else None
        truncated = bool(was_truncated or (diff_info and diff_info[1])) or None
        events.append(ToolResult(
            tool_use_id=tool_id, content=text, is_error=bool(is_error),
            truncated=truncated, status="failed" if is_error else "succeeded",
            summary=summary, diff=diff,
        ))
        self._finished_tool_items.add(tool_id)
        self._tool_outputs.pop(tool_id, None)
        self._tool_delta_totals.pop(tool_id, None)
        self._tool_last_progress.pop(tool_id, None)
        for key in [key for key in self._tool_last_emit if key[0] == tool_id]:
            self._tool_last_emit.pop(key, None)

    def _admit_tool_item(self, tool_id: str, events: list) -> bool:
        if tool_id in self._finished_tool_items:
            return False
        if tool_id in self._tool_items:
            return True
        if len(self._tool_items) < _MAX_LIVE_TOOL_ITEMS:
            self._tool_items.add(tool_id)
            return True
        if not self._tool_items_truncated:
            self._tool_items_truncated = True
            events.append(ProcessEvent(
                item_id=_LIVE_TOOL_ITEMS_OMITTED_ID,
                kind="compaction",
                phase="snapshot",
                status="succeeded",
                turn_id=self.turn_id,
                title="较早过程已省略",
                summary="此回合的工具项目过多，后续新增项目未实时展示。",
            ))
        return False

    def _queue_tool_delta(self, tool_id: str, stream: str, delta: str) -> list:
        """Coalesce high-frequency SDK progress before it reaches ring/WS.

        The first chunk is immediate. Bursts within 50 ms stay in one bounded
        pending chunk and flush on the next spaced event or ToolResult. This is
        intentionally synchronous so it cannot create a second consumer of the
        SDK response stream.
        """
        if not delta:
            return []
        # Preserve cross-stream chronology: output buffered just before a
        # progress/summary frame must be emitted first, not delayed until result.
        events = self._flush_tool_deltas(tool_id, except_stream=stream)
        key = (tool_id, stream)
        total = self._tool_delta_totals.get(tool_id, 0)
        remaining = max(0, self.tool_result_max - total)
        if remaining <= 0:
            return events
        delta = delta[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
        pending = self._tool_pending.get(key, "")
        if stream in {"progress", "summary"} and pending:
            pending = pending + "\n" + delta
        else:
            pending += delta
        pending = pending[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
        now = time.monotonic()
        last = self._tool_last_emit.get(key)
        if last is not None and now - last < _TOOL_DELTA_FLUSH_SECONDS:
            self._tool_pending[key] = pending
            return events
        self._tool_pending.pop(key, None)
        self._tool_last_emit[key] = now
        self._tool_delta_totals[tool_id] = total + len(pending)
        events.append(ToolDelta(tool_use_id=tool_id, stream=stream, delta=pending))
        return events

    def _flush_tool_deltas(self, tool_id: str, except_stream: str | None = None) -> list:
        events = []
        for key in [key for key in self._tool_pending
                    if key[0] == tool_id and key[1] != except_stream]:
            pending = self._tool_pending.pop(key, "")
            if not pending:
                continue
            total = self._tool_delta_totals.get(tool_id, 0)
            remaining = max(0, self.tool_result_max - total)
            pending = pending[:min(remaining, _MAX_TOOL_DELTA_CHARS)]
            if pending:
                events.append(ToolDelta(
                    tool_use_id=tool_id, stream=key[1], delta=pending))
                self._tool_delta_totals[tool_id] = total + len(pending)
        return events

    def _feed_stream_event(self, msg: StreamEvent) -> list:
        events: list = []
        ev = msg.event if isinstance(msg.event, dict) else {}
        if ev.get("type") != "content_block_delta":
            return events
        delta = ev.get("delta") if isinstance(ev.get("delta"), dict) else {}
        kind = delta.get("type")
        if kind == "text_delta":
            self._append_text(events, "text", "unknown", delta.get("text"), msg.uuid)
        elif kind == "thinking_delta":
            # signature_delta is deliberately ignored: signatures are opaque
            # verification material, not user-visible reasoning.
            self._append_text(
                events, "thinking", "thinking", delta.get("thinking"), msg.uuid)
        return events

    def _feed_assistant(self, msg: AssistantMessage) -> list:
        events: list = []
        if (isinstance(msg.uuid, str)
                and _CLAUDE_MESSAGE_UUID.fullmatch(msg.uuid)):
            self._last_assistant_uuid = msg.uuid
        blocks = msg.content if isinstance(msg.content, list) else []
        has_client_tool = any(isinstance(block, ToolUseBlock) for block in blocks)
        has_server_tool = any(isinstance(block, ServerToolUseBlock) for block in blocks)
        has_text = any(isinstance(block, TextBlock) for block in blocks)
        has_visible_text = any(
            isinstance(block, TextBlock) and bool(block.text) for block in blocks)
        has_tool_activity = (
            bool(msg.parent_tool_use_id) or has_client_tool or has_server_tool
            or msg.stop_reason == "tool_use"
            or any(isinstance(block, (
                ToolResultBlock, ServerToolResultBlock)) for block in blocks)
        )
        text_channel = _assistant_text_channel(
            msg.stop_reason,
            has_client_tool or (has_server_tool and not has_text),
            msg.parent_tool_use_id)
        ambiguous_candidate = (
            not self._has_final_text and not has_tool_activity
            and msg.stop_reason is None and has_visible_text
        )
        if has_tool_activity:
            self._ambiguous_final_mid = None
        if text_channel == "final" and has_visible_text:
            self._has_final_text = True
            self._ambiguous_final_mid = None
        parent = (_wire_id(msg.parent_tool_use_id, "tool")
                  if msg.parent_tool_use_id else None)
        assembled_lengths = {"thinking": 0, "text": 0}

        for block in blocks:
            if isinstance(block, ThinkingBlock):
                previous = assembled_lengths["thinking"]
                assembled_lengths["thinking"] += len(block.thinking)
                already = self._emitted["thinking"]
                if already < assembled_lengths["thinking"]:
                    offset = max(0, already - previous)
                    self._append_text(
                        events, "thinking", "thinking", block.thinking[offset:], msg.uuid)
            elif isinstance(block, TextBlock):
                previous = assembled_lengths["text"]
                assembled_lengths["text"] += len(block.text)
                already = self._emitted["text"]
                if already < assembled_lengths["text"]:
                    offset = max(0, already - previous)
                    self._append_text(
                        events, "text", text_channel, block.text[offset:], msg.uuid)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                mid = self._ensure_channel(
                    events, "text", "commentary", msg.uuid or msg.message_id)
                self._emit_tool_use(
                    events, block, mid, parent,
                    server_tool=isinstance(block, ServerToolUseBlock),
                )
            elif isinstance(block, ToolResultBlock):
                self._emit_tool_result(
                    events, block.tool_use_id, block.content, bool(block.is_error))
            elif isinstance(block, ServerToolResultBlock):
                content_type = (block.content.get("type", "")
                                if isinstance(block.content, dict) else "")
                self._emit_tool_result(
                    events, block.tool_use_id, block.content,
                    "error" in str(content_type).lower(),
                )
        candidate_mid = (
            self._message_ids.get("text") if ambiguous_candidate else None)
        self._finish_message(events, text_channel)
        if candidate_mid is not None:
            self._ambiguous_final_mid = candidate_mid
        return events

    def _feed_user(self, msg: UserMessage) -> list:
        events: list = []
        content = msg.content if isinstance(msg.content, list) else []
        result_meta = msg.tool_use_result if isinstance(msg.tool_use_result, dict) else {}
        summary_bits = []
        for key, label in (("agentType", "代理"), ("status", "状态"),
                           ("totalToolUseCount", "工具调用")):
            value = result_meta.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                summary_bits.append(f"{label}：{value}")
        duration = result_meta.get("totalDurationMs")
        if isinstance(duration, (int, float)) and duration >= 0:
            summary_bits.append(f"耗时：{duration / 1000:g}s")
        summary = _short_text(" · ".join(summary_bits), 64 * 1024) if summary_bits else None
        if any(isinstance(block, (
                ToolResultBlock, ServerToolResultBlock)) for block in content):
            self._ambiguous_final_mid = None
        for block in content:
            if isinstance(block, ToolResultBlock):
                self._emit_tool_result(
                    events, block.tool_use_id, block.content,
                    bool(block.is_error), summary=summary)
            elif isinstance(block, ServerToolResultBlock):
                self._emit_tool_result(events, block.tool_use_id, block.content)
        return events

    def _feed_progress_system(self, msg: SystemMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        subtype = msg.subtype
        tool_id_raw = (data.get("tool_use_id") or data.get("toolUseID")
                       or data.get("toolUseId"))
        if not tool_id_raw:
            return []
        self._ambiguous_final_mid = None
        tool_id = _wire_id(tool_id_raw, "tool")
        events: list = []
        if not self._admit_tool_item(tool_id, events):
            return events
        if subtype == "bash_progress":
            raw = data.get("output")
            if not isinstance(raw, str):
                raw = data.get("full_output") if isinstance(data.get("full_output"), str) else ""
            previous = self._tool_outputs.get(tool_id, "")
            delta = raw[len(previous):] if raw.startswith(previous) else raw
            # Some CLI versions send cumulative full_output. Retain only the
            # display budget prefix; an unbounded copy would defeat ToolDelta's
            # ring/transport bounds during a verbose command.
            self._tool_outputs[tool_id] = raw[:self.tool_result_max]
            delta, _ = bounded_text(delta, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
            return events + self._queue_tool_delta(tool_id, "output", delta)
        if subtype == "tool_progress":
            progress = (data.get("progress") or data.get("message")
                        or data.get("description"))
            if not isinstance(progress, str):
                elapsed = data.get("elapsed_time_seconds")
                progress = f"已运行 {elapsed:g}s" if isinstance(elapsed, (int, float)) else ""
            progress, _ = bounded_text(
                progress, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
            if progress == self._tool_last_progress.get(tool_id):
                return []
            self._tool_last_progress[tool_id] = progress
            return events + self._queue_tool_delta(tool_id, "progress", progress)
        return events

    def _feed_tool_summary(self, msg: SystemMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary:
            return []
        self._ambiguous_final_mid = None
        summary, _ = bounded_text(
            summary, min(self.tool_result_max, _MAX_TOOL_DELTA_CHARS))
        ids = (data.get("preceding_tool_use_ids")
               or data.get("precedingToolUseIds")
               or data.get("tool_use_ids") or data.get("toolUseIds")
               or data.get("tool_use_id") or data.get("toolUseId") or [])
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            return []
        events = []
        for value in ids[:64]:
            if value:
                tool_id = _wire_id(value, "tool")
                if self._admit_tool_item(tool_id, events):
                    events.extend(self._queue_tool_delta(
                        tool_id, "summary", summary))
        return events

    def _feed_task(self, msg: SystemMessage) -> list:
        task_raw = getattr(msg, "task_id", None)
        if not task_raw:
            return []
        task_id = _wire_id(task_raw, "task")
        parent_raw = getattr(msg, "tool_use_id", None)
        remembered_kind, remembered_parent = self.item_meta.get(
            task_id, ("task", None))
        parent = (_wire_id(parent_raw, "tool") if parent_raw
                  else remembered_parent)
        turn = self._remember_turn(task_id, parent)
        title = (getattr(msg, "description", None)
                 or self.item_titles.get(task_id) or "后台任务")
        title = _short_text(title, 1000) or "后台任务"
        self.item_titles[task_id] = title
        if isinstance(msg, TaskStartedMessage):
            kind = "agent" if (msg.task_type or "").lower() in {"agent", "subagent"} or parent else "task"
            self.item_meta[task_id] = (kind, parent)
            return [ProcessEvent(
                item_id=task_id, kind=kind, phase="start", status="running",
                turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.task_type, 1024),
            )]
        if isinstance(msg, TaskProgressMessage):
            kind = remembered_kind if task_id in self.item_meta else ("agent" if parent else "task")
            self.item_meta[task_id] = (kind, parent)
            return [ProcessEvent(
                item_id=task_id, kind=kind,
                phase="update", status="running", turn_id=turn,
                parent_id=parent, title=title,
                progress=_task_progress(msg.usage, msg.last_tool_name),
            )]
        if isinstance(msg, TaskUpdatedMessage):
            kind = remembered_kind
            status = _task_status(msg.status)
            terminal = status in {"succeeded", "failed", "cancelled"}
            patch_summary = None
            if isinstance(msg.patch, dict):
                patch_summary = _short_text(
                    msg.patch.get("description") or msg.patch.get("subject"), 4096)
            return [ProcessEvent(
                item_id=task_id, kind=kind, phase="end" if terminal else "update",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=patch_summary,
            )]
        if isinstance(msg, TaskNotificationMessage):
            kind = remembered_kind if task_id in self.item_meta else ("agent" if parent else "task")
            status = _task_status(msg.status)
            return [ProcessEvent(
                item_id=task_id, kind=kind, phase="end",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.summary, 64 * 1024),
                progress=_task_progress(msg.usage),
            )]
        return []

    def _feed_hook(self, msg: HookEventMessage) -> list:
        data = msg.data if isinstance(msg.data, dict) else {}
        parent_raw = (data.get("tool_use_id") or data.get("toolUseID")
                      or data.get("toolUseId"))
        parent = _wire_id(parent_raw, "tool") if parent_raw else None
        correlation = (data.get("hook_id") or data.get("hookId")
                       or parent_raw or data.get("command")
                       or msg.uuid or msg.hook_event_name)
        # Always hash hook correlation. A raw hook command must never become a
        # protocol id merely because it happens to match WireId's character set.
        hook_digest = hashlib.sha256(
            f"{msg.hook_event_name}\0{correlation}".encode(
                "utf-8", "surrogatepass")
        ).hexdigest()[:24]
        item_id = f"hook-{hook_digest}"
        turn = self._remember_turn(item_id, parent)
        known_hooks = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
            "Stop", "SubagentStop", "PreCompact", "Notification",
            "SubagentStart", "PermissionRequest",
        }
        hook_name = msg.hook_event_name if msg.hook_event_name in known_hooks else "unknown"
        title = f"Hook · {hook_name}"
        if msg.subtype == "hook_started":
            return [ProcessEvent(
                item_id=item_id, kind="hook", phase="start", status="running",
                turn_id=turn, parent_id=parent, title=title,
            )]
        exit_code = data.get("exit_code")
        exit_code = exit_code if isinstance(exit_code, int) else None
        outcome = data.get("outcome")
        outcome_text = str(outcome).lower() if isinstance(outcome, str) else ""
        status = ("declined" if outcome_text in {"blocked", "deny", "denied"}
                  else "failed" if (exit_code not in (None, 0) or outcome_text in {"error", "failed"})
                  else "succeeded")
        duration = data.get("duration_ms") or data.get("durationMs")
        duration_ms = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
        summary = (f"结果：{outcome}" if outcome_text in {
            "success", "succeeded", "blocked", "deny", "denied", "error", "failed",
        } else None)
        # Never forward data.output, commands, environment variables, or hook
        # callback payloads. Lifecycle metadata is sufficient for the UI.
        return [ProcessEvent(
            item_id=item_id, kind="hook", phase="end", status=status,
            turn_id=turn, parent_id=parent, title=title, summary=summary,
            exit_code=exit_code, duration_ms=duration_ms,
        )]

    def feed(self, msg) -> list:
        if isinstance(msg, StreamEvent):
            return self._feed_stream_event(msg)
        if isinstance(msg, AssistantMessage):
            return self._feed_assistant(msg)
        if isinstance(msg, UserMessage):
            return self._feed_user(msg)
        if isinstance(msg, HookEventMessage):
            return self._feed_hook(msg)
        if isinstance(msg, (TaskStartedMessage, TaskProgressMessage,
                            TaskUpdatedMessage, TaskNotificationMessage)):
            return self._feed_task(msg)
        if isinstance(msg, SystemMessage):
            if msg.subtype in {"tool_progress", "bash_progress"}:
                return self._feed_progress_system(msg)
            if msg.subtype == "tool_use_summary":
                return self._feed_tool_summary(msg)
            return []
        if isinstance(msg, ResultMessage):
            events = []
            if (not msg.is_error and not self._has_final_text
                    and self._ambiguous_final_mid is not None):
                events.append(AssistantMsgEnd(
                    message_id=self._ambiguous_final_mid, channel="final"))
            for tool_id in sorted({key[0] for key in self._tool_pending}):
                events.extend(self._flush_tool_deltas(tool_id))
            events.append(TurnEnd(result=TurnResult(
                subtype=msg.subtype,
                duration_ms=msg.duration_ms,
                is_error=msg.is_error,
                total_cost_usd=msg.total_cost_usd,
                num_turns=msg.num_turns,
            ), turn_id=self._last_assistant_uuid))
            self._last_assistant_uuid = None
            self._ambiguous_final_mid = None
            self._has_final_text = False
            return events
        return []


def _cc_img_block(b: dict) -> dict | None:
    """A cc transcript image block {type:image, source:{type:base64, media_type,
    data}} -> {media_type, data} (the web's QueryImg shape). None if not base64."""
    src = b.get("source")
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        return {"media_type": src.get("media_type") or "image/png", "data": src["data"]}
    return None


def extract_session_id(msg) -> str | None:
    """Pull the cc session id out of any SDK message that carries it."""
    if isinstance(msg, ResultMessage):
        return msg.session_id
    if isinstance(msg, SystemMessage):
        data = msg.data
        if isinstance(data, dict):
            return data.get("session_id")
    return None


def extract_model(msg) -> str | None:
    """Pull the current model out of the init SystemMessage."""
    if isinstance(msg, SystemMessage) and msg.subtype == "init":
        data = msg.data
        if isinstance(data, dict):
            return data.get("model")
    return None


# ---- on-disk history -> wire events (for session switch) ----

def transcript_path(session_id: str) -> str | None:
    """Absolute path of a cc session's transcript .jsonl, or None. session_id is
    globally unique, so a glob across all project dirs finds it regardless of cwd.

    Used by the transcript watcher to spot writes made by an EXTERNAL process (a
    native `claude` in the user's terminal). Watch st_size, NOT st_mtime: merely
    spawning `claude --resume <id>` touches mtime without changing a byte, so mtime
    would false-positive on every session the wrapper opens."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    try:
        safe_id = glob.escape(session_id)
        root = os.path.realpath(os.path.expanduser("~/.claude/projects"))
        matches = glob.iglob(os.path.join(root, "*", f"{safe_id}.jsonl"))
        for index, match in enumerate(matches):
            if index >= _MAX_TRANSCRIPT_MATCHES:
                break
            resolved = os.path.realpath(match)
            if os.path.commonpath((root, resolved)) == root:
                return resolved
        return None
    except Exception:
        return None


def _bounded_jsonl_lines(file):
    """Yield complete records while skipping a single pathological long line."""
    while True:
        line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)
        if not line:
            return
        complete = line.endswith("\n") or len(line) < _MAX_TRANSCRIPT_RECORD_CHARS + 1
        if complete:
            yield line
            continue
        while line and not line.endswith("\n"):
            line = file.readline(_MAX_TRANSCRIPT_RECORD_CHARS + 1)


def transcript_timestamps(session_id: str) -> dict[str, float]:
    """Map each transcript entry's uuid -> epoch seconds, read straight from the
    .jsonl. The SDK's SessionMessage drops the per-message timestamp, so without
    this, history events default their `ts` to now (making every past message show
    the current time — "like a clock"). Best-effort: {} if not found/readable.
    session_id is globally unique, so a glob across all project dirs locates it."""
    out: dict[str, float] = {}
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return out
    try:
        path = transcript_path(session_id)
        if not path:
            return out
        with open(path) as f:
            for line in _bounded_jsonl_lines(f):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                uid, ts = d.get("uuid"), d.get("timestamp")
                if not uid or not isinstance(ts, str):
                    continue
                try:
                    out[uid] = datetime.fromisoformat(
                        ts.replace("Z", "+00:00") if ts.endswith("Z") else ts).timestamp()
                    if len(out) >= _MAX_TIMESTAMP_ENTRIES:
                        break
                except Exception:
                    continue
    except Exception:
        pass
    return out


def translate_history(messages, tool_result_max: int, timestamps: dict | None = None) -> list:
    """Translate a session's on-disk transcript (list[SessionMessage]) into wire
    events the client reducer renders as past turns.

    The transcript carries no ResultMessage, so synthetic TurnEnd frames delimit
    turns. `timestamps` (uuid -> epoch seconds, from transcript_timestamps) stamps
    each UserMsg with its real ask-time and each TurnEnd with the turn's last
    message time (answer-done time) — otherwise history shows "now". Rich
    assistant blocks retain the same thinking/commentary/final and semantic tool
    structure as the live stream. Non-conversational user turns (compact summaries,
    slash-command envelopes, local-command stdout) remain hidden.
    """
    events: list = []
    turn_open = False
    last_ts = None  # transcript ts of the most-recent message in the open turn
    last_assistant_uuid = None
    current_turn_id = None
    history_tool_diffs: dict[str, tuple[str, bool]] = {}
    history_tool_names: dict[str, str] = {}
    history_plan_id: str | None = None
    ambiguous_final_mid: str | None = None
    ambiguous_final_start: int | None = None

    def _history_id(value, kind: str, position: str) -> str:
        """Keep valid engine ids; deterministically repair malformed legacy rows.

        Old hand-edited/corrupt transcripts can omit a message/tool id.  WireId is
        intentionally strict, but one bad block must not make the entire otherwise
        readable conversation disappear.  Transcript positions are append-stable,
        so the fallback also remains a valid pagination/dedup key across reparses.
        """
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        raw = value[:1024] if isinstance(value, str) else type(value).__name__
        digest = hashlib.sha256(
            f"{kind}\0{position}\0{raw}".encode("utf-8", "surrogatepass")
        ).hexdigest()[:24]
        return f"hist-{kind}-{digest}"

    def _ts(uid):
        return timestamps.get(uid) if timestamps else None

    def _um(uid, prompt):
        um = UserMsg(msg_id=uid, prompt=prompt)
        t = _ts(uid)
        if t is not None:
            um.ts = t   # question time, not load time
        return um

    def close_turn():
        nonlocal turn_open, last_assistant_uuid, current_turn_id, history_plan_id
        nonlocal ambiguous_final_mid, ambiguous_final_start
        if turn_open:
            # SessionMessage rows can omit stop_reason. Live must conservatively
            # treat such text as commentary, but history has the next user/EOF as
            # an authoritative turn boundary. Promote only the final top-level
            # ambiguous text row that was not followed by any tool activity.
            if (ambiguous_final_mid is not None
                    and ambiguous_final_start is not None):
                for event_index in range(ambiguous_final_start, len(events)):
                    event = events[event_index]
                    if (isinstance(event, (
                            AssistantMsgStart, Delta, AssistantMsgEnd))
                            and event.message_id == ambiguous_final_mid
                            and event.channel == "commentary"):
                        event.channel = "final"
            te = TurnEnd(
                result=TurnResult(
                    subtype="success", duration_ms=0, is_error=False),
                turn_id=last_assistant_uuid,
            )
            if last_ts is not None:
                te.ts = last_ts   # answer-done time = last message of the turn
            events.append(te)
            turn_open = False
            last_assistant_uuid = None
            current_turn_id = None
            history_plan_id = None
            ambiguous_final_mid = None
            ambiguous_final_start = None

    for message_index, m in enumerate(messages):
        msg = m.message
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or m.type
        content = msg.get("content")
        source_uid = m.uuid if isinstance(m.uuid, str) else ""
        message_uid = _history_id(source_uid, "msg", str(message_index))

        if role == "user":
            if isinstance(content, str):
                if _is_meta_user_text(content):
                    continue
                close_turn()
                events.append(_um(message_uid, content))
                turn_open = True
                current_turn_id = message_uid
            elif isinstance(content, list):
                # collect any uploaded images up front so they attach to this turn's
                # UserMsg (replay on reload — the transcript stores the base64).
                imgs = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "image":
                        img = _cc_img_block(b)
                        if img:
                            imgs.append(img)
                made = False
                for block_index, b in enumerate(content):
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        ambiguous_final_mid = None
                        ambiguous_final_start = None
                        tool_id = _history_id(
                            b.get("tool_use_id"), "tool",
                            f"{message_index}-{block_index}-result")
                        text, was_truncated = bounded_text(
                            _safe_result_content(
                                history_tool_names.get(tool_id), b.get("content")),
                            tool_result_max)
                        diff_info = history_tool_diffs.pop(tool_id, None)
                        is_error = bool(b.get("is_error"))
                        truncated = bool(
                            was_truncated or (diff_info and diff_info[1])) or None
                        events.append(ToolResult(
                            tool_use_id=tool_id,
                            content=text,
                            is_error=is_error,
                            truncated=truncated,
                            status="failed" if is_error else "succeeded",
                            diff=diff_info[0] if diff_info and not is_error else None,
                        ))
                    elif bt == "text":
                        txt = b.get("text", "")
                        if txt and not _is_meta_user_text(txt):
                            close_turn()
                            um = _um(message_uid, txt)
                            if imgs and not made:
                                um.images = imgs
                                made = True
                            events.append(um)
                            turn_open = True
                            current_turn_id = message_uid
                if imgs and not made:   # image-only user turn
                    close_turn()
                    um = _um(message_uid, "")
                    um.images = imgs
                    events.append(um)
                    turn_open = True
                    current_turn_id = message_uid
        elif role == "assistant":
            if not isinstance(content, list):
                continue
            if _CLAUDE_MESSAGE_UUID.fullmatch(source_uid):
                last_assistant_uuid = source_uid
            mid = message_uid
            thinking_mid = _wire_id(f"{mid}:thinking", "msg", str(message_index))
            has_client_tool = any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content)
            has_server_tool = any(
                isinstance(b, dict) and b.get("type") == "server_tool_use"
                for b in content)
            has_text = any(
                isinstance(b, dict) and b.get("type") == "text"
                for b in content)
            text_channel = _assistant_text_channel(
                msg.get("stop_reason"),
                has_client_tool or (has_server_tool and not has_text),
                getattr(m, "parent_tool_use_id", None))
            parent_raw = getattr(m, "parent_tool_use_id", None)
            parent = (_history_id(parent_raw, "tool", f"{message_index}-parent")
                      if parent_raw else None)
            has_tool_activity = (
                bool(parent) or has_client_tool or has_server_tool or any(
                    isinstance(b, dict) and (
                        b.get("type") == "tool_result"
                        or (isinstance(b.get("type"), str)
                            and b.get("type").endswith("_tool_result")))
                    for b in content)
            )
            if has_tool_activity:
                ambiguous_final_mid = None
                ambiguous_final_start = None
            elif (parent is None and msg.get("stop_reason") is None
                  and any(isinstance(b, dict) and b.get("type") == "text"
                          and isinstance(b.get("text"), str) and b.get("text")
                          for b in content)):
                ambiguous_final_mid = mid
                ambiguous_final_start = len(events)
            elif text_channel == "final" and has_text:
                ambiguous_final_mid = None
                ambiguous_final_start = None
            text_started = False
            thinking_started = False
            for block_index, b in enumerate(content):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text", "")
                    if not text_started:
                        events.append(AssistantMsgStart(
                            message_id=mid, channel=text_channel))
                        text_started = True
                    if txt:
                        events.append(Delta(
                            message_id=mid, text=txt, channel=text_channel))
                elif bt == "thinking":
                    thinking = b.get("thinking", "")
                    if not thinking_started:
                        events.append(AssistantMsgStart(
                            message_id=thinking_mid, channel="thinking"))
                        thinking_started = True
                    if isinstance(thinking, str) and thinking:
                        safe_thinking, _ = bounded_text(thinking, tool_result_max)
                        if safe_thinking:
                            events.append(Delta(
                                message_id=thinking_mid, text=safe_thinking,
                                channel="thinking"))
                elif bt in {"tool_use", "server_tool_use"}:
                    if not text_started:
                        events.append(AssistantMsgStart(
                            message_id=mid, channel="commentary"))
                        text_started = True
                    # a stored tool_use input SHOULD be a dict, but old/odd history
                    # can carry a scalar (e.g. 3); coerce so ToolUse validation
                    # doesn't crash the whole history load (and thus the resume).
                    _inp = b.get("input")
                    raw_input = (_inp if isinstance(_inp, dict)
                                 else ({} if _inp is None else {"value": _inp}))
                    tool_id = _history_id(
                        b.get("id"), "tool",
                        f"{message_index}-{block_index}-use")
                    server_tool = bt == "server_tool_use"
                    redacted_input = _redact_sensitive_input(raw_input)
                    category, title, server = _tool_meta(
                        b.get("name") or "", redacted_input,
                        server_tool=server_tool)
                    events.append(ToolUse(
                        message_id=mid,
                        tool_use_id=tool_id,
                        tool=b.get("name") or "",
                        input=bounded_tool_input(redacted_input, tool_result_max),
                        category=category, title=title, parent_id=parent,
                        server=server,
                    ))
                    history_tool_names[tool_id] = b.get("name") or ""
                    diff, diff_truncated = _tool_diff(
                        b.get("name") or "", raw_input, tool_result_max)
                    if diff:
                        history_tool_diffs[tool_id] = (diff, diff_truncated)
                    lower = str(b.get("name") or "").lower()
                    if lower == "enterplanmode":
                        history_plan_id = _wire_id(
                            f"plan:{current_turn_id or tool_id}", "plan")
                        events.append(ProcessEvent(
                            item_id=history_plan_id, kind="plan", phase="start",
                            status="running", turn_id=current_turn_id,
                            title="计划模式", summary="正在制定计划",
                        ))
                    elif lower == "exitplanmode":
                        plan_id = history_plan_id or _wire_id(
                            f"plan:{current_turn_id or tool_id}", "plan")
                        plan_text = raw_input.get("plan")
                        if isinstance(plan_text, str) and plan_text.strip():
                            explanation, _ = bounded_text(plan_text, 64 * 1024)
                            events.append(TurnPlan(
                                item_id=plan_id, turn_id=current_turn_id,
                                explanation=explanation, plan=[]))
                        events.append(ProcessEvent(
                            item_id=plan_id, kind="plan", phase="end",
                            status="succeeded", turn_id=current_turn_id,
                            title="计划模式", summary="计划已完成"))
                elif bt == "tool_result" or (
                        isinstance(bt, str) and bt.endswith("_tool_result")
                        and b.get("tool_use_id")):
                    tool_id = _history_id(
                        b.get("tool_use_id"), "tool",
                        f"{message_index}-{block_index}-assistant-result")
                    text, was_truncated = bounded_text(
                        _safe_result_content(
                            history_tool_names.get(tool_id), b.get("content")),
                        tool_result_max)
                    result_type = (b.get("content") or {}).get("type", "") if isinstance(
                        b.get("content"), dict) else ""
                    is_error = bool(b.get("is_error")) or "error" in str(result_type).lower()
                    diff_info = history_tool_diffs.pop(tool_id, None)
                    events.append(ToolResult(
                        tool_use_id=tool_id, content=text, is_error=is_error,
                        status="failed" if is_error else "succeeded",
                        truncated=bool(
                            was_truncated or (diff_info and diff_info[1])) or None,
                        diff=diff_info[0] if diff_info and not is_error else None,
                    ))
            if thinking_started:
                events.append(AssistantMsgEnd(
                    message_id=thinking_mid, channel="thinking"))
            if text_started:
                events.append(AssistantMsgEnd(
                    message_id=mid, channel=text_channel))
                turn_open = True
        # advance last_ts AFTER handling m: a leading close_turn (for the next user
        # msg) stamps the PRIOR turn's tail; the final close_turn stamps this turn's
        # last (assistant) message = answer-done time.
        mts = _ts(source_uid)
        if mts is not None:
            last_ts = mts
    close_turn()
    return events


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00") if value.endswith("Z") else value
        ).timestamp()
    except Exception:
        return None


def translate_subagent_history(session_id: str, tool_result_max: int) -> list:
    """Recover bounded Claude subagent timelines omitted by SessionMessage.

    Claude stores each subagent below ``<session>/subagents/agent-*.jsonl``.
    The SDK's ``get_session_messages`` intentionally returns only the main chain,
    so process history would otherwise disappear after reload. We correlate each
    agentId through the main Agent/Task tool result, omit the private initial
    subagent prompt, and reuse ``translate_history`` for the public assistant/tool
    blocks. Uncorrelated files are skipped rather than creating phantom turns.
    """
    main_path = transcript_path(session_id)
    if not main_path:
        return []
    subagent_dir = os.path.join(os.path.splitext(main_path)[0], "subagents")
    if not os.path.isdir(subagent_dir):
        return []

    tool_turns: dict[str, str] = {}
    tool_titles: dict[str, str] = {}
    agent_tools: dict[str, str] = {}
    current_turn: str | None = None
    try:
        with open(main_path, encoding="utf-8") as source:
            for index, line in enumerate(_bounded_jsonl_lines(source)):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                msg = row.get("message") if isinstance(row.get("message"), dict) else {}
                role = msg.get("role") or row.get("type")
                content = msg.get("content")
                if role == "user":
                    visible = (isinstance(content, str) and content
                               and not _is_meta_user_text(content))
                    if isinstance(content, list):
                        visible = any(
                            isinstance(block, dict) and block.get("type") == "text"
                            and block.get("text")
                            and not _is_meta_user_text(block.get("text"))
                            for block in content)
                    if visible:
                        current_turn = _wire_id(row.get("uuid"), "msg", str(index))
                    result_meta = row.get("toolUseResult")
                    if isinstance(result_meta, dict):
                        agent_id = result_meta.get("agentId")
                        tool_id = next((
                            block.get("tool_use_id") for block in (content or [])
                            if isinstance(block, dict) and block.get("type") == "tool_result"
                        ), None) if isinstance(content, list) else None
                        if agent_id and tool_id:
                            agent_tools[str(agent_id)] = _wire_id(tool_id, "tool")
                elif role == "assistant" and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        if str(block.get("name") or "").lower() not in {"agent", "task"}:
                            continue
                        tool_id = _wire_id(
                            block.get("id"), "tool", f"{index}-agent")
                        if current_turn:
                            tool_turns[tool_id] = current_turn
                        tool_input = block.get("input") if isinstance(
                            block.get("input"), dict) else {}
                        tool_titles[tool_id] = (
                            _short_text(tool_input.get("description"), 1000)
                            or _short_text(tool_input.get("subagent_type"), 1000)
                            or "协作代理")
    except (OSError, UnicodeError):
        return []

    paths = sorted(glob.iglob(os.path.join(subagent_dir, "agent-*.jsonl")))
    events: list = []
    total_bytes = 0
    for file_index, path in enumerate(paths[:_MAX_SUBAGENT_FILES]):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        total_bytes += size
        if size > _MAX_SUBAGENT_TOTAL_BYTES or total_bytes > _MAX_SUBAGENT_TOTAL_BYTES:
            break
        raw_rows = []
        agent_id = None
        timestamps: dict[str, float] = {}
        first_ts = last_ts = None
        final_summary = None
        try:
            with open(path, encoding="utf-8") as source:
                for row_index, line in enumerate(_bounded_jsonl_lines(source)):
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not agent_id and row.get("agentId"):
                        agent_id = str(row.get("agentId"))
                    uid = _wire_id(
                        row.get("uuid"), "msg", f"{file_index}-{row_index}")
                    ts = _parse_timestamp(row.get("timestamp"))
                    if ts is not None:
                        timestamps[uid] = ts
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = ts if last_ts is None else max(last_ts, ts)
                    msg = row.get("message") if isinstance(
                        row.get("message"), dict) else None
                    if not msg:
                        continue
                    content = msg.get("content")
                    role = msg.get("role") or row.get("type")
                    # The private delegated prompt is already present in the
                    # parent Agent tool input. Do not render it a second time.
                    if role == "user" and not (
                            isinstance(content, list) and any(
                                isinstance(block, dict)
                                and block.get("type") == "tool_result"
                                for block in content)):
                        continue
                    if role == "assistant" and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                candidate = _short_text(block.get("text"), 4096)
                                if candidate:
                                    final_summary = candidate
                    raw_rows.append(SimpleNamespace(
                        uuid=uid,
                        type=role,
                        message=msg,
                        # Nest all internal tools under the agent process row.
                        parent_tool_use_id=None,
                    ))
        except (OSError, UnicodeError):
            continue
        if not agent_id:
            match = re.fullmatch(r"agent-([A-Za-z0-9._:@-]+)\.jsonl", os.path.basename(path))
            agent_id = match.group(1) if match else None
        parent = agent_tools.get(agent_id or "")
        turn = tool_turns.get(parent or "")
        if not parent or not turn:
            continue
        agent_item = _wire_id(f"agent:{agent_id}", "agent")
        title = tool_titles.get(parent, "协作代理")
        start = ProcessEvent(
            item_id=agent_item, kind="agent", phase="start", status="running",
            turn_id=turn, parent_id=parent, title=title,
        )
        if first_ts is not None:
            start.ts = first_ts
        sequence = [start]
        translated = translate_history(
            raw_rows, tool_result_max, timestamps=timestamps)
        for event in translated:
            if isinstance(event, TurnEnd) or isinstance(event, UserMsg):
                continue
            if isinstance(event, (AssistantMsgStart, Delta, AssistantMsgEnd)):
                if event.channel != "thinking":
                    event.channel = "commentary"
            if isinstance(event, ToolUse):
                event.parent_id = agent_item
            if isinstance(event, ProcessEvent):
                event.turn_id = turn
                event.parent_id = event.parent_id or agent_item
            sequence.append(event)
        end = ProcessEvent(
            item_id=agent_item, kind="agent", phase="end", status="succeeded",
            turn_id=turn, parent_id=parent, title=title, summary=final_summary,
        )
        if last_ts is not None:
            end.ts = last_ts
        sequence.append(end)
        if len(events) + len(sequence) > _MAX_SUBAGENT_EVENTS:
            break
        events.extend(sequence)
    return events


def merge_subagent_history(events: list, subagent_events: list) -> list:
    """Insert each recovered agent timeline immediately below its Agent tool."""
    if not subagent_events:
        return events
    groups: dict[str, list[list]] = {}
    current: list | None = None
    current_parent = None
    for event in subagent_events:
        if (isinstance(event, ProcessEvent) and event.kind == "agent"
                and event.phase == "start"):
            current = [event]
            current_parent = event.parent_id
            continue
        if current is None:
            continue
        current.append(event)
        if (isinstance(event, ProcessEvent) and event.kind == "agent"
                and event.phase == "end"):
            if current_parent:
                groups.setdefault(current_parent, []).append(current)
            current = None
            current_parent = None
    merged = []
    for event in events:
        merged.append(event)
        if isinstance(event, ToolUse):
            for sequence in groups.pop(event.tool_use_id, []):
                merged.extend(sequence)
    return merged


def _is_meta_user_text(text: str) -> bool:
    """Skip non-conversational user turns that would just clutter the history:
    compact summaries, slash-command envelopes, and local-command stdout/stderr."""
    t = text.lstrip()
    return (
        t.startswith("This session is being continued from a previous conversation")
        or t.startswith("<command-name>")
        or t.startswith("<command-message>")
        or t.startswith("<command-args>")
        or t.startswith("<local-command-stdout>")
        or t.startswith("<local-command-stderr>")
    )


def last_assistant_model(messages) -> str | None:
    """Most recent assistant message's model id, for restoring the model readout
    when loading a switched session's history."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "assistant" and isinstance(m.message, dict):
            mdl = m.message.get("model")
            if mdl:
                return mdl
    return None
