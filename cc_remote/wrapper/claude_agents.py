"""Read-only Claude Agent projection and routing.

Claude owns Agent creation, permissions, execution and cancellation.  This
module only separates forwarded subagent messages from the parent turn and
materializes a bounded public projection for ``GetAgentDetail``.  Raw agent
ids, delegated prompts and temporary output-file paths never leave the wrapper.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

from claude_agent_sdk import get_subagent_messages
from claude_agent_sdk.types import (
    AssistantMessage,
    HookEventMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.protocol import ProcessEvent, ToolUse, TurnEnd, UserMsg
from cc_remote.wrapper import claude_catalog
from cc_remote.wrapper.stream import (
    StreamTranslator,
    _is_agent_task_type,
    _short_text,
    _task_status,
    _wire_id,
    public_agent_run_id,
    transcript_path,
    translate_history,
    translate_subagent_history,
)


_MAX_AGENT_RUNS = 256
_MAX_AGENT_EVENTS = 4_000
_MAX_AGENT_EVENT_CHARS = 32 * 1024 * 1024
_MAX_AGENT_FILES = 128
_MAX_AGENT_TOOL_OWNERS = 8_192
_MAX_AGENT_METADATA_BYTES = 64 * 1024
_MAX_AGENT_SOURCE_BYTES = 64 * 1024 * 1024
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}$")


class AgentSourceTooLarge(RuntimeError):
    """A cold Agent transcript exceeds the bounded SDK read budget."""


def _tool_result_id(message: UserMessage) -> str | None:
    content = message.content if isinstance(message.content, list) else []
    for block in content:
        if isinstance(block, ToolResultBlock):
            return _wire_id(block.tool_use_id, "tool")
    return None


def _agent_tool_blocks(message: AssistantMessage) -> list[ToolUseBlock]:
    content = message.content if isinstance(message.content, list) else []
    return [
        block for block in content
        if isinstance(block, ToolUseBlock)
        and block.name.lower() in {"agent", "task"}
    ]


def _tool_id_from_data(message: object) -> str | None:
    if isinstance(message, (
        TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
    )):
        raw = message.tool_use_id
        return _wire_id(raw, "tool") if raw else None
    if isinstance(message, (SystemMessage, HookEventMessage)):
        data = message.data if isinstance(message.data, dict) else {}
        raw = data.get("tool_use_id") or data.get("toolUseID") or data.get("toolUseId")
        return _wire_id(raw, "tool") if raw else None
    return None


def _terminal_status(message: object) -> str | None:
    if isinstance(message, TaskUpdatedMessage):
        status = _task_status(message.status)
        return status if status in {"succeeded", "failed", "cancelled"} else None
    if isinstance(message, TaskNotificationMessage):
        return _task_status(message.status)
    return None


@dataclass
class AgentRunProjection:
    run_id: str
    tool_use_id: str
    title: str = "协作代理"
    parent_run_id: str | None = None
    agent_id: str | None = None
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    event_chars: int = 0
    through_seq: int = 0
    item_turns: dict[str, str] = field(default_factory=dict)
    item_titles: dict[str, str] = field(default_factory=dict)
    item_meta: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    translator: StreamTranslator | None = None
    revision_epoch: int = 0
    subscribers: dict[str, None] = field(default_factory=dict)
    owned_tool_ids: set[str] = field(default_factory=set)
    pending_stream_events: list[dict[str, Any]] = field(default_factory=list)
    last_stream_flush: float | None = None

    def ensure_translator(self, tool_result_max: int) -> StreamTranslator:
        if self.translator is None:
            self.translator = StreamTranslator(
                tool_result_max,
                item_turns=self.item_turns,
                item_titles=self.item_titles,
                item_meta=self.item_meta,
            )
        return self.translator

    def append(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return self.through_seq
        self.through_seq += 1
        for event in events:
            encoded = len(json.dumps(
                event, ensure_ascii=False, separators=(",", ":")))
            self.events.append(event)
            self.event_chars += encoded
        # Keep a strict resident bound. Source-backed GetAgentDetail remains the
        # canonical recovery path after older live groups are evicted.
        while (len(self.events) > _MAX_AGENT_EVENTS
               or self.event_chars > _MAX_AGENT_EVENT_CHARS):
            removed = self.events.pop(0)
            self.revision_epoch += 1
            self.event_chars = max(0, self.event_chars - len(json.dumps(
                removed, ensure_ascii=False, separators=(",", ":"))))
        return self.through_seq

    def subscribe(self, client_id: str) -> None:
        self.subscribers.pop(client_id, None)
        self.subscribers[client_id] = None
        while len(self.subscribers) > 16:
            self.subscribers.pop(next(iter(self.subscribers)))


@dataclass(frozen=True)
class AgentRoute:
    target: str  # "main" or "detail"
    run_id: str | None = None
    events: tuple[dict[str, Any], ...] = ()
    through_seq: int = 0
    touched_run_ids: tuple[str, ...] = ()


class ClaudeAgentRegistry:
    """Per-resident-session isolated translators for forwarded Agent traffic."""

    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self.runs: dict[str, AgentRunProjection] = {}
        self.run_by_tool: dict[str, str] = {}
        self.run_by_agent: dict[str, str] = {}
        self.tool_owner: dict[str, str] = {}
        self.task_owner: dict[str, str] = {}

    def _ensure_run(
        self,
        tool_use_id: str,
        *,
        title: str | None = None,
        parent_run_id: str | None = None,
    ) -> AgentRunProjection:
        tool_id = _wire_id(tool_use_id, "tool")
        run_id = self.run_by_tool.get(tool_id) or public_agent_run_id(tool_id)
        run = self.runs.get(run_id)
        if run is None:
            if len(self.runs) >= _MAX_AGENT_RUNS:
                # Bound the session lifetime without reusing an old run id.
                oldest = next(iter(self.runs))
                retired = self.runs.pop(oldest)
                self.run_by_tool.pop(retired.tool_use_id, None)
                if retired.agent_id:
                    self.run_by_agent.pop(retired.agent_id, None)
                for owned_tool_id in retired.owned_tool_ids:
                    if self.tool_owner.get(owned_tool_id) == retired.run_id:
                        self.tool_owner.pop(owned_tool_id, None)
            run = AgentRunProjection(
                run_id=run_id,
                tool_use_id=tool_id,
                title=title or "协作代理",
                parent_run_id=parent_run_id,
            )
            self.runs[run_id] = run
            self.run_by_tool[tool_id] = run_id
        else:
            if title:
                run.title = title
            if parent_run_id is not None:
                run.parent_run_id = parent_run_id
        return run

    def _bind_agent(self, run: AgentRunProjection, agent_id: Any) -> None:
        if not isinstance(agent_id, str) or not _SAFE_AGENT_ID.fullmatch(agent_id):
            return
        run.agent_id = agent_id
        self.run_by_agent[agent_id] = run.run_id

    def observe_main(self, message: object) -> tuple[str, ...]:
        """Learn root Agent identities without changing the main translator."""
        if isinstance(message, AssistantMessage) and not message.parent_tool_use_id:
            for block in _agent_tool_blocks(message):
                title = (
                    _short_text(block.input.get("description"), 1000)
                    if isinstance(block.input, dict) else None
                ) or "协作代理"
                self._ensure_run(block.id, title=title)
            return ()
        if isinstance(message, UserMessage) and not message.parent_tool_use_id:
            tool_id = _tool_result_id(message)
            result = message.tool_use_result if isinstance(
                message.tool_use_result, dict) else {}
            run = self.runs.get(self.run_by_tool.get(tool_id or "", ""))
            if run is not None:
                self._bind_agent(run, result.get("agentId"))
                title = _short_text(result.get("description"), 1000)
                if title:
                    run.title = title
                raw_status = result.get("status")
                if bool(result.get("isAsync")) or str(raw_status).lower() == "async_launched":
                    run.status = "running"
                else:
                    mapped = _task_status(raw_status)
                    if mapped != "unknown":
                        run.status = mapped
                return (run.run_id,)
        return ()

    def _remember_tool_owner(
        self, tool_use_id: str, run: AgentRunProjection,
    ) -> None:
        previous_run_id = self.tool_owner.pop(tool_use_id, None)
        if previous_run_id:
            previous = self.runs.get(previous_run_id)
            if previous is not None:
                previous.owned_tool_ids.discard(tool_use_id)
        self.tool_owner[tool_use_id] = run.run_id
        run.owned_tool_ids.add(tool_use_id)
        while len(self.tool_owner) > _MAX_AGENT_TOOL_OWNERS:
            retired_tool_id = next(iter(self.tool_owner))
            retired_run_id = self.tool_owner.pop(retired_tool_id)
            retired_run = self.runs.get(retired_run_id)
            if retired_run is not None:
                retired_run.owned_tool_ids.discard(retired_tool_id)

    def _run_for_task(self, message: object) -> AgentRunProjection | None:
        tool_id = _tool_id_from_data(message)
        if tool_id:
            run_id = self.run_by_tool.get(tool_id)
            if run_id:
                return self.runs.get(run_id)
        task_id = getattr(message, "task_id", None)
        if isinstance(task_id, str):
            run_id = self.run_by_agent.get(task_id)
            if run_id:
                return self.runs.get(run_id)
        # A local background Bash task also carries tool_use_id. Never create a
        # clickable Agent run from correlation alone: require the explicit SDK
        # task type when the preceding Agent ToolUse was not observed.
        if (tool_id and isinstance(message, TaskStartedMessage)
                and _is_agent_task_type(message.task_type)):
            return self._ensure_run(tool_id)
        return None

    @staticmethod
    def _without_direct_parent(message: object) -> object:
        if isinstance(message, (AssistantMessage, UserMessage, StreamEvent)):
            return replace(message, parent_tool_use_id=None)
        return message

    def _record_detail_events(
        self, run: AgentRunProjection, message: object,
    ) -> AgentRoute:
        translator = run.ensure_translator(self.tool_result_max)
        translated = translator.feed(self._without_direct_parent(message))
        public_events = []
        for event in translated:
            if isinstance(event, (TurnEnd, UserMsg)):
                continue
            if isinstance(event, ProcessEvent) and event.kind == "agent":
                event.background = True
            if isinstance(event, ToolUse):
                self._remember_tool_owner(event.tool_use_id, run)
                if event.category == "agent":
                    self._ensure_run(
                        event.tool_use_id,
                        title=event.title or "协作代理",
                        parent_run_id=run.run_id,
                    )
            public_events.append(event.model_dump(mode="json"))
        # Show child output before its complete AssistantMessage arrives.
        # Coalesce the burst rather than broadcasting an empty status packet
        # for every native token. Assembled/tool messages flush pending text;
        # StreamTranslator already deduplicates their repeated content.
        for event in public_events:
            previous = run.pending_stream_events[-1] if run.pending_stream_events else None
            if (previous and event["type"] == previous["type"] == "delta"
                    and event.get("message_id") == previous.get("message_id")
                    and event.get("channel") == previous.get("channel")
                    and len(previous["text"]) + len(event["text"]) <= 32 * 1024):
                previous["text"] += event["text"]
            else:
                run.pending_stream_events.append(event)
        now = time.monotonic()
        if (isinstance(message, StreamEvent)
                and run.last_stream_flush is not None
                and now - run.last_stream_flush < 0.1
                and sum(len(event.get("text", ""))
                        for event in run.pending_stream_events) < 32 * 1024):
            return AgentRoute("detail", run.run_id)
        public_events = run.pending_stream_events
        run.pending_stream_events = []
        if not public_events:
            return AgentRoute("detail", run.run_id)
        run.last_stream_flush = now
        seq = run.append(public_events)
        return AgentRoute(
            "detail", run.run_id, tuple(public_events), seq, (run.run_id,))

    def route(self, message: object) -> AgentRoute:
        """Return the only projection which may consume this SDK message."""
        direct_parent = getattr(message, "parent_tool_use_id", None)
        if isinstance(direct_parent, str) and direct_parent:
            tool_id = _wire_id(direct_parent, "tool")
            run_id = self.run_by_tool.get(tool_id)
            run = self.runs.get(run_id or "")
            if run is None:
                run = self._ensure_run(tool_id)
            route = self._record_detail_events(run, message)
            touched = list(route.touched_run_ids)
            # A nested Agent launch result carries the private child id.
            if isinstance(message, UserMessage):
                result = message.tool_use_result if isinstance(
                    message.tool_use_result, dict) else {}
                child_tool = _tool_result_id(message)
                child = self.runs.get(self.run_by_tool.get(child_tool or "", ""))
                if child is not None:
                    self._bind_agent(child, result.get("agentId"))
                    raw_status = result.get("status")
                    if (bool(result.get("isAsync"))
                            or str(raw_status).lower() == "async_launched"):
                        child.status = "running"
                    else:
                        mapped = _task_status(raw_status)
                        if mapped != "unknown":
                            child.status = mapped
                    touched.append(child.run_id)
            return AgentRoute(
                route.target, route.run_id, route.events, route.through_seq,
                tuple(dict.fromkeys(touched)),
            )

        if isinstance(message, (
            TaskStartedMessage, TaskProgressMessage,
            TaskUpdatedMessage, TaskNotificationMessage,
        )):
            run = self._run_for_task(message)
            if run is None:
                # Child Bash tasks are forwarded as top-level SDK system
                # messages, without parent_tool_use_id. Their tool identity
                # still belongs to the child. Keep later task-id-only updates
                # there too; they cannot reserve a response in the main turn.
                tool_id = _tool_id_from_data(message)
                task_id = getattr(message, "task_id", None)
                owner = self.runs.get(
                    self.tool_owner.get(tool_id or "", "")
                    or self.task_owner.get(task_id, ""))
                if owner is not None:
                    if isinstance(task_id, str) and task_id:
                        self.task_owner[task_id] = owner.run_id
                        while len(self.task_owner) > _MAX_AGENT_TOOL_OWNERS:
                            self.task_owner.pop(next(iter(self.task_owner)))
                    return self._record_detail_events(owner, message)
                return AgentRoute("main")
            if isinstance(message, TaskStartedMessage):
                self._bind_agent(run, message.task_id)
                if message.description:
                    run.title = _short_text(message.description, 1000) or run.title
                run.status = "running"
            terminal = _terminal_status(message)
            if terminal:
                run.status = terminal
            elif isinstance(message, (TaskProgressMessage, TaskUpdatedMessage)):
                mapped = _task_status(getattr(message, "status", None))
                if mapped != "unknown":
                    run.status = mapped
            if run.parent_run_id:
                parent = self.runs.get(run.parent_run_id)
                if parent is not None:
                    routed = self._record_detail_events(parent, message)
                    return AgentRoute(
                        routed.target, routed.run_id, routed.events,
                        routed.through_seq,
                        tuple(dict.fromkeys((*routed.touched_run_ids, run.run_id))),
                    )
            return AgentRoute("main", touched_run_ids=(run.run_id,))

        tool_id = _tool_id_from_data(message)
        if tool_id:
            owner = self.runs.get(self.tool_owner.get(tool_id, ""))
            if owner is not None:
                return self._record_detail_events(owner, message)

        return AgentRoute(
            "main", touched_run_ids=self.observe_main(message))

    def snapshot(self, run_id: str) -> AgentRunProjection | None:
        return self.runs.get(run_id)


@dataclass(frozen=True)
class SourceAgentDetail:
    run_id: str
    title: str
    parent_run_id: str | None
    status: str
    agent_id: str
    source_path: str | None
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SourceAgentLocation:
    run_id: str
    title: str
    parent_run_id: str | None
    status: str
    agent_id: str
    source_path: str | None


def _notification_event(content: str) -> ProcessEvent | None:
    task = re.search(r"<task-id>([^<]{1,256})</task-id>", content)
    tool = re.search(r"<tool-use-id>([^<]{1,256})</tool-use-id>", content)
    status = re.search(r"<status>([^<]{1,64})</status>", content)
    summary = re.search(r"<summary>([\s\S]{0,65536}?)</summary>", content)
    if not task:
        return None
    task_id = _wire_id(task.group(1), "task")
    tool_id = _wire_id(tool.group(1), "tool") if tool else None
    mapped = _task_status(status.group(1) if status else None)
    terminal = mapped in {"succeeded", "failed", "cancelled"}
    return ProcessEvent(
        # A task notification's tool-use-id is correlation, not Agent proof.
        # translate_history has the preceding ToolUse name and promotes this
        # event only when that parent is actually Agent/Task. In particular,
        # Bash(run_in_background=true) must remain an ordinary background task
        # even when it runs inside a real Agent transcript.
        item_id=task_id, kind="task",
        phase="end" if terminal else "update",
        status=mapped, parent_id=tool_id, title="后台任务",
        summary=_short_text(summary.group(1), 64 * 1024) if summary else None,
        background=True,
    )


def _source_agent_files(
    session_id: str,
    *,
    main_path: str | None = None,
) -> dict[str, str]:
    """Collect bounded Agent transcript paths without opening their payloads."""
    main = main_path or transcript_path(session_id)
    if not main:
        return {}
    root = os.path.join(os.path.splitext(main)[0], "subagents")
    pattern = os.path.join(root, "**", "agent-*.jsonl")
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for path in glob.iglob(pattern, recursive=True):
        name = os.path.basename(path)
        agent_id = name[len("agent-"):-len(".jsonl")]
        if not _SAFE_AGENT_ID.fullmatch(agent_id):
            continue
        if agent_id in found:
            ambiguous.add(agent_id)
        else:
            found[agent_id] = path
        if len(found) + len(ambiguous) >= _MAX_AGENT_FILES:
            break
    for agent_id in ambiguous:
        found.pop(agent_id, None)
    return found


def _source_agent_metadata(
    source_path: str,
) -> tuple[str | None, str | None, str | None]:
    """Read only the small SDK sidecar, never the Agent transcript body."""
    sidecar = os.path.splitext(source_path)[0] + ".meta.json"
    try:
        with open(sidecar, "rb") as source:
            raw = source.read(_MAX_AGENT_METADATA_BYTES + 1)
    except OSError:
        return None, None, None
    if len(raw) > _MAX_AGENT_METADATA_BYTES:
        return None, None, None
    try:
        metadata = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, None, None
    if not isinstance(metadata, dict):
        return None, None, None
    tool_id = metadata.get("toolUseId")
    parent_agent_id = metadata.get("parentAgentId")
    title = metadata.get("description") or metadata.get("agentType")
    return (
        tool_id if isinstance(tool_id, str) else None,
        parent_agent_id if isinstance(parent_agent_id, str) else None,
        _short_text(title, 1000),
    )


def _main_metadata(
    session_id: str,
    run_id: str,
    fallback_title: str | None = None,
    *,
    main_path: str | None = None,
) -> tuple[str, str]:
    title = fallback_title or "协作代理"
    status = "unknown"
    # The shared lifecycle scanner handles launch titles, notifications and
    # resumed/no-terminal records in one pass over the main transcript.
    events = (
        translate_subagent_history(session_id, 64 * 1024)
        if main_path is None
        else translate_subagent_history(
            session_id, 64 * 1024, path=main_path)
    )
    for event in events:
        if isinstance(event, ProcessEvent) and event.item_id == run_id:
            title = event.title or title
            status = event.status
    return title, status


def resolve_source_agent(
    session_id: str,
    run_id: str,
    directory: str | None,
    *,
    main_path: str | None = None,
) -> SourceAgentLocation | None:
    """Resolve public identity without reading the complete Agent transcript."""
    del directory  # transcript_path already resolves the canonical session
    source_files = _source_agent_files(
        session_id, main_path=main_path)
    resolved: dict[str, tuple[str, str | None, str, str | None]] = {}
    target_id = None
    for agent_id, source_path in source_files.items():
        parent_tool, parent_agent, title = _source_agent_metadata(source_path)
        if not parent_tool:
            continue
        candidate = public_agent_run_id(parent_tool)
        resolved[agent_id] = (
            candidate, parent_agent, source_path, title)
        if candidate == run_id:
            target_id = agent_id
    if target_id is None:
        return None
    parent_agent = resolved[target_id][1]
    parent_run_id = resolved.get(parent_agent or "", (None, None))[0]
    source_path = resolved[target_id][2]
    title, status = _main_metadata(
        session_id,
        run_id,
        resolved[target_id][3],
        main_path=main_path,
    )
    return SourceAgentLocation(
        run_id=run_id,
        title=title,
        parent_run_id=parent_run_id,
        status=status,
        agent_id=target_id,
        source_path=source_path,
    )


def translate_source_agent(
    session_id: str,
    location: SourceAgentLocation,
    directory: str | None,
    tool_result_max: int,
    *,
    config_dir: str | os.PathLike[str] | None = None,
) -> SourceAgentDetail:
    """Translate one resolved Agent while omitting every delegated user prompt."""
    if location.source_path:
        try:
            source_bytes = os.path.getsize(location.source_path)
        except OSError:
            source_bytes = 0
        if source_bytes > _MAX_AGENT_SOURCE_BYTES:
            raise AgentSourceTooLarge(
                "Claude Agent transcript exceeds the bounded read budget")
    target_messages = (
        get_subagent_messages(
            session_id, location.agent_id, directory=directory)
        if config_dir is None
        else claude_catalog.get_subagent_messages(
            config_dir,
            session_id,
            location.agent_id,
            directory=directory,
        )
    )

    rows: list[SimpleNamespace] = []
    internal_events: dict[str, ProcessEvent] = {}
    for message in target_messages:
        payload = message.message if isinstance(message.message, dict) else None
        if payload is None:
            continue
        content = payload.get("content")
        if message.type == "user":
            if isinstance(content, str):
                if content.lstrip().startswith("<task-notification>"):
                    event = _notification_event(content)
                    if event is not None:
                        internal_events[message.uuid] = event
                    else:
                        continue
                else:
                    # The first/delegated prompt and any private follow-up prompt
                    # are context for the model, not public process output.
                    continue
            elif not (
                isinstance(content, list)
                and any(isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        for block in content)
            ):
                continue
        rows.append(SimpleNamespace(
            uuid=message.uuid,
            type=message.type,
            message=payload,
            parent_tool_use_id=None,
        ))

    translated = translate_history(
        rows,
        tool_result_max,
        internal_user_events=internal_events or None,
        snapshot_in_progress=True,
    )
    events: list[dict[str, Any]] = []
    for event in translated:
        if isinstance(event, (UserMsg, TurnEnd)):
            continue
        if isinstance(event, ProcessEvent) and event.kind == "agent":
            event.background = True
        events.append(event.model_dump(mode="json"))

    return SourceAgentDetail(
        run_id=location.run_id,
        title=location.title,
        parent_run_id=location.parent_run_id,
        status=location.status,
        agent_id=location.agent_id,
        source_path=location.source_path,
        events=tuple(events),
    )


def load_source_agent_detail(
    session_id: str,
    run_id: str,
    directory: str | None,
    tool_result_max: int,
    *,
    main_path: str | None = None,
    config_dir: str | os.PathLike[str] | None = None,
) -> SourceAgentDetail | None:
    """Compatibility helper combining official identity and detail reads."""
    location = resolve_source_agent(
        session_id, run_id, directory, main_path=main_path)
    if location is None:
        return None
    return translate_source_agent(
        session_id,
        location,
        directory,
        tool_result_max,
        config_dir=config_dir,
    )
