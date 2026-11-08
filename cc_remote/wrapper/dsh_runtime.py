"""Optional DSH engine integration. DSH owns execution; Wrapper owns routing.

Cold reads use official projections/page endpoints, never session/follow.
Only explicit execution/control admission (or an already-running session) opens
an Agent subscription. Detaching closes subscriptions, not native Agents.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cc_remote.attachments import validate_attachments
from cc_remote.protocol import (
    ERR_BAD_PROMPT, ERR_NOT_STEERABLE,
    AgentDetail, BackgroundProcessItem, BackgroundProcessSync, AskQuestion, DshCommandInfo, DshCommandResult, DshGoal, DshState, Effort, EngineCapabilities, Error, FilePreview, FilesListed, History, HistoryImage, Model,
    Models, Perm, Query, SessionActivity, SessionFocus, SessionForked, SessionInfo,
    SessionList, SessionListInvalidated, StateEvent, TurnBinding, TurnDetail,
    TurnEnd, TurnFileChanges, UserMsg,
)
from cc_remote.log import logger
from cc_remote.wrapper.command_router import UNHANDLED_COMMAND
from cc_remote.wrapper.claude_questions import AskUnavailable
from cc_remote.wrapper.dsh_client import (
    DshClient, DshConnection, DshError, native_session_id, wire_session_id,
)
from cc_remote.wrapper.dsh_stream import DshProjection, history_events, identity, model_id, model_parts
from cc_remote.wrapper.history_store import group_history_events, materialize_history_turns
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import ActiveTurnBinding, SessionContext
from cc_remote.wrapper.dsh_features import DshFeatures
from cc_remote.wrapper.dsh_changes import DshChanges
from cc_remote.wrapper.dsh_sidechats import DshSidechats
from cc_remote.wrapper.dsh_mutations import DshMutationJournal, digest

_ARTIFACT_READS = frozenset({"get_session_artifacts", "get_session_artifact_preview"})
_COMMON = frozenset({
    "hello", "ping", "list_dir", "get_file_preview", "browse_files", "save_markdown",
    "get_preview_asset", "authorize_preview", "get_diff", "get_turn_file_changes",
    "answer_question", "acknowledge_completion", "cancel_queued_query",
    "get_queued_query", "update_queued_query", "reorder_queued_queries", "query",
    *_ARTIFACT_READS,
})
log = logger("dsh_runtime")

# Match the shipped DSH picker: only unnamed built-ins use localized copy.
# A user-defined name (even on a built-in ID) keeps its own description.
_PRESET_COPY = {
    "standard": ("标准模式", "处理代码、文件和资料，适合大多数任务。Agent 会按需使用检索、编辑和终端等工具。"),
    "ptc": ("PTC 模式", "包含标准模式的所有能力，更适合批量调用工具，并对结果进行筛选、整理、去重、统计或汇总的任务。"),
    "minimal": ("极简模式", "Agent 使用终端和图片读取工具，并支持上下文压缩，适合测试和对比其基础表现。"),
    "cordis": ("创造模式", "用对话定制 DSH：让 Agent 编写插件，添加新功能或界面；也能组合工具和提示词，创建自己的模式。"),
}


def preset_presentation(preset: dict) -> dict:
    name, description = (_PRESET_COPY[preset["id"]]
                         if "name" not in preset and preset["id"] in _PRESET_COPY
                         else (preset.get("name") or preset["id"], preset.get("description", "")))
    return {"id": preset["id"], "name": name, "description": description,
            "is_default": preset.get("isDefault", False), "available": "broken" not in preset}


@dataclass
class DshHandle:
    native_id: str
    model: str | None = None
    effort: str | None = None
    permission_mode: str = ""
    state: DshState = field(default_factory=DshState)
    projection: DshProjection = field(default_factory=DshProjection)
    watch: asyncio.Task | None = None
    jobs_watch: asyncio.Task | None = None
    children: list[dict] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    commands: list[dict] = field(default_factory=list)
    pending_images: dict[str, list] = field(default_factory=dict)
    pending_files: dict[str, list] = field(default_factory=dict)
    # Ambiguous prompt writes must not be retried automatically by the queue.
    admitted: set[str] = field(default_factory=set)
    connected: bool = False
    projection_seqs: dict[str, int] = field(default_factory=dict)
    goal_epoch: int = 0
    goal_activation: dict | None = None

    async def disconnect(self):
        tasks = [task for task in (self.watch, self.jobs_watch) if task]
        self.watch = self.jobs_watch = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.connected = False


class DshRuntime:
    def __init__(self, machine, client: DshClient | None = None):
        self.machine = machine
        self.client = client
        self.lock = asyncio.Lock()
        self.context_lock = asyncio.Lock()
        self.events_task: asyncio.Task | None = None
        self.control_task: asyncio.Task | None = None
        self.events_ready = asyncio.Event()
        self.event_client: str | None = None
        self.questions: dict[str, asyncio.Task] = {}
        self.question_clients: dict[str, str] = {}
        self.question_progress: OrderedDict[str, dict] = OrderedDict()
        self.catalog: list[dict] = []
        self.presets: list[dict] = []
        self.permissions: list[dict] = []
        self.default_model: str | None = None
        self.default_effort: str | None = None
        self.history_pages: OrderedDict[tuple[str, str], tuple[list, int]] = OrderedDict()
        self.agent_pages: OrderedDict[tuple[str, str, str], tuple[list, dict]] = OrderedDict()
        self.changes = DshChanges(self)
        self.history_cursors: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.fork_boundaries: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.image_refs: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self.builds: dict[str, int] = {}
        self.closed = False
        self.features = DshFeatures(self)
        self.sidechats = DshSidechats(self)
        self.workspace_task: asyncio.Task | None = None
        self.workspace_ready = asyncio.Event()
        self.archived: set[str] = set()
        self.jobs: dict[str, list] = {}
        self.mutations = DshMutationJournal(machine.cfg.state_dir)

    async def mutation_intent(self, cmd, kind, payload):
        # IDs are scoped to the native authority and the authenticated owner.
        # Legacy untracked callers have no recovery identity to replay.
        request = (getattr(cmd, "msg_id", None) if kind in {"command", "file_upload"} else None)
        if kind == "fork":
            # A business retry can change cmd_id while retaining this identity.
            request = cmd.request_id
        request = request or getattr(cmd, "cmd_id", None) or getattr(cmd, "request_id", None) or str(uuid4())
        owner = getattr(cmd, "owner_id", None) or getattr(cmd, "client_id", None)
        identity = digest([self.client.connection.origin, owner, kind, request])
        fresh, receipt = await asyncio.to_thread(self.mutations.begin, identity, digest(payload))
        return identity, fresh, receipt

    @staticmethod
    def uncertain_mutation():
        return DshError("mutation_unknown", "上次 DSH 操作的结果尚未确认，已停止自动重试。请先查看会话和原生状态。",
                        outcome_unknown=True)

    @asynccontextmanager
    async def metadata_mutation(self, cmd, payload):
        # Claim before reads/locks as well as writes: a stale replay must not
        # overwrite newer native metadata or restore an old archive snapshot.
        identity, fresh, receipt = await self.mutation_intent(cmd, cmd.type, payload)
        if not fresh:
            if receipt["result"] is None:
                raise self.uncertain_mutation()
            result = json.loads(receipt["result"])
            if "error" in result:
                raise DshError(**result["error"])
            yield False
            return
        try:
            yield True
        except DshError as exc:
            await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
            raise
        else:
            # Commit before notification/ACK delivery. Cancellation or an
            # unexpected failure leaves a pending claim that cannot run again.
            await asyncio.to_thread(self.mutations.finish, identity, {"status": "success"})

    async def connection(self) -> DshClient:
        async with self.lock:
            path = getattr(self.machine.cfg, "dsh_connection_file", "")
            if path:
                connection = await asyncio.to_thread(DshConnection.load, Path(path))
                previous = self.client
                if previous and connection.origin != previous.connection.origin:
                    raise DshError("connection_changed", "DSH 连接地址已更改，请重启 Wrapper 以切换实例。")
                if previous is None or connection != previous.connection:
                    self.client = DshClient(connection)
                    # Re-pairing the same authority refreshes subscriptions;
                    # no native Agent is stopped or prompt submitted again.
                    if previous:
                        await previous.close()
            elif self.client is None:
                raise DshError("not_configured", "此设备尚未连接 DSH。请在设备上完成 DSH 本机配对。")
            if self.events_task is None or self.events_task.done():
                self.events_task = asyncio.create_task(self._events(), name="dsh-events")
            if self.control_task is None or self.control_task.done():
                self.control_task = asyncio.create_task(self._control(), name="dsh-control")
            if self.workspace_task is None or self.workspace_task.done():
                self.workspace_task = asyncio.create_task(self._workspaces(), name="dsh-workspaces")
        return self.client

    def targets(self, cmd) -> bool:
        engine = getattr(cmd, "engine", None)
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None)
        if engine == "dsh" or isinstance(sid, str) and sid.startswith("dsh@"):
            return True
        if engine is not None or sid is not None:
            return False
        return cmd.type not in {"hello", "ping", "new_session", "list_sessions", "list_dir"} and bool(
            (ctx := self.machine._focused_ctx()) and ctx.engine == "dsh")

    async def dispatch(self, cmd):
        if not self.targets(cmd):
            return UNHANDLED_COMMAND
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None)
        if getattr(cmd, "engine", None) not in {None, "dsh"}:
            return await self.error(cmd, DshError("invalid_session", "会话与引擎不匹配。"))
        if getattr(cmd, "space", "code") != "code":
            return await self.error(cmd, DshError("unsupported", "DSH 目前支持 Code。"))
        if sid is not None:
            try:
                native_session_id(sid)
            except DshError as exc:
                return await self.error(cmd, exc)
        # Reject stale clients before connecting or creating a native fork.
        # Keep private associations and the implementation for future support.
        if cmd.type == "open_btw":
            return await self.handle_open_btw(cmd)
        owner = getattr(cmd, "owner_id", None) or getattr(cmd, "client_id", None)
        for target in (sid, getattr(cmd, "target_sid", None)):
            try:
                private = self.sidechats.check().get(target)
                if private and private["owner"] != owner:
                    raise DshError("sidechat_owner", "这个侧聊属于其他用户。")
            except DshError as exc:
                return await self.error(cmd, exc)
        if cmd.type in {"get_file_preview", "browse_files"} and sid and sid not in self.machine.sessions:
            try:
                await self.connection()
                await self.ctx(sid)
            except DshError as exc:
                response = (FilePreview if cmd.type == "get_file_preview" else FilesListed)(
                    sid=sid, path=cmd.path, request_id=cmd.request_id, to=cmd.client_id, error=str(exc))
                await self.machine.transport.send(response)
                return response
        if cmd.type in _COMMON:
            if (cmd.type not in _ARTIFACT_READS
                    and sid in self.sidechats.check() and sid not in self.machine.sessions):
                try:
                    await self.connection()
                    await self.ctx(sid)
                except DshError as exc:
                    return await self.error(cmd, exc)
            return UNHANDLED_COMMAND
        try:
            await self.connection()
            handler = getattr(self, "handle_" + cmd.type, None)
            if handler is None:
                raise DshError("unsupported", "当前 DSH 未提供此操作。可查看会话的原生命令。")
            return await handler(cmd)
        except DshError as exc:
            return await self.error(cmd, exc)

    async def error(self, cmd, exc):
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None)
        if cmd.type == "open_btw":
            return await self.machine._send_btw_error(cmd, "dsh_" + exc.code, str(exc))
        if (cmd.type == "act_dsh_goal" or (cmd.type == "set_dsh_control" and cmd.kind == "command")) and cmd.cmd_id:
            msg = DshCommandResult(sid=sid, request_id=cmd.cmd_id,
                status="unknown" if exc.outcome_unknown else "error", text=str(exc), to=cmd.client_id)
            await self.machine.transport.send(msg)
            return msg
        if cmd.type == "get_agent_detail":
            msg = AgentDetail(session_id=sid, sid=sid, run_id=cmd.run_id,
                request_id=cmd.request_id, to=cmd.client_id, revision=self.machine.instance_id,
                detail_revision="unavailable", title="DSH 子代理", status="unknown",
                events=[], through_seq=0, authoritative=False, error=str(exc))
            await self.machine.transport.send(msg)
            return msg
        if cmd.type == "get_history":
            msg = History(session_id=sid, sid=sid, revision=self.machine.instance_id,
                generation=self.machine.instance_id, detail=cmd.detail, before=cmd.before,
                authoritative=False, error=str(exc), to=cmd.client_id)
            await self.machine.transport.send(msg)
            return msg
        if cmd.type == "get_history_image":
            msg = self.history_image_response(cmd, error=str(exc)[:512])
            await self.machine.transport.send(msg)
            return msg
        if cmd.type == "get_turn_detail":
            msg = TurnDetail(session_id=sid, turn_id=cmd.turn_id,
                revision=self.machine.instance_id, before=cmd.before,
                authoritative=False, error=str(exc), to=cmd.client_id)
            await self.machine.transport.send(msg)
            return msg
        if cmd.type == "list_sessions":
            await self.machine.transport.send(SessionList(engine="dsh", sessions=[],
                request_id=cmd.cmd_id, to=cmd.client_id))
        # Shared input rejections must release the browser's exact acceptance
        # latch without terminating the native turn that a steer was targeting.
        code = "dsh_" + exc.code
        if exc.code == "bad_prompt":
            code = ERR_NOT_STEERABLE if cmd.type == "steer" else ERR_BAD_PROMPT
        msg = Error(code=code, message=str(exc), sid=sid,
                    to=getattr(cmd, "client_id", None), request_id=getattr(cmd, "cmd_id", None),
                    msg_id=getattr(cmd, "msg_id", None))
        await self.machine.transport.send(msg)
        if cmd.type == "get_models":
            await self.machine.transport.send(Models(
                engine="dsh", models=[], error=str(exc), to=getattr(cmd, "client_id", None)))
        return msg

    async def ctx(self, sid) -> SessionContext:
        # Native pending-event replay can race a browser focus after restart.
        # Both must share one context and its question futures, never overwrite
        # an independently restored owner after an awaited cold read.
        async with self.context_lock:
            return await self._ctx(sid)

    async def session_cwd(self, sid):
        """Resolve native metadata without occupying a resident slot or watching."""
        native = native_session_id(sid)
        ctx = self.machine.sessions.get(sid)
        if ctx is not None:
            if ctx.engine != "dsh" or ctx.space != "code":
                raise DshError("invalid_session", "会话与引擎不匹配。")
            return ctx.cwd
        row = next((item for item in await self.client.list_sessions() if item["sessionId"] == native), None)
        cwd = row.get("cwd") if row else None
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise DshError("invalid_cwd", "DSH 会话目录不可读取，请刷新会话目录。")
        return cwd

    async def _ctx(self, sid) -> SessionContext:
        if sid is None:
            ctx = self.machine._focused_ctx()
            if ctx is None or ctx.engine != "dsh":
                raise DshError("invalid_session", "请选择 DSH 会话。")
            return ctx
        native = native_session_id(sid)
        ctx = self.machine.sessions.get(sid)
        if ctx:
            if ctx.engine != "dsh":
                raise DshError("invalid_session", "会话与引擎不匹配。")
            return ctx
        snapshot = await self.client.history_snapshot(sid, max_messages=8)
        handle = DshHandle(native)
        # A successful read proves reachability without activating an Agent.
        # Explicit controls (e.g. resume goal) may establish follow afterwards.
        handle.state.connected = True
        cwd = await self.session_cwd(sid)
        # Validate cold metadata before releasing an eligible resident of any
        # engine. Active work and focused/private contexts keep their slots.
        await self.ensure_capacity()
        ctx = SessionContext(session_id=sid, key=sid, sdk=handle, cwd=cwd
                             , engine="dsh", buffer=RingBuffer(
                                 self.machine.cfg.ring_max_events, self.machine.cfg.ring_max_bytes))
        self.sidechats.bind(ctx)
        self._project(self.sidechats.filter_snapshot(sid, snapshot), handle.projection)
        ctx.state = "running" if handle.projection.running else "idle"
        self.machine.sessions[sid] = ctx
        handle.model, handle.effort = handle.projection.model, handle.projection.effort
        await self.snapshot_projections(ctx, snapshot)
        handle.jobs_watch = asyncio.create_task(self._job_watch(ctx))
        if ctx.state == "running":
            # A rejected cold control (e.g. archive while busy) may never call
            # activate(). Observe the real terminal so it can release capacity.
            self.start_follow(ctx)
        return ctx

    async def ensure_capacity(self):
        if not await self.machine._ensure_session_capacity():
            raise DshError("session_limit", "驻留会话已满，请先结束一个运行中的会话。")

    def _project(self, snapshot, projection=None):
        records = [record["event"] for record in snapshot["records"]]
        projection = projection or DshProjection()
        projection.cursor = records[0]["seq"] - 1 if records else snapshot["cursor"]
        frames = []
        for record in records:
            try:
                frames.extend(projection.record(record))
            except (KeyError, TypeError, ValueError):
                raise DshError("invalid_history", "DSH 记录格式不兼容，请检查版本后重新读取。") from None
        return projection, frames

    def _cache_history(self, sid, snapshot, projection, events):
        self.cache_forks(sid, projection)
        groups = group_history_events(events)
        for group in groups:
            rows = materialize_history_turns(group)
            for row in rows:
                key = (sid, row["id"])
                self.history_pages[key] = (group, snapshot["cursor"])
                self.history_pages.move_to_end(key)
                self.history_cursors[key] = snapshot["cursor"]
                self.history_cursors.move_to_end(key)
        # Finite heavyweight cache. Misses trigger another read-only page.
        while len(self.history_pages) > 64 or sum(
            sum(len(str(e)) for e in page[0]) for page in self.history_pages.values()
        ) > 32 * 1024 * 1024:
            self.history_pages.popitem(last=False)
        while len(self.history_cursors) > 8192:
            self.history_cursors.popitem(last=False)
        for refs in projection.image_refs.values():
            for ref in refs:
                key = (sid, ref["image_id"])
                self.image_refs[key] = ref["attachment"]
                self.image_refs.move_to_end(key)
        while len(self.image_refs) > 512:
            self.image_refs.popitem(last=False)

    def cache_forks(self, sid, projection):
        for turn_id, end in projection.fork_seqs.items():
            key = (sid, turn_id)
            self.fork_boundaries[key] = end
            self.fork_boundaries.move_to_end(key)
        while len(self.fork_boundaries) > 8192:
            self.fork_boundaries.popitem(last=False)

    async def handle_get_history(self, cmd, *, publish=True):
        sid = cmd.session_id
        ctx = self.machine.sessions.get(sid)
        # A page may race live deltas. Its replacement fence covers only events
        # observed before this read, never newer output produced during I/O.
        live_seq = ctx.seq if ctx else 0
        before_seq = None
        if cmd.before:
            cached = self.history_pages.get((sid, cmd.before))
            if cached:
                bindings = [e for e in cached[0] if e["type"] == "turn_binding"]
                before_seq = self._sequence(bindings[0]["turn_id"]) if bindings else None
            elif cmd.before.startswith("dsh-seq-"):
                before_seq = self._sequence(cmd.before)
            else:
                raise DshError("history_expired", "历史分页已过期，请刷新会话。")
        snapshot = await self.client.history_snapshot(sid, before_seq=before_seq,
                                                       max_messages=min(100, (cmd.limit or 4)*3))
        snapshot = self.sidechats.filter_snapshot(sid, snapshot)
        projection, frames = self._project(snapshot)
        changes = {}
        for owner, reference in projection.change_refs.items():
            capture = getattr(cmd, "capture_changes", True)
            summary = await self.changes.capture(sid, owner, reference, read_native=capture)
            if summary is None and not capture and ctx:
                self.changes.schedule(ctx, owner, reference)
            # Explicit empty projection withdraws a superseded announcement;
            # it cannot silently display an older turn snapshot as current.
            changes[owner] = summary or {"revision": f"dsh-missing-{reference[0]}", "files": []}
            frames.append(TurnFileChanges(turn_id=owner, changes=changes[owner]))
        events = history_events(frames)
        self._cache_history(sid, snapshot, projection, events)
        rows = list(materialize_history_turns(events, include_live_detail=True))
        owners = {frame.msg_id: frame.turn_id for frame in frames if isinstance(frame, TurnBinding)}
        for row in rows:
            owner = owners.get(row["id"], row["id"])
            if owner in changes:
                row["fileChanges"] = changes[owner]
                if owner != row["id"]:
                    saved = await self.changes.alias(sid, owner, row["id"], changes[owner]["revision"])
                    if not saved and changes[owner]["files"]:
                        row["fileChanges"] = {"revision": "unavailable:" + changes[owner]["revision"], "files": [
                            {"path": file["path"], "state": "unavailable", "reason": "历史差异暂不可用"}
                            for file in changes[owner]["files"]]}
            if row.get("forkPointId") not in projection.fork_seqs:
                row.pop("forkPointId", None)
            refs = projection.image_refs.get(row["id"])
            if refs:
                # These attachments load through GetHistoryImage, not turn
                # detail. Do not advertise them as deferred answer content.
                row["imageRefs"] = [{k: v for k, v in ref.items() if k != "attachment"} for ref in refs]
        self.builds[sid] = self.builds.get(sid, 0) + (0 if cmd.before else 1)
        result = History(
            sid=sid, session_id=sid, revision=self.machine.instance_id,
            generation=self.machine.instance_id, build_seq=self.builds[sid],
            live_seq=live_seq, to=cmd.client_id,
            owner_id=ctx.owner_client_id if ctx and ctx.btw else None,
            detail=cmd.detail, events=events if cmd.detail == "full" else [],
            turns=rows if cmd.detail == "summary" else [],
            before=cmd.before, has_more=snapshot["hasMore"],
            oldest_id=f"dsh-seq-{snapshot['records'][0]['event']['seq']}" if snapshot["records"] else None,
            newest_id=rows[-1]["id"] if rows else None,
            # Cold reads and the first follow snapshot can precede resident
            # State(running). Only the native source owns this lifecycle fence.
            in_progress=projection.running,
            control=self.machine._session_control(ctx) if ctx else None,
        )
        if publish:
            await self.machine.transport.send(result)
        if ctx:
            await self.snapshot_projections(ctx, snapshot)
        return result

    async def handle_get_turn_detail(self, cmd):
        key = (cmd.session_id, cmd.turn_id)
        if cmd.revision not in {None, self.machine.instance_id}:
            cached = None
        else:
            cached = self.history_pages.get(key)
            if cached is None and key in self.history_cursors:
                snapshot = await self.client.history_snapshot(cmd.session_id,
                    through_seq=self.history_cursors[key], max_messages=100)
                projection, frames = self._project(snapshot)
                self._cache_history(cmd.session_id, snapshot, projection,
                    history_events(frames))
                cached = self.history_pages.get(key)
        if cached is None:
            # A reload paints history before requesting details. Explicitly ask
            # for a refresh if its bounded backing page is no longer available.
            result = TurnDetail(session_id=cmd.session_id, turn_id=cmd.turn_id,
                                revision=self.machine.instance_id, authoritative=False,
                                error="详情缓存已更新，请刷新会话后重试。", reset_required=True, to=cmd.client_id)
        else:
            events = cached[0]
            try:
                end = int(cmd.before[len("dsh-detail-"):]) if cmd.before else len(events)
                if cmd.before and not cmd.before.startswith("dsh-detail-"):
                    raise ValueError
            except ValueError:
                raise DshError("history_expired", "详情分页已过期，请刷新后重试。") from None
            if not 0 < end <= len(events):
                raise DshError("history_expired", "详情分页已过期，请刷新后重试。")
            start = max(0, end - cmd.limit)
            result = TurnDetail(session_id=cmd.session_id, turn_id=cmd.turn_id,
                                revision=self.machine.instance_id, events=events[start:end],
                                has_more=start > 0, oldest_cursor=f"dsh-detail-{start}" if start else None,
                                before=cmd.before, to=cmd.client_id)
        await self.machine.transport.send(result)
        return result

    async def handle_get_agent_detail(self, cmd):
        # Read only the proven descendant. session/follow would activate a
        # prepared Agent and is deliberately never used by this reader.
        from cc_remote.wrapper.machine import _turn_detail_page

        result = AgentDetail(session_id=cmd.session_id, sid=cmd.session_id,
            run_id=cmd.run_id, request_id=cmd.request_id, to=cmd.client_id,
            revision=self.machine.instance_id, detail_revision="unavailable",
            title="DSH 子代理", status="unknown", events=[], through_seq=0)
        try:
            if cmd.revision not in {None, self.machine.instance_id}:
                raise DshError("detail_expired", "代理记录已更新，请重新打开。")
            await self.features.descendant(cmd.session_id, cmd.run_id)
            rows = await self.client.list_sessions()
            row = next(r for r in rows if r["sessionId"] == native_session_id(cmd.run_id))
            parent = wire_session_id(row["parentSessionId"])
            catalog = await self.client.list_subagents(parent)
            entry = next((r for r in catalog["entries"] if r["id"] == row["sessionId"]), {})
            result.title = str(entry.get("label") or "DSH 子代理")[:1024]
            result.parent_run_id = parent if parent != cmd.session_id else None
            key = (cmd.session_id, cmd.run_id, cmd.detail_revision or "")
            before = cmd.before
            if before and before.startswith("native:"):
                snapshot = await self.client.history_snapshot(cmd.run_id, before_seq=int(before[7:]), max_messages=40)
                before = None
            elif before:
                cached = self.agent_pages.get(key)
                if cached is None:
                    raise DshError("detail_expired", "代理记录已更新，请重新打开。")
                snapshot = cached[1]
            else:
                snapshot = await self.client.history_snapshot(cmd.run_id, max_messages=40)
            _, frames = self._project(snapshot)
            events = history_events(frames)
            source = f"dsh-agent-{snapshot['cursor']}-{snapshot['records'][0]['event']['seq'] if snapshot['records'] else 0}"
            key = (cmd.session_id, cmd.run_id, source)
            self.agent_pages[key] = (events, snapshot)
            self.agent_pages.move_to_end(key)
            while len(self.agent_pages) > 12 or sum(len(json.dumps(p).encode()) for p in self.agent_pages.values()) > 32 * 1024 * 1024:
                self.agent_pages.popitem(last=False)
            page, more, oldest, newer, newest = _turn_detail_page(events, before=before,
                limit=cmd.limit or 192, max_bytes=2 * 1024 * 1024)
            if not more and snapshot.get("hasMore") and snapshot["records"]:
                more, oldest = True, f"native:{snapshot['records'][0]['event']['seq']}"
            terminal = next((e for e in reversed(events) if e["type"] == "turn_end"), None)
            result.status = "running" if row.get("running") else (
                "failed" if terminal and terminal["result"].get("is_error") else
                "interrupted" if terminal and terminal["result"]["subtype"] == "interrupted" else
                "succeeded" if terminal else "unknown")
            result.events, result.detail_revision = page, source
            result.has_more, result.oldest_cursor = more, oldest
            result.has_newer, result.newer_cursor = newer, newest
            result.before = cmd.before
        except (DshError, ValueError, KeyError, StopIteration) as exc:
            result.authoritative = False
            result.error = str(exc) if isinstance(exc, DshError) else "代理记录暂不可用，请重新打开。"
        await self.machine.transport.send(result)
        return result

    async def handle_get_history_image(self, cmd, *, publish=True):
        if cmd.revision not in {None, self.machine.instance_id}:
            raise DshError("history_expired", "图片引用已更新，请刷新对应历史页。")
        attachment = self.image_refs.get((cmd.session_id, cmd.image_id))
        if not attachment:
            raise DshError("history_expired", "图片引用已过期，请刷新对应历史页。")
        value = await self.client.rpc("session/attachment", {"request": {
            "sessionId": native_session_id(cmd.session_id), "attachment": attachment}})
        import base64
        from cc_remote.wrapper.machine import _render_history_image
        async with self.machine._preview_conversion_limit:
            media_type, width, height, raw = await asyncio.to_thread(_render_history_image,
                {"data": value["data"], "media_type": value["attachment"]["mediaType"]}, cmd.variant)
        result = self.history_image_response(cmd, data=base64.b64encode(raw).decode(),
            media_type=media_type, width=width, height=height)
        if publish:
            await self.machine.transport.send(result)
        return result

    def history_image_response(self, cmd, **fields):
        return HistoryImage(session_id=cmd.session_id, turn_id=cmd.turn_id,
            image_id=cmd.image_id, variant=cmd.variant, request_id=cmd.request_id,
            revision=self.machine.instance_id, to=cmd.client_id, **fields)

    async def read_catalog(self):
        catalog, presets, permissions = await asyncio.gather(
            self.client.rpc("session/modelCatalog"), self.client.rpc("agentPresets/list"),
            self.client.rpc("permissionPresets/catalog"))
        self.permissions = permissions.get("options", [])
        routable = set(catalog.get("routableProviders", []))
        self.catalog = [{
            "id": model_id(group["id"], model["id"]),
            "display_name": model.get("name") or model["id"],
            "description": model.get("description", ""),
            "efforts": [e["id"] for e in model.get("reasoning", {}).get("efforts", [])],
            "default_effort": model.get("reasoning", {}).get("defaultEffort"),
        } for group in catalog["groups"] if group["id"] in routable
          for model in group["models"]]
        default = catalog.get("default") or {}
        default_id = model_id(default["provider"], default["model"]) if default else None
        selected = next((m for m in self.catalog if m["id"] == default_id), None)
        self.default_model = selected["id"] if selected else None
        self.default_effort = (default.get("reasoningEffort") or selected["default_effort"]) if selected else None
        self.presets = [preset_presentation(p) for p in presets["presets"]]

    async def handle_get_models(self, cmd):
        await self.read_catalog()
        result = Models(engine="dsh", models=self.catalog, dsh_presets=self.presets, dsh_permissions=self.permissions,
                        default_model=self.default_model, default_effort=self.default_effort,
                        to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(result)
        return result

    async def handle_list_sessions(self, cmd):
        if self.workspace_task is not None:
            try:
                await asyncio.wait_for(self.workspace_ready.wait(), 10)
            except TimeoutError:
                raise DshError("workspace_unavailable", "DSH 归档状态暂不可读取，请重试。") from None
        items = await self.client.list_sessions()
        rows = []
        pins = self.machine._session_pins.ids("dsh") if self.machine._session_pins else set()
        for item in items:
            # The native catalog also contains delegated agents. They belong
            # in the parent's subagent panel; ordinary user forks stay here.
            # Keep DshClient.list_sessions unfiltered for descendant controls.
            if item.get("origin") == "subagent" or wire_session_id(item["sessionId"]) in self.sidechats.check():
                continue
            sid = wire_session_id(item["sessionId"])
            values = item.get("projections", {}).get("values", {})
            row = SessionInfo(session_id=sid, native_session_id=item["sessionId"],
                              engine="dsh", summary=values.get("title"), cwd=item.get("cwd"),
                              state="running" if item["running"] else "idle", pinned=sid in pins,
                              tag="archived" if item["sessionId"] in self.archived else None,
                              last_modified=datetime.fromtimestamp(item["updatedAt"]/1000, timezone.utc).isoformat(),
                              forked_from_id=wire_session_id(item["parentSessionId"]) if item.get("parentSessionId") else None)
            if self.machine._session_presentation:
                presentation = self.machine._session_presentation.get("dsh", sid)
                row.completion_id = presentation.completion_id
                row.completion_unread = presentation.completion_unread
                row.completion_revision = presentation.completion_revision
            rows.append(row)
        result = SessionList(engine="dsh", sessions=rows, request_id=getattr(cmd, "cmd_id", None),
                             to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(result)
        return result

    async def handle_archive_session(self, cmd):
        async with self.metadata_mutation(cmd, {"sid": cmd.session_id, "archived": cmd.archived}) as fresh:
            if not fresh:
                return
            native = native_session_id(cmd.session_id)
            ctx = await self.ctx(cmd.session_id)
            if ctx.query_lock.locked():
                raise DshError("archive_busy", "会话正在处理请求，请稍后归档。")
            async with ctx.query_lock:
                items = await self.client.list_sessions()
                row = next((x for x in items if x["sessionId"] == native), None)
                if row is None:
                    raise DshError("not_found", "DSH 会话不存在。")
                if row.get("running") or ctx.state != "idle" or ctx.queued_queries:
                    raise DshError("archive_busy", "请先停止任务并清空排队，再归档会话。")
                value = await self.client.rpc("workspace/archiveSession" if cmd.archived else "workspace/unarchiveSession", {"request": {"sessionId": native}})
                self.archived = set(value["archivedSessionIds"])
        await self.invalidate()

    async def handle_read_dsh(self, cmd):
        return await self.features.read(cmd)

    async def handle_act_dsh_subagent(self, cmd):
        return await self.features.act(cmd)

    async def handle_download_dsh(self, cmd):
        return await self.features.download(cmd)

    async def handle_switch_session(self, cmd):
        ctx = await self.ctx(cmd.session_id)
        self.machine.focused_sid = ctx.key
        await self.machine.transport.send(SessionFocus(
            session_id=ctx.key, cwd=ctx.cwd, to=getattr(cmd, "client_id", None)))
        # Discovery is needed before the first prompt, including after a
        # Wrapper restart. The native commands/list RPC would resume the Agent.
        snapshot = await self.client.history_snapshot(ctx.key, max_messages=1, commands=True)
        await self.snapshot_projections(ctx, snapshot)
        await self.projections(ctx, {})
        await self.machine._emit(ctx, self.machine._session_control(ctx))
        items = await self.client.list_sessions()
        if any(i["sessionId"] == ctx.sdk.native_id and i["running"] for i in items):
            await self.activate(ctx)
        return None

    async def handle_new_session(self, cmd):
        if cmd.prompt is not None or cmd.images or cmd.files:
            self.validate_input(cmd)
        payload = cmd.model_dump(exclude={"ts", "v", "client_id", "owner_id", "cmd_id", "machine_id"})
        identity, fresh, receipt = await self.mutation_intent(cmd, "create", payload)
        if not fresh:
            focus = None
            if receipt["sid"]:
                # A recovery read must not follow, reconfigure or resubmit work.
                ctx = await self.ctx(receipt["sid"])
                focus = await self.created_focus(ctx, cmd)
            if receipt["result"] is None:
                raise self.uncertain_mutation()
            result = json.loads(receipt["result"])
            if "error" in result:
                raise DshError(**result["error"])
            reply = result.get("reply")
            if reply:
                reply = {"error": Error, "dsh_command_result": DshCommandResult}[reply["type"]].model_validate(reply)
                reply.to = cmd.client_id
                await self.machine.transport.send(reply)
            return tuple(frame for frame in (focus, reply) if frame is not None)
        try:
            focus, reply = await self.create_session(cmd, identity)
        except DshError as exc:
            await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
            raise
        await asyncio.to_thread(self.mutations.finish, identity, {
            "reply": reply.model_dump(exclude={"v", "ts", "to"}) if reply is not None else None})
        return tuple(frame for frame in (focus, reply) if frame is not None)

    async def created_focus(self, ctx, cmd):
        self.machine.focused_sid = ctx.key
        focus = SessionFocus(session_id=ctx.key, cwd=ctx.cwd,
                             request_id=cmd.request_id, to=cmd.client_id)
        await self.machine.transport.send(focus)
        return focus

    async def create_session(self, cmd, identity):
        await self.read_catalog()
        preset = getattr(cmd, "dsh_agent_preset", None)
        if preset and not any(p["id"] == preset and p["available"] for p in self.presets):
            raise DshError("invalid_preset", "所选 Agent Preset 当前不可用。")
        cwd = str(Path(cmd.cwd or self.machine.cfg.cc_cwd).expanduser())
        if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise DshError("invalid_cwd", "工作目录不存在。")
        selected = cmd.model or self.default_model
        effort = cmd.dsh_effort or cmd.effort or self.default_effort
        if not selected:
            raise DshError("invalid_model", "DSH 当前没有可路由的默认模型，请检查本机模型配置。")
        self.validate_model(selected, effort)
        if cmd.dsh_permission and not any(p["value"] == cmd.dsh_permission for p in self.permissions):
            raise DshError("invalid_permission", "所选 DSH 权限当前不可用。")
        # Reject a genuinely busy pool before creating a native session that
        # cannot accept its initial prompt.
        await self.ensure_capacity()
        value = await self.client.rpc("session/create", {"request": {
            "cwd": cwd, **({"agentPreset": preset} if preset else {})}})
        sid = wire_session_id(value["sessionId"])
        # Persist the known identity before activation or the initial prompt.
        await asyncio.to_thread(self.mutations.created, identity, sid)
        ctx = await self.ctx(sid)
        await self.activate(ctx)
        # A native default or Agent Preset may still select another model.
        # Always establish the offered model before admitting the first prompt.
        await self.select_model(ctx, selected, effort)
        if cmd.dsh_permission:
            await self.command(ctx, "/permission " + cmd.dsh_permission)
        focus = await self.created_focus(ctx, cmd)
        await self.projections(ctx, {})
        await self.invalidate()
        if cmd.prompt is not None or cmd.images or cmd.files:
            reply = await self.query(ctx, Query(sid=sid, prompt=cmd.prompt or "", msg_id=cmd.msg_id,
                                              images=cmd.images, files=cmd.files, client_id=cmd.client_id, cmd_id=cmd.cmd_id or cmd.request_id))
            return focus, reply
        return focus, None

    def start_follow(self, ctx):
        handle = ctx.sdk
        if handle.watch is None or handle.watch.done():
            handle.ready.clear()
            handle.watch = asyncio.create_task(self._follow(ctx), name=f"dsh-follow-{ctx.key}")

    async def activate(self, ctx):
        self.start_follow(ctx)
        handle = ctx.sdk
        try:
            await asyncio.wait_for(handle.ready.wait(), 15)
            await asyncio.wait_for(self.events_ready.wait(), 15)
        except asyncio.TimeoutError:
            raise DshError("disconnected", "DSH 连接尚未就绪，请检查本机 DSH。") from None
        if not handle.connected:
            raise DshError("disconnected", handle.state.error or "DSH 连接中断。")

    async def ensure_unarchived(self, sid):
        if self.workspace_task is not None:
            try:
                await asyncio.wait_for(self.workspace_ready.wait(), 10)
            except TimeoutError as exc:
                raise DshError("workspace_unavailable", "暂时无法确认会话归档状态，请重试。") from exc
        if native_session_id(sid) in self.archived:
            raise DshError("archived", "该会话已归档，仍可查看历史和导出记录。")

    @staticmethod
    def validate_input(cmd, *, allow_empty=False):
        if not allow_empty and not cmd.prompt and not cmd.images and not cmd.files:
            raise DshError("bad_prompt", "消息内容为空，请输入内容或添加附件。")
        if validate_attachments(cmd.images, cmd.files):
            raise DshError("bad_prompt", "附件不符合要求，请调整后重试。")

    async def query(self, ctx, cmd, *, steer=False, launch_receipt=None):
        try:
            self.validate_input(cmd)
            await self.connection()
            await self.ensure_unarchived(ctx.key)
            await self.activate(ctx)
            if not steer and ctx.state != "idle":
                raise DshError("busy", "DSH 正在运行，请使用引导或排队。")
            handle = ctx.sdk
            if cmd.msg_id in handle.admitted:
                if launch_receipt and not launch_receipt.done():
                    launch_receipt.set_result(True)
                return None
            is_command = bool(re.match(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)", cmd.prompt.lstrip())) and not steer
            if is_command:
                name = cmd.prompt.strip()[1:].split(maxsplit=1)[0]
                if not any(c["name"] == name for c in handle.commands):
                    skills = await self.client.rpc("skills/list", {"request": {"sessionId": handle.native_id}})
                    if not any(skill["name"] == name for skill in skills["skills"]):
                        raise DshError("unknown_command", "此 DSH 会话没有这个命令或 Skill。")
                    is_command = False
            if is_command:
                self.validate_command(ctx, cmd.prompt.strip(), bool(cmd.images or cmd.files))
                attachments = await self.content(ctx, cmd, include_text=False)
                value = await self.command(ctx, cmd.prompt.strip(), attachments, request=cmd)
                await self.machine._artifacts.uploads(ctx, cmd.msg_id, cmd.files, cmd.images)
                handle.admitted.add(cmd.msg_id)
                result = DshCommandResult(sid=ctx.key, to=cmd.client_id, request_id=cmd.cmd_id or cmd.msg_id,
                    status="success", text=str((value.get("result") or {}).get("text", "命令已执行"))[:16384])
                await self.machine.transport.send(result)
                if launch_receipt and not launch_receipt.done():
                    launch_receipt.set_result(True)
                return result
            content = await self.content(ctx, cmd)
            handle.pending_images[cmd.msg_id] = list(cmd.images or [])
            handle.pending_files[cmd.msg_id] = [{"filename": f["filename"]} for f in cmd.files or []]
            # Request ID belongs to the durable human message; no optimistic
            # Assistant/User event is fabricated by the wrapper.
            request = {"requestId": cmd.msg_id, "sessionId": handle.native_id,
                       "mode": "steer" if steer else "queue", "content": content}
            if not steer:
                ctx.state = "running"
                await self.machine._emit(ctx, StateEvent(state="running"))
            try:
                await self.client.rpc("session/prompt", {"request": request})
            except DshError as exc:
                if exc.outcome_unknown:
                    handle.admitted.add(cmd.msg_id)
                    # The native request id is observable in follow. Never
                    # retry this mutation as a side effect of queue recovery.
                    if launch_receipt and not launch_receipt.done():
                        launch_receipt.set_result(True)
                if not exc.outcome_unknown and not steer:
                    ctx.state = "idle"
                    await self.machine._emit(ctx, StateEvent(state="idle"))
                if not exc.outcome_unknown:
                    handle.pending_images.pop(cmd.msg_id, None)
                    handle.pending_files.pop(cmd.msg_id, None)
                raise
            await self.machine._artifacts.uploads(ctx, cmd.msg_id, cmd.files, cmd.images)
            handle.admitted.add(cmd.msg_id)
            if len(handle.admitted) > 1024:
                handle.admitted = {cmd.msg_id}
            if ctx.state != "idle":
                ctx.active_msg_id = cmd.msg_id
            if launch_receipt and not launch_receipt.done():
                launch_receipt.set_result(True)
            return None
        except DshError as exc:
            if launch_receipt and not launch_receipt.done():
                launch_receipt.set_result(False)
            return await self.error(cmd, exc)

    async def content(self, ctx, cmd, *, include_text=True):
        blocks = [{"type": "text", "text": cmd.prompt}] if include_text and cmd.prompt else []
        for img in cmd.images or []:
            blocks.append({"type": "image", "mediaType": img["media_type"], "data": img["data"]})
        if cmd.files:
            blocks.extend(await self.upload_files(ctx, cmd))
        return blocks

    async def upload_files(self, ctx, cmd):
        # Uploads have no native idempotency key. Claim the entire attachment
        # batch before its first write; a partial/unknown batch must not repeat.
        # The journal stores only hashes and receipts, never attachment bytes.
        identity, fresh, receipt = await self.mutation_intent(cmd, "file_upload", {
            "sid": ctx.key, "files": cmd.files})
        if not fresh:
            if receipt["result"] is None:
                raise DshError("upload_unknown", "附件上传结果未确认，已暂停重试。请核实后移除排队消息并重新发送。",
                               outcome_unknown=True)
            result = json.loads(receipt["result"])
            if "error" in result:
                raise DshError(**result["error"])
            return result["blocks"]
        blocks = []
        try:
            for file in cmd.files:
                uploaded = await self.client.rpc("fileUploads/upload", {
                    "agentId": ctx.sdk.native_id, "request": {"name": file["filename"], "data": file["data"]}})
                blocks.append({"type": "file", "receiptId": uploaded["receiptId"]})
        except DshError as exc:
            error = DshError("upload_unknown" if exc.outcome_unknown else "upload_failed",
                "附件上传结果未确认，已暂停重试。请核实后移除排队消息并重新发送。" if exc.outcome_unknown
                else "附件上传失败，已暂停重试。请移除排队消息并重新发送。",
                outcome_unknown=exc.outcome_unknown)
            await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                "code": error.code, "message": str(error), "outcome_unknown": error.outcome_unknown}})
            raise error from exc
        await asyncio.to_thread(self.mutations.finish, identity, {"blocks": blocks})
        return blocks

    async def handle_steer(self, cmd):
        ctx = await self.ctx(cmd.sid)
        async with ctx.query_lock:
            return await self.query(ctx, cmd, steer=True)

    async def handle_interrupt(self, cmd):
        # Older browsers can replay an unacknowledged stop after a restart.
        # Claim before native I/O: a session-wide cancel must never be repeated
        # against a newer queued, scheduled or locally started turn.
        sid = cmd.sid or self.machine.focused_sid
        identity, fresh, receipt = await self.mutation_intent(cmd, "interrupt", {"sid": sid})
        if not fresh:
            if receipt["result"] is None:
                raise self.uncertain_mutation()
            result = json.loads(receipt["result"])
            if "error" in result:
                raise DshError(**result["error"])
            return None
        try:
            ctx = await self.ctx(sid)
            await self.client.rpc("session/cancel", {"request": {"sessionId": ctx.sdk.native_id}})
        except DshError as exc:
            await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
            raise
        await asyncio.to_thread(self.mutations.finish, identity, {"status": "success"})
        # Only the native terminal/status closes the turn.
        return None

    def validate_model(self, selected, effort):
        model_parts(selected)
        item = next((m for m in self.catalog if m["id"] == selected), None)
        if item is None:
            raise DshError("invalid_model", "所选模型已不在 DSH 模型目录中。")
        if effort and effort not in set(item["efforts"]):
            raise DshError("invalid_effort", "所选推理强度不适用于此模型。")

    async def select_model(self, ctx, selected, effort=None):
        await self.ensure_unarchived(ctx.key)
        await self.read_catalog()
        if selected is not None:
            self.validate_model(selected, effort)
        await self.activate(ctx)
        if selected is None:
            # Follow may refresh a stale cold projection. Effort-only changes
            # must use that current model, not the pre-activation selection.
            selected = ctx.sdk.model or self.default_model
            self.validate_model(selected, effort)
        provider, model = model_parts(selected)
        value = await self.client.rpc("session/selectModel", {"request": {
            "sessionId": ctx.sdk.native_id, "provider": provider, "model": model,
            **({"reasoningEffort": effort} if effort else {})}})
        chosen = value["selected"]
        await self.projections(ctx, {"modelSelection": {"next": chosen}})

    async def handle_set_model(self, cmd):
        return await self.change_model(cmd, selected=cmd.model)

    async def change_model(self, cmd, *, selected=None, effort=None):
        # Bind the requested change, not the current model used to resolve an
        # effort-only change. A replay must not overwrite a newer native choice.
        sid = cmd.sid or self.machine.focused_sid
        identity, fresh, receipt = await self.mutation_intent(cmd, "model_selection", {
            "sid": sid, "model": selected, "effort": effort})
        if not fresh:
            if receipt["result"] is None:
                raise self.uncertain_mutation()
            result = json.loads(receipt["result"])
            if "error" in result:
                raise DshError(**result["error"])
            return None
        try:
            ctx = await self.ctx(sid)
            async with ctx.query_lock:
                await self.select_model(ctx, selected, effort)
        except DshError as exc:
            await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
            raise
        await asyncio.to_thread(self.mutations.finish, identity, {"status": "success"})

    async def handle_set_dsh_control(self, cmd):
        if cmd.kind in {"job_stop", "plugin_enable", "plugin_disable", "manage", "terminal"}:
            return await self.features.control(cmd)
        if cmd.kind == "effort":
            return await self.change_model(cmd, effort=cmd.value)
        if cmd.kind == "command":
            self.validate_input(cmd, allow_empty=True)
        ctx = await self.ctx(cmd.sid)
        async with ctx.query_lock:
            return await self.session_control(ctx, cmd)

    async def session_control(self, ctx, cmd):
        await self.ensure_unarchived(ctx.key)
        await self.activate(ctx)
        if cmd.kind == "permission":
            if cmd.value == "custom" or cmd.value not in {p.value for p in ctx.sdk.state.permissions}:
                raise DshError("invalid_permission", "请选择 DSH 提供的权限预设。")
            line = "/permission " + cmd.value
        else:
            line = cmd.value
        attachments = None
        if cmd.kind == "command":
            self.validate_command(ctx, line, bool(cmd.images or cmd.files))
            attachments = await self.content(ctx, cmd, include_text=False)
        value = await self.command(ctx, line, attachments, request=cmd)
        if cmd.kind == "command" and cmd.cmd_id:
            result = DshCommandResult(sid=ctx.key, request_id=cmd.cmd_id, status="success",
                text=str(value.get("result", {}).get("text", ""))[:16384], to=cmd.client_id)
            await self.machine.transport.send(result)
            return result
        return None

    async def handle_act_dsh_goal(self, cmd):
        # Also protect tracked requests from older browsers after a Wrapper
        # restart. Claim before activation and save the outcome before refresh
        # or delivery; a pending receipt never authorizes another goals/* RPC.
        identity, fresh, receipt = await self.mutation_intent(cmd, "goal", {
            "sid": cmd.sid, "action": cmd.action, "goal_id": cmd.goal_id,
            "revision": cmd.revision, "objective": cmd.objective,
            "max_rounds": cmd.max_rounds})
        if not fresh:
            if receipt["result"] is None:
                raise self.uncertain_mutation()
            saved = json.loads(receipt["result"])
            if "error" in saved:
                raise DshError(**saved["error"])
        else:
            try:
                ctx = await self.apply_goal(cmd)
            except DshError as exc:
                await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                    "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
                ctx = self.machine.sessions.get(cmd.sid)
                if ctx:
                    await self.refresh_goal_projection(ctx)
                raise
            await asyncio.to_thread(self.mutations.finish, identity, {"status": "success"})
            await self.refresh_goal_projection(ctx)
        result = DshCommandResult(sid=cmd.sid, request_id=cmd.cmd_id,
            status="success", text="", to=cmd.client_id)
        await self.machine.transport.send(result)
        return result

    async def apply_goal(self, cmd):
        ctx = await self.ctx(cmd.sid)
        await self.ensure_unarchived(ctx.key)
        await self.activate(ctx)
        self.validate_command(ctx, "/goal")
        # Pass the browser's ref unchanged; never overwrite another client's edit.
        args = {"agentId": ctx.sdk.native_id}
        if cmd.action != "create":
            args["ref"] = {"id": cmd.goal_id, "revision": cmd.revision}
        if cmd.action in {"create", "edit"}:
            args["request"] = {
                **({"objective": cmd.objective} if cmd.objective is not None else {}),
                **({"maxGoalRounds": cmd.max_rounds} if cmd.max_rounds is not None else {}),
            }
        await self.client.rpc("goals/" + cmd.action, args)
        return ctx

    async def refresh_goal_projection(self, ctx):
        with suppress(DshError):
            snapshot = await self.client.history_snapshot(ctx.key, max_messages=1)
            await self.snapshot_projections(ctx, snapshot)
        await self.refresh_goal(ctx)

    def validate_command(self, ctx, line, attachments=False):
        name = line.lstrip("/").split(maxsplit=1)[0] if line.strip() else ""
        command = next((c for c in ctx.sdk.commands if c["name"] == name), None)
        if command is None:
            raise DshError("unknown_command", "此 DSH 会话没有这个命令。")
        if attachments and not (command.get("input") or {}).get("attachments"):
            raise DshError("command_attachments", "这个 DSH 命令不接受附件。")

    async def command(self, ctx, line, attachments=None, *, request=None):
        identity = None
        if request is not None:
            # Fingerprint source attachments, not newly generated upload receipts.
            identity, fresh, receipt = await self.mutation_intent(request, "command", {
                "sid": ctx.key, "line": line, "images": getattr(request, "images", None),
                "files": getattr(request, "files", None)})
            if not fresh:
                if receipt["result"] is None:
                    raise self.uncertain_mutation()
                result = json.loads(receipt["result"])
                if "error" in result:
                    raise DshError(**result["error"])
                return result["value"]
        try:
            await self.activate(ctx)
            self.validate_command(ctx, line, attachments)
            value = await self.client.rpc("commands/execute", {
                "agentId": ctx.sdk.native_id, "line": line, "submittedAttachments": attachments or []})
            if value is None:
                raise DshError("unknown_command", "此 DSH 会话没有这个命令。")
            if value.get("result", {}).get("kind") == "error":
                raise DshError("command_failed", str(value["result"].get("text", "DSH 命令未成功。"))[:4096])
        except DshError as exc:
            if identity:
                await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                    "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
            raise
        if identity:
            # Only the bounded command result is needed for a duplicate reply.
            value = {"result": {
                "kind": value.get("result", {}).get("kind", "success"),
                "text": str(value.get("result", {}).get("text", ""))[:16384]}}
            await asyncio.to_thread(self.mutations.finish, identity, {"value": value})
        await self.refresh_goal(ctx)
        return value

    async def handle_compact_session(self, cmd):
        await self.command(await self.ctx(cmd.session_id), "/compact", request=cmd)

    async def handle_get_context(self, cmd):
        ctx = await self.ctx(getattr(cmd, "sid", None))
        snapshot = await self.client.history_snapshot(ctx.key, max_messages=8)
        await self.snapshot_projections(ctx, snapshot)
        projection = ctx.sdk.projection
        report = projection.context()
        report.sid, report.to = ctx.key, getattr(cmd, "client_id", None)
        report.request_id = getattr(cmd, "cmd_id", None)
        await self.machine.transport.send(report)
        return report

    async def handle_get_engine_capabilities(self, cmd):
        sid = getattr(cmd, "sid", None)
        if sid:
            ctx = await self.ctx(sid)
            cwd = ctx.cwd
            value = await self.client.rpc("skills/list", {"request": {"sessionId": ctx.sdk.native_id}})
            value["commands"] = (await self.client.metadata(sid))["commands"]
        else:
            cwd = str(Path(cmd.cwd or self.machine.cfg.cc_cwd).expanduser())
            value = await self.client.draft_catalog(cwd, cmd.dsh_agent_preset)
        result = EngineCapabilities(engine="dsh", space="code", cwd=cwd, sid=sid,
                                    request_id=cmd.cmd_id, to=cmd.client_id, skills_only=cmd.skills_only,
                                    items=[{"kind": "skill", "id": s["name"], "name": s["name"],
                                            "description": s.get("description", ""), "enabled": True,
                                            "actions": []} for s in value["skills"]],
                                    dsh_commands=[DshCommandInfo(name=c["name"], description=c.get("description", ""),
                                        attachments=bool((c.get("input") or {}).get("attachments"))) for c in value.get("commands", [])],
                                    notes=["DSH 插件由设备上的 DSH 配置管理。"])
        if not cmd.skills_only:
            inventory = await self.client.rpc("pluginInventory/list")
            from cc_remote.protocol import EngineCapabilityItem
            result.items.extend(EngineCapabilityItem(kind="plugin", id=row["entryId"], name=row["moduleName"],
                enabled=bool(row.get("enabled")), installed=True, status=str(row.get("fiberPhase") or ""))
                for row in inventory.get("entries", [])[:1000])
            result.notes = ["可在 DSH 会话工具 → 插件中启停原生插件；安装和配置由本机 DSH 管理器处理。"]
        await self.machine.transport.send(result)
        return result

    async def handle_rename_session(self, cmd):
        async with self.metadata_mutation(cmd, {"sid": cmd.session_id, "title": cmd.title}) as fresh:
            if not fresh:
                return
            await self.client.rpc("session/rename", {"request": {
                "sessionId": native_session_id(cmd.session_id), "title": cmd.title}})
        await self.invalidate()

    async def handle_pin_session(self, cmd):
        if self.machine._session_pins is None:
            raise DshError("storage_unavailable", "置顶存储暂不可用。")
        await asyncio.to_thread(self.machine._session_pins.set_pinned, "dsh", cmd.session_id, cmd.pinned)
        await self.invalidate()

    @staticmethod
    def _sequence(turn_id):
        try:
            if not turn_id.startswith("dsh-seq-"):
                raise ValueError
            seq = int(turn_id[len("dsh-seq-"):])
            if seq < 0:
                raise ValueError
            return seq
        except (ValueError, AttributeError):
            raise DshError("invalid_turn", "DSH 分支点无效，请刷新会话。") from None

    async def handle_open_btw(self, cmd):
        return await self.error(cmd, DshError(
            "sidechat_unavailable", "DSH 侧聊暂未开放，等待官方提供会话删除能力后再接入。"))

    async def handle_sync_btw(self, cmd):
        return await self.sidechats.sync(cmd)

    async def handle_close_btw(self, cmd):
        return await self.sidechats.release(cmd)

    async def handle_set_effort(self, cmd):
        return await self.change_model(cmd, effort=cmd.effort)

    async def handle_fork_session(self, cmd):
        identity, fresh, receipt = await self.mutation_intent(cmd, "fork", {
            "parent": cmd.session_id, "turn": cmd.last_turn_id})
        if receipt["result"] is not None:
            saved = json.loads(receipt["result"])
            if "error" in saved:
                raise DshError(**saved["error"])
            result = SessionForked.model_validate(saved["reply"])
            result.to = cmd.client_id
        else:
            sid = receipt["sid"]
            if fresh:
                try:
                    sid = await self.create_fork(cmd)
                except DshError as exc:
                    await asyncio.to_thread(self.mutations.finish, identity, {"error": {
                        "code": exc.code, "message": str(exc), "outcome_unknown": exc.outcome_unknown}})
                    raise
                # A subsequent read, delivery or process failure must only
                # reconcile this child, never call session/fork again.
                await asyncio.to_thread(self.mutations.created, identity, sid)
            elif not sid:
                raise self.uncertain_mutation()
            try:
                await self.client.history_snapshot(sid, max_messages=1)
                rows = await self.client.list_sessions()
                cwd = next((row.get("cwd") for row in rows
                            if row["sessionId"] == native_session_id(sid)), None)
                if not isinstance(cwd, str) or not Path(cwd).is_absolute():
                    raise DshError("invalid_cwd", "DSH 分支会话目录尚不可读取。")
                result = SessionForked(parent_session_id=cmd.session_id, session_id=sid,
                    cwd=cwd, target="same_cwd", last_turn_id=cmd.last_turn_id,
                    request_id=cmd.request_id, to=cmd.client_id)
                await asyncio.to_thread(self.mutations.finish, identity, {
                    "reply": result.model_dump(exclude={"v", "ts", "to"})})
            except DshError as exc:
                # Do not cache/ACK a transient recovery read as a business
                # rejection. The reliable retry resumes from the durable sid.
                raise RuntimeError("DSH fork is awaiting read-only recovery") from exc
        await self.machine.transport.send(result)
        await self.invalidate()
        return result

    async def create_fork(self, cmd):
        native = native_session_id(cmd.session_id)
        request = {"sessionId": native}
        turn_id = getattr(cmd, "last_turn_id", None)
        if turn_id:
            # Fork only a complete native prefix, never guess from array index.
            if not (turn_id.startswith("dsh-seq-") or turn_id.startswith("dsh-turn-")):
                raise DshError("invalid_turn", "DSH 分支点无效，请刷新会话。")
            cached = self.fork_boundaries.get((cmd.session_id, turn_id))
            # Revalidate cached historical boundaries against the current
            # native source. Never trust a browser-supplied array position.
            snapshot = await self.client.history_snapshot(cmd.session_id,
                through_seq=cached, max_messages=100)
            projection, _ = self._project(snapshot)
            end = projection.fork_seqs.get(turn_id)
            if end is None:
                raise DshError("invalid_turn", "该分支点尚未完成或已不在当前历史页，请刷新。")
            request["atSeq"] = end
        value = await self.client.rpc("session/fork", {"request": request})
        return wire_session_id(value["sessionId"])

    async def invalidate(self):
        await self.machine.transport.send(SessionListInvalidated(engine="dsh"))

    async def snapshot_projections(self, ctx, snapshot):
        if "commands" in snapshot:
            self.set_commands(ctx, snapshot["commands"])
        projection = snapshot.get("projections", {})
        await self.projections(ctx, projection.get("values", {}), seq=projection.get("asOfSeq", snapshot.get("cursor")))

    def set_commands(self, ctx, commands):
        if not isinstance(commands, list) or len(commands) > 256:
            raise DshError("invalid_commands", "DSH 命令列表格式不兼容。")
        try:
            info = [DshCommandInfo(
                name=c["name"], description=c.get("description", ""),
                input_hint=(c.get("input") or {}).get("hint"),
                attachments=(c.get("input") or {}).get("attachments", False)) for c in commands]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise DshError("invalid_commands", "DSH 命令列表格式不兼容。") from None
        ctx.sdk.commands = commands
        ctx.sdk.state.commands = info

    async def projections(self, ctx, values, *, seq=None):
        handle = ctx.sdk
        if seq is not None:
            values = {key: value for key, value in values.items()
                      if seq >= handle.projection_seqs.get(key, -1)}
            for key in values:
                handle.projection_seqs[key] = seq
        if "workingDirectory" in values and isinstance(values["workingDirectory"], str) and Path(values["workingDirectory"]).is_absolute():
            ctx.cwd = values["workingDirectory"]
        if "title" in values:
            self.machine._remember_notification_title(ctx.key, values["title"])
            await self.invalidate()
        if "subagentCatalog" in values:
            await self.refresh_children(ctx)
        if values and not {"agentPreset", "permissions", "modelSelection", "contextPressure", "goal", "plan"}.intersection(values):
            return
        state = handle.state.model_copy(deep=True)
        state.jobs = self.jobs.get(handle.native_id, [])
        if "plan" in values:
            plan = values["plan"]
            state.plan_active = bool(plan["active"]) if isinstance(plan, dict) else None
            state.plan_pending = bool(plan.get("pending")) if isinstance(plan, dict) else False
        if "agentPreset" in values:
            state.agent_preset = values["agentPreset"]
        if "permissions" in values and values["permissions"] is not None:
            permissions = values["permissions"]
            state.permissions = []
            from cc_remote.protocol import DshPermissionOption
            options = permissions.get("options")
            if options is None:
                # Durable session history can contain only the selection.
                # The process catalog is read-only and may change on reload.
                try:
                    options = (await self.client.rpc("permissionPresets/catalog")).get("options", [])
                except DshError:
                    options = []
            for option in options:
                state.permissions.append(DshPermissionOption(
                    value=option["value"], name=option["name"], description=option.get("description", "")))
            state.permission = permissions.get("currentValue")
            handle.permission_mode = state.permission or ""
            await self.machine._emit(ctx, Perm(mode=handle.permission_mode))
        selection = values.get("modelSelection") or {}
        model = selection.get("next") or selection.get("lastUsed")
        if model:
            handle.model = model_id(model["provider"], model["model"])
            handle.effort = model.get("reasoningEffort")
            handle.projection.model = handle.model
            handle.projection.effort = handle.effort
            await self.machine._emit(ctx, Model(model=handle.model))
            if handle.effort:
                await self.machine._emit(ctx, Effort(effort=handle.effort))
        if "contextPressure" in values:
            await self.machine._emit(ctx, handle.projection.context_pressure(values["contextPressure"], seq=seq))
        if "goal" in values:
            native = values["goal"]
            goal = native.get("goal") if isinstance(native, dict) else None
            previous = state.goal
            state.goal = DshGoal(id=goal["id"], revision=goal["revision"], objective=goal["objective"],
                phase=goal["phase"], rounds=native["roundsStarted"], max_rounds=goal["maxGoalRounds"],
                blocked_reason=(goal.get("blockedReason") or {}).get("message")) if goal else None
            if state.goal and previous and (state.goal.id, state.goal.revision) == (previous.id, previous.revision):
                state.goal.activation = previous.activation
            else:
                handle.goal_epoch += 1
            activation = handle.goal_activation
            if state.goal and activation and (state.goal.id, state.goal.revision) == (activation["id"], activation["revision"]):
                state.goal.activation = activation["activation"]
        changed = state != handle.state
        handle.state = state
        if changed or not values:
            await self.machine._emit(ctx, state.model_copy(deep=True))

    async def refresh_goal(self, ctx):
        handle = ctx.sdk
        if not handle.connected or not any(c["name"] == "goal" for c in handle.commands):
            return
        epoch = handle.goal_epoch
        with suppress(DshError):
            value = await self.client.rpc("goals/get", {"agentId": handle.native_id})
            goal = handle.state.goal
            if value and goal and epoch == handle.goal_epoch and (value["id"], value["revision"]) == (goal.id, goal.revision):
                goal.activation = value["activation"]
                handle.goal_activation = {key: value[key] for key in ("id", "revision", "activation")}
                await self.machine._emit(ctx, handle.state.model_copy(deep=True))

    async def _follow(self, ctx):
        handle = ctx.sdk
        delay = 0.5
        while not self.closed:
            try:
                await self.connection()
                async for frame in self.client.stream("session/follow", {"request": {
                    "address": {"kind": "session", "sessionId": handle.native_id},
                    "maxMessages": 16, "assistantStream": True}}):
                    kind = frame.get("type")
                    if kind == "snapshot":
                        snapshot = await self.client.history_snapshot(ctx.key, through_seq=frame["cursor"], max_messages=100)
                        projection, _ = self._project(self.sidechats.filter_snapshot(ctx.key, snapshot))
                        handle.projection = projection
                        ctx.state = "running" if projection.running else "idle"
                        handle.connected = True
                        handle.state.connected, handle.state.error = True, None
                        await self.machine._set_session_control(ctx, control_mode="remote",
                            write_state="writable", terminal_attached=False)
                        await self.snapshot_projections(ctx, frame)
                        commands = await self.client.rpc("commands/list", {"agentId": handle.native_id})
                        self.set_commands(ctx, commands)
                        await self.machine._emit(ctx, handle.state.model_copy(deep=True))
                        # Reconnect restores the canonical tail before additional deltas.
                        history_cmd = SimpleNamespace(type="get_history",
                            session_id=ctx.key, before=None, limit=4, detail="summary", client_id=None,
                            capture_changes=False)
                        try:
                            await self.handle_get_history(history_cmd)
                        except DshError as exc:
                            # A failed history read must not detach a healthy
                            # live stream or turn a running Agent read-only.
                            await self.error(history_cmd, exc)
                        if projection.running and projection.owner:
                            # The original user echo can be outside live replay.
                            # Bind after History has installed its row; an open
                            # DSH summary deliberately has no fork point yet.
                            await self.emit(ctx, TurnBinding(
                                msg_id=projection.client_id or projection.owner,
                                turn_id=projection.owner, autonomous=projection.client_id is None,
                                continuation=projection.continuation))
                        for message in projection.baseline(frame.get("assistantStream") or {}):
                            await self.emit(ctx, message)
                        if not projection.running:
                            ctx.active_msg_id = None
                            ctx.active_turn_binding = None
                        # A reconnect may have missed turn/end. Publish the
                        # restored state and wake the wrapper-owned queue only
                        # after the canonical history and baseline are ready.
                        await self.machine._set_state(ctx, "running" if projection.running else "idle")
                        handle.ready.set()
                        await self.refresh_goal(ctx)
                        delay = 0.5
                    elif kind == "event":
                        for message in handle.projection.record(frame["event"]):
                            await self.emit(ctx, message)
                        if frame["event"]["type"] == "workspace/changes":
                            for owner, reference in handle.projection.change_refs.items():
                                if reference[0] == frame["event"]["seq"]:
                                    self.changes.schedule(ctx, owner, reference)
                        if frame["event"]["type"] == "turn/start":
                            await self.machine._set_state(ctx, "running")
                        if frame["event"]["type"] == "turn/end":
                            self.cache_forks(ctx.key, handle.projection)
                            ctx.active_msg_id = None
                            ctx.active_turn_binding = None
                            await self.machine._set_state(ctx, "idle")
                    elif kind == "assistant-stream":
                        for message in handle.projection.frame(frame["frame"]):
                            await self.emit(ctx, message)
                raise DshError("disconnected", "DSH 连接已断开，正在重新连接。")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("DSH follow resynchronizing", sid=ctx.key,
                            error_type=type(exc).__name__, code=getattr(exc, "code", None))
                handle.connected = False
                handle.goal_activation = None
                handle.goal_epoch += 1
                if handle.state.goal:
                    handle.state.goal.activation = None
                handle.state.connected = False
                handle.state.error = str(exc) if isinstance(exc, DshError) else "DSH 流状态异常，正在重新同步。"
                await self.machine._set_session_control(ctx, control_mode="remote",
                    write_state="read_only", terminal_attached=False, reason=handle.state.error)
                await self.machine._emit(ctx, handle.state.model_copy(deep=True))
                await self.machine._emit(ctx, self.machine._session_control(ctx))
                handle.ready.set()
                await asyncio.sleep(delay)
                delay = min(15, delay * 2)

    async def emit(self, ctx, frame):
        if isinstance(frame, TurnBinding):
            ctx.active_turn_binding = ActiveTurnBinding(frame.msg_id, frame.turn_id,
                                                       ctx.seq + 1, self.machine.instance_id)
        if isinstance(frame, UserMsg):
            frame.images = ctx.sdk.pending_images.pop(frame.msg_id, None) or None
            frame.files = ctx.sdk.pending_files.pop(frame.msg_id, None) or frame.files
        if isinstance(frame, Model):
            ctx.sdk.model = frame.model
        if isinstance(frame, Effort):
            ctx.sdk.effort = frame.effort
        await self.machine._emit(ctx, frame)
        if isinstance(frame, TurnEnd) and frame.result.subtype != "steered":
            await self.invalidate()

    async def _control(self):
        while not self.closed:
            try:
                async for frame in self.client.stream("session/control"):
                    if frame.get("type") == "baseline":
                        for sid, projection in frame["value"].get("projections", {}).items():
                            ctx = self.machine.sessions.get(wire_session_id(sid))
                            if ctx:
                                await self.projections(ctx, projection.get("values", {}), seq=projection.get("asOfSeq"))
                    elif frame.get("type") == "projection":
                        ctx = self.machine.sessions.get(wire_session_id(frame["sessionId"]))
                        if ctx:
                            changes = frame.get("changes")
                            if (not isinstance(changes, list) or not changes
                                    or any(not isinstance(change, dict) or not isinstance(change.get("key"), str)
                                           or "val" not in change for change in changes)
                                    or len({change["key"] for change in changes}) != len(changes)):
                                raise DshError("invalid_projection", "DSH 状态更新格式不兼容。")
                            await self.projections(ctx, {change["key"]: change["val"] for change in changes}, seq=frame.get("seq"))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)

    async def update_jobs(self, sid, rows):
        from cc_remote.protocol import DshJob
        jobs = [DshJob(id=str(row["id"])[:256], label=str(row.get("label") or row.get("kind") or "后台任务")[:2048],
            status=row["status"], detail=str(row.get("detail") or "")[:4096]) for row in rows[:64]]
        self.jobs[sid] = jobs
        ctx = self.machine.sessions.get(wire_session_id(sid))
        if ctx:
            ctx.sdk.state.jobs = jobs
            await self.machine._emit(ctx, ctx.sdk.state.model_copy(deep=True))
            await self.background(ctx)

    async def _job_watch(self, ctx):
        while not self.closed:
            try:
                async for frame in self.client.stream("job/list", {"request": {"sessionId": ctx.sdk.native_id}}):
                    if frame.get("type") == "rows":
                        await self.update_jobs(ctx.sdk.native_id, frame["jobs"])
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)

    async def refresh_children(self, ctx):
        value = await self.client.list_subagents(ctx.key)
        ctx.sdk.children = value.get("entries", [])[:256]
        await self.background(ctx)

    async def background(self, ctx):
        items = [BackgroundProcessItem(item_id=identity(job.id, "dsh-job"), kind="task", title=job.label[:1024],
            summary=job.detail[:2048] or None, status="running") for job in ctx.sdk.state.jobs if job.status in {"running", "stopping"}]
        for child in ctx.sdk.children:
            if child.get("activity") == "running":
                local = child.get("mode") in {"one-shot", "continuable"}
                items.append(BackgroundProcessItem(
                    item_id=wire_session_id(child["id"]) if local else identity(child["id"], "dsh-external"),
                    kind="agent" if local else "task",
                    summary=None if local else "外部代理正在执行，结果返回当前会话。",
                    title=str(child.get("label") or "DSH 子代理")[:1024], status="running"))
        await self.machine._emit(ctx, BackgroundProcessSync(items=items[:64]))

    async def _workspaces(self):
        while not self.closed:
            try:
                async for frame in self.client.stream("workspace/follow"):
                    values = frame.get("value", {}) if frame.get("type") == "baseline" else frame
                    if frame.get("type") in {"baseline", "archived"}:
                        self.archived = set(values.get("archivedSessionIds", []))
                        self.workspace_ready.set()
                        await self.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.closed:
                    await asyncio.sleep(2)

    async def _events(self):
        while not self.closed:
            try:
                async for frame in self.client.stream("$events"):
                    kind = frame.get("type")
                    if kind == "ready":
                        self.event_client = frame["clientId"]
                        self.events_ready.set()
                    elif kind == "waterfall":
                        event_id = frame["eventId"]
                        self.question_clients[event_id] = self.event_client
                        if event_id not in self.questions:
                            self.questions[event_id] = asyncio.create_task(self._question(frame, self.event_client))
                    elif kind == "cancel":
                        self.question_clients.pop(frame["eventId"], None)
                        self.question_progress.pop(frame["eventId"], None)
                        task = self.questions.pop(frame["eventId"], None)
                        if task:
                            task.cancel()
                    elif kind == "emit":
                        args = frame.get("args", [])
                        if frame.get("event") == "goal/activation-changed" and len(args) == 1:
                            value = args[0]
                            ctx = self.machine.sessions.get(wire_session_id(value["sessionId"]))
                            if ctx:
                                ctx.sdk.goal_epoch += 1
                                goal = ctx.sdk.state.goal
                                update = value.get("goal")
                                ctx.sdk.goal_activation = update
                                if goal and update and (goal.id, goal.revision) == (update["id"], update["revision"]):
                                    goal.activation = update["activation"]
                                    await self.machine._emit(ctx, ctx.sdk.state.model_copy(deep=True))
                        if frame.get("event") == "api-session/status" and len(args) == 2:
                            sid = wire_session_id(args[0])
                            await self.machine.transport.send(SessionActivity(
                                engine="dsh", session_id=sid, state="running" if args[1] else "idle"))
                            ctx = self.machine.sessions.get(sid)
                            for parent in list(self.machine.sessions.values()):
                                if parent.engine == "dsh" and any(child["id"] == args[0] for child in parent.sdk.children):
                                    await self.refresh_children(parent)
                            if ctx and args[1]:
                                self.start_follow(ctx)
                        if frame.get("event") in {"api-session/added", "api-session/removed"}:
                            await self.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self.events_ready.clear()
                self.event_client = None
                # A transport detach does not cancel a native question or its
                # shared UI draft. A fresh pending-event delivery binds the
                # eventual answer to the new API Peer before it may be sent.
                self.question_clients.clear()
            if not self.closed:
                await asyncio.sleep(2)

    async def question_ctx(self, sid):
        ctx = self.machine.sessions.get(sid)
        if ctx is not None:
            return ctx if ctx.engine == "dsh" else None
        # Pending native questions are replayed after reconnect, often before
        # the user focuses their session. Restore only a catalog-proven owner
        # with a cold read; do not focus it, activate it or promote a subagent.
        items = {item["sessionId"]: item for item in await self.client.list_sessions()}
        native = native_session_id(sid)
        visited = set()
        # Depth is a native runtime setting. Visit each catalog entry at most
        # once, including the ordinary root after the last subagent edge.
        while native in items and native not in visited:
            visited.add(native)
            item = items[native]
            if item.get("origin") != "subagent":
                ctx = await self.ctx(wire_session_id(native))
                if item.get("running"):
                    # This Agent is already executing and waiting for input.
                    # Follow its continuation/terminal even if never focused.
                    self.start_follow(ctx)
                return ctx
            native = item.get("parentSessionId")
            if not native:
                return None
            ctx = self.machine.sessions.get(wire_session_id(native))
            if ctx is not None:
                return ctx if ctx.engine == "dsh" else None
        return None

    async def question_outcome(self, ctx, frame, progress):
        if ctx is None:
            raise DshError("question_owner_unavailable", "DSH 问题的所属会话暂不可读取。")
        request, event = frame.get("request", {}), frame.get("event")
        # Native event identity survives a $events transport reconnect. Never
        # ask already-answered pages again or bind answers to a client socket.
        ask_key = progress["signature"]
        if event == "approval/request":
            replies = await self.machine._on_ask_batch(ctx, [AskQuestion(
                question=f"DSH 请求使用工具：{request.get('toolName', '工具')}\n{request.get('reason', '')}"[:16000],
                options=[{"label": "允许一次", "ds": "仅批准这一次工具调用"}, {"label": "拒绝", "ds": "不执行这次调用"}],
                allow_text=False)], ask_id=identity(ask_key, "dsh-ask"))
            answer = replies[0]
            return {"kind": "result", "value": "allowed-once" if answer == "允许一次" else "rejected" if answer else "cancelled"}
        if event != "user-questions/request":
            return {"kind": "next"}
        answers = progress["answers"]
        questions = request.get("questions", [])
        for start in range(len(answers), len(questions), 4):
            batch = questions[start:start + 4]
            pages = []
            for q in batch:
                options = [{"label": o["label"], "ds": o.get("description", "")} for o in q.get("options", [])]
                plan_review = q.get("intent", {}).get("kind") == "plan-review"
                text = ("是否批准以下计划并开始执行？" if plan_review else q["question"]) + ("\n\n" + q["detail"] if q.get("detail") else "")
                if len(options) > 5:
                    text += "\n其他选项可在回答框填写：\n" + "\n".join(o["label"] for o in options[5:])
                pages.append(AskQuestion(question=text, options=options[:5], allow_text=True,
                    header="计划确认" if plan_review else q.get("header"), multi_select=q.get("multiSelect", False)))
            reply = await self.machine._on_ask_batch(ctx, pages, ask_id=identity(f"{ask_key}:{start}", "dsh-ask"))
            for q, answer in zip(batch, reply, strict=True):
                selected = answer if isinstance(answer, list) else [answer]
                labels = {o["label"] for o in q.get("options", [])}
                answers.append({"id": q["id"], "selected": [a for a in selected if a in labels],
                    **({"custom": "\n".join(a for a in selected if a not in labels)} if any(a not in labels for a in selected) else {})})
        return {"kind": "result", "value": {"answers": answers}}

    async def _question(self, frame, client_id):
        event_id = frame["eventId"]
        self.question_clients[event_id] = client_id
        signature = identity(json.dumps(frame, sort_keys=True, ensure_ascii=False))
        progress = self.question_progress.get(event_id)
        if progress is None or progress["signature"] != signature:
            progress = {"signature": signature, "answers": [], "outcome": None}
            self.question_progress[event_id] = progress
        self.question_progress.move_to_end(event_id)
        while len(self.question_progress) > 128:
            self.question_progress.popitem(last=False)
        try:
            while not self.closed:
                try:
                    if progress["outcome"] is None:
                        ctx = await self.question_ctx(wire_session_id(frame.get("agentId", "")))
                        try:
                            progress["outcome"] = await self.question_outcome(ctx, frame, progress)
                        except AskUnavailable:
                            await asyncio.sleep(2)
                            continue
                    delivery = self.question_clients.get(event_id)
                    if delivery is None or delivery != self.event_client:
                        await asyncio.sleep(.2)
                        continue
                    try:
                        await self.client.rpc("$events/result", {"clientId": delivery, "eventId": event_id,
                                                                 "outcome": progress["outcome"]})
                    except DshError as exc:
                        if exc.outcome_unknown:
                            # Never repeat an ambiguous answer write in this generation.
                            # A new native pending-event delivery is required to resume it.
                            if self.question_clients.get(event_id) == delivery:
                                self.question_clients.pop(event_id, None)
                            continue
                        raise
                    self.question_progress.pop(event_id, None)
                    self.question_clients.pop(event_id, None)
                    return
                except (DshError, ConnectionError, TimeoutError):
                    # A transient catalog/read/result failure is not a user
                    # skip. Retain this delivery and retry, or let the native
                    # pending-event replay resume it on the next connection.
                    await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("DSH question projection failed", event_id=event_id,
                        error_type=type(exc).__name__)
            # Keep the original native question pending; a rendering failure is
            # neither user consent nor a request to skip it.
        finally:
            if self.questions.get(event_id) is asyncio.current_task():
                self.questions.pop(event_id, None)

    async def close(self):
        self.closed = True
        await self.changes.close()
        await self.features.close()
        await asyncio.gather(*(ctx.sdk.disconnect() for ctx in self.machine.sessions.values()
                               if ctx.engine == "dsh"), return_exceptions=True)
        tasks = [task for task in (self.events_task, self.control_task, self.workspace_task, *self.questions.values()) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.close()
