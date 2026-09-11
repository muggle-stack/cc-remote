"""Optional DSH engine integration. DSH owns execution; Wrapper owns routing.

Cold reads use the authenticated Host history bridge, never session/follow.
Only explicit execution/control admission (or an already-running session) opens
an Agent subscription. Detaching closes subscriptions, not native Agents.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from cc_remote.protocol import (
    DshCommandResult, DshGoal, DshState, Effort, EngineCapabilities, Error, History, HistoryImage, Model,
    Models, Perm, Query, SessionActivity, SessionFocus, SessionForked, SessionInfo,
    SessionList, SessionListInvalidated, StateEvent, TurnBinding, TurnDetail,
    TurnEnd, UserMsg,
)
from cc_remote.wrapper.command_router import UNHANDLED_COMMAND
from cc_remote.wrapper.dsh_client import (
    DshClient, DshConnection, DshError, native_session_id, wire_session_id,
)
from cc_remote.wrapper.dsh_stream import DshProjection, history_events, identity, model_id, model_parts
from cc_remote.wrapper.history_store import group_history_events, materialize_history_turns
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import ActiveTurnBinding, SessionContext

_COMMON = frozenset({
    "hello", "ping", "list_dir", "get_file_preview", "browse_files", "save_markdown",
    "get_preview_asset", "authorize_preview", "get_diff", "get_turn_file_changes",
    "answer_question", "acknowledge_completion", "cancel_queued_query",
    "get_queued_query", "update_queued_query", "query",
})


@dataclass
class DshHandle:
    native_id: str
    model: str | None = None
    effort: str | None = None
    permission_mode: str = ""
    state: DshState = field(default_factory=DshState)
    projection: DshProjection = field(default_factory=DshProjection)
    watch: asyncio.Task | None = None
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
        task, self.watch = self.watch, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.connected = False


class DshRuntime:
    def __init__(self, machine, client: DshClient | None = None):
        self.machine = machine
        self.client = client
        self.lock = asyncio.Lock()
        self.events_task: asyncio.Task | None = None
        self.control_task: asyncio.Task | None = None
        self.events_ready = asyncio.Event()
        self.event_client: str | None = None
        self.questions: dict[str, asyncio.Task] = {}
        self.catalog: list[dict] = []
        self.presets: list[dict] = []
        self.default_model: str | None = None
        self.default_effort: str | None = None
        self.history_pages: OrderedDict[tuple[str, str], tuple[list, int]] = OrderedDict()
        self.history_cursors: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.fork_boundaries: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.image_refs: OrderedDict[tuple[str, str], str] = OrderedDict()
        self.builds: dict[str, int] = {}
        self.closed = False

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
        if cmd.type in _COMMON:
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
        if cmd.type == "set_dsh_control" and cmd.kind == "command" and cmd.cmd_id:
            msg = DshCommandResult(sid=sid, request_id=cmd.cmd_id,
                status="unknown" if exc.outcome_unknown else "error", text=str(exc), to=cmd.client_id)
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
        msg = Error(code="dsh_" + exc.code, message=str(exc), sid=sid,
                    to=getattr(cmd, "client_id", None), request_id=getattr(cmd, "cmd_id", None),
                    msg_id=getattr(cmd, "msg_id", None))
        await self.machine.transport.send(msg)
        if cmd.type == "get_models":
            await self.machine.transport.send(Models(
                engine="dsh", models=[], error=str(exc), to=getattr(cmd, "client_id", None)))
        return msg

    async def ctx(self, sid) -> SessionContext:
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
        # Respect the shared pool bound; a cold history read never evicts work.
        if len(self.machine.sessions) >= self.machine.cfg.max_concurrent_sessions:
            candidates = [c for c in self.machine.sessions.values()
                          if c.engine == "dsh" and c.state == "idle" and not c.queued_queries
                          and not c.pending_asks and not c.query_lock.locked()
                          and c.key != self.machine.focused_sid]
            if not candidates:
                raise DshError("session_limit", "驻留会话已满，请先结束一个运行中的会话。")
            victim = candidates[0]
            await victim.sdk.disconnect()
            self.machine.sessions.pop(victim.key, None)
        handle = DshHandle(native)
        # A successful read proves reachability without activating an Agent.
        # Explicit controls (e.g. resume goal) may establish follow afterwards.
        handle.state.connected = True
        ctx = SessionContext(session_id=sid, key=sid, sdk=handle, cwd=snapshot["header"]["cwd"],
                             engine="dsh", buffer=RingBuffer(
                                 self.machine.cfg.ring_max_events, self.machine.cfg.ring_max_bytes))
        self.machine.sessions[sid] = ctx
        self._project(snapshot, handle.projection)
        handle.model, handle.effort = handle.projection.model, handle.projection.effort
        await self.snapshot_projections(ctx, snapshot)
        return ctx

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
                self.image_refs[key] = ref["attachment_id"]
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

    async def handle_get_history(self, cmd):
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
        projection, frames = self._project(snapshot)
        events = history_events(frames)
        self._cache_history(sid, snapshot, projection, events)
        rows = list(materialize_history_turns(events))
        for row in rows:
            if row.get("forkPointId") not in projection.fork_seqs:
                row.pop("forkPointId", None)
            refs = projection.image_refs.get(row["id"])
            if refs:
                row["imageRefs"] = [{k: v for k, v in ref.items() if k != "attachment_id"} for ref in refs]
                row["detailReasons"] = list(dict.fromkeys([*row.get("detailReasons", []), "image_deferred"]))
        self.builds[sid] = self.builds.get(sid, 0) + (0 if cmd.before else 1)
        result = History(
            sid=sid, session_id=sid, revision=self.machine.instance_id,
            generation=self.machine.instance_id, build_seq=self.builds[sid],
            live_seq=live_seq, to=cmd.client_id,
            detail=cmd.detail, events=events if cmd.detail == "full" else [],
            turns=rows if cmd.detail == "summary" else [],
            before=cmd.before, has_more=snapshot["hasMore"],
            oldest_id=f"dsh-seq-{snapshot['records'][0]['event']['seq']}" if snapshot["records"] else None,
            newest_id=rows[-1]["id"] if rows else None,
            in_progress=bool(ctx and ctx.state != "idle"),
            control=self.machine._session_control(ctx) if ctx else None,
        )
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

    async def handle_get_history_image(self, cmd):
        if cmd.revision not in {None, self.machine.instance_id}:
            raise DshError("history_expired", "图片引用已更新，请刷新对应历史页。")
        attachment = self.image_refs.get((cmd.session_id, cmd.image_id))
        if not attachment:
            raise DshError("history_expired", "图片引用已过期，请刷新对应历史页。")
        value = await self.client.rpc("session/attachment", {"request": {
            "sessionId": native_session_id(cmd.session_id), "attachmentId": attachment}})
        result = self.history_image_response(cmd, data=value["data"],
            media_type=value["attachment"]["mediaType"],
            width=value["attachment"].get("width"), height=value["attachment"].get("height"))
        await self.machine.transport.send(result)
        return result

    def history_image_response(self, cmd, **fields):
        return HistoryImage(session_id=cmd.session_id, turn_id=cmd.turn_id,
            image_id=cmd.image_id, variant=cmd.variant, request_id=cmd.request_id,
            revision=self.machine.instance_id, to=cmd.client_id, **fields)

    async def read_catalog(self):
        catalog, presets = await asyncio.gather(
            self.client.rpc("session/modelCatalog"), self.client.rpc("agentPresets/list"))
        self.catalog = [{
            "id": model_id(group["id"], model["id"]),
            "display_name": model.get("name") or model["id"],
            "description": model.get("description", ""),
            "efforts": [e["id"] for e in model.get("reasoning", {}).get("efforts", [])],
            "default_effort": model.get("reasoning", {}).get("defaultEffort"),
        } for group in catalog["groups"] if group["id"] == "deepseek-official"
          for model in group["models"] if model["id"] == "deepseek-flash"]
        default = catalog.get("default") or {}
        self.default_model = self.catalog[0]["id"] if self.catalog else None
        self.default_effort = self.catalog[0]["default_effort"] if self.catalog else None
        if (self.catalog and default.get("provider") == "deepseek-official"
                and default.get("model") == "deepseek-flash"
                and default.get("reasoningEffort") in self.catalog[0]["efforts"]):
            self.default_effort = default["reasoningEffort"]
        self.presets = [{"id": p["id"], "name": p.get("name") or p["id"],
                         "description": p.get("description", ""), "is_default": p.get("isDefault", False),
                         "available": not bool(p.get("broken"))} for p in presets["presets"]]

    async def handle_get_models(self, cmd):
        await self.read_catalog()
        result = Models(engine="dsh", models=self.catalog, dsh_presets=self.presets,
                        default_model=self.default_model, default_effort=self.default_effort,
                        to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(result)
        return result

    async def handle_list_sessions(self, cmd):
        items = await self.client.list_sessions()
        rows = []
        pins = self.machine._session_pins.ids("dsh") if self.machine._session_pins else set()
        for item in items:
            sid = wire_session_id(item["sessionId"])
            values = item.get("projections", {}).get("values", {})
            row = SessionInfo(session_id=sid, native_session_id=item["sessionId"],
                              engine="dsh", summary=values.get("title"), cwd=item.get("cwd"),
                              state="running" if item["running"] else "idle", pinned=sid in pins,
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

    async def handle_switch_session(self, cmd):
        ctx = await self.ctx(cmd.session_id)
        self.machine.focused_sid = ctx.key
        await self.machine.transport.send(SessionFocus(
            session_id=ctx.key, cwd=ctx.cwd, to=getattr(cmd, "client_id", None)))
        await self.projections(ctx, {})
        await self.machine._emit(ctx, self.machine._session_control(ctx))
        items = await self.client.list_sessions()
        if any(i["sessionId"] == ctx.sdk.native_id and i["running"] for i in items):
            await self.activate(ctx)
        return None

    async def handle_new_session(self, cmd):
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
            raise DshError("invalid_model", "DSH 当前未提供 DeepSeek V4.1 Flash，请检查本机模型配置。")
        self.validate_model(selected, effort)
        value = await self.client.rpc("session/create", {"request": {
            "cwd": cwd, **({"agentPreset": preset} if preset else {})}})
        sid = wire_session_id(value["sessionId"])
        ctx = await self.ctx(sid)
        await self.activate(ctx)
        # A native default or Agent Preset may still select another model.
        # Always establish the offered model before admitting the first prompt.
        await self.select_model(ctx, selected, effort)
        self.machine.focused_sid = sid
        await self.machine.transport.send(SessionFocus(
            session_id=sid, cwd=ctx.cwd, request_id=cmd.request_id, to=cmd.client_id))
        await self.projections(ctx, {})
        await self.invalidate()
        if cmd.prompt is not None or cmd.images or cmd.files:
            return await self.query(ctx, Query(sid=sid, prompt=cmd.prompt or "", msg_id=cmd.msg_id,
                                              images=cmd.images, files=cmd.files, client_id=cmd.client_id))
        return None

    async def activate(self, ctx):
        handle = ctx.sdk
        if handle.watch is None or handle.watch.done():
            handle.ready.clear()
            handle.watch = asyncio.create_task(self._follow(ctx), name=f"dsh-follow-{ctx.key}")
        try:
            await asyncio.wait_for(handle.ready.wait(), 15)
            await asyncio.wait_for(self.events_ready.wait(), 15)
        except asyncio.TimeoutError:
            raise DshError("disconnected", "DSH 连接尚未就绪，请检查本机 DSH。") from None
        if not handle.connected:
            raise DshError("disconnected", handle.state.error or "DSH 连接中断。")

    async def query(self, ctx, cmd, *, steer=False, launch_receipt=None):
        try:
            await self.connection()
            await self.activate(ctx)
            if not steer and ctx.state != "idle":
                raise DshError("busy", "DSH 正在运行，请使用引导或排队。")
            handle = ctx.sdk
            if cmd.msg_id in handle.admitted:
                if launch_receipt and not launch_receipt.done():
                    launch_receipt.set_result(True)
                return None
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

    async def content(self, ctx, cmd):
        blocks = [{"type": "text", "text": cmd.prompt}] if cmd.prompt else []
        for img in cmd.images or []:
            blocks.append({"type": "image", "mediaType": img["media_type"], "data": img["data"]})
        for file in cmd.files or []:
            uploaded = await self.client.rpc("fileUploads/upload", {
                "agentId": ctx.sdk.native_id, "request": {"name": file["filename"], "data": file["data"]}})
            blocks.append({"type": "file", "receiptId": uploaded["receiptId"]})
        return blocks

    async def handle_steer(self, cmd):
        ctx = await self.ctx(cmd.sid)
        async with ctx.query_lock:
            return await self.query(ctx, cmd, steer=True)

    async def handle_interrupt(self, cmd):
        ctx = await self.ctx(getattr(cmd, "sid", None))
        await self.client.rpc("session/cancel", {"request": {"sessionId": ctx.sdk.native_id}})
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
        await self.read_catalog()
        self.validate_model(selected, effort)
        await self.activate(ctx)
        provider, model = model_parts(selected)
        value = await self.client.rpc("session/selectModel", {"request": {
            "sessionId": ctx.sdk.native_id, "provider": provider, "model": model,
            **({"reasoningEffort": effort} if effort else {})}})
        chosen = value["selected"]
        await self.projections(ctx, {"modelSelection": {"next": chosen}})

    async def handle_set_model(self, cmd):
        ctx = await self.ctx(getattr(cmd, "sid", None))
        await self.select_model(ctx, cmd.model)

    async def handle_set_dsh_control(self, cmd):
        ctx = await self.ctx(cmd.sid)
        await self.activate(ctx)
        if cmd.kind == "effort":
            return await self.select_model(ctx, ctx.sdk.model or self.default_model, cmd.value)
        if cmd.kind == "permission":
            if cmd.value == "custom" or cmd.value not in {p.value for p in ctx.sdk.state.permissions}:
                raise DshError("invalid_permission", "请选择 DSH 提供的权限预设。")
            line = "/permission " + cmd.value
        else:
            line = cmd.value
        attachments = None
        if cmd.kind == "command":
            self.validate_command(ctx, line, bool(cmd.images or cmd.files))
            attachments = await self.content(ctx, SimpleNamespace(prompt="", images=cmd.images, files=cmd.files))
        value = await self.command(ctx, line, attachments)
        if cmd.kind == "command" and cmd.cmd_id:
            result = DshCommandResult(sid=ctx.key, request_id=cmd.cmd_id, status="success",
                text=str(value.get("result", {}).get("text", ""))[:16384], to=cmd.client_id)
            await self.machine.transport.send(result)
            return result
        return None

    def validate_command(self, ctx, line, attachments=False):
        name = line.lstrip("/").split(maxsplit=1)[0] if line.strip() else ""
        command = next((c for c in ctx.sdk.commands if c["name"] == name), None)
        if command is None:
            raise DshError("unknown_command", "此 DSH 会话没有这个命令。")
        if attachments and not (command.get("input") or {}).get("attachments"):
            raise DshError("command_attachments", "这个 DSH 命令不接受附件。")

    async def command(self, ctx, line, attachments=None):
        await self.activate(ctx)
        self.validate_command(ctx, line, attachments)
        value = await self.client.rpc("commands/execute", {
            "agentId": ctx.sdk.native_id, "line": line, "submittedAttachments": attachments or []})
        if value is None:
            raise DshError("unknown_command", "此 DSH 会话没有这个命令。")
        if value.get("result", {}).get("kind") == "error":
            raise DshError("command_failed", str(value["result"].get("text", "DSH 命令未成功。"))[:4096])
        await self.refresh_goal(ctx)
        return value

    async def handle_compact_session(self, cmd):
        return await self.command(await self.ctx(cmd.session_id), "/compact")

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
        ctx = await self.ctx(getattr(cmd, "sid", None))
        value = await self.client.rpc("skills/list", {"request": {"sessionId": ctx.sdk.native_id}})
        result = EngineCapabilities(engine="dsh", space="code", cwd=ctx.cwd,
                                    request_id=cmd.cmd_id, to=cmd.client_id, skills_only=cmd.skills_only,
                                    items=[{"kind": "skill", "id": s["name"], "name": s["name"],
                                            "description": s.get("description", ""), "enabled": True,
                                            "actions": []} for s in value["skills"]],
                                    notes=["DSH 插件由设备上的 DSH 配置管理。"])
        await self.machine.transport.send(result)
        return result

    async def handle_rename_session(self, cmd):
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

    async def handle_fork_session(self, cmd):
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
        sid = wire_session_id(value["sessionId"])
        snapshot = await self.client.history_snapshot(sid, max_messages=1)
        result = SessionForked(parent_session_id=cmd.session_id, session_id=sid,
                              cwd=snapshot["header"]["cwd"], target="same_cwd",
                              last_turn_id=turn_id, request_id=cmd.request_id, to=cmd.client_id)
        await self.machine.transport.send(result)
        await self.invalidate()
        return result

    async def invalidate(self):
        await self.machine.transport.send(SessionListInvalidated(engine="dsh"))

    async def snapshot_projections(self, ctx, snapshot):
        projection = snapshot.get("projections", {})
        await self.projections(ctx, projection.get("values", {}), seq=projection.get("asOfSeq", snapshot.get("cursor")))

    async def projections(self, ctx, values, *, seq=None):
        handle = ctx.sdk
        if seq is not None:
            values = {key: value for key, value in values.items()
                      if seq >= handle.projection_seqs.get(key, -1)}
            for key in values:
                handle.projection_seqs[key] = seq
        if "title" in values:
            self.machine._remember_notification_title(ctx.key, values["title"])
            await self.invalidate()
        if values and not {"agentPreset", "permissions", "modelSelection", "contextPressure", "goal"}.intersection(values):
            return
        state = handle.state.model_copy(deep=True)
        if "agentPreset" in values:
            state.agent_preset = values["agentPreset"]
        if "permissions" in values and values["permissions"] is not None:
            permissions = values["permissions"]
            state.permissions = []
            from cc_remote.protocol import DshPermissionOption
            for option in permissions.get("options", []):
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
            value = await self.client.rpc("goals/get", {"agent": handle.native_id})
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
                        projection, _ = self._project(snapshot)
                        handle.projection = projection
                        handle.connected = True
                        handle.state.connected, handle.state.error = True, None
                        await self.machine._set_session_control(ctx, control_mode="remote",
                            write_state="writable", terminal_attached=False)
                        await self.snapshot_projections(ctx, frame)
                        commands = await self.client.rpc("commands/list", {"agentId": handle.native_id})
                        handle.commands = commands
                        from cc_remote.protocol import DshCommandInfo
                        handle.state.commands = [DshCommandInfo(
                            name=c["name"], description=c.get("description", ""),
                            input_hint=(c.get("input") or {}).get("hint"),
                            attachments=(c.get("input") or {}).get("attachments", False)) for c in commands]
                        await self.machine._emit(ctx, handle.state.model_copy(deep=True))
                        # Reconnect restores the canonical tail before additional deltas.
                        await self.handle_get_history(SimpleNamespace(
                            session_id=ctx.key, before=None, limit=4, detail="summary", client_id=None))
                        for message in projection.baseline(frame.get("assistantStream") or {}):
                            await self.emit(ctx, message)
                        ctx.state = "running" if projection.running else "idle"
                        await self.machine._emit(ctx, StateEvent(state=ctx.state))
                        handle.ready.set()
                        await self.refresh_goal(ctx)
                        delay = 0.5
                    elif kind == "event":
                        for message in handle.projection.record(frame["event"]):
                            await self.emit(ctx, message)
                        if frame["event"]["type"] == "turn/start":
                            ctx.state = "running"
                            await self.machine._emit(ctx, StateEvent(state="running"))
                        if frame["event"]["type"] == "turn/end":
                            self.cache_forks(ctx.key, handle.projection)
                            ctx.state = "idle"
                            ctx.active_msg_id = None
                            ctx.active_turn_binding = None
                            ctx.queued_query_wakeup.set()
                            await self.machine._emit(ctx, StateEvent(state="idle"))
                    elif kind == "assistant-stream":
                        for message in handle.projection.frame(frame["frame"]):
                            await self.emit(ctx, message)
                raise DshError("disconnected", "DSH 连接已断开，正在重新连接。")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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
                            await self.projections(ctx, {frame["key"]: frame.get("value")}, seq=frame.get("seq"))
            except asyncio.CancelledError:
                raise
            except Exception:
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
                        if event_id not in self.questions:
                            self.questions[event_id] = asyncio.create_task(self._question(frame, self.event_client))
                    elif kind == "cancel":
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
                            if ctx and args[1] and (ctx.sdk.watch is None or ctx.sdk.watch.done()):
                                ctx.sdk.watch = asyncio.create_task(self._follow(ctx))
                        if frame.get("event") in {"api-session/added", "api-session/removed"}:
                            await self.invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                self.events_ready.clear()
                self.event_client = None
                pending = list(self.questions.values())
                self.questions.clear()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if not self.closed:
                await asyncio.sleep(2)

    async def _question(self, frame, client_id):
        event_id = frame["eventId"]
        try:
            sid = wire_session_id(frame.get("agentId", ""))
            ctx = self.machine.sessions.get(sid)
            if ctx is None:
                # Native delegated agents ask in their own scope. Display their
                # question on a proven resident ancestor, never on current focus.
                items = {item["sessionId"]: item for item in await self.client.list_sessions()}
                native = native_session_id(sid)
                for _ in range(8):
                    item = items.get(native, {})
                    if item.get("origin") != "subagent" or not item.get("parentSessionId"):
                        break
                    native = item["parentSessionId"]
                    ctx = self.machine.sessions.get(wire_session_id(native))
                    if ctx is not None:
                        break
            outcome = {"kind": "next"}
            if ctx is not None and ctx.engine == "dsh":
                request = frame.get("request", {})
                event = frame.get("event")
                if event == "approval/request":
                    answer = await self.machine._on_ask_optional(
                        ctx, f"DSH 请求使用工具：{request.get('toolName', '工具')}\n{request.get('reason', '')}"[:16000],
                        [{"label": "允许一次", "ds": "仅批准这一次工具调用"}, {"label": "拒绝", "ds": "不执行这次调用"}],
                        ask_id=identity(f"{client_id}:{event_id}", "dsh-ask"))
                    outcome = {"kind": "result", "value": "allowed-once" if answer == "允许一次" else "rejected" if answer else "cancelled"}
                elif event == "user-questions/request":
                    answers = []
                    async with ctx.ask_lock:
                        for index, q in enumerate(request.get("questions", [])):
                            options = [{"label": o["label"], "ds": o.get("description", "")} for o in q.get("options", [])]
                            question = q["question"] + ("\n" + q["detail"] if q.get("detail") else "")
                            if len(options) > 5:
                                question += "\n其余选项可在回答框填写：\n" + "\n".join(o["label"] + ": " + o["ds"] for o in options[5:])
                            answer = await self.machine._on_ask_locked(
                                ctx, question,
                                options[:5], header=q.get("header"), allow_text=True,
                                multi_select=q.get("multiSelect", False),
                                ask_id=identity(f"{client_id}:{event_id}:{index}", "dsh-ask"))
                            selected = answer if isinstance(answer, list) else [answer]
                            labels = {o["label"] for o in options}
                            answers.append({"id": q["id"], "selected": [a for a in selected if a in labels],
                                            **({"custom": "\n".join(a for a in selected if a not in labels)} if any(a not in labels for a in selected) else {})})
                    outcome = {"kind": "result", "value": {"answers": answers}}
            if client_id == self.event_client:
                await self.client.rpc("$events/result", {"clientId": client_id, "eventId": event_id, "outcome": outcome})
        except asyncio.CancelledError:
            raise
        except Exception:
            if client_id == self.event_client:
                with suppress(DshError):
                    await self.client.rpc("$events/result", {"clientId": client_id, "eventId": event_id,
                                                             "outcome": {"kind": "next"}})
        finally:
            self.questions.pop(event_id, None)

    async def close(self):
        self.closed = True
        await asyncio.gather(*(ctx.sdk.disconnect() for ctx in self.machine.sessions.values()
                               if ctx.engine == "dsh"), return_exceptions=True)
        tasks = [task for task in (self.events_task, self.control_task, *self.questions.values()) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.close()
