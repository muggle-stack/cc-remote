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
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping

from claude_agent_sdk.types import (
    AssistantMessage, ResultMessage, UserMessage, SystemMessage,
    StreamEvent, ToolUseBlock, ToolResultBlock, TextBlock, ThinkingBlock,
    ServerToolUseBlock, ServerToolResultBlock,
    TaskStartedMessage, TaskProgressMessage, TaskUpdatedMessage,
    TaskNotificationMessage, HookEventMessage,
)

from cc_remote.claude_paths import claude_projects_dir
from cc_remote.protocol import (
    MAX_BACKGROUND_PROCESS_COMMAND_CHARS, MAX_BACKGROUND_PROCESS_ITEMS,
    AssistantMsgStart, Delta, ToolUse, ToolResult, AssistantMsgEnd,
    ToolDelta, ProcessEvent, BackgroundProcessItem, BackgroundProcessSync,
    TurnPlan,
    TurnEnd, TurnResult, UserMsg,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input
from cc_remote.wrapper.claude_model_fallback import FALLBACK_TOOL, model_fallback_event
from cc_remote.wrapper.turn_changes import native_claude_diff

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CLAUDE_MESSAGE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_MAX_TRANSCRIPT_MATCHES = 1000
_MAX_TRANSCRIPT_RECORD_CHARS = 64 * 1024 * 1024
_MAX_TIMESTAMP_ENTRIES = 200_000
_MAX_TRANSCRIPT_CHAIN_ENTRIES = 200_000
_MAX_DELAYED_RETRY_TAIL_ROWS = 4096
_MAX_DELAYED_RETRY_TAIL_BYTES = 64 * 1024 * 1024
_MAX_INTERNAL_USER_EVENTS = 10_000
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
_CLAUDE_IMAGE_READ_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".webp",
})
_SYNTHETIC_NO_RESPONSE_TEXT = "No response requested."
_INTERRUPTED_USER_TEXT = "[Request interrupted by user]"
_SYNTHETIC_API_ERROR_PREFIX = "API Error:"
_CLAUDE_AGENT_TASK_TYPES = frozenset({
    # Current Claude Code / Agent SDK task types.
    "local_agent", "local_workflow", "in_process_teammate",
    # Older/alternate runtimes retained for source compatibility.
    "agent", "subagent",
})
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


def _claude_image_read_path(name: Any, tool_input: Any) -> str | None:
    """Recognize Claude's built-in binary image read before its result arrives.

    SVG deliberately remains a normal text Read: Claude Code returns SVG source
    rather than an image content block.  The binary formats below are the ones
    which produce ``tool_result.content[].type=image`` in the transcript.
    """
    normalized = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if normalized not in {"read", "readfile"} or not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("path")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or os.path.splitext(path)[1].lower() not in _CLAUDE_IMAGE_READ_SUFFIXES
    ):
        return None
    return path


def _claude_image_blocks(content: Any) -> list[dict[str, str]]:
    """Extract only public base64 image blocks from one Claude tool result."""
    blocks = (
        [content]
        if isinstance(content, dict) and content.get("type") == "image"
        else content.get("content") if isinstance(content, dict)
        else content
    )
    if isinstance(blocks, dict):
        blocks = [blocks]
    if not isinstance(blocks, list):
        return []
    images: list[dict[str, str]] = []
    for block in blocks[:8]:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        image = _cc_img_block(block)
        if (
            image is not None
            and isinstance(image.get("media_type"), str)
            and isinstance(image.get("data"), str)
        ):
            images.append(image)
    return images


def _claude_history_tool_use_block(block: Any) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") in {"tool_use", "server_tool_use"}
    )


def _claude_history_tool_result_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    block_type = block.get("type")
    return bool(
        block_type == "tool_result"
        or (
            isinstance(block_type, str)
            and block_type.endswith("_tool_result")
            and block.get("tool_use_id")
        )
    )


def _claude_history_tool_result_error(block: dict[str, Any]) -> bool:
    content = block.get("content")
    content_type = content.get("type", "") if isinstance(content, dict) else ""
    return bool(block.get("is_error")) or "error" in str(content_type).lower()


def claude_history_image_id(tool_use_id: str, index: int = 0) -> str:
    """Stable opaque id for an image returned by one native Claude tool call."""
    digest = hashlib.sha256(
        f"claude-tool-image\0{tool_use_id}\0{index}".encode(
            "utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"img-{digest}"


@dataclass(frozen=True)
class ClaudeHistoryImageAsset:
    """Private transcript image body paired with its public process item."""

    item_id: str
    image_id: str
    media_type: str
    data: str


def extract_claude_history_image_assets(
    messages: Any,
    *,
    item_ids: set[str] | frozenset[str] | None = None,
    max_assets: int = 64,
) -> tuple[ClaudeHistoryImageAsset, ...]:
    """Read only requested Claude ``Read`` image bodies from loaded history.

    The ordinary history translator emits no base64.  This side channel is used
    after pagination selected the visible turns, so a transcript containing many
    old images cannot make one four-turn History request retain every body.
    """
    wanted = set(item_ids) if item_ids is not None else None
    if max_assets <= 0 or wanted == set():
        return ()
    image_reads: dict[str, str] = {}
    assets: list[ClaudeHistoryImageAsset] = []
    for message in messages or ():
        payload = getattr(message, "message", None)
        if not isinstance(payload, dict):
            continue
        role = payload.get("role") or getattr(message, "type", None)
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        if role not in {"assistant", "user"}:
            continue
        for block in content:
            if _claude_history_tool_use_block(block):
                raw_id = block.get("id")
                if (
                    not isinstance(raw_id, str)
                    or not _SAFE_WIRE_ID.fullmatch(raw_id)
                    or (wanted is not None and raw_id not in wanted)
                ):
                    continue
                if _claude_image_read_path(
                    block.get("name"), block.get("input")
                ):
                    image_reads[raw_id] = raw_id
                continue
            if not _claude_history_tool_result_block(block):
                continue
            raw_id = block.get("tool_use_id")
            item_id = image_reads.pop(raw_id, None) if isinstance(raw_id, str) else None
            if item_id is None or _claude_history_tool_result_error(block):
                continue
            images = _claude_image_blocks(block.get("content"))
            if not images:
                continue
            image = images[0]
            assets.append(ClaudeHistoryImageAsset(
                item_id=item_id,
                image_id=claude_history_image_id(item_id),
                media_type=image["media_type"],
                data=image["data"],
            ))
            if len(assets) >= max_assets:
                return tuple(assets)
    return tuple(assets)


def _is_agent_task_type(value: Any) -> bool:
    return isinstance(value, str) \
        and value.lower() in _CLAUDE_AGENT_TASK_TYPES


def replayed_user_message_id(message: Any) -> str | None:
    """Return the native UUID for one top-level replayed human input.

    ``--replay-user-messages`` is the only authoritative live bridge between a
    browser Query and Claude's independently-generated transcript identity.
    Tool-result user envelopes are part of the same turn and must never become
    additional aliases for the optimistic browser row.
    """
    if not isinstance(message, UserMessage) or message.parent_tool_use_id:
        return None
    origin = getattr(message, "origin", None)
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        # Streaming-input sessions replay autonomous task/channel/peer prompts
        # through the same UserMessage class. Only a human/unattributed prompt
        # can own the browser's optimistic row.
        return None
    if _is_interrupted_user_content(message.content):
        # Claude replays its synthetic interrupt marker through the same
        # UserMessage channel as a real browser prompt.  A replacement query
        # can start before that late marker reaches the SDK reader; binding it
        # would give the next optimistic row the preceding turn's UUID.
        return None
    content = message.content if isinstance(message.content, list) else []
    if any(isinstance(block, (ToolResultBlock, ServerToolResultBlock))
           for block in content):
        return None
    native_id = message.uuid
    return (
        native_id
        if isinstance(native_id, str) and _CLAUDE_MESSAGE_UUID.fullmatch(native_id)
        else None
    )


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


def _public_tool_input(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded public payload for one tool invocation.

    Agent/Task ``prompt`` is delegated model context, not user-facing process
    output.  Keep only the small presentation fields already used by the card;
    an allowlist also fails closed when a future SDK adds new private fields.
    """
    if (name or "").lower() not in {"agent", "task"}:
        value = _redact_sensitive_input(tool_input)
        return value if isinstance(value, dict) else {}
    public: dict[str, Any] = {}
    for key in ("description", "subagent_type", "agent_type"):
        value = _short_text(tool_input.get(key), 1000)
        if value:
            public[key] = value
    return public


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
    if (tool_name or "").lower() in {"agent", "task"}:
        # Claude's Agent launch result contains an internal agent id, delegated
        # prompt and temporary output-file path. None of those are part of the
        # public cc-remote projection; the dedicated Agent detail endpoint owns
        # the useful process and final text.
        text = content if isinstance(content, str) else ""
        return (
            "协作代理已启动"
            if "async agent launched" in text.lower()
            else "协作代理已完成"
        )
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


def _safe_image_read_result_content(tool_name: str, content: Any) -> Any:
    """Preserve diagnostics without ever projecting an image body as text."""
    blocks = (
        [content]
        if isinstance(content, dict) and content.get("type") == "image"
        else content.get("content") if isinstance(content, dict)
        else content
    )
    if isinstance(blocks, dict):
        blocks = [blocks]
    if isinstance(blocks, list):
        removed_image = False
        filtered = []
        for block in blocks[:64]:
            if isinstance(block, dict) and block.get("type") == "image":
                removed_image = True
                continue
            filtered.append(block)
        return filtered or ("图片读取结果不可用" if removed_image else [])
    return _safe_result_content(tool_name, content)


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


def _agent_process_id(tool_id: str) -> str:
    """Stable public run id paired with one Claude Agent/Task tool call."""
    digest = hashlib.sha256(
        f"claude-agent\0{tool_id}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"agent-{digest}"


def public_agent_run_id(tool_id: str) -> str:
    """Public helper shared by live routing and source-backed detail lookup."""
    return _agent_process_id(_wire_id(tool_id, "tool"))


def claude_background_tasks(
    message: object,
) -> tuple[dict[str, str | bool], ...] | None:
    """Return Claude's authoritative live-background-task level.

    Claude Code documents ``background_tasks_changed.tasks`` as a full
    replacement, not an edge stream.  ``None`` means this is not a valid level
    event; an empty tuple is the meaningful authoritative empty set.  Ambient
    runner bookkeeping is intentionally excluded from user-visible work.
    """
    if not (
        isinstance(message, SystemMessage)
        and message.subtype == "background_tasks_changed"
    ):
        return None
    data = message.data if isinstance(message.data, dict) else {}
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        return None
    tasks: list[dict[str, str | bool]] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict) or raw.get("ambient") is True:
            continue
        raw_id = raw.get("task_id")
        if not isinstance(raw_id, str) or not raw_id or raw_id in seen:
            continue
        seen.add(raw_id)
        task: dict[str, str | bool] = {"task_id": raw_id}
        task_type = raw.get("task_type")
        if isinstance(task_type, str) and task_type:
            task["task_type"] = task_type
        description = raw.get("description")
        if isinstance(description, str) and description:
            task["description"] = description
        tasks.append(task)
    return tuple(tasks)


class StreamTranslator:
    def __init__(self, tool_result_max: int, turn_id: str | None = None,
                 item_turns: dict[str, str] | None = None,
                 item_titles: dict[str, str] | None = None,
                 item_meta: dict[str, tuple[str, str | None]] | None = None,
                 item_commands: dict[str, str] | None = None):
        self.tool_result_max = tool_result_max
        self.turn_id = _wire_id(turn_id, "turn") if turn_id else None
        # These maps are optionally shared by every translator for one resident
        # session. Claude's queue is continuous across ResultMessage boundaries;
        # a background task update consumed at the start of the next query must
        # still update the turn that created it.
        self.item_turns = item_turns if item_turns is not None else {}
        self.item_titles = item_titles if item_titles is not None else {}
        self.item_meta = item_meta if item_meta is not None else {}
        self.item_commands = (
            item_commands if item_commands is not None else {})
        self._message_ids: dict[str, str] = {}
        self._started_channels: set[str] = set()
        # Only the emitted prefix LENGTH is needed to deduplicate the assembled
        # AssistantMessage after streaming deltas.  Retaining and repeatedly
        # concatenating the complete text made long turns unbounded and O(n^2).
        self._emitted: dict[str, int] = {"thinking": 0, "text": 0}
        self._tool_diffs: dict[str, tuple[str, bool]] = {}
        self._tool_paths: dict[str, str] = {}
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
        # Claude exposes viewed images as a normal Read tool followed by an
        # image-valued ToolResult. Project those calls through the existing
        # engine-neutral view_image process instead of serializing base64 as
        # ordinary tool output.
        self._image_reads: dict[str, tuple[str, str | None]] = {}
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
        # rewind_files() and rewind_conversation target the top-level user
        # transcript UUID. Keep it separate from the browser's optimistic turn
        # id and from tool-result user envelopes.
        self._last_user_uuid: str | None = None

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
                self.item_commands.pop(old, None)
        return turn

    def _background_for_turn(self, turn: str | None) -> bool | None:
        """Mark an item restored from an older translator as detached work."""
        return bool(
            turn is not None
            and self.turn_id is not None
            and turn != self.turn_id
        ) or None

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
            events.append(AssistantMsgStart(
                message_id=mid, turn_id=self.turn_id, channel=channel))
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
        events.append(Delta(
            message_id=mid, turn_id=self.turn_id,
            text=bounded, channel=channel))
        self._emitted[channel_key] += len(bounded)

    def _finish_message(self, events: list, text_channel: str) -> None:
        if "thinking" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["thinking"],
                turn_id=self.turn_id, channel="thinking"))
        if "text" in self._started_channels:
            events.append(AssistantMsgEnd(
                message_id=self._message_ids["text"],
                turn_id=self.turn_id, channel=text_channel))
        self._message_ids.clear()
        self._started_channels.clear()
        self._emitted = {"thinking": 0, "text": 0}

    def _agent_tool_event(
        self, tool_id: str, *, phase: str, status: str,
        title: str | None = None, summary: str | None = None,
        progress: str | None = None, duration_ms: int | None = None,
    ) -> ProcessEvent:
        item_id = _agent_process_id(tool_id)
        resolved_title = (title or self.item_titles.get(item_id)
                          or self.item_titles.get(tool_id) or "协作代理")
        self.item_titles[item_id] = resolved_title
        self.item_meta[item_id] = ("agent", tool_id)
        return ProcessEvent(
            item_id=item_id, kind="agent", phase=phase, status=status,
            turn_id=self._remember_turn(item_id, tool_id), parent_id=tool_id,
            title=resolved_title, summary=summary, progress=progress,
            duration_ms=duration_ms, background=True,
        )

    def _emit_tool_use(self, events: list, block: ToolUseBlock | ServerToolUseBlock,
                       message_id: str, parent_id: str | None,
                       server_tool: bool = False) -> None:
        self._ambiguous_final_mid = None
        tool_id = _wire_id(block.id, "tool")
        if not self._admit_tool_item(tool_id, events):
            return
        parent = _wire_id(parent_id, "tool") if parent_id else None
        tool_turn = self._remember_turn(tool_id, parent)
        redacted_input = _redact_sensitive_input(block.input)
        public_input = _public_tool_input(block.name, block.input)
        safe_input = bounded_tool_input(public_input, self.tool_result_max)
        category, title, server = _tool_meta(
            block.name, redacted_input, server_tool=server_tool)
        image_path = _claude_image_read_path(block.name, block.input)
        self._tool_names[tool_id] = block.name
        if image_path is not None:
            self._image_reads[tool_id] = (image_path, parent)
            self.item_titles[tool_id] = "查看图片"
            events.append(ProcessEvent(
                item_id=tool_id,
                kind="server_tool",
                phase="start",
                status="running",
                turn_id=tool_turn,
                parent_id=parent,
                title="查看图片",
                input={"file_path": image_path},
                tool="view_image",
                background=self._background_for_turn(tool_turn),
            ))
            return
        events.append(ToolUse(
            message_id=message_id, tool_use_id=tool_id,
            turn_id=tool_turn, tool=block.name,
            input=safe_input, category=category, title=title,
            parent_id=parent, server=server,
            background=self._background_for_turn(tool_turn),
        ))
        self.item_titles[tool_id] = title
        if category == "command":
            command = safe_input.get("command") or safe_input.get("cmd")
            if isinstance(command, str) and command:
                safe_command, _ = bounded_text(
                    command, MAX_BACKGROUND_PROCESS_COMMAND_CHARS)
                if safe_command:
                    self.item_commands[tool_id] = safe_command
        diff, was_truncated = _tool_diff(
            block.name, block.input, self.tool_result_max)
        if block.name.lower() in {"edit", "write", "multiedit"}:
            path = block.input.get("file_path") or block.input.get("path")
            if isinstance(path, str) and len(path) <= 4096:
                self._tool_paths[tool_id] = path
        if diff:
            self._tool_diffs[tool_id] = (diff, was_truncated)

        lower = block.name.lower()
        if category == "agent":
            agent_type = (_short_text(block.input.get("subagent_type"), 1024)
                          or _short_text(block.input.get("agent_type"), 1024))
            events.append(self._agent_tool_event(
                tool_id, phase="start", status="running", title=title,
                summary=(f"类型：{agent_type}" if agent_type else None),
            ))
        elif lower == "enterplanmode":
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
                          is_error: bool = False, summary: str | None = None,
                          duration_ms: int | None = None,
                          agent_terminal: bool | None = None,
                          agent_status: str | None = None,
                          native_result: dict | None = None) -> None:
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
        image_read = self._image_reads.pop(tool_id, None)
        if image_read is not None:
            image_path, parent = image_read
            tool_turn = self._remember_turn(tool_id, parent)
            tool_name = self._tool_names.get(tool_id) or "Read"
            status = "failed" if is_error else "succeeded"
            if not is_error and _claude_image_blocks(content):
                events.append(ProcessEvent(
                    item_id=tool_id,
                    kind="server_tool",
                    phase="end",
                    status=status,
                    turn_id=tool_turn,
                    parent_id=parent,
                    title="查看图片",
                    summary=summary,
                    input={"file_path": image_path},
                    tool="view_image",
                    duration_ms=duration_ms,
                    background=self._background_for_turn(tool_turn),
                ))
            else:
                safe_content = _safe_image_read_result_content(
                    tool_name, content)
                output, was_truncated = bounded_text(
                    safe_content, self.tool_result_max)
                _category, fallback_title, _server = _tool_meta(
                    tool_name, {"file_path": image_path})
                events.append(ProcessEvent(
                    item_id=tool_id,
                    kind="server_tool",
                    phase="end",
                    status=status,
                    turn_id=tool_turn,
                    parent_id=parent,
                    title=fallback_title,
                    summary=summary,
                    input={"file_path": image_path},
                    output=output or None,
                    tool=tool_name,
                    duration_ms=duration_ms,
                    truncated=was_truncated or None,
                    background=self._background_for_turn(tool_turn),
                ))
            self._finish_tool_item(tool_id)
            return
        content = _safe_result_content(self._tool_names.get(tool_id), content)
        text, was_truncated = bounded_text(content, self.tool_result_max)
        diff_info = self._tool_diffs.pop(tool_id, None)
        native_diff = (native_claude_diff(native_result, self._tool_paths.get(tool_id))
                       if not is_error else None)
        diff = native_diff or (diff_info[0] if diff_info and not is_error else None)
        truncated = bool(was_truncated or (diff_info and diff_info[1])) or None
        is_agent = (self._tool_names.get(tool_id) or "").lower() in {
            "agent", "task"}
        result_status = (
            "failed" if is_error else
            agent_status if is_agent and agent_status else "succeeded"
        )
        tool_turn = self._remember_turn(tool_id)
        events.append(ToolResult(
            tool_use_id=tool_id, turn_id=tool_turn,
            content=text, is_error=bool(is_error),
            truncated=truncated, status=result_status,
            summary=summary, diff=diff,
            diff_source="native" if native_diff else "fragment" if diff else None,
            diff_truncated=False if native_diff else bool(diff_info and diff_info[1]),
            background=self._background_for_turn(tool_turn),
        ))
        if is_agent and agent_terminal is not False:
            events.append(self._agent_tool_event(
                tool_id, phase="end",
                status=("failed" if is_error else agent_status or "succeeded"),
                summary=summary, duration_ms=duration_ms,
            ))
        elif is_agent:
            events.append(self._agent_tool_event(
                tool_id, phase="update", status=agent_status or "running",
                summary=summary, duration_ms=duration_ms,
            ))
        self._finish_tool_item(tool_id)

    def _finish_tool_item(self, tool_id: str) -> None:
        self._tool_paths.pop(tool_id, None)
        self._finished_tool_items.add(tool_id)
        self._image_reads.pop(tool_id, None)
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
        tool_turn = self._remember_turn(tool_id)
        events.append(ToolDelta(
            tool_use_id=tool_id, turn_id=tool_turn,
            stream=stream, delta=pending,
            background=self._background_for_turn(tool_turn)))
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
                tool_turn = self._remember_turn(tool_id)
                events.append(ToolDelta(
                    tool_use_id=tool_id,
                    turn_id=tool_turn,
                    stream=key[1], delta=pending,
                    background=self._background_for_turn(tool_turn)))
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
        has_tool_result = any(isinstance(block, (
            ToolResultBlock, ServerToolResultBlock)) for block in content)
        replayed_id = replayed_user_message_id(msg)
        if replayed_id is not None:
            self._last_user_uuid = replayed_id
        result_meta = msg.tool_use_result if isinstance(msg.tool_use_result, dict) else {}
        summary_bits = []
        for key, label in (("agentType", "代理"), ("status", "状态"),
                           ("totalToolUseCount", "工具调用")):
            value = result_meta.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                summary_bits.append(f"{label}：{value}")
        duration = result_meta.get("totalDurationMs")
        if (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and duration >= 0):
            summary_bits.append(f"耗时：{duration / 1000:g}s")
            duration_ms = int(duration)
        else:
            duration_ms = None
        summary = _short_text(" · ".join(summary_bits), 64 * 1024) if summary_bits else None
        if has_tool_result:
            self._ambiguous_final_mid = None
        for block in content:
            if isinstance(block, ToolResultBlock):
                raw_status = result_meta.get("status")
                async_launched = bool(result_meta.get("isAsync")) or (
                    isinstance(raw_status, str)
                    and raw_status.lower() == "async_launched"
                )
                mapped_status = _task_status(
                    raw_status if isinstance(raw_status, str) else None)
                if async_launched:
                    mapped_status = "running"
                self._emit_tool_result(
                    events, block.tool_use_id, block.content,
                    bool(block.is_error), summary=summary,
                    duration_ms=duration_ms,
                    agent_terminal=not async_launched,
                    agent_status=mapped_status if mapped_status != "unknown" else None,
                    native_result=result_meta)
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
            emitted = events + self._queue_tool_delta(
                tool_id, "progress", progress)
            if (self._tool_names.get(tool_id) or "").lower() in {"agent", "task"}:
                emitted.append(self._agent_tool_event(
                    tool_id, phase="update", status="running",
                    progress=progress,
                ))
            return emitted
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
                    if (self._tool_names.get(tool_id) or "").lower() in {
                            "agent", "task"}:
                        events.append(self._agent_tool_event(
                            tool_id, phase="update", status="running",
                            progress=summary,
                        ))
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
        task_type = (
            (msg.task_type or "").lower()
            if isinstance(msg, TaskStartedMessage) else ""
        )
        parent_kind = (
            self.item_meta.get(_agent_process_id(parent), (None, None))[0]
            if parent else None
        )
        parent_tool = (self._tool_names.get(parent) or "").lower() \
            if parent else ""
        # A tool_use_id is only correlation. Bash(run_in_background=true)
        # carries its Bash tool id in the exact same field as an Agent task.
        # Promote only explicit Agent task types or a parent already proven to
        # be an Agent/Task tool; otherwise keep the background job ordinary.
        kind = "agent" if (
            _is_agent_task_type(task_type)
            or remembered_kind == "agent"
            or parent_kind == "agent"
            or parent_tool in {"agent", "task"}
        ) else "task"
        item_id = _agent_process_id(parent) \
            if kind == "agent" and parent else task_id
        turn = self._remember_turn(item_id, parent)
        title = (getattr(msg, "description", None)
                 or self.item_titles.get(item_id)
                 or self.item_titles.get(task_id) or "后台任务")
        title = _short_text(title, 1000) or "后台任务"
        self.item_titles[task_id] = title
        self.item_titles[item_id] = title
        self.item_meta[task_id] = (kind, parent)
        self.item_meta[item_id] = (kind, parent)
        command = self.item_commands.get(parent or "")
        if command:
            self.item_commands[task_id] = command
            self.item_commands[item_id] = command
        if isinstance(msg, TaskStartedMessage):
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="start", status="running",
                turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.task_type, 1024),
                command=command,
                background=True,
            )]
        if isinstance(msg, TaskProgressMessage):
            return [ProcessEvent(
                item_id=item_id, kind=kind,
                phase="update", status="running", turn_id=turn,
                parent_id=parent, title=title,
                progress=_task_progress(msg.usage, msg.last_tool_name),
                command=command,
                background=True,
            )]
        if isinstance(msg, TaskUpdatedMessage):
            status = _task_status(msg.status)
            terminal = status in {"succeeded", "failed", "cancelled"}
            patch_summary = None
            if isinstance(msg.patch, dict):
                patch_summary = _short_text(
                    msg.patch.get("description") or msg.patch.get("subject"), 4096)
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="end" if terminal else "update",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=patch_summary,
                command=command,
                background=True,
            )]
        if isinstance(msg, TaskNotificationMessage):
            status = _task_status(msg.status)
            return [ProcessEvent(
                item_id=item_id, kind=kind, phase="end",
                status=status, turn_id=turn, parent_id=parent, title=title,
                summary=_short_text(msg.summary, 64 * 1024),
                progress=_task_progress(msg.usage),
                command=command,
                background=True,
            )]
        return []

    def _feed_background_tasks_changed(
        self, msg: SystemMessage,
    ) -> list[BackgroundProcessSync]:
        tasks = claude_background_tasks(msg)
        if tasks is None:
            return []
        items: list[BackgroundProcessItem] = []
        for task in tasks[:MAX_BACKGROUND_PROCESS_ITEMS]:
            task_id = _wire_id(task["task_id"], "task")
            remembered_kind, parent = self.item_meta.get(
                task_id, ("task", None))
            task_type = task.get("task_type")
            kind = "agent" if (
                _is_agent_task_type(task_type)
                or remembered_kind == "agent"
            ) else "task"
            item_id = (
                _agent_process_id(parent)
                if kind == "agent" and parent
                else task_id
            )
            description = task.get("description")
            title = (
                description if isinstance(description, str) else None
            ) or self.item_titles.get(item_id) \
                or self.item_titles.get(task_id) \
                or "后台任务"
            title = _short_text(title, 1000) or "后台任务"
            self.item_titles[task_id] = title
            self.item_titles[item_id] = title
            self.item_meta[task_id] = (kind, parent)
            self.item_meta[item_id] = (kind, parent)
            command = (
                self.item_commands.get(item_id)
                or self.item_commands.get(task_id)
                or self.item_commands.get(parent or "")
            )
            turn_id = self._remember_turn(item_id, parent)
            items.append(BackgroundProcessItem(
                item_id=item_id,
                kind=kind,
                status="running",
                turn_id=turn_id,
                parent_id=parent,
                title=title,
                command=command,
            ))
        return [BackgroundProcessSync(items=items)]

    def _feed_compaction(self, msg: SystemMessage) -> list[ProcessEvent]:
        event = _compaction_event_from_row(
            msg.data if isinstance(msg.data, dict) else {})
        if event is None:
            return []
        event.turn_id = self.turn_id
        return [event]

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
                background=self._background_for_turn(turn),
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
            background=self._background_for_turn(turn),
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
            fallback = model_fallback_event(msg.data, turn_id=self.turn_id)
            if fallback is not None:
                return [fallback]
            if msg.subtype in {"tool_progress", "bash_progress"}:
                return self._feed_progress_system(msg)
            if msg.subtype == "tool_use_summary":
                return self._feed_tool_summary(msg)
            if msg.subtype == "background_tasks_changed":
                return self._feed_background_tasks_changed(msg)
            if msg.subtype == "compact_boundary":
                return self._feed_compaction(msg)
            return []
        if isinstance(msg, ResultMessage):
            events = []
            if (not msg.is_error and not self._has_final_text
                    and self._ambiguous_final_mid is not None):
                events.append(AssistantMsgEnd(
                    message_id=self._ambiguous_final_mid,
                    turn_id=self.turn_id, channel="final"))
            for tool_id in sorted({key[0] for key in self._tool_pending}):
                events.extend(self._flush_tool_deltas(tool_id))
            terminal = TurnEnd(result=TurnResult(
                subtype=msg.subtype,
                duration_ms=msg.duration_ms,
                is_error=msg.is_error,
                total_cost_usd=msg.total_cost_usd,
                num_turns=msg.num_turns,
            ), turn_id=self._last_assistant_uuid,
                checkpoint_id=self._last_user_uuid)
            terminal._changes_turn_id = self.turn_id
            events.append(terminal)
            self._last_assistant_uuid = None
            self._last_user_uuid = None
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
    if isinstance(msg, SystemMessage):
        fallback = model_fallback_event(msg.data)
        if (fallback is not None and fallback.input
                and fallback.input.get("scope") == "session"):
            return fallback.input["fallback_model"]
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
        # The SDK deliberately preserves a relative CLAUDE_CONFIG_DIR. Resolve
        # it at the point of filesystem access so containment compares two
        # absolute paths while retaining the SDK's literal "~" semantics.
        root = str(claude_projects_dir().resolve())
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


def transcript_presence(session_id: str) -> bool | None:
    """Return exact Claude transcript presence, preserving lookup uncertainty.

    ``transcript_path`` intentionally collapses every filesystem failure into
    ``None`` for ordinary history fallbacks. Engine ownership migration cannot:
    an unreadable catalog is not proof that the same UUID belongs to Codex.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    try:
        root = claude_projects_dir().resolve()
        entries = os.scandir(root)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    scanned = 0
    try:
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        return None
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    return None
                scanned += 1
                if scanned > _MAX_TRANSCRIPT_MATCHES:
                    return None
                candidate = os.path.join(entry.path, f"{session_id}.jsonl")
                try:
                    info = os.lstat(candidate)
                except FileNotFoundError:
                    continue
                except OSError:
                    return None
                return True if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None
    return False


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


def read_claude_history_image_asset(
    source_path: str,
    image_id: str,
) -> ClaudeHistoryImageAsset | None:
    """Rehydrate one already-authorized image id from the canonical JSONL.

    Callers must first prove that ``image_id`` is referenced by the requested
    turn.  This function performs no path discovery and returns no neighboring
    image, so an opaque id cannot be used to enumerate transcript attachments.
    """
    if not isinstance(image_id, str) or not _SAFE_WIRE_ID.fullmatch(image_id):
        return None
    try:
        with open(source_path, encoding="utf-8") as source:
            for line in _bounded_jsonl_lines(source):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                message = row.get("message")
                content = (
                    message.get("content") if isinstance(message, dict) else None
                )
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        not _claude_history_tool_result_block(block)
                        or _claude_history_tool_result_error(block)
                    ):
                        continue
                    tool_id = block.get("tool_use_id")
                    if (
                        not isinstance(tool_id, str)
                        or not _SAFE_WIRE_ID.fullmatch(tool_id)
                    ):
                        continue
                    for index, image in enumerate(
                        _claude_image_blocks(block.get("content"))
                    ):
                        candidate_id = claude_history_image_id(tool_id, index)
                        if candidate_id != image_id:
                            continue
                        return ClaudeHistoryImageAsset(
                            item_id=tool_id,
                            image_id=candidate_id,
                            media_type=image["media_type"],
                            data=image["data"],
                        )
    except (OSError, UnicodeError):
        return None
    return None


@dataclass(frozen=True)
class CompactTranscriptPage:
    messages: list[SimpleNamespace]
    timestamps: dict[str, float]
    internal_events: dict[str, ProcessEvent]
    has_more: bool
    oldest_cursor: str | None


def _compact_visible_user(row: dict[str, Any]) -> bool:
    origin = row.get("origin")
    if (
        row.get("type") != "user"
        or origin == "task-notification"
        or (
            isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
        )
    ):
        return False
    message = row.get("message")
    if not isinstance(message, dict):
        return False
    role = message.get("role") or row.get("type")
    if role != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not _is_meta_user_text(content)
    if not isinstance(content, list) or _is_interrupted_user_content(content):
        return False
    return any(
        isinstance(block, dict) and (
            block.get("type") == "image"
            or (
                block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and bool(block["text"].strip())
                and not _is_meta_user_text(block["text"])
            )
        )
        for block in content
    )


def _transcript_graph_index(
    source_path: str,
    *,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
):
    """Return the bounded raw ancestry graph without retaining payloads.

    The same source-bound index backs compact pagination and the narrow delayed
    request-retry repair below. Keeping graph construction in one place avoids
    a second whole-transcript scan for ordinary production history reads.
    """
    rows: dict[str, tuple[object, ...]] = {}
    leaf: str | None = None
    queued: set[tuple[int, str]] = set()
    if index_store is not None:
        try:
            indexed = index_store.get_claude_compact_index(
                source_path,
                snapshot_size=snapshot_size,
                max_record_bytes=max_record_bytes,
                max_entries=_MAX_TRANSCRIPT_CHAIN_ENTRIES,
                visible_user=_compact_visible_user,
            )
        except Exception:
            indexed = None
        if indexed is None:
            return None
        rows = indexed.rows
        leaf = indexed.leaf
        queued = set(indexed.queued_notifications)
    else:
        try:
            with open(source_path, "rb") as source:
                stat = os.fstat(source.fileno())
                target_size = min(
                    int(stat.st_size),
                    int(snapshot_size) if snapshot_size is not None
                    else int(stat.st_size),
                )
                while source.tell() < target_size:
                    remaining = target_size - source.tell()
                    if remaining <= 0:
                        break
                    record_limit = max(1024, int(max_record_bytes))
                    offset = source.tell()
                    line = source.readline(min(remaining, record_limit + 1))
                    if not line:
                        break
                    complete = line.endswith(b"\n") \
                        or (source.tell() == target_size
                            and len(line) <= record_limit)
                    if not complete:
                        while (line and not line.endswith(b"\n")
                               and source.tell() < target_size):
                            line = source.readline(min(
                                target_size - source.tell(), record_limit + 1))
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if (row.get("type") == "queue-operation"
                            and row.get("operation") == "enqueue"):
                        content = row.get("content")
                        if (isinstance(content, str)
                                and content.lstrip().startswith(
                                    "<task-notification>")):
                            queued.add((len(content), hashlib.sha256(
                                content.encode("utf-8", "surrogatepass")
                            ).hexdigest()))
                    uid = row.get("uuid")
                    if not (isinstance(uid, str)
                            and _SAFE_WIRE_ID.fullmatch(uid)):
                        continue
                    if len(rows) >= _MAX_TRANSCRIPT_CHAIN_ENTRIES \
                            and uid not in rows:
                        return None
                    rows[uid] = (
                        row.get("type"),
                        row.get("subtype"),
                        row.get("parentUuid"),
                        row.get("logicalParentUuid"),
                        row.get("isSidechain"),
                        offset,
                        _compact_visible_user(row),
                        len(line),
                    )
                    if row.get("isSidechain") is not True:
                        leaf = uid
        except OSError:
            return None
    if not leaf:
        return None
    return leaf, rows, queued


def _ordered_graph_chain(
    leaf: str,
    rows: dict[str, tuple[object, ...]],
) -> list[str] | None:
    """Follow the engine's active ancestry, honoring compact logical parents."""
    chain_ids: list[str] = []
    seen: set[str] = set()
    cursor: str | None = leaf
    while cursor and cursor not in seen:
        seen.add(cursor)
        metadata = rows.get(cursor)
        if metadata is None:
            return None
        chain_ids.append(cursor)
        row_type, subtype, parent_uuid, logical_parent_uuid = metadata[:4]
        parent = (
            logical_parent_uuid
            if row_type == "system" and subtype == "compact_boundary"
            else parent_uuid
        )
        if not isinstance(parent, str) or not _SAFE_WIRE_ID.fullmatch(parent):
            parent = None
        cursor = parent
    if cursor is not None:
        return None
    return list(reversed(chain_ids))


def _compact_chain_index(
    source_path: str,
    *,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
):
    """Return compact main-chain ids plus bounded graph metadata."""
    graph = _transcript_graph_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if graph is None:
        return None
    leaf, rows, queued = graph
    chain_ids = _ordered_graph_chain(leaf, rows)
    if chain_ids is None:
        return None

    if not any(
        rows[uid][0] == "system" and rows[uid][1] == "compact_boundary"
        for uid in chain_ids
    ):
        return None
    return chain_ids, rows, queued


def _indexed_transcript_row(
    source,
    uid: str,
    metadata: tuple[object, ...],
    *,
    max_record_bytes: int,
) -> dict[str, Any] | None:
    """Seek and validate one source-bound graph row."""
    offset = metadata[5]
    record_bytes = metadata[7]
    if (
        not isinstance(offset, int)
        or not isinstance(record_bytes, int)
        or record_bytes <= 0
        or record_bytes > max(1024, int(max_record_bytes))
    ):
        return None
    try:
        source.seek(offset)
        raw = source.read(record_bytes)
        if len(raw) != record_bytes:
            return None
        row = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or row.get("uuid") != uid:
        return None
    return row


def _transcript_epoch(row: dict[str, Any]) -> float | None:
    value = row.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00") if value.endswith("Z") else value
        ).timestamp()
    except (ValueError, OverflowError):
        return None


def _tool_result_user_row(row: dict[str, Any]) -> bool:
    message = row.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    kinds = [
        block.get("type")
        for block in content
        if isinstance(block, dict)
    ]
    return bool(kinds) and len(kinds) == len(content) and all(
        kind == "tool_result"
        or isinstance(kind, str) and kind.endswith("_tool_result")
        for kind in kinds
    )


def _delayed_retry_tail(
    source,
    retry_uid: str,
    retry_index: int,
    active_chain: list[str],
    canonical_ids: set[str],
    rows: dict[str, tuple[object, ...]],
    children: dict[str, list[str]],
    *,
    max_record_bytes: int,
) -> tuple[str, list[SimpleNamespace], dict[str, float]] | None:
    """Return one unambiguous completed sibling bypassed by a delayed retry."""
    retry_metadata = rows[retry_uid]
    retry_parent = retry_metadata[2]
    retry_offset = retry_metadata[5]
    if (
        not isinstance(retry_parent, str)
        or retry_parent not in canonical_ids
        or not isinstance(retry_offset, int)
    ):
        return None
    retry_row = _indexed_transcript_row(
        source, retry_uid, retry_metadata,
        max_record_bytes=max_record_bytes,
    )
    if retry_row is None or (
        retry_row.get("source") != "request_retry"
        or not isinstance(retry_row.get("retryAttempt"), int)
        or retry_row["retryAttempt"] < 1
        or not isinstance(retry_row.get("maxRetries"), int)
        or retry_row["maxRetries"] < retry_row["retryAttempt"]
    ):
        return None
    retry_epoch = _transcript_epoch(retry_row)
    if retry_epoch is None:
        return None

    # The later prompt must actually continue through this retry node. Without
    # that proof this may be an ordinary abandoned API-error branch.
    next_user_uid = next((
        uid for uid in active_chain[retry_index + 1:]
        if bool(rows[uid][6]) and uid in canonical_ids
    ), None)
    if next_user_uid is None:
        return None
    next_user_metadata = rows[next_user_uid]
    next_user_offset = next_user_metadata[5]
    if not isinstance(next_user_offset, int) or next_user_offset <= retry_offset:
        return None
    next_user_row = _indexed_transcript_row(
        source, next_user_uid, next_user_metadata,
        max_record_bytes=max_record_bytes,
    )
    if next_user_row is None:
        return None

    active_ids = set(active_chain)
    alternatives = [
        uid for uid in children.get(retry_parent, ())
        if uid != retry_uid
        and uid not in active_ids
        and rows[uid][4] is not True
        and isinstance(rows[uid][5], int)
        and rows[uid][5] < retry_offset
    ]
    # Competing siblings are a real fork. Never guess which answer to revive.
    if len(alternatives) != 1:
        return None

    cursor = alternatives[0]
    visited: set[str] = set()
    path_rows: list[dict[str, Any]] = []
    path_meta: list[tuple[object, ...]] = []
    total_bytes = 0
    while cursor not in visited:
        visited.add(cursor)
        if len(visited) > _MAX_DELAYED_RETRY_TAIL_ROWS:
            return None
        metadata = rows.get(cursor)
        if (
            metadata is None
            or metadata[4] is True
            or cursor in active_ids
            or bool(metadata[6])
            or not isinstance(metadata[5], int)
            or metadata[5] >= retry_offset
            or not isinstance(metadata[7], int)
        ):
            return None
        total_bytes += metadata[7]
        if total_bytes > _MAX_DELAYED_RETRY_TAIL_BYTES:
            return None
        row = _indexed_transcript_row(
            source, cursor, metadata,
            max_record_bytes=max_record_bytes,
        )
        if row is None:
            return None
        row_type = metadata[0]
        if row_type == "user":
            if not _tool_result_user_row(row):
                return None
        elif row_type == "assistant":
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            stop_reason = message.get("stop_reason")
            if stop_reason not in {None, "tool_use", "end_turn"}:
                return None
        elif row_type != "attachment":
            # Compact/system/error/task rows change conversation semantics and
            # are never presentation-only completion tails.
            return None
        path_rows.append(row)
        path_meta.append(metadata)

        successors = [
            uid for uid in children.get(cursor, ())
            if uid not in active_ids
            and rows[uid][4] is not True
            and isinstance(rows[uid][5], int)
            and rows[uid][5] < retry_offset
        ]
        if not successors:
            break
        if len(successors) != 1:
            return None
        cursor = successors[0]
    else:
        return None

    terminal_row = path_rows[-1] if path_rows else None
    terminal_message = (
        terminal_row.get("message")
        if isinstance(terminal_row, dict) else None
    )
    if (
        not isinstance(terminal_message, dict)
        or terminal_row.get("type") != "assistant"
        or terminal_message.get("role") != "assistant"
        or terminal_message.get("stop_reason") != "end_turn"
    ):
        return None
    terminal_epoch = _transcript_epoch(terminal_row)
    next_user_epoch = _transcript_epoch(next_user_row)
    if (
        terminal_epoch is None
        or next_user_epoch is None
        or not retry_epoch < terminal_epoch < next_user_epoch
        or path_meta[-1][5] >= retry_offset
    ):
        return None

    messages: list[SimpleNamespace] = []
    recovered_timestamps: dict[str, float] = {}
    for row in path_rows:
        row_type = row.get("type")
        message = row.get("message")
        if row_type not in {"user", "assistant"} or not isinstance(message, dict):
            continue
        messages.append(SimpleNamespace(
            type=row_type,
            uuid=row["uuid"],
            session_id=(
                row.get("sessionId")
                if isinstance(row.get("sessionId"), str) else None
            ),
            message=message,
            parent_tool_use_id=(
                row.get("parentToolUseID")
                or row.get("parent_tool_use_id")
            ),
        ))
        epoch = _transcript_epoch(row)
        if epoch is not None:
            recovered_timestamps[row["uuid"]] = epoch
    if not messages or messages[-1].uuid != terminal_row["uuid"]:
        return None
    return retry_parent, messages, recovered_timestamps


def recover_claude_delayed_retry_tail(
    session_id: str,
    messages,
    *,
    path: str | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
    timestamps: dict[str, float] | None = None,
) -> list:
    """Restore only completed tails bypassed by delayed request-retry records.

    Claude's supported session reader follows the newest ``parentUuid`` chain.
    A network retry can be appended much later with an older timestamp and make
    the next human prompt branch from the middle of an already completed turn.
    The raw answer remains on disk but disappears from that projection. This
    repair is intentionally narrower than general branch recovery: physical
    order, engine timestamps, retry provenance, a later human prompt and one
    linear successful sibling must all agree, otherwise the SDK result wins.
    """
    canonical = list(messages or ())
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return canonical
    source_path = path or transcript_path(session_id)
    if not source_path:
        return canonical
    graph = _transcript_graph_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if graph is None:
        return canonical
    leaf, rows, _queued = graph
    active_chain = _ordered_graph_chain(leaf, rows)
    if active_chain is None:
        return canonical
    canonical_ids = {
        uid for message in canonical
        if isinstance((uid := getattr(message, "uuid", None)), str)
    }
    if not canonical_ids:
        return canonical
    children: dict[str, list[str]] = {}
    for uid, metadata in rows.items():
        parent = metadata[2]
        if isinstance(parent, str):
            children.setdefault(parent, []).append(uid)
    for siblings in children.values():
        siblings.sort(key=lambda uid: (
            rows[uid][5] if isinstance(rows[uid][5], int) else -1))

    insertions: dict[
        str, tuple[list[SimpleNamespace], dict[str, float]]
    ] = {}
    recovered_ids: set[str] = set()
    try:
        with open(source_path, "rb") as source:
            for index, uid in enumerate(active_chain):
                metadata = rows[uid]
                if metadata[0] != "system" or metadata[1] != "api_error":
                    continue
                recovered = _delayed_retry_tail(
                    source,
                    uid,
                    index,
                    active_chain,
                    canonical_ids,
                    rows,
                    children,
                    max_record_bytes=max_record_bytes,
                )
                if recovered is None:
                    continue
                parent_uid, tail, recovered_timestamps = recovered
                if parent_uid in insertions or any(
                    item.uuid in canonical_ids or item.uuid in recovered_ids
                    for item in tail
                ):
                    continue
                insertions[parent_uid] = (tail, recovered_timestamps)
                recovered_ids.update(item.uuid for item in tail)
    except OSError:
        return canonical
    if not insertions:
        return canonical

    output: list = []
    for message in canonical:
        output.append(message)
        uid = getattr(message, "uuid", None)
        insertion = insertions.get(uid)
        if insertion:
            tail, recovered_timestamps = insertion
            output.extend(tail)
            if timestamps is not None:
                timestamps.update(recovered_timestamps)
    return output


def recover_claude_native_metadata(
    session_id: str,
    messages: list,
    *,
    path: str | None,
    timestamps: dict[str, float],
    internal_events: dict[str, ProcessEvent],
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> list:
    """Restore model notes and native tool results on the active ancestry only.

    The SDK catalog omits these rows. These positional shells are solely a UI
    projection, never a prompt sent back to Claude. Exact native tool results
    additionally prove historical Edit/Write patches. Page boundaries, rewinds
    and sidechains must not resurrect unrelated metadata.
    """
    if not path or not messages:
        return messages
    graph = _transcript_graph_index(
        path, index_store=index_store, snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes)
    if graph is None:
        return messages
    leaf, rows, _ = graph
    chain = _ordered_graph_chain(leaf, rows)
    if chain is None:
        return messages
    visible = {getattr(message, "uuid", None) for message in messages}
    by_id = {getattr(message, "uuid", None): message for message in messages}
    insertions: dict[str, list[SimpleNamespace]] = {}
    anchor = None
    restored = 0
    try:
        with open(path, "rb") as source:
            for uid in chain:
                metadata = rows[uid]
                if metadata[0] in {"user", "assistant"} and uid not in visible:
                    anchor = None
                if uid in visible:
                    anchor = uid
                    if metadata[0] == "user":
                        row = _indexed_transcript_row(
                            source, uid, metadata, max_record_bytes=max_record_bytes)
                        result = row.get("toolUseResult") if row else None
                        if isinstance(result, dict):
                            by_id[uid].tool_use_result = result
                if metadata[0:2] != ("system", FALLBACK_TOOL):
                    continue
                if anchor is None or restored >= 64:
                    continue
                row = _indexed_transcript_row(
                    source, uid, metadata, max_record_bytes=max_record_bytes)
                event = model_fallback_event(row)
                if event is None or row is None:
                    continue
                internal_events[uid] = event
                stamp = _transcript_epoch(row)
                if stamp is not None:
                    timestamps[uid] = stamp
                if uid not in visible:
                    insertions.setdefault(anchor, []).append(SimpleNamespace(
                        type="system", uuid=uid, session_id=session_id,
                        message={"role": "system", "content": ""},
                        parent_tool_use_id=None,
                    ))
                restored += 1
    except OSError:
        return messages
    output = []
    for message in messages:
        output.append(message)
        output.extend(insertions.get(getattr(message, "uuid", None), ()))
    return output


def _load_compact_chain_messages(
    session_id: str,
    source_path: str,
    chain_ids: list[str],
    rows: dict[str, tuple[object, ...]],
    queued: set[tuple[int, str]],
    *,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> tuple[
    list[SimpleNamespace], dict[str, float], dict[str, ProcessEvent]
] | None:
    messages: list[SimpleNamespace] = []
    timestamps: dict[str, float] = {}
    internal_events: dict[str, ProcessEvent] = {}
    try:
        with open(source_path, "rb") as source:
            for uid in chain_ids:
                metadata = rows.get(uid)
                if metadata is None or not isinstance(metadata[5], int):
                    return None
                source.seek(metadata[5])
                record_limit = max(1024, int(max_record_bytes))
                line = source.readline(record_limit + 1)
                if (not line or len(line) > record_limit
                        and not line.endswith(b"\n")):
                    return None
                try:
                    row = json.loads(line)
                except Exception:
                    return None
                if row.get("uuid") != uid:
                    return None
                internal = _internal_user_event_from_row(row, queued)
                if internal is None:
                    internal = _compaction_event_from_row(row)
                if internal is None:
                    internal = model_fallback_event(row)
                if internal is not None:
                    internal_events[uid] = internal
                row_type = row.get("type")
                message = row.get("message")
                if row_type in {"user", "assistant"} \
                        and isinstance(message, dict):
                    messages.append(SimpleNamespace(
                        type=row_type,
                        uuid=uid,
                        session_id=session_id,
                        message=message,
                        parent_tool_use_id=(
                            row.get("parentToolUseID")
                            or row.get("parent_tool_use_id")
                        ),
                        tool_use_result=row.get("toolUseResult"),
                    ))
                elif row_type == "system" and internal is not None:
                    # The SDK projection drops system rows. Preserve a tiny
                    # positional shell so translate_history can place the
                    # existing compaction ProcessEvent at the true boundary.
                    messages.append(SimpleNamespace(
                        type="system",
                        subtype=row.get("subtype"),
                        uuid=uid,
                        session_id=session_id,
                        message={
                            "role": "system",
                            "content": "",
                        },
                        parent_tool_use_id=None,
                    ))
                timestamp = row.get("timestamp")
                if not isinstance(timestamp, str):
                    continue
                try:
                    timestamps[uid] = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                        if timestamp.endswith("Z") else timestamp
                    ).timestamp()
                except Exception:
                    pass
    except OSError:
        return None
    return messages, timestamps, internal_events


def transcript_timestamps(
    session_id: str,
    *,
    path: str | None = None,
) -> dict[str, float]:
    """Map each transcript entry's uuid -> epoch seconds, read straight from the
    .jsonl. The SDK's SessionMessage drops the per-message timestamp, so without
    this, history events default their `ts` to now (making every past message show
    the current time — "like a clock"). Best-effort: {} if not found/readable.
    session_id is globally unique, so a glob across all project dirs locates it."""
    out: dict[str, float] = {}
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return out
    try:
        source_path = path or transcript_path(session_id)
        if not source_path:
            return out
        with open(source_path) as f:
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


def transcript_compact_main_chain(
    session_id: str,
    *,
    path: str | None = None,
) -> tuple[list[SimpleNamespace], dict[str, float]] | None:
    """Recover the active Claude message chain across compact boundaries.

    Claude Agent SDK's ``get_session_messages`` deliberately starts at the
    compact summary. The raw transcript retains the prior active ancestry and
    links it through ``system/compact_boundary.logicalParentUuid``. Follow that
    graph instead of replaying JSONL file order (which can contain abandoned
    branches), and return ``None`` when the active chain has no compact marker
    so ordinary sessions keep using the SDK's supported projection.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(source_path)
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    loaded = _load_compact_chain_messages(
        session_id, source_path, ordered_ids, rows, queued)
    if loaded is None or not loaded[0]:
        return None
    return loaded[0], loaded[1]


def transcript_compact_snapshot(
    session_id: str,
    *,
    path: str | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> tuple[
    list[SimpleNamespace], dict[str, float], dict[str, ProcessEvent]
] | None:
    """Load the compact active chain plus indexed internal task events."""
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    loaded = _load_compact_chain_messages(
        session_id, source_path, ordered_ids, rows, queued,
        max_record_bytes=max_record_bytes,
    )
    if loaded is None or not loaded[0]:
        return None
    return loaded


def transcript_compact_history_page(
    session_id: str,
    *,
    path: str | None = None,
    before: str | None = None,
    limit: int = 60,
    max_payload_bytes: int | None = None,
    index_store=None,
    snapshot_size: int | None = None,
    max_record_bytes: int = _MAX_TRANSCRIPT_RECORD_CHARS,
) -> CompactTranscriptPage | None:
    """Load one visible turn page from a compacted Claude transcript.

    The graph pass is payload-light and capped; the payload pass seeks only to
    main-chain rows belonging to this page. This is the safe oversized-source
    path used when the general SDK projection would exceed the configured
    whole-transcript limit.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return None
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    indexed = _compact_chain_index(
        source_path,
        index_store=index_store,
        snapshot_size=snapshot_size,
        max_record_bytes=max_record_bytes,
    )
    if indexed is None:
        return None
    ordered_ids, rows, queued = indexed
    visible = [
        (index, uid)
        for index, uid in enumerate(ordered_ids)
        if bool(rows[uid][6])
    ]
    if not visible:
        return None
    end = len(visible)
    if before is not None:
        found = next(
            (index for index, (_, uid) in enumerate(visible)
             if uid == before),
            None,
        )
        if found is None:
            return None
        end = found
    bounded_limit = max(1, min(200, int(limit)))
    start = max(0, end - bounded_limit)
    chain_end = visible[end][0] if end < len(visible) else len(ordered_ids)
    payload_prefix = [0]
    for uid in ordered_ids:
        metadata = rows[uid]
        payload_prefix.append(payload_prefix[-1] + (
            int(metadata[7]) if metadata[0] in {"user", "assistant"} else 0
        ))
    while True:
        chain_start = 0 if start == 0 else visible[start][0]
        payload_bytes = payload_prefix[chain_end] - payload_prefix[chain_start]
        if max_payload_bytes is None or payload_bytes <= max_payload_bytes:
            break
        if end - start <= 1:
            return None
        start += 1
    selected_ids = ordered_ids[chain_start:chain_end]
    loaded = _load_compact_chain_messages(
        session_id, source_path, selected_ids, rows, queued,
        max_record_bytes=max_record_bytes)
    if loaded is None:
        return None
    messages, timestamps, internal_events = loaded
    return CompactTranscriptPage(
        messages=messages,
        timestamps=timestamps,
        internal_events=internal_events,
        has_more=start > 0,
        oldest_cursor=visible[start][1] if start < end else None,
    )


def _notification_tag(text: str, name: str, limit: int) -> str | None:
    start_token = f"<{name}>"
    end_token = f"</{name}>"
    start = text.find(start_token)
    if start < 0:
        return None
    start += len(start_token)
    end = text.find(end_token, start)
    if end < 0:
        return None
    return _short_text(text[start:end].strip(), limit)


def _compaction_event_from_row(row: dict[str, Any]) -> ProcessEvent | None:
    uid = row.get("uuid")
    if not (
        row.get("type") == "system"
        and row.get("subtype") == "compact_boundary"
        and isinstance(uid, str)
        and _SAFE_WIRE_ID.fullmatch(uid)
    ):
        return None
    metadata = row.get("compactMetadata")
    if not isinstance(metadata, dict):
        metadata = {}
    trigger = metadata.get("trigger")
    pre_tokens = metadata.get("preTokens")
    post_tokens = metadata.get("postTokens")
    summary_bits: list[str] = []
    if trigger == "auto":
        summary_bits.append("自动压缩")
    elif trigger == "manual":
        summary_bits.append("手动压缩")
    if all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (pre_tokens, post_tokens)
    ):
        summary_bits.append(f"{pre_tokens:,} → {post_tokens:,} tokens")
    duration = metadata.get("durationMs")
    duration_ms = (
        duration
        if isinstance(duration, int) and not isinstance(duration, bool)
        and duration >= 0
        else None
    )
    event = ProcessEvent(
        item_id=uid,
        kind="compaction",
        phase="end",
        status="succeeded",
        title="压缩上下文",
        summary=" · ".join(summary_bits) or None,
        duration_ms=duration_ms,
    )
    timestamp = _parse_timestamp(row.get("timestamp"))
    if timestamp is not None:
        event.ts = timestamp
    return event


def _internal_user_event_from_row(
    row: dict[str, Any],
    queued: set[tuple[int, str]] | frozenset[tuple[int, str]],
) -> ProcessEvent | None:
    origin = row.get("origin")
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    uid = row.get("uuid")
    if not (row.get("type") == "user"
            and isinstance(origin, dict)
            and origin.get("kind") == "task-notification"
            and isinstance(content, str)
            and isinstance(uid, str)
            and _SAFE_WIRE_ID.fullmatch(uid)):
        return None
    fingerprint = (len(content), hashlib.sha256(
        content.encode("utf-8", "surrogatepass")
    ).hexdigest())
    if fingerprint not in queued:
        return None
    task_id = _notification_tag(content, "task-id", 128)
    tool_id = _notification_tag(content, "tool-use-id", 128)
    if not task_id or not _SAFE_WIRE_ID.fullmatch(task_id):
        return None
    if tool_id and not _SAFE_WIRE_ID.fullmatch(tool_id):
        tool_id = None
    raw_status = _notification_tag(content, "status", 64)
    summary = _notification_tag(content, "summary", 1000)
    usage: dict[str, int] = {}
    for tag in ("tool_uses", "total_tokens", "duration_ms"):
        value = _notification_tag(content, tag, 32)
        if value and value.isdigit():
            usage[tag] = int(value)
    status = _task_status(raw_status)
    return ProcessEvent(
        # tool-use-id is correlation, not proof of an Agent. translate_history
        # has the preceding ToolUse name and promotes this exact event only when
        # its parent is an Agent/Task tool.
        item_id=task_id,
        kind="task",
        phase="end",
        status=status,
        parent_id=tool_id,
        title="后台任务",
        summary=summary,
        progress=_task_progress(usage),
        duration_ms=usage.get("duration_ms"),
        background=True,
    )


def transcript_internal_user_events(
    session_id: str,
    *,
    path: str | None = None,
) -> dict[str, ProcessEvent]:
    """Recover structured Claude-internal user rows from raw transcript proof.

    ``get_session_messages`` drops ``origin`` and queue-operation records.  We
    therefore classify a task notification only when the raw JSONL contains
    both Claude's enqueue record and a user row whose authoritative origin is
    ``task-notification`` with the exact same content.  XML-looking human text
    remains ordinary conversation content.
    """
    if not _SAFE_SESSION_ID.fullmatch(session_id):
        return {}
    source_path = path or transcript_path(session_id)
    if not source_path:
        return {}
    queued: set[tuple[int, str]] = set()
    events: dict[str, ProcessEvent] = {}
    try:
        with open(source_path, encoding="utf-8") as source:
            for line in _bounded_jsonl_lines(source):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("type") == "queue-operation" \
                        and row.get("operation") == "enqueue":
                    content = row.get("content")
                    if (isinstance(content, str)
                            and content.lstrip().startswith("<task-notification>")):
                        queued.add((len(content), hashlib.sha256(
                            content.encode("utf-8", "surrogatepass")
                        ).hexdigest()))
                    continue
                uid = row.get("uuid")
                event = _internal_user_event_from_row(row, queued)
                if event is None or not isinstance(uid, str):
                    continue
                events[uid] = event
                if len(events) >= _MAX_INTERNAL_USER_EVENTS:
                    break
    except (OSError, UnicodeError):
        return {}
    return events


def translate_history(
    messages,
    tool_result_max: int,
    timestamps: dict | None = None,
    internal_user_events: dict[str, ProcessEvent] | None = None,
    *,
    client_message_ids: Mapping[str, str] | None = None,
    snapshot_in_progress: bool = False,
) -> list:
    """Translate a session's on-disk transcript (list[SessionMessage]) into wire
    events the client reducer renders as past turns.

    The transcript carries no ResultMessage, so synthetic TurnEnd frames delimit
    turns. `timestamps` (uuid -> epoch seconds, from transcript_timestamps) stamps
    each UserMsg with its real ask-time and each TurnEnd with the turn's last
    conversational message time (answer-done time) — otherwise history shows
    "now". Late internal task bookkeeping remains visible in the process view,
    but cannot move an already-settled answer's terminal clock. Rich
    assistant blocks retain the same thinking/commentary/final and semantic tool
    structure as the live stream. Non-conversational user turns (compact summaries,
    slash-command envelopes, local-command stdout) remain hidden.
    """
    events: list = []
    turn_open = False
    last_ts = None  # transcript ts of the most-recent message in the open turn
    turn_start_ts = None  # timestamp of the visible human message
    last_assistant_uuid = None
    current_turn_id = None
    history_tool_diffs: dict[str, tuple[str, bool]] = {}
    history_tool_names: dict[str, str] = {}
    history_image_reads: dict[str, tuple[str, str | None]] = {}
    history_plan_id: str | None = None
    ambiguous_final_mid: str | None = None
    ambiguous_final_start: int | None = None
    settled_answer_seen = False
    background_followup = False
    turn_failed = False
    # Older wrappers could bind a replacement query's browser id to Claude's
    # late ``[Request interrupted by user]`` record.  The marker terminates the
    # preceding turn; transfer that proven-but-misplaced alias only when the
    # immediately following canonical message is the replacement's visible
    # top-level user row.  Any intervening row cancels the repair, preventing an
    # alias from crossing tool protocol, model output, metadata, or a branch.
    pending_interrupted_alias: tuple[int, str] | None = None

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

    def _history_image_result_event(
        tool_id: str,
        image_read: tuple[str, str | None],
        raw_content: Any,
        *,
        is_error: bool,
        source_uid: str | None = None,
    ) -> ProcessEvent:
        image_path, image_parent = image_read
        tool_name = history_tool_names.get(tool_id) or "Read"
        if not is_error and _claude_image_blocks(raw_content):
            event = ProcessEvent(
                item_id=tool_id,
                kind="server_tool",
                phase="end",
                status="succeeded",
                turn_id=current_turn_id,
                parent_id=image_parent,
                title="查看图片",
                input={"file_path": image_path},
                tool="view_image",
                background=background_followup or None,
            )
        else:
            output, was_truncated = bounded_text(
                _safe_image_read_result_content(tool_name, raw_content),
                tool_result_max,
            )
            _category, fallback_title, _server = _tool_meta(
                tool_name, {"file_path": image_path})
            event = ProcessEvent(
                item_id=tool_id,
                kind="server_tool",
                phase="end",
                status="failed" if is_error else "succeeded",
                turn_id=current_turn_id,
                parent_id=image_parent,
                title=fallback_title,
                input={"file_path": image_path},
                output=output or None,
                tool=tool_name,
                truncated=was_truncated or None,
                background=background_followup or None,
            )
        timestamp = _ts(source_uid) if source_uid is not None else None
        if timestamp is not None:
            event.ts = timestamp
        return event

    client_message_ids = client_message_ids or {}

    def _um(uid, prompt):
        nonlocal pending_interrupted_alias
        client_msg_id = client_message_ids.get(uid)
        if (
            client_msg_id is None
            and pending_interrupted_alias is not None
            and pending_interrupted_alias[0] == message_index
        ):
            client_msg_id = pending_interrupted_alias[1]
        pending_interrupted_alias = None
        um = UserMsg(
            msg_id=uid,
            client_msg_id=client_msg_id,
            prompt=prompt,
        )
        t = _ts(uid)
        if t is not None:
            um.ts = t   # question time, not load time
        return um

    def close_turn(
        subtype: str | None = None,
        is_error: bool | None = None,
    ):
        nonlocal turn_open, last_assistant_uuid, current_turn_id, history_plan_id
        nonlocal ambiguous_final_mid, ambiguous_final_start
        nonlocal turn_start_ts, settled_answer_seen, turn_failed
        nonlocal background_followup
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
            duration_ms = 0
            if turn_start_ts is not None and last_ts is not None:
                duration_ms = max(
                    0, round((last_ts - turn_start_ts) * 1000))
            te = TurnEnd(
                result=TurnResult(
                    subtype=(
                        subtype
                        or ("error" if turn_failed else "success")
                    ),
                    duration_ms=duration_ms,
                    is_error=(
                        is_error
                        if is_error is not None else turn_failed
                    ),
                ),
                turn_id=last_assistant_uuid,
                checkpoint_id=current_turn_id,
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
            turn_start_ts = None
            settled_answer_seen = False
            background_followup = False
            turn_failed = False

    for message_index, m in enumerate(messages):
        advance_terminal_clock = True
        if (
            pending_interrupted_alias is not None
            and pending_interrupted_alias[0] < message_index
        ):
            pending_interrupted_alias = None
        msg = m.message
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or m.type
        content = msg.get("content")
        source_uid = m.uuid if isinstance(m.uuid, str) else ""
        message_uid = _history_id(source_uid, "msg", str(message_index))

        if role == "user":
            if isinstance(content, str):
                internal_event = (internal_user_events or {}).get(source_uid)
                if internal_event is not None:
                    # Claude may append a task-notification hours after an
                    # ``end_turn`` while cold-resuming old background work.
                    # Keep the lifecycle update in detail, but the already
                    # completed answer remains the turn's terminal boundary.
                    if settled_answer_seen or ambiguous_final_mid is not None:
                        advance_terminal_clock = False
                        background_followup = True
                    event = internal_event.model_copy(deep=True)
                    parent_name = (
                        history_tool_names.get(event.parent_id or "") or ""
                    ).lower()
                    if parent_name in {"agent", "task"} and event.parent_id:
                        event.item_id = _agent_process_id(event.parent_id)
                        event.kind = "agent"
                        event.title = event.summary or "协作代理"
                        event.background = True
                    event.turn_id = event.turn_id or current_turn_id
                    t = _ts(source_uid)
                    if t is not None:
                        event.ts = t
                    events.append(event)
                    turn_open = True
                elif _is_meta_user_text(content):
                    continue
                else:
                    close_turn()
                    turn_start_ts = _ts(source_uid)
                    events.append(_um(message_uid, content))
                    turn_open = True
                    current_turn_id = message_uid
                    background_followup = False
            elif isinstance(content, list):
                if _is_interrupted_user_content(content):
                    misplaced_alias = client_message_ids.get(message_uid)
                    pending_interrupted_alias = (
                        (message_index + 1, misplaced_alias)
                        if misplaced_alias is not None else None
                    )
                    marker_ts = _ts(source_uid)
                    if marker_ts is not None:
                        last_ts = marker_ts
                    close_turn("interrupted", False)
                    continue
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
                    if _claude_history_tool_result_block(b):
                        ambiguous_final_mid = None
                        ambiguous_final_start = None
                        tool_id = _history_id(
                            b.get("tool_use_id"), "tool",
                            f"{message_index}-{block_index}-result")
                        raw_result_content = b.get("content")
                        image_read = history_image_reads.pop(tool_id, None)
                        is_error = _claude_history_tool_result_error(b)
                        if image_read is not None:
                            events.append(_history_image_result_event(
                                tool_id,
                                image_read,
                                raw_result_content,
                                is_error=is_error,
                                source_uid=source_uid,
                            ))
                            continue
                        text, was_truncated = bounded_text(
                            _safe_result_content(
                                history_tool_names.get(tool_id), raw_result_content),
                            tool_result_max)
                        diff_info = history_tool_diffs.pop(tool_id, None)
                        native_diff = (native_claude_diff(getattr(msg, "tool_use_result", None))
                                       if not is_error else None)
                        truncated = bool(
                            was_truncated or (diff_info and diff_info[1])) or None
                        agent_result = (history_tool_names.get(tool_id) or "").lower() in {
                            "agent", "task"}
                        result_text = (
                            raw_result_content
                            if isinstance(raw_result_content, str) else ""
                        )
                        async_launched = agent_result and (
                            "async agent launched" in result_text.lower()
                        )
                        events.append(ToolResult(
                            tool_use_id=tool_id,
                            content=text,
                            is_error=is_error,
                            truncated=truncated,
                            status=("failed" if is_error else
                                    "running" if async_launched else "succeeded"),
                            diff=native_diff or (diff_info[0] if diff_info and not is_error else None),
                            diff_source="native" if native_diff else "fragment",
                            diff_truncated=False if native_diff else bool(diff_info and diff_info[1]),
                        ))
                        if agent_result:
                            events.append(ProcessEvent(
                                item_id=_agent_process_id(tool_id), kind="agent",
                                phase=("update" if async_launched else "end"),
                                status=("failed" if is_error else
                                        "running" if async_launched else "succeeded"),
                                turn_id=current_turn_id, parent_id=tool_id,
                                title="协作代理", background=True,
                            ))
                    elif bt == "text":
                        txt = b.get("text", "")
                        if txt and not _is_meta_user_text(txt):
                            close_turn()
                            turn_start_ts = _ts(source_uid)
                            um = _um(message_uid, txt)
                            if imgs and not made:
                                um.images = imgs
                                made = True
                            events.append(um)
                            turn_open = True
                            current_turn_id = message_uid
                            background_followup = False
                if imgs and not made:   # image-only user turn
                    close_turn()
                    turn_start_ts = _ts(source_uid)
                    um = _um(message_uid, "")
                    um.images = imgs
                    events.append(um)
                    turn_open = True
                    current_turn_id = message_uid
                    background_followup = False
        elif role == "system":
            internal_event = (internal_user_events or {}).get(source_uid)
            if internal_event is not None:
                event = internal_event.model_copy(deep=True)
                event.turn_id = event.turn_id or current_turn_id
                timestamp = _ts(source_uid)
                if timestamp is not None:
                    event.ts = timestamp
                events.append(event)
                turn_open = True
        elif role == "assistant":
            if not isinstance(content, list):
                continue
            if _is_synthetic_no_response(msg):
                continue
            pending_interrupted_alias = None
            if _is_synthetic_api_error(msg):
                turn_failed = True
            if _CLAUDE_MESSAGE_UUID.fullmatch(source_uid):
                last_assistant_uuid = source_uid
            assistant_event_start = len(events)
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
            # Any later assistant activity re-opens the conversational tail.
            # A top-level end_turn text settles it again; nested Agent answers
            # and tool-bearing messages are not enclosing-turn boundaries.
            settled_answer_seen = (
                parent is None
                and not has_tool_activity
                and text_channel == "final"
                and has_text
                and msg.get("stop_reason") == "end_turn"
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
                            message_id=mid, channel=text_channel,
                            background=background_followup or None))
                        text_started = True
                    if txt:
                        events.append(Delta(
                            message_id=mid, text=txt, channel=text_channel,
                            background=background_followup or None))
                elif bt == "thinking":
                    thinking = b.get("thinking", "")
                    if not thinking_started:
                        events.append(AssistantMsgStart(
                            message_id=thinking_mid, channel="thinking",
                            background=background_followup or None))
                        thinking_started = True
                    if isinstance(thinking, str) and thinking:
                        safe_thinking, _ = bounded_text(thinking, tool_result_max)
                        if safe_thinking:
                            events.append(Delta(
                                message_id=thinking_mid, text=safe_thinking,
                                channel="thinking",
                                background=background_followup or None))
                elif bt in {"tool_use", "server_tool_use"}:
                    if not text_started:
                        events.append(AssistantMsgStart(
                            message_id=mid, channel="commentary",
                            background=background_followup or None))
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
                    public_input = _public_tool_input(
                        b.get("name") or "", raw_input)
                    category, title, server = _tool_meta(
                        b.get("name") or "", redacted_input,
                        server_tool=server_tool)
                    image_path = _claude_image_read_path(
                        b.get("name"), raw_input)
                    if image_path is not None:
                        history_image_reads[tool_id] = (image_path, parent)
                        events.append(ProcessEvent(
                            item_id=tool_id,
                            kind="server_tool",
                            phase="start",
                            status="running",
                            turn_id=current_turn_id,
                            parent_id=parent,
                            title="查看图片",
                            input={"file_path": image_path},
                            tool="view_image",
                            background=background_followup or None,
                        ))
                    else:
                        events.append(ToolUse(
                            message_id=mid,
                            tool_use_id=tool_id,
                            tool=b.get("name") or "",
                            input=bounded_tool_input(
                                public_input, tool_result_max),
                            category=category, title=title, parent_id=parent,
                            server=server,
                            background=background_followup or None,
                        ))
                    history_tool_names[tool_id] = b.get("name") or ""
                    if image_path is None and category == "agent":
                        events.append(ProcessEvent(
                            item_id=_agent_process_id(tool_id), kind="agent",
                            phase="start", status="running",
                            turn_id=current_turn_id, parent_id=tool_id,
                            title=title, background=True,
                        ))
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
                elif _claude_history_tool_result_block(b):
                    tool_id = _history_id(
                        b.get("tool_use_id"), "tool",
                        f"{message_index}-{block_index}-assistant-result")
                    raw_result_content = b.get("content")
                    image_read = history_image_reads.pop(tool_id, None)
                    is_error = _claude_history_tool_result_error(b)
                    if image_read is not None:
                        events.append(_history_image_result_event(
                            tool_id,
                            image_read,
                            raw_result_content,
                            is_error=is_error,
                            source_uid=source_uid,
                        ))
                        continue
                    text, was_truncated = bounded_text(
                        _safe_result_content(
                            history_tool_names.get(tool_id), raw_result_content),
                        tool_result_max)
                    diff_info = history_tool_diffs.pop(tool_id, None)
                    agent_result = (history_tool_names.get(tool_id) or "").lower() in {
                        "agent", "task"}
                    result_text = (
                        raw_result_content
                        if isinstance(raw_result_content, str) else ""
                    )
                    async_launched = agent_result and (
                        "async agent launched" in result_text.lower()
                    )
                    events.append(ToolResult(
                        tool_use_id=tool_id, content=text, is_error=is_error,
                        status=("failed" if is_error else
                                "running" if async_launched else "succeeded"),
                        truncated=bool(
                            was_truncated or (diff_info and diff_info[1])) or None,
                        diff=diff_info[0] if diff_info and not is_error else None,
                        background=background_followup or None,
                    ))
                    if agent_result:
                        events.append(ProcessEvent(
                            item_id=_agent_process_id(tool_id), kind="agent",
                            phase=("update" if async_launched else "end"),
                            status=("failed" if is_error else
                                    "running" if async_launched else "succeeded"),
                            turn_id=current_turn_id, parent_id=tool_id,
                            title="协作代理", background=True,
                        ))
            if thinking_started:
                events.append(AssistantMsgEnd(
                    message_id=thinking_mid, channel="thinking",
                    background=background_followup or None))
            if text_started:
                events.append(AssistantMsgEnd(
                    message_id=mid, channel=text_channel,
                    background=background_followup or None))
                turn_open = True
            assistant_ts = _ts(source_uid)
            if assistant_ts is not None:
                for event in events[assistant_event_start:]:
                    event.ts = assistant_ts
        # advance last_ts AFTER handling m: a leading close_turn (for the next user
        # msg) stamps the PRIOR turn's tail; the final close_turn stamps this turn's
        # last (assistant) message = answer-done time.
        mts = _ts(source_uid)
        if (
            mts is not None
            and advance_terminal_clock
            and not background_followup
        ):
            last_ts = mts
    # Claude's transcript does not persist the SDK ResultMessage. EOF normally
    # acts as a synthetic completed boundary for an idle historical snapshot,
    # but it is not lifecycle evidence while the resident SDK iterator is still
    # active. Keep only that final group open; every earlier group was already
    # closed authoritatively by the next visible user message.
    if not snapshot_in_progress:
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


def translate_subagent_history(
    session_id: str,
    tool_result_max: int,
    *,
    path: str | None = None,
) -> list:
    """Recover only lightweight Claude Agent lifecycle cards.

    Full subagent conversations belong to ``GetAgentDetail`` and must never be
    flattened back into the parent turn.  The main transcript is authoritative
    for launch/notification state; EOF without a terminal is deliberately
    ``unknown`` rather than a fabricated success.
    """
    main_path = path or transcript_path(session_id)
    if not main_path:
        return []

    records: dict[str, dict[str, Any]] = {}
    agent_tools: dict[str, str] = {}
    order: list[str] = []
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
                    origin = row.get("origin")
                    visible = (isinstance(content, str) and content
                               and not _is_meta_user_text(content))
                    if (isinstance(origin, dict)
                            and origin.get("kind") == "task-notification"):
                        visible = False
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
                        if agent_id:
                            agent_key = str(agent_id)
                            known_tool = agent_tools.get(agent_key)
                            candidate = _wire_id(tool_id, "tool") if tool_id else None
                            if known_tool is None and candidate in records:
                                known_tool = candidate
                                agent_tools[agent_key] = candidate
                            if known_tool is not None:
                                record = records[known_tool]
                                record["agent_id"] = agent_key
                                title = _short_text(
                                    result_meta.get("description"), 1000)
                                if title:
                                    record["title"] = title
                                raw_status = result_meta.get("status")
                                if (bool(result_meta.get("isAsync"))
                                        or str(raw_status).lower()
                                        == "async_launched"):
                                    # The same agent can resume after an earlier
                                    # notification. A later launch re-opens the
                                    # same public run rather than creating a ghost.
                                    record["async"] = True
                                    record["terminal"] = None
                    origin = row.get("origin")
                    if (isinstance(origin, dict)
                            and origin.get("kind") == "task-notification"
                            and isinstance(content, str)):
                        task_match = re.search(
                            r"<task-id>([^<]{1,256})</task-id>", content)
                        tool_match = re.search(
                            r"<tool-use-id>([^<]{1,256})</tool-use-id>", content)
                        status_match = re.search(
                            r"<status>([^<]{1,64})</status>", content)
                        summary_match = re.search(
                            r"<summary>([\s\S]{0,65536}?)</summary>", content)
                        agent_key = task_match.group(1) if task_match else None
                        parent = (
                            _wire_id(tool_match.group(1), "tool")
                            if tool_match else agent_tools.get(agent_key or "")
                        )
                        if parent in records:
                            record = records[parent]
                            record["terminal"] = _task_status(
                                status_match.group(1) if status_match else None)
                            record["terminal_ts"] = _parse_timestamp(
                                row.get("timestamp"))
                            if summary_match:
                                record["summary"] = _short_text(
                                    summary_match.group(1), 64 * 1024)
                elif role == "assistant" and isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        if str(block.get("name") or "").lower() not in {"agent", "task"}:
                            continue
                        tool_id = _wire_id(
                            block.get("id"), "tool", f"{index}-agent")
                        tool_input = block.get("input") if isinstance(
                            block.get("input"), dict) else {}
                        if tool_id not in records:
                            order.append(tool_id)
                        records[tool_id] = {
                            **records.get(tool_id, {}),
                            "tool_id": tool_id,
                            "turn": current_turn,
                            "start_ts": _parse_timestamp(row.get("timestamp")),
                            "async": records.get(tool_id, {}).get("async", False),
                            "terminal": records.get(tool_id, {}).get("terminal"),
                            "title": (
                            _short_text(tool_input.get("description"), 1000)
                            or _short_text(tool_input.get("subagent_type"), 1000)
                            or "协作代理"),
                        }
    except (OSError, UnicodeError):
        return []

    events: list = []
    for tool_id in order[:_MAX_SUBAGENT_FILES]:
        record = records[tool_id]
        if not record.get("turn") or not record.get("async"):
            continue
        status = record.get("terminal")
        terminal = status in {"succeeded", "failed", "cancelled"}
        event = ProcessEvent(
            item_id=_agent_process_id(tool_id), kind="agent",
            phase="end" if terminal else "snapshot",
            status=status if terminal else "unknown",
            turn_id=record["turn"], parent_id=tool_id,
            title=record.get("title") or "协作代理",
            summary=(record.get("summary") if terminal
                     else "未收到结束信号"),
            background=True,
        )
        start_ts = record.get("start_ts")
        end_ts = record.get("terminal_ts")
        if isinstance(start_ts, float) and isinstance(end_ts, float):
            event.duration_ms = max(0, round((end_ts - start_ts) * 1000))
        if isinstance(end_ts, float):
            event.ts = end_ts
        events.append(event)
    return events


def merge_subagent_history(events: list, subagent_events: list) -> list:
    """Insert recovered lifecycle refinements below their Agent tool call."""
    if not subagent_events:
        return events
    groups: dict[str, list] = {}
    for event in subagent_events:
        if (isinstance(event, ProcessEvent) and event.kind == "agent"
                and event.parent_id):
            groups.setdefault(event.parent_id, []).append(event)
    merged = []
    for event in events:
        merged.append(event)
        if isinstance(event, ToolUse):
            merged.extend(groups.pop(event.tool_use_id, []))
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


def _is_synthetic_no_response(message: dict[str, Any]) -> bool:
    """Hide Claude's non-response placeholder for cancelled native commands.

    Claude persists this as an assistant row even though no model response was
    produced. Match both the synthetic model marker and the exact single text
    block so a real assistant reply with the same words remains visible.
    """
    if message.get("model") != "<synthetic>":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].strip() == _SYNTHETIC_NO_RESPONSE_TEXT
    )


def _is_interrupted_user_content(content: Any) -> bool:
    """Recognize Claude's persisted SDK interrupt marker.

    This row is lifecycle metadata for the preceding prompt, not a second
    human-authored message. Claude currently persists it as one text block.
    """
    if not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    if isinstance(block, dict):
        block_type = block.get("type")
        text = block.get("text")
    elif isinstance(block, TextBlock):
        block_type = "text"
        text = block.text
    else:
        return False
    return (
        block_type == "text"
        and isinstance(text, str)
        and text.strip() == _INTERRUPTED_USER_TEXT
    )


def _is_synthetic_api_error(message: dict[str, Any]) -> bool:
    """Keep Claude's provider error text visible while marking the turn failed."""
    if message.get("model") != "<synthetic>":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].lstrip().startswith(
            _SYNTHETIC_API_ERROR_PREFIX)
    )


def last_assistant_model(messages) -> str | None:
    """Most recent assistant message's model id, for restoring the model readout
    when loading a switched session's history."""
    for m in reversed(messages):
        if getattr(m, "type", None) == "assistant" and isinstance(m.message, dict):
            if _is_synthetic_no_response(m.message):
                continue
            mdl = m.message.get("model")
            if mdl == "<synthetic>":
                continue
            if mdl:
                return mdl
    return None
