"""Translate Antigravity ACP session updates into CC Remote's shared events."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import OrderedDict
from itertools import islice
from pathlib import PurePath

from cc_remote.attachments import validate_attachments
from cc_remote.protocol import (
    MAX_BACKGROUND_PROCESS_COMMAND_CHARS, MAX_BACKGROUND_PROCESS_ITEMS, MAX_BACKGROUND_PROCESS_SUMMARY_CHARS,
    MAX_SAFE_WIRE_INTEGER,
    AntigravityChoice, AntigravityOption, AssistantMsgEnd, AssistantMsgStart,
    BackgroundProcessItem, ContextReport, Delta, NativeCommandInfo, TokenUsage,
    ToolDelta, ToolResult, ToolUse, TurnBinding, TurnEnd, TurnPlan, TurnResult, UserMsg,
)
from cc_remote.session_messages import parse_message
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_WIRE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,110}")
_COMMAND = re.compile(r"[A-Za-z][A-Za-z0-9:_-]{0,127}")
_CONFIG_CATEGORIES = {"model", "thought_level", "mode"}
_TOOL_CATEGORIES = {
    "read": "file", "edit": "file", "delete": "file", "move": "file",
    "execute": "command", "fetch": "web_search", "think": "tool",
}
_TOOL_TERMINALS = {"completed": "succeeded", "failed": "failed"}
MCP_SEND_MESSAGE = "mcp_cc_remote_messages_send_message"
_MESSAGE_TOOL_NAMES = {
    f"mcp_cc_remote_messages_{name}": f"mcp__cc_remote_messages__{name}"
    for name in ("list_sessions", "send_message", "message_status")
}

# Keep recent canonical history independent of the reconnect ring. Long
# transcripts cannot allocate their full native replay in Wrapper memory.
_MAX_TURNS = 500
_MAX_HISTORY_BYTES = 24 * 1024 * 1024
_MAX_TURN_BYTES = 12 * 1024 * 1024


def text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def records(value):
    return (item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()


def wire_session_id(native_id):
    if not isinstance(native_id, str) or not _WIRE_COMPONENT.fullmatch(native_id):
        return None
    return "agy@" + native_id


def event_id(prefix, owner, native_id):
    return prefix + "-" + hashlib.sha256(f"{owner}\0{native_id}".encode()).hexdigest()[:32]


def config_options(value) -> dict[str, tuple[str, AntigravityOption]]:
    result = {}
    for option in records(value):
        category, config_id, current = option.get("category"), option.get("id"), option.get("currentValue")
        if not (isinstance(category, str) and category in _CONFIG_CATEGORIES
                and isinstance(config_id, str) and 0 < len(config_id) <= 256
                and isinstance(current, str) and len(current) <= 256
                and option.get("type") == "select"):
            continue
        choices = {}
        for entry in islice(records(option.get("options")), 256):
            # ACP selectors may group choices. The public picker uses the
            # native value even when the display hierarchy is flattened.
            for item in islice(records(entry["options"] if "options" in entry else [entry]), 256):
                option_id = item.get("value")
                if isinstance(option_id, str) and 0 < len(option_id) <= 256:
                    choices[option_id] = AntigravityChoice(
                        value=option_id, name=text(item.get("name"), 512) or option_id,
                        description=text(item.get("description"), 4096))
                if len(choices) >= 256:
                    break
            if len(choices) >= 256:
                break
        result[category] = config_id, AntigravityOption(
            category=category, current=current, choices=list(choices.values()))
    return result


def slash_commands(value) -> list[NativeCommandInfo]:
    result = {}
    for command in islice(records(value), 256):
        name = command.get("name")
        if isinstance(name, str) and _COMMAND.fullmatch(name):
            argument = command.get("input")
            result[name] = NativeCommandInfo(
                name=name, description=text(command.get("description"), 4096),
                input_hint=text(argument.get("hint"), 1024) if isinstance(argument, dict) else None,
            )
    return list(result.values())


def context_usage(update: dict) -> ContextReport | None:
    used, size = update.get("used"), update.get("size")
    if type(used) is int and type(size) is int and 0 <= used <= 10**9 and 0 < size <= 10**9:
        return ContextReport(total_tokens=used, max_tokens=size, percentage=used / size * 100,
                             available=True, source="native_estimate")
    return None


def prompt_usage(value: object) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None

    def count(name):
        n = value.get(name)
        return n if type(n) is int and 0 <= n <= MAX_SAFE_WIRE_INTEGER else None

    input_tokens = count("inputTokens")
    cache_read = count("cachedReadTokens")
    cache_write = count("cachedWriteTokens")
    if input_tokens is not None:
        # agy reports uncached input separately, and includes thoughts in output.
        input_tokens += (cache_read or 0) + (cache_write or 0)
        if input_tokens > MAX_SAFE_WIRE_INTEGER:
            input_tokens = None
    usage = TokenUsage(
        input_tokens=input_tokens, output_tokens=count("outputTokens"),
        cache_read_tokens=cache_read, cache_write_tokens=cache_write,
        thought_tokens=count("thoughtTokens"),
    )
    return usage if any(n is not None for n in usage.model_dump().values()) else None


def native_diff(content):
    patches = []
    remaining = 512 * 1024
    for item in islice(records(content), 64):
        if item.get("type") != "diff":
            continue
        path, old, new = item.get("path"), item.get("oldText"), item.get("newText")
        if (not isinstance(path, str) or not path or len(path) > 4096
                or any(c in path for c in "\n\r\0")
                or old is not None and not isinstance(old, str)
                or not isinstance(new, str)
                or len(old or "") + len(new) > remaining):
            continue
        # Avoid quadratic diff work on pathological model/tool payloads.
        before, after = (old or "").splitlines(keepends=True), new.splitlines(keepends=True)
        if len(before) + len(after) > 6000:
            continue
        display_path = PurePath(path).as_posix()
        source = "a/" + display_path if old is not None else "/dev/null"
        target = "b/" + display_path
        if "\t" in display_path:
            source = json.dumps(source) if old is not None else source
            target = json.dumps(target)
        patch = "\n".join(line.rstrip("\n") for line in difflib.unified_diff(
            before, after, fromfile=source, tofile=target,
        ))
        if patch and len(patch) <= remaining:
            patches.append(patch + "\n")
            remaining -= len(patch) + 1
    return "".join(patches)


class AntigravityTranscript:
    """Render live and native replay through one translator with small pages."""

    def __init__(self, native_id: str = ""):
        self.native_id = native_id
        self.groups: OrderedDict[str, list[dict]] = OrderedDict()
        self.sizes: dict[str, int] = {}
        self.total_bytes = 0
        self.turn_id: str | None = None
        self.message_id: str | None = None
        self.channel: str | None = None
        self.segment = 0
        self.replay_turns = 0
        # A native tool can survive a user steer. Its events still belong to
        # the visible turn that originally started it.
        self.tools: dict[str, tuple[str, str]] = {}
        self.tool_results: set[str] = set()
        self.detachable: dict[str, BackgroundProcessItem] = {}
        self.background: dict[str, BackgroundProcessItem] = {}

    def remember(self, frame, *, replay=False) -> None:
        owner = getattr(frame, "turn_id", None) or self.turn_id
        group = self.groups.get(owner)
        if group is None:
            return
        entry = frame.model_dump(mode="json", exclude_none=True)
        if replay:
            # ACP replay has no message times. Do not substitute the date of
            # the current history read for the original transcript time.
            entry.pop("ts", None)
        elif entry["type"] == "user_msg":
            self.annotate_session_message(entry)
        size = len(frame.model_dump_json(exclude_none=True).encode())
        if entry["type"] == "user_msg" and size > _MAX_TURN_BYTES:
            # Give attachments predictable room without losing the replay
            # boundary even when the prompt uses four-byte UTF-8 characters.
            entry["prompt"] = entry["prompt"][:256 * 1024]
            size = len(frame.model_copy(update={"prompt": entry["prompt"]}).model_dump_json(exclude_none=True).encode())
        previous = group[-1] if group else {}
        if entry["type"] == previous.get("type") == "delta" and entry.get("message_id") == previous.get("message_id"):
            # Collapse token-sized chunks to avoid oversized detail pages.
            extra = entry["text"]
            if len(previous["text"]) + len(extra) <= 128 * 1024 and self.sizes[owner] + size <= _MAX_TURN_BYTES:
                previous["text"] += extra
                self.sizes[owner] += size
                self.total_bytes += size
                self.trim()
                return
        if self.sizes[owner] + size > _MAX_TURN_BYTES and entry["type"] not in {"turn_end", "assistant_msg_end", "turn_usage"}:
            return
        if previous.get("type") == "turn_end":
            group.insert(len(group) - 1, entry)
        else:
            group.append(entry)
        self.sizes[owner] += size
        self.total_bytes += size
        self.trim()

    def trim(self):
        while len(self.groups) > _MAX_TURNS or self.total_bytes > _MAX_HISTORY_BYTES:
            oldest = next(iter(self.groups))
            if oldest == self.turn_id:
                break
            self.groups.pop(oldest)
            self.total_bytes -= self.sizes.pop(oldest)

    def _start(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self.segment = 0
        self.groups[turn_id] = []
        self.sizes[turn_id] = 0

    def start(self, prompt: UserMsg, *, replay=False) -> None:
        self._start(prompt.msg_id)
        self.remember(prompt, replay=replay)

    def start_followup(self, turn_id: str) -> TurnBinding:
        self._start(turn_id)
        return TurnBinding(msg_id=turn_id, turn_id=turn_id, autonomous=True)

    def followup_input(self, update: dict) -> bool:
        kind = update.get("sessionUpdate")
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content")
            return isinstance(content, dict) and bool(text(content.get("text"), 1))
        if kind == "tool_call":
            native = text(update.get("toolCallId"), 512)
            return bool(native and native not in self.tools and native not in self.tool_results)
        return kind == "plan" and any(text(entry.get("content"), 1) for entry in records(update.get("entries")))

    def foreground_tools_pending(self) -> bool:
        return bool(self.tools.keys() - self.tool_results - self.background.keys() - self.detachable.keys())

    def close_message(self):
        if self.message_id is None:
            return []
        frame = AssistantMsgEnd(message_id=self.message_id, turn_id=self.turn_id, channel=self.channel)
        self.message_id = self.channel = None
        return [frame]

    def close_background(self, *, interrupted: bool):
        frames = []
        for native, item in self.background.items():
            self.tools.pop(native, None)
            self.detachable.pop(native, None)
            frames.append(ToolResult(
                tool_use_id=item.item_id, turn_id=item.turn_id, background=True,
                content="", status="interrupted" if interrupted else "unknown", is_error=False,
            ))
        self.background.clear()
        return frames

    def complete(self, subtype="success", *, duration_ms=0, is_error=False, replay=False, keep_background=True):
        if self.turn_id is None:
            return []
        if replay and (group := self.groups.get(self.turn_id)):
            self.annotate_session_message(group[0])
        frames = self.close_message()
        if subtype != "steered":
            for native, (owner, tool) in self.tools.items():
                if native in self.tool_results or native in self.background:
                    continue
                candidate = self.detachable.get(native)
                if (not replay and keep_background and subtype in {"success", "background_followup"} and candidate
                        and len(self.background) < MAX_BACKGROUND_PROCESS_ITEMS):
                    # Native commands can still be running at the successful
                    # prompt receipt. Keep their owner until the actual tool fence.
                    self.background[native] = candidate
                    continue
                frames.append(ToolResult(tool_use_id=tool, turn_id=owner, content="",
                                         status="interrupted" if subtype == "interrupted" else "unknown",
                                         is_error=is_error))
            self.tools = {native: self.tools[native] for native in self.background}
            self.detachable = {native: candidate for native, candidate in self.detachable.items()
                               if native in self.background}
            self.tool_results.clear()
        background_ids = [item.item_id for item in self.background.values() if item.turn_id == self.turn_id]
        frames.append(TurnEnd(turn_id=self.turn_id, result=TurnResult(
            subtype=subtype, duration_ms=duration_ms, is_error=is_error,
            background_tool_ids=background_ids or None)))
        for frame in frames:
            self.remember(frame, replay=replay)
        self.turn_id = None
        return frames

    @staticmethod
    def annotate_session_message(entry):
        if entry["type"] == "user_msg" and (message := parse_message(entry["prompt"])):
            entry.update(prompt=message.prompt, source_thread_id=message.source_thread_id)

    def load_update(self, update: dict) -> None:
        if update.get("sessionUpdate") == "user_message_chunk":
            block = update.get("content")
            if not isinstance(block, dict):
                return
            prompt = text(block.get("text"), 2 * 1024 * 1024)
            last = self.groups.get(self.turn_id)
            # ACP replays text and image blocks for one user turn as adjacent
            # chunks. A native agent update separates successive user turns.
            current = last[0] if last and len(last) == 1 else None
            if current and prompt:
                combined = (current["prompt"] + prompt)[:2 * 1024 * 1024]
                extra = len(combined.encode()) - len(current["prompt"].encode())
                if self.sizes[self.turn_id] + extra <= _MAX_TURN_BYTES:
                    current["prompt"] = combined
                    self.sizes[self.turn_id] += extra
                    self.total_bytes += extra
            elif not current:
                # A native replay can show the steer message before the
                # current tool completes, just like the live stream.
                self.complete("steered" if self.tools.keys() - self.tool_results else "success", replay=True)
                self.replay_turns += 1
                self.start(UserMsg(msg_id=event_id("agy-history", self.native_id, self.replay_turns),
                                   prompt=prompt), replay=True)
            if block.get("type") == "image" and self.turn_id:
                self._load_image(block)
        else:
            for frame in self.translate(update, replay=True):
                self.remember(frame, replay=True)

    def _load_image(self, block):
        # Native replay may contain only a URI. Never dereference agent-
        # supplied paths or URLs while serving browser history.
        image = {"media_type": block.get("mimeType"), "data": block.get("data")}
        data = image["data"]
        if not isinstance(data, str) or len(data) > _MAX_TURN_BYTES:
            return
        first = self.groups[self.turn_id][0]
        images = [*(first.get("images") or []), image]
        if (self.sizes[self.turn_id] + len(data) <= _MAX_TURN_BYTES
                and not validate_attachments(images, None)):
            first["images"] = images
            self.sizes[self.turn_id] += len(data)
            self.total_bytes += len(data)
            self.trim()

    def translate(self, update: dict, *, replay=False):
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            return self.tool_events(update, replay=replay)
        if not self.turn_id:
            return []
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            block = update.get("content")
            value = text(block.get("text"), 512 * 1024) if isinstance(block, dict) else ""
            if not value:
                return []
            channel = "thinking" if kind == "agent_thought_chunk" else "unknown"
            frames = []
            if self.channel != channel:
                frames.extend(self.close_message())
                self.segment += 1
                self.message_id = event_id("agy-text", self.turn_id, self.segment)
                self.channel = channel
                frames.append(AssistantMsgStart(
                    message_id=self.message_id, turn_id=self.turn_id, channel=channel))
            frames.append(Delta(message_id=self.message_id, turn_id=self.turn_id, text=value, channel=channel))
            return frames
        if kind == "plan":
            plan = [{"step": text(item.get("content"), 16 * 1024),
                     "status": {"in_progress": "inProgress", "completed": "completed"}.get(
                         text(item.get("status"), 32), "pending")}
                    for item in islice(records(update.get("entries")), 128)
                    if text(item.get("content"), 1)]
            return [TurnPlan(item_id=event_id("agy-plan", self.turn_id, 0), turn_id=self.turn_id, plan=plan)]
        return []

    def tool_events(self, update, *, replay=False):
        native = text(update.get("toolCallId"), 512)
        if not native:
            return []
        if native in self.tool_results:
            return []
        status = _TOOL_TERMINALS.get(text(update.get("status"), 32))
        frames = []
        if native not in self.tools:
            if not self.turn_id or len(self.tools) >= 2048:
                return frames
            frames.extend(self.close_message())
            tool = event_id("agy-tool", self.turn_id, native)
            self.tools[native] = self.turn_id, tool
            kind = text(update.get("kind"), 64) or "tool"
            name = text(update.get("name"), 256)
            message_tool = _MESSAGE_TOOL_NAMES.get(name)
            title = text(update.get("title"), 1024) or kind
            fields = bounded_tool_input(update.get("rawInput") or {}, 32 * 1024)
            paths = [text(location.get("path"), 4096)
                     for location in islice(records(update.get("locations")), 32)]
            if paths := list(dict.fromkeys(filter(None, [*paths, text(fields.get("Path"), 4096)]))):
                fields["file_paths"] = paths
            tool_name = message_tool or ("edit" if kind in {"delete", "move"} else kind)
            category = _TOOL_CATEGORIES.get(kind, "tool")
            timer = name == "schedule"
            if name == "invoke_subagent":
                # ACP completes this tool when it dispatches the child. The
                # child's own lifetime is separate and has no ACP ID here.
                role = text(fields.get("Role"), 1000)
                title = f"派发子代理 · {role}" if role else "派发子代理"
                tool_name, category = name, "agent"
            elif timer:
                title, tool_name = "定时任务", name
            running_timer = timer and not replay and not status and len(self.background) < MAX_BACKGROUND_PROCESS_ITEMS
            frames.append(ToolUse(message_id=tool, turn_id=self.turn_id, tool_use_id=tool,
                                  tool=tool_name, server="cc_remote_messages" if message_tool else None,
                                  title=title, category=category, input=fields,
                                  background=True if running_timer else None))
            if running_timer:
                self.background[native] = BackgroundProcessItem(
                    item_id=tool, kind="task", turn_id=self.turn_id, title=title,
                    summary=text(fields.get("Prompt"), MAX_BACKGROUND_PROCESS_SUMMARY_CHARS) or None,
                    started_at=frames[-1].ts,
                )
            elif kind == "execute" and name == "run_command":
                self.detachable[native] = BackgroundProcessItem(
                    item_id=tool, kind="task", turn_id=self.turn_id,
                    title=text(fields.get("toolSummary"), 1024) or title,
                    command=text(fields.get("CommandLine"), MAX_BACKGROUND_PROCESS_COMMAND_CHARS) or None,
                    started_at=frames[-1].ts,
                )
        owner, tool = self.tools[native]
        blocks = list(islice(records(update.get("content")), 64))
        body = "\n".join(text(data["content"].get("text"), 128 * 1024)
                         for data in blocks if data.get("type") == "content"
                         and isinstance(data.get("content"), dict))
        output = update.get("rawOutput")
        if not body and output not in (None, {}, []):
            body, _ = bounded_text(output, 128 * 1024)
        if status:
            # The diff is for this one tool. TurnDiff is a cumulative snapshot
            # and would overwrite changes made by earlier tools in the turn.
            patch = native_diff(blocks)
            background = self.background.pop(native, None)
            run_command = native in self.detachable
            self.detachable.pop(native, None)
            exit_code = output.get("exit_code") if run_command and isinstance(output, dict) else None
            if type(exit_code) is not int:
                exit_code = None
            elif exit_code:
                status = "failed"
            if background:
                self.tools.pop(native)
            else:
                self.tool_results.add(native)
            frames.append(ToolResult(
                tool_use_id=tool, turn_id=owner, status=status,
                background=True if background else None, exit_code=exit_code,
                content=body[:128 * 1024], is_error=status == "failed",
                diff=patch or None, diff_source="native" if patch else None,
            ))
        elif body:
            frames.append(ToolDelta(tool_use_id=tool, turn_id=owner,
                                    background=True if native in self.background else None,
                                    stream="output", delta=body[:128 * 1024]))
        return frames
