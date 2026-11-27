"""Route Gemini Code sessions to the device's native Antigravity ACP server."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time
from collections import OrderedDict
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from cc_remote.attachments import decode_attachment, validate_attachments
from cc_remote.log import logger
from cc_remote.update import UpdateError
from cc_remote.update_jobs import read_json, write_json
from cc_remote.protocol import (
    ASK_OPTION_MAX_COUNT, ASK_QUESTION_MAX_CHARS, ASK_QUESTION_MAX_COUNT, ERR_BAD_PROMPT, ERR_BUSY, ERR_NOT_STEERABLE,
    AntigravityAuthResult, AntigravityAvailability, AntigravityOption, AntigravityState, AskQuestion, ContextReport, Effort,
    EngineCapabilities, Error, History, HistoryImage, Model, Models, NativeCommandInfo, Notice, Perm, Query, SessionActivity,
    SessionFocus, SessionInfo, SessionList, SessionListInvalidated, StateEvent,
    TurnBinding, TurnDetail, TurnSteered, TurnUsage, UserMsg,
)
from cc_remote.session_messages import reference
from cc_remote.wrapper.antigravity_client import AcpClient, AcpError, AntigravityLaunch, antigravity_opted_out
from cc_remote.wrapper.antigravity_controls import CONTROL_CATEGORIES, AntigravityControlStore
from cc_remote.wrapper.antigravity_history import apply_native_turn_times, read_native_turn_times
from cc_remote.wrapper.antigravity_metadata import AntigravityMetadata, AntigravityMetadataStore
from cc_remote.wrapper.antigravity_quota import AntigravityQuotaReader
from cc_remote.wrapper.antigravity_stream import (
    MCP_SEND_MESSAGE, AntigravityTranscript, config_options, context_usage, event_id, native_diff, prompt_usage, records, slash_commands,
    text, wire_session_id,
)
from cc_remote.wrapper.antigravity_tools import AntigravityTools
from cc_remote.wrapper.artifact_inspection import FILE_LINK_GUIDANCE
from cc_remote.wrapper.claude_questions import AskDeclined, AskUnavailable
from cc_remote.wrapper.command_router import UNHANDLED_COMMAND
from cc_remote.wrapper.dsh_mutations import DshMutationJournal, digest
from cc_remote.wrapper.engine_capabilities import antigravity_capabilities
from cc_remote.wrapper.history_store import history_image_id, materialize_history_turns
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import SessionContext

log = logger("antigravity_runtime")
_ENGINE = "antigravity"
_MISSING_DIRECTORY = "Gemini 工作目录已不存在。仍可查看历史，恢复原目录后可继续。"
_DISCONNECTED = "本机 agy 连接已断开，请重新打开会话。"
_WORKSPACE_ENV = "CC_REMOTE_ANTIGRAVITY_TRUSTED_WORKSPACES"
_WORKSPACE_COMMANDS = frozenset({"/add-dir", "/projects", "/resume"})
_CONTROL_LABELS = {"model": "模型", "thought_level": "思考强度", "mode": "权限模式"}
_CODE_PROMPT_APPEND = (
    "When you need the user's decision, use the CC Remote ask_user tool. "
    "It presents a questionnaire and returns the user's submitted answers.\n\n"
    + FILE_LINK_GUIDANCE
)
_CANCEL_TIMEOUT = 15
_CLOSE_TIMEOUT = 4
_FOLLOWUP_NOTICE_AFTER = 15.0
_FOLLOWUP_STOP_SETTLE = .2
_NEW_WORK = frozenset({"new_session", "query", "steer", "get_models", "antigravity_auth"})
_COMMON = frozenset({
    "hello", "ping", "list_dir", "answer_question",
    "acknowledge_completion", "cancel_queued_query", "get_queued_query", "update_queued_query",
    "reorder_queued_queries", "browse_files", "get_file_preview", "save_markdown",
    "get_preview_asset", "authorize_preview", "get_session_artifacts",
    "get_session_artifact_preview", "get_diff", "get_turn_file_changes",
})


def _cwd(value):
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if path.is_dir():
            return str(path)
    except (OSError, ValueError, TypeError, RuntimeError):
        pass
    raise AcpError("directory", "Gemini 工作目录不存在或不可读取。")


@dataclass
class PromptTurn:
    started: float = field(default_factory=time.monotonic)
    responses: list[tuple[str, asyncio.Future[dict]]] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    stopping: bool = False
    force_requested: bool = False
    delivery_msg_id: str | None = None
    autonomous: bool = False
    activity_at: float = field(default_factory=time.monotonic)
    native_stop: tuple[float, str] | None = None
    interactions: int = 0

    def touch(self):
        self.activity_at = time.monotonic()
        self.changed.set()

    @contextmanager
    def interaction(self):
        self.interactions += 1
        try:
            yield
        finally:
            self.interactions -= 1
            self.touch()


@dataclass
class AntigravityHandle:
    client: AcpClient
    native_id: str = ""
    model: str | None = None
    effort: str | None = None
    permission_mode: str = ""
    options: dict[str, tuple[str, AntigravityOption]] = field(default_factory=dict)
    commands: list[NativeCommandInfo] = field(default_factory=list)
    commands_ready: asyncio.Event = field(default_factory=asyncio.Event)
    usage: ContextReport | None = None
    transcript: AntigravityTranscript = field(default_factory=AntigravityTranscript)
    turn: PromptTurn | None = None
    stream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_ready: asyncio.Event = field(default_factory=asyncio.Event)
    revision: str = field(default_factory=lambda: uuid4().hex)
    build_seq: int = 0
    title: str | None = None
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    attached: bool = True
    disconnect_published: bool = False
    tools: AntigravityTools | None = None
    saved_controls: dict[str, str] = field(default_factory=dict)
    unrestored_controls: dict[str, str] = field(default_factory=dict)
    history_params: dict | None = None

    def __post_init__(self):
        self.input_ready.set()

    @property
    def connected(self):
        return self.attached and self.client.running

    @property
    def turn_active(self):
        return self.turn is not None

    def controls(self):
        return AntigravityState(connected=self.connected,
                                options=[option for _, option in self.options.values()],
                                commands=self.commands)

    def update(self, update):
        kind = update.get("sessionUpdate")
        if kind == "config_option_update":
            self.set_options(update)
        elif kind == "available_commands_update":
            self.commands = slash_commands(update.get("availableCommands"))
            self.commands_ready.set()
        elif kind == "usage_update":
            self.usage = context_usage(update) or self.usage
        elif kind == "session_info_update":
            self.title = text(update.get("title"), 512) or self.title
        elif kind == "current_mode_update" and "mode" in self.options:
            config_id, option = self.options["mode"]
            current = text(update.get("currentModeId"), 256)
            if current:
                self.options["mode"] = (config_id, option.model_copy(update={"current": current}))
                self.permission_mode = current

    def set_options(self, result):
        if "configOptions" not in result:
            return
        self.options = config_options(result["configOptions"])
        model = self.current("model")
        if self.model and model != self.model:
            self.usage = None
        self.model = model
        self.effort = self.current("thought_level")
        self.permission_mode = self.current("mode") or ""

    def current(self, category):
        pair = self.options.get(category)
        return pair[1].current if pair else None

    async def disconnect(self):
        capabilities = self.client.capabilities.get("sessionCapabilities") or {}
        try:
            if self.native_id and self.connected and "close" in capabilities:
                with suppress(AcpError):
                    await self.client.call("session/close", {"sessionId": self.native_id})
        finally:
            self.attached = False
            if self.tools:
                await self.tools.close()


class AntigravityRuntime:
    def __init__(self, machine, *, launch_factory=AntigravityLaunch.local, args=()):
        self.machine = machine
        self.launch_factory = launch_factory
        self.opted_out = antigravity_opted_out
        self.args = args
        self.client: AcpClient | None = None
        self.connect_lock = asyncio.Lock()
        self.setup_lock = asyncio.Lock()
        self.controls_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.quota = AntigravityQuotaReader()
        self.draining = False
        self.install_fence: Path | None = None
        self.opening: AntigravityHandle | None = None
        self.early_controls: OrderedDict[str, AntigravityHandle] = OrderedDict()
        owner_state = Path(machine.cfg.antigravity_owner_state_dir or machine.cfg.state_dir)
        self.mutations = DshMutationJournal(owner_state, engine=_ENGINE, label="Gemini", error_type=AcpError)
        self.control_store = AntigravityControlStore(owner_state)
        self.metadata = AntigravityMetadataStore(owner_state)
        # Earlier catalogs may point at sessions created in a project.
        self.catalog_path = owner_state / "antigravity-control-catalog.json"
        self.catalog: OrderedDict[str | None, tuple[float, AntigravityHandle, str | None]] = OrderedDict()
        self.cached_history: OrderedDict[str, tuple[float, AntigravityHandle]] = OrderedDict()

    def available(self):
        return not self.opted_out() and bool(self.launch_factory())

    def availability(self, cmd):
        return AntigravityAvailability(
            enabled=self.available(), to=cmd.client_id,
            route_id=getattr(cmd, "route_id", None),
        )

    def admission_paused(self):
        return self.draining or (self.install_fence is not None and self.install_fence.exists())

    def targets(self, cmd):
        engine = getattr(cmd, "engine", None)
        sid = self.sid(cmd)
        if engine == _ENGINE or (isinstance(sid, str) and sid.startswith("agy@")):
            return True
        return (engine is None and sid is None
                and cmd.type in {"interrupt", "get_context", "set_model", "set_effort", "set_perm"}
                and (ctx := self.machine._focused_ctx()) is not None and ctx.engine == _ENGINE)

    @staticmethod
    def sid(cmd):
        return getattr(cmd, "session_id", None) or getattr(cmd, "sid", None)

    async def dispatch(self, cmd):
        if not self.targets(cmd):
            return UNHANDLED_COMMAND
        try:
            if (getattr(cmd, "engine", None) not in (None, _ENGINE)
                    or getattr(cmd, "space", "code") != "code"):
                raise AcpError("scope", "此操作与 Gemini Code 会话不匹配。")
            # Reject new admissions while letting accepted turns, approvals
            # and wrapper-owned queues continue on the resident ACP process.
            if cmd.type in _NEW_WORK and self.opted_out():
                raise AcpError("unavailable", "此设备已关闭 Gemini 新会话和消息。")
            if cmd.type == "query":
                await self.context(cmd.sid)
                return UNHANDLED_COMMAND
            if cmd.type in _COMMON:
                return UNHANDLED_COMMAND
            handler = getattr(self, "handle_" + cmd.type, None)
            if handler is None:
                raise AcpError("unsupported", "当前 Gemini ACP 不支持此操作。")
            return await handler(cmd)
        except AcpError as exc:
            return await self.error(cmd, exc)

    async def error(self, cmd, exc):
        sid = self.sid(cmd)
        target = {"sid": sid, "to": getattr(cmd, "client_id", None)}
        if cmd.type == "get_models":
            return await self.send(Models(engine=_ENGINE, cwd=cmd.cwd, antigravity_model=cmd.antigravity_model, error=str(exc), **target))
        if cmd.type == "antigravity_auth":
            client = self.client
            return await self.send(AntigravityAuthResult(
                request_id=cmd.cmd_id, to=cmd.client_id, error=str(exc),
                methods=client.auth_methods if client else [],
            ))
        if cmd.type == "get_history":
            return await self.send(History(
                session_id=sid, revision=self.machine.instance_id, detail=cmd.detail,
                before=cmd.before, authoritative=False, error=str(exc), **target))
        if cmd.type == "get_history_image":
            return await self.history_image(cmd, self.machine.instance_id, error=str(exc))
        if cmd.type == "get_turn_detail":
            return await self.send(TurnDetail(
                session_id=sid, turn_id=cmd.turn_id, before=cmd.before,
                revision=self.machine.instance_id, authoritative=False,
                reset_required=True, error=str(exc), **target))
        if cmd.type == "list_sessions":
            await self.send(SessionList(engine=_ENGINE, sessions=[], request_id=cmd.cmd_id, **target))
        if cmd.type == "open_btw":
            return await self.machine._send_btw_error(cmd, "gemini_unsupported", str(exc))
        code = exc.code
        if code not in {ERR_BUSY, ERR_BAD_PROMPT, ERR_NOT_STEERABLE}:
            code = "gemini_" + code
        return await self.send(Error(
            code=code, message=str(exc), request_id=getattr(cmd, "request_id", None) or cmd.cmd_id,
            msg_id=getattr(cmd, "msg_id", None), **target))

    async def send(self, frame):
        await self.machine.transport.send(frame)
        return frame

    def trusted_workspace(self, cwd: str) -> str:
        try:
            workspace = str(Path(cwd).resolve())
        except (OSError, ValueError, RuntimeError):
            raise AcpError("directory", "Gemini 工作目录无效或不可读取。") from None
        try:
            allowed = json.loads(self.machine.cfg.antigravity_trusted_workspaces)
        except (ValueError, RecursionError):
            allowed = None
        if not isinstance(allowed, list) or any(
            not isinstance(path, str) or not os.path.isabs(path) or "\0" in path for path in allowed
        ):
            raise AcpError("workspace_config", f"请检查 Wrapper 上的 {_WORKSPACE_ENV}，它需要一个绝对路径的 JSON 数组。")
        # Compare with the configured physical path verbatim. A symlink named
        # in the trust list must not silently transfer trust to a new target.
        if workspace not in allowed:
            raise AcpError("workspace_untrusted", f"此 Gemini 目录尚未受信任。请在 Wrapper 的 {_WORKSPACE_ENV} 中加入该目录的物理路径。")
        return workspace

    async def connection(self):
        async with self.connect_lock:
            if self.client is not None:
                if self.client.running:
                    return self.client
                await self.client.close()
                await self.close_tools(self.client)
            launch = self.launch_factory()
            if launch is None:
                raise AcpError("unavailable", "此设备未启用 Gemini 或没有安装 agy。")
            client = AcpClient(launch, self.on_update,
                               self.on_request, args=self.args, on_disconnect=self.on_disconnect,
                               on_lifecycle=self.on_native_lifecycle)
            await client.start()
            self.client = client
            return client

    async def close(self):
        self.quota.invalidate()
        if self.client:
            client = self.client
            await self.client.close()
            await self.close_tools(client)
            for ctx in list(self.machine.sessions.values()):
                if ctx.engine == _ENGINE and ctx.sdk.client is client and ctx.sdk.turn:
                    ctx.sdk.turn.changed.set()
        self.catalog.clear()
        self.cached_history.clear()

    async def close_tools(self, client):
        await asyncio.gather(*(ctx.sdk.tools.close() for ctx in list(self.machine.sessions.values())
                               if ctx.engine == _ENGINE and ctx.sdk.client is client and ctx.sdk.tools))

    async def on_disconnect(self, client):
        for ctx in list(self.machine.sessions.values()):
            if ctx.engine != _ENGINE:
                continue
            handle = ctx.sdk
            # wait_turn owns the terminal events of an in-flight prompt.
            if handle.client is not client or not handle.attached:
                continue
            if handle.turn is not None:
                handle.turn.changed.set()
            else:
                try:
                    await self.publish_disconnect(ctx, handle)
                except Exception as exc:
                    log.warning("Gemini disconnect event unavailable", sid=ctx.key,
                                error_type=type(exc).__name__)

    async def publish_disconnect(self, ctx, handle, *, interrupted=False):
        if (ctx.sdk is not handle or not self.machine._is_resident_context(ctx)
                or handle.disconnect_published):
            return
        handle.disconnect_published = True
        if handle.tools:
            await handle.tools.close()
        await self.machine._set_session_control(
            ctx, control_mode="remote", write_state="read_only", terminal_attached=False,
            reason=_DISCONNECTED)
        await self.machine._emit(ctx, handle.controls())
        if background := handle.transcript.close_background(interrupted=interrupted):
            for frame in background:
                await self.emit(ctx, frame)
            await self.machine._emit(ctx, self.machine._background_process_sync(ctx))

    async def rows(self, *, wanted=None):
        client = await self.connection()
        if "list" not in (client.capabilities.get("sessionCapabilities") or {}):
            raise AcpError("version", "本机 agy 缺少 ACP 会话列表支持，请更新 Antigravity CLI。")
        params = {}
        visited = set()
        rows = {}
        for _ in range(100):
            result = await client.call("session/list", params)
            for row in records(result.get("sessions")):
                sid = wire_session_id(row.get("sessionId"))
                if sid:
                    rows[sid] = row
                if wanted is not None and sid == wanted:
                    return rows
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor or cursor in visited:
                break
            visited.add(cursor)
            params = {"cursor": cursor}
        return rows

    async def session_cwd(self, sid):
        ctx = self.machine.sessions.get(sid)
        if ctx and ctx.engine == _ENGINE:
            return ctx.cwd
        row = (await self.rows(wanted=sid)).get(sid)
        if row is None:
            raise AcpError("session", "找不到此 Gemini 会话，请刷新会话列表。")
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd or len(cwd) > 4096:
            raise AcpError("directory", "Gemini 历史记录中的工作目录无效。")
        # ACP can replay history at its recorded path even after a worktree
        # was removed. Only commands capable of working in it need existence.
        return cwd

    async def open_native(self, cwd: str | None, sid=None, *, interactive=False):
        """Call under setup_lock so early native replay has one exact owner."""
        if cwd is not None:
            cwd = self.trusted_workspace(cwd)
        client = await self.connection()
        # Only the internal, prompt-free catalog passes no user directory.
        cwd = cwd or client.cwd
        if cwd is None:
            raise AcpError("startup", "无法创建 Gemini 私有控制目录。")
        handle = AntigravityHandle(client, sid[4:] if sid else "")
        handle.transcript.native_id = handle.native_id
        self.opening = handle
        tools = None
        identified = False
        try:
            saved = {}
            if sid and interactive:
                async with self.controls_lock:
                    saved = await asyncio.to_thread(self.control_store.get, sid)
            if interactive:
                tools = AntigravityTools(self, cwd)
            params = {"cwd": cwd, "mcpServers": await tools.start() if tools else []}
            if tools:
                metadata = client.capabilities.get("_meta")
                extension = metadata.get("antigravity") if isinstance(metadata, dict) else None
                prompt = extension.get("systemPrompt") if isinstance(extension, dict) else None
                if isinstance(prompt, dict) and prompt.get("append") is True:
                    params["_meta"] = {"antigravity": {"systemPrompt": {"append": _CODE_PROMPT_APPEND}}}
            method = "session/new"
            if sid:
                if not client.capabilities.get("loadSession"):
                    raise AcpError("version", "本机 agy 缺少 ACP 历史加载支持。")
                params["sessionId"] = handle.native_id
                method = ("session/resume" if interactive and
                          "resume" in (client.capabilities.get("sessionCapabilities") or {})
                          else "session/load")
            result = await client.call(method, params)
            native_id = result.get("sessionId", handle.native_id)
            if wire_session_id(native_id) is None or (handle.native_id and handle.native_id != native_id):
                raise AcpError("identity", "本机 agy 返回了不匹配的会话标识。")
            # The session ID is established by the RPC reply. A late update
            # from an earlier catalog probe cannot claim a new session.
            handle = self.opening = self.early_controls.get(native_id, handle)
            handle.native_id = handle.transcript.native_id = native_id
            handle.tools = tools
            if method == "session/resume":
                handle.history_params = params
            identified = True
            await self.finish_history_replay(handle)
            handle.set_options(result)
            # agy sends its first command update just after the new/load
            # response. Keep accepting notifications for this unbound session.
            with suppress(TimeoutError):
                await asyncio.wait_for(handle.commands_ready.wait(), 0.75)
            if saved:
                await self.restore_controls(handle, saved)
            return handle
        except BaseException:
            if identified:
                await handle.disconnect()
            elif tools:
                await tools.close()
            raise
        finally:
            self.opening = None
            self.early_controls.clear()

    @staticmethod
    async def finish_history_replay(handle):
        transcript = handle.transcript
        transcript.complete(replay=True)
        home = getattr(handle.client.launch, "environment", {}).get("HOME")
        if transcript.replay_turns and home and os.path.isabs(home):
            times = await asyncio.to_thread(read_native_turn_times, Path(home), handle.native_id)
            apply_native_turn_times(transcript, times)

    async def load_resumed_history(self, handle):
        """Populate the transcript lazily under setup_lock before any new input."""
        params = handle.history_params
        if params is None:
            return
        params = {**params, "cwd": self.trusted_workspace(params["cwd"])}
        staged = self.opening = AntigravityHandle(handle.client, handle.native_id)
        staged.transcript.native_id = handle.native_id
        try:
            result = await handle.client.call("session/load", params)
            if result.get("sessionId", handle.native_id) != handle.native_id:
                raise AcpError("identity", "本机 agy 返回了不匹配的会话标识。")
            await self.finish_history_replay(staged)
            handle.transcript = staged.transcript
            handle.usage = staged.usage or handle.usage
            handle.history_params = None
        finally:
            self.opening = None

    async def context(self, sid):
        if sid is None:
            focused = self.machine._focused_ctx()
            if focused is None or focused.engine != _ENGINE:
                raise AcpError("session", "请选择 Gemini 会话。")
            sid = focused.key
        ctx = self.machine.sessions.get(sid)
        if self.reusable(ctx):
            self.trusted_workspace(ctx.cwd)
            if ctx.state == "idle":
                await self.sync_directory_control(ctx)
            return ctx
        async with self.setup_lock:
            ctx = self.machine.sessions.get(sid)
            if self.reusable(ctx):
                self.trusted_workspace(ctx.cwd)
                if ctx.state == "idle":
                    await self.sync_directory_control(ctx)
                return ctx
            if ctx and ctx.engine != _ENGINE:
                raise AcpError("scope", "会话与 Gemini 引擎不匹配。")
            cwd = await self.session_cwd(sid)
            if not ctx:
                await self.ensure_capacity()
            else:
                await ctx.sdk.disconnect()
            handle = await self.open_native(cwd, sid, interactive=os.path.isdir(cwd))
            self.cached_history.pop(sid, None)
            if ctx:
                ctx.sdk = handle
                if handle.tools:
                    handle.tools.bind(ctx)
            else:
                ctx = self.add_context(handle, cwd)
            await self.sync_directory_control(ctx)
            await self.persist_controls(ctx)
            await self.publish_controls(ctx)
            if handle.unrestored_controls:
                labels = "、".join(_CONTROL_LABELS[category] for category in handle.unrestored_controls)
                await self.machine._emit(ctx, Notice(
                    notice_id="gemini-controls-restore", severity="warning", category="config",
                    title="Gemini 会话设置需要确认", message=f"本机 agy 无法恢复原来的{labels}，请核对底部的会话设置。",
                ))
            return ctx

    @staticmethod
    def reusable(ctx):
        if ctx is None or ctx.engine != _ENGINE:
            return False
        tools = ctx.sdk.tools
        return ctx.state != "idle" or (ctx.sdk.connected and (
            not os.path.isdir(ctx.cwd) or (tools is not None and tools.workspace_matches())))

    async def sync_directory_control(self, ctx):
        present = os.path.isdir(ctx.cwd)
        await self.machine._set_session_control(
            ctx, control_mode="remote", write_state="writable" if present else "read_only",
            terminal_attached=False, reason=None if present else _MISSING_DIRECTORY,
        )

    async def ensure_capacity(self):
        if not await self.machine._ensure_session_capacity():
            raise AcpError("capacity", "会话驻留已满，请先结束一个运行中的会话。")

    def add_context(self, handle, cwd):
        sid = wire_session_id(handle.native_id)
        ctx = SessionContext(
            session_id=sid, key=sid, engine=_ENGINE, sdk=handle, cwd=cwd,
            buffer=RingBuffer(self.machine.cfg.ring_max_events, self.machine.cfg.ring_max_bytes),
        )
        self.machine.sessions[sid] = ctx
        if handle.tools:
            handle.tools.bind(ctx)
        return ctx

    async def list_sessions(self):
        entries = await self.rows()
        for sid, ctx in self.machine.sessions.items():
            if ctx.engine == _ENGINE and sid not in entries and ctx.sdk.connected:
                entries[sid] = {"sessionId": ctx.sdk.native_id, "cwd": ctx.cwd, "updatedAt": ctx.sdk.opened_at}
        metadata = await asyncio.to_thread(self.metadata.all)
        sessions = []
        for sid, row in entries.items():
            ctx = self.machine.sessions.get(sid)
            local = metadata.get(sid, AntigravityMetadata())
            session = SessionInfo(
                session_id=sid, native_session_id=row["sessionId"], engine=_ENGINE,
                cwd=text(row.get("cwd"), 4096) or None,
                summary=local.title or (ctx.sdk.title if ctx else None) or text(row.get("title"), 512) or None,
                last_modified=text(row.get("updatedAt"), 128) or None,
                state=ctx.state if ctx else "idle", pinned=local.pinned,
                tag="archived" if local.hidden else None,
            )
            if self.machine._session_presentation:
                presentation = self.machine._session_presentation.get(_ENGINE, sid)
                session.completion_id = presentation.completion_id
                session.completion_unread = presentation.completion_unread
                session.completion_revision = presentation.completion_revision
            sessions.append(session)
        sessions.sort(key=lambda row: row.last_modified or "", reverse=True)
        return sessions

    async def handle_list_sessions(self, cmd):
        sessions = await self.list_sessions()
        return await self.send(SessionList(
            engine=_ENGINE, sessions=sessions, request_id=cmd.cmd_id, to=cmd.client_id))

    async def handle_switch_session(self, cmd):
        ctx = await self.context(cmd.session_id)
        self.machine.focused_sid = ctx.key
        await self.publish_controls(ctx)
        await self.machine._emit(ctx, self.machine._background_process_sync(ctx))
        return await self.send(SessionFocus(session_id=ctx.key, cwd=ctx.cwd, to=cmd.client_id))

    async def handle_archive_session(self, cmd):
        sid = cmd.session_id
        ctx = self.machine.sessions.get(sid)
        if not sid.startswith("agy@") or (ctx is not None and ctx.engine != _ENGINE):
            raise AcpError("scope", "此操作需要 Gemini 会话。")
        if ctx is None:
            await self.session_cwd(sid)
            await asyncio.to_thread(self.metadata.set_hidden, sid, cmd.archived)
        else:
            async with ctx.query_lock:
                if cmd.archived and (ctx.state != "idle" or ctx.queued_queries
                                     or ctx.sdk.connected and ctx.sdk.transcript.background):
                    raise AcpError(ERR_BUSY, "请在 Gemini 会话和排队都结束后隐藏会话。")
                await asyncio.to_thread(self.metadata.set_hidden, sid, cmd.archived)
        return await self.send(SessionListInvalidated(engine=_ENGINE))

    async def handle_rename_session(self, cmd):
        await self.session_cwd(cmd.session_id)
        await asyncio.to_thread(self.metadata.set_title, cmd.session_id, cmd.title)
        return await self.send(SessionListInvalidated(engine=_ENGINE))

    async def mutation_intent(self, cmd, kind, fields):
        if kind in {"prompt", "steer"}:
            request_id = cmd.msg_id
        else:
            request_id = getattr(cmd, "request_id", None) or cmd.cmd_id or uuid4().hex
        owner = getattr(cmd, "owner_id", None) or cmd.client_id

        def claim():
            identity = digest([kind, owner, request_id])
            fresh, row = self.mutations.begin(identity, digest(fields))
            return identity, fresh, row

        return await asyncio.to_thread(claim)

    @staticmethod
    def uncertain():
        return AcpError("outcome_unknown", "上次 Gemini 请求结果待核实，请查看原生会话后再发送。", unknown=True)

    async def handle_antigravity_auth(self, cmd):
        client = await self.connection()
        fields = {"request_id": cmd.cmd_id, "to": cmd.client_id, "methods": client.auth_methods}
        if cmd.method is None:
            return await self.send(AntigravityAuthResult(**fields))
        if cmd.method not in client.auth_methods:
            raise AcpError("unsupported", "当前 agy 未提供这种 Gemini 登录方式。")
        if self.auth_lock.locked() or any(
                ctx.engine == _ENGINE and (ctx.state != "idle" or ctx.query_lock.locked() or ctx.queued_queries
                                           or ctx.sdk.connected and ctx.sdk.transcript.background)
                for ctx in self.machine.sessions.values()):
            raise AcpError(ERR_BUSY, "请等本机 Gemini 会话和队列空闲后再登录。")
        async with self.auth_lock:
            with self.quota.account_change():
                identity, fresh, row = await self.mutation_intent(cmd, "auth", [cmd.method])
                if fresh:
                    await client.call("authenticate", {"methodId": cmd.method}, timeout=300)
                    self.catalog.clear()
                    await asyncio.to_thread(self.mutations.finish, identity, {"status": "signed_in"})
                elif not (row.get("result") and json.loads(row["result"]).get("status") == "signed_in"):
                    raise self.uncertain()
            if fresh:
                await self.send(SessionListInvalidated(engine=_ENGINE))
            return await self.send(AntigravityAuthResult(signed_in=True, **fields))

    async def handle_get_antigravity_quota(self, cmd):
        launch = None if self.opted_out() else self.launch_factory()
        report = await self.quota.refresh(launch)
        return await self.send(report.model_copy(update={
            "request_id": cmd.cmd_id, "to": cmd.client_id,
        }))

    async def handle_new_session(self, cmd):
        if cmd.prompt is not None or cmd.images or cmd.files:
            self.validate_input(cmd)
        cwd = self.trusted_workspace(_cwd(cmd.cwd or self.machine.cfg.cc_cwd))
        fields = cmd.model_dump(exclude={"v", "ts", "cmd_id", "client_id", "owner_id", "to", "route_id"})
        identity, fresh, previous = await self.mutation_intent(cmd, "create", fields)
        if not fresh:
            sid = previous["sid"]
            if not sid or previous["result"] is None:
                raise self.uncertain()
            ctx = await self.context(sid)
            self.machine.focused_sid = sid
            return await self.send(SessionFocus(
                session_id=sid, cwd=ctx.cwd, request_id=cmd.request_id, to=cmd.client_id))
        async with self.setup_lock:
            await self.ensure_capacity()
            try:
                handle = await self.open_native(cwd, interactive=True)
            except AcpError as exc:
                if exc.code in {"workspace_untrusted", "workspace_config", "directory"}:
                    await asyncio.to_thread(self.mutations.discard_unsent, identity)
                raise
            sid = wire_session_id(handle.native_id)
            try:
                await asyncio.to_thread(self.mutations.created, identity, sid)
                configured = handle.client.launch.default_model
                offered = self.choices(handle, "model")
                if cmd.model or configured in offered:
                    await self.set_option(handle, "model", cmd.model or configured)
                if cmd.antigravity_effort:
                    await self.set_option(handle, "thought_level", cmd.antigravity_effort)
                if cmd.antigravity_mode:
                    await self.set_option(handle, "mode", cmd.antigravity_mode)
                ctx = self.add_context(handle, cwd)
                await self.persist_controls(ctx)
            except BaseException:
                self.machine.sessions.pop(sid, None)
                await handle.disconnect()
                raise
        self.machine.focused_sid = sid
        focus = await self.send(SessionFocus(session_id=sid, cwd=cwd, request_id=cmd.request_id, to=cmd.client_id))
        await self.publish_controls(ctx)
        await self.send(SessionListInvalidated(engine=_ENGINE))
        if cmd.msg_id:
            initial = Query(sid=sid, prompt=cmd.prompt or "", msg_id=cmd.msg_id,
                            images=cmd.images, files=cmd.files, client_id=cmd.client_id,
                            owner_id=cmd.owner_id, cmd_id=cmd.cmd_id)
            async with ctx.query_lock:
                result = await self.query(ctx, initial)
        else:
            result = None
        await asyncio.to_thread(self.mutations.finish, identity, {"status": "created"})
        return focus, result

    @staticmethod
    def choices(handle, category):
        pair = handle.options.get(category)
        return {item.value: item for item in pair[1].choices} if pair else {}

    async def set_option(self, handle, category, value):
        if value not in self.choices(handle, category):
            raise AcpError("option", "当前 Gemini 会话未提供所选选项。")
        if value == handle.current(category):
            return
        config_id, _ = handle.options[category]
        updated = await handle.client.call("session/set_config_option", {
            "sessionId": handle.native_id, "configId": config_id, "value": value,
        })
        handle.set_options(updated)
        if handle.current(category) != value:
            raise AcpError("option", "本机 agy 未确认此选项，请刷新会话后重试。")

    async def restore_controls(self, handle, saved):
        handle.saved_controls = saved
        for category in CONTROL_CATEGORIES:
            value = saved.get(category)
            if value is None or value == handle.current(category):
                continue
            if category == "thought_level" and saved.get("model", handle.model) != handle.model:
                continue
            try:
                await self.set_option(handle, category, value)
            except AcpError as exc:
                if not handle.connected or exc.unknown:
                    raise
                log.warning("Gemini selection unavailable", category=category, error_code=exc.code)
        handle.unrestored_controls = {category: value for category, value in saved.items()
                                      if handle.current(category) != value}

    async def persist_controls(self, ctx):
        handle = ctx.sdk
        if not handle.tools:
            return
        async with self.controls_lock:
            if ctx.sdk is not handle or not handle.connected or not self.machine._is_resident_context(ctx):
                return
            for category, value in tuple(handle.unrestored_controls.items()):
                if value == handle.current(category):
                    handle.unrestored_controls.pop(category)
            snapshot = {category: value for category in CONTROL_CATEGORIES
                        if (value := handle.current(category))}
            snapshot.update(handle.unrestored_controls)
            if snapshot == handle.saved_controls:
                return
            try:
                await asyncio.to_thread(self.control_store.save, ctx.key, snapshot)
            except (OSError, ValueError):
                await self.machine._emit(ctx, Notice(
                    notice_id="gemini-controls-save", severity="warning", category="config",
                    title="Gemini 设置尚未保存", message="当前选择已生效，本机状态文件写入失败。",
                ))
            else:
                handle.saved_controls = snapshot

    async def apply_option(self, ctx, category, value):
        handle = ctx.sdk
        await self.set_option(handle, category, value)
        handle.unrestored_controls.pop(category, None)
        if category == "model":
            handle.unrestored_controls.pop("thought_level", None)
        await self.persist_controls(ctx)
        await self.publish_controls(ctx)

    async def handle_set_antigravity_option(self, cmd):
        await self.change_option(cmd.sid, cmd.category, cmd.value)

    async def handle_set_model(self, cmd):
        await self.change_option(cmd.sid, "model", cmd.model)

    async def handle_set_effort(self, cmd):
        await self.change_option(cmd.sid, "thought_level", cmd.effort)

    async def handle_set_perm(self, cmd):
        await self.change_option(cmd.sid, "mode", "accept-edits" if cmd.mode == "acceptEdits" else cmd.mode)

    async def change_option(self, sid, category, value):
        ctx = await self.context(sid)
        async with ctx.query_lock:
            if ctx.state != "idle":
                raise AcpError(ERR_BUSY, "Gemini 正在处理消息，结束当前回合后可以切换设置。")
            if ctx.write_state != "writable":
                raise AcpError("directory", _MISSING_DIRECTORY)
            await self.apply_option(ctx, category, value)

    async def open_catalog_probe(self):
        # A prompt-free session remains on disk after session/close. Persist
        # its identity so every catalogue scope reuses it across restarts.
        try:
            saved = await asyncio.to_thread(read_json, self.catalog_path, os.getuid(), limit=4096)
        except FileNotFoundError:
            saved = {}
        except (OSError, ValueError, UpdateError):
            raise AcpError("catalog_state", "无法读取本机 Gemini 模型目录状态。") from None
        native_id = saved.get("session_id")
        sid = wire_session_id(native_id)
        if sid:
            try:
                handle = await self.open_native(None, sid)
            except AcpError as exc:
                if exc.code != "session_missing":
                    raise
            else:
                if not handle.transcript.groups:
                    return handle, text(saved.get("default_model"), 256) or handle.model
                await handle.disconnect()
        elif saved:
            raise AcpError("catalog_state", "本机 Gemini 模型目录会话标识无效。")

        handle = await self.open_native(None)
        try:
            await asyncio.to_thread(write_json, self.catalog_path, {
                "session_id": handle.native_id, "default_model": handle.model,
            }, mode=0o600)
        except OSError:
            await handle.disconnect()
            raise AcpError("catalog_state", "无法保存本机 Gemini 模型目录状态。") from None
        return handle, handle.model

    async def handle_get_models(self, cmd):
        cwd = cmd.cwd
        native_default = None
        if cmd.sid:
            ctx = await self.context(cmd.sid)
            handle = ctx.sdk
        else:
            scope = cmd.antigravity_model
            async with self.setup_lock:
                cached = self.catalog.get(scope)
                if cached and time.monotonic() - cached[0] < 60 and cached[1].client.running:
                    _, handle, native_default = cached
                else:
                    handle, native_default = await self.open_catalog_probe()
                    try:
                        choices = self.choices(handle, "model")
                        configured = handle.client.launch.default_model
                        if native_default not in choices:
                            native_default = handle.model
                        if configured in choices:
                            native_default = configured
                        if selected := cmd.antigravity_model or native_default:
                            await self.set_option(handle, "model", selected)
                    finally:
                        await handle.disconnect()
                    self.catalog[scope] = (time.monotonic(), handle, native_default)
                    self.catalog.move_to_end(scope)
                    if len(self.catalog) > 8:
                        self.catalog.popitem(last=False)
        thinking = self.choices(handle, "thought_level")
        models = [dict(id=choice.value, display_name=choice.name, description=choice.description,
                       efforts=list(thinking) if choice.value == handle.model else [],
                       effort_names={key: item.name for key, item in thinking.items()}
                       if choice.value == handle.model else {},
                       effort_descriptions={key: item.description for key, item in thinking.items()}
                       if choice.value == handle.model else {},
                       default_effort=handle.effort if choice.value == handle.model else None)
                  for choice in self.choices(handle, "model").values()]
        return await self.send(Models(
            engine=_ENGINE, sid=cmd.sid, cwd=cwd, to=cmd.client_id, models=models,
            default_model=handle.model if cmd.sid else native_default, default_effort=handle.effort,
            antigravity_model=cmd.antigravity_model, antigravity_options=handle.controls().options,
            antigravity_commands=handle.commands,
        ))

    async def handle_get_engine_capabilities(self, cmd):
        cwd = _cwd(cmd.cwd or self.machine.cfg.cc_cwd)
        gemini_dir = Path.home() / ".gemini"
        items = await asyncio.to_thread(antigravity_capabilities, cwd, gemini_dir,
                                        skills_only=cmd.skills_only)
        return await self.send(EngineCapabilities(
            engine=_ENGINE, space="code", request_id=cmd.cmd_id, cwd=cmd.cwd or self.machine.cfg.cc_cwd,
            items=items, to=cmd.client_id, skills_only=cmd.skills_only,
            notes=["配置由 Antigravity CLI 管理。原生命令随会话加载。"],
        ))

    async def handle_pin_session(self, cmd):
        await self.session_cwd(cmd.session_id)
        await asyncio.to_thread(self.metadata.set_pinned, cmd.session_id, cmd.pinned)
        await self.send(SessionListInvalidated(engine=_ENGINE))

    async def publish_controls(self, ctx):
        handle = ctx.sdk
        await self.machine._emit(ctx, handle.controls())
        if handle.model:
            await self.machine._emit(ctx, Model(model=handle.model))
        await self.machine._emit(ctx, Effort(effort=handle.effort or ""))
        await self.machine._emit(ctx, Perm(mode=handle.permission_mode))
        if handle.usage:
            await self.send(handle.usage.model_copy(update={"sid": ctx.key, "model": handle.model}))

    async def replay_controls(self, ctx, cmd):
        handle = ctx.sdk
        fields = {"sid": ctx.key, "to": cmd.client_id, "route_id": getattr(cmd, "route_id", None)}
        await self.send(handle.controls().model_copy(update=fields))
        if handle.model:
            await self.send(Model(model=handle.model, **fields))
        await self.send(Effort(effort=handle.effort or "", **fields))
        if handle.usage:
            await self.send(handle.usage.model_copy(update={**fields, "model": handle.model}))

    async def handle_get_context(self, cmd):
        ctx = await self.context(cmd.sid)
        report = ctx.sdk.usage or ContextReport(
            total_tokens=0, max_tokens=0, percentage=0, available=False)
        return await self.send(report.model_copy(update={
            "sid": ctx.key, "model": ctx.sdk.model,
            "request_id": cmd.cmd_id, "to": cmd.client_id,
        }))

    async def on_update(self, params):
        native_id, update = params.get("sessionId"), params.get("update")
        sid = wire_session_id(native_id)
        if sid is None or not isinstance(update, dict):
            return
        try:
            update = self.with_message_receipt(sid, update)
            if self.opening and not self.opening.native_id and sid not in self.machine.sessions:
                staged = self.early_controls.get(native_id)
                if staged is None:
                    staged = self.early_controls[native_id] = AntigravityHandle(self.opening.client, native_id)
                    if len(self.early_controls) > 8:
                        self.early_controls.popitem(last=False)
                staged.update(update)
                return
            if self.opening and self.opening.native_id == native_id:
                # Reopening a disconnected resident; the old projection still
                # owns the browser until the new native replay completes.
                self.opening.update(update)
                self.opening.transcript.load_update(update)
                return
            ctx = self.machine.sessions.get(sid)
            if not ctx or ctx.engine != _ENGINE:
                return
            handle = ctx.sdk
            await handle.input_ready.wait()
            async with handle.stream_lock:
                if (ctx.sdk is not handle or not handle.connected
                        or not self.machine._is_resident_context(ctx)):
                    return
                await self.apply_update(ctx, handle, update)
        except Exception as exc:
            log.warning("Gemini ACP update unavailable", sid=sid, error_type=type(exc).__name__)

    async def apply_update(self, ctx, handle, update):
        if (handle.turn is None and ctx.state == "idle" and handle.connected
                and handle.transcript.followup_input(update)):
            await self.start_followup(ctx, handle)
        kind = update.get("sessionUpdate")
        turn = handle.turn
        messages = self.machine._session_messages
        # ACP associates streaming updates with a session, not a specific
        # prompt RPC. Once steered, use the original RPC's terminal reply.
        if (messages is not None and turn is not None and turn.delivery_msg_id
                and len(turn.responses) == 1 and not turn.stopping
                and kind in {"agent_message_chunk", "agent_thought_chunk", "tool_call", "plan"}):
            messages.accepted(turn.delivery_msg_id)
        handle.update(update)
        active_tasks = len(handle.transcript.background)
        native_tool = text(update.get("toolCallId"), 512)
        detached_command = native_tool in handle.transcript.background and native_tool in handle.transcript.detachable
        frames = handle.transcript.translate(update)
        detached_completed = detached_command and any(frame.type == "tool_result" for frame in frames)
        if turn is None and ctx.state == "idle" and detached_completed:
            # Claim the possible wakeup before forwarding the tool result or
            # letting a new prompt enter the session-scoped native stream.
            await self.start_followup(ctx, handle)
        if turn and turn.autonomous:
            if detached_completed or kind == "tool_call":
                turn.native_stop = None
            if frames or kind == "usage_update":
                turn.touch()
        for frame in frames:
            await self.emit(ctx, frame)
        if len(handle.transcript.background) != active_tasks:
            await self.machine._emit(ctx, self.machine._background_process_sync(ctx))
        if kind == "usage_update" and handle.usage:
            await self.send(handle.usage.model_copy(update={"sid": ctx.key, "model": handle.model}))
        if kind in {"config_option_update", "current_mode_update"}:
            await self.persist_controls(ctx)
        if kind in {"config_option_update", "available_commands_update", "current_mode_update"}:
            await self.publish_controls(ctx)
        if kind == "session_info_update":
            await self.send(SessionListInvalidated(engine=_ENGINE))

    async def start_followup(self, ctx, handle):
        turn_id = event_id("agy-followup", handle.native_id, uuid4().hex)
        turn = handle.turn = PromptTurn(autonomous=True)
        ctx.interrupt_event.clear()
        ctx.active_msg_id = turn_id
        ctx.turn_task = asyncio.create_task(self.wait_followup(ctx, handle, turn), name=f"agy-followup-{ctx.key}")
        await self.emit(ctx, handle.transcript.start_followup(turn_id))
        await self.machine._set_state(ctx, "running", msg_id=turn_id)
        await self.send(SessionActivity(engine=_ENGINE, session_id=ctx.key, state="running"))

    async def on_native_lifecycle(self, client, native, kind, reason):
        ctx = self.machine.sessions.get(wire_session_id(native))
        if ctx is None or ctx.engine != _ENGINE:
            return
        handle = ctx.sdk
        await handle.input_ready.wait()
        async with handle.stream_lock:
            if (ctx.sdk is not handle or handle.client is not client or not handle.connected
                    or (self.opening and self.opening.native_id == native)
                    or not self.machine._is_resident_context(ctx)):
                return
            turn = handle.turn
            if kind == "invoke":
                if turn is None and ctx.state == "idle" and ctx.write_state == "writable":
                    await self.start_followup(ctx, handle)
                elif turn and turn.autonomous:
                    turn.native_stop = None
                    turn.touch()
            elif turn and turn.autonomous and not turn.stopping:
                turn.native_stop = (time.monotonic(), reason.upper())
                turn.changed.set()

    def with_message_receipt(self, sid, update):
        if update.get("name") != MCP_SEND_MESSAGE or update.get("status") != "completed":
            return update
        messages = self.machine._session_messages
        arguments = update.get("rawInput")
        if messages is None or not isinstance(arguments, dict):
            return update
        # agy exposes completion but can omit the MCP output in ACP, including
        # during history replay. The caller's durable receipt owns this status.
        receipt = messages.send_receipt(reference(_ENGINE, sid), arguments)
        return {**update, "rawOutput": json.dumps(receipt, ensure_ascii=False), "content": []} if receipt is not None else update

    async def emit(self, ctx, frame):
        ctx.sdk.transcript.remember(frame)
        await self.machine._emit(ctx, frame)

    @staticmethod
    def validate_input(cmd):
        if not cmd.prompt and not cmd.images and not cmd.files:
            raise AcpError(ERR_BAD_PROMPT, "请输入内容或添加附件。")
        if validate_attachments(cmd.images, cmd.files):
            raise AcpError(ERR_BAD_PROMPT, "附件不符合大小或格式要求。")
        prompt = (cmd.prompt or "").lstrip()
        if prompt.startswith("/") and prompt.split(maxsplit=1)[0].casefold() in _WORKSPACE_COMMANDS:
            raise AcpError("workspace_command", "请从 CC Remote 的会话入口切换 Gemini 工作目录或恢复会话。")

    @staticmethod
    def prompt_blocks(client, cmd):
        capabilities = client.capabilities.get("promptCapabilities") or {}
        if cmd.images and not capabilities.get("image"):
            raise AcpError(ERR_BAD_PROMPT, "当前 agy 未声明图片输入支持。")
        if cmd.files and not capabilities.get("embeddedContext"):
            raise AcpError(ERR_BAD_PROMPT, "当前 agy 未声明文件附件支持。")
        prompt = [{"type": "text", "text": cmd.prompt}] if cmd.prompt else []
        prompt += [
            {"type": "image", "mimeType": "image/jpeg" if image["media_type"] == "image/jpg" else image["media_type"],
             "data": image["data"]}
            for image in cmd.images or []
        ]
        for i, item in enumerate(cmd.files or []):
            name = item["filename"]
            resource = {"uri": f"cc-remote://attachment/{i}/{quote(name, safe='')}"}
            mime_type, _ = mimetypes.guess_type(name)
            try:
                resource["text"] = decode_attachment(item["data"]).decode("utf-8")
                resource["mimeType"] = mime_type or "text/plain"
            except UnicodeDecodeError:
                resource.update(blob=item["data"], mimeType=mime_type or "application/octet-stream")
            prompt.append({"type": "resource", "resource": resource})
        return prompt

    async def handle_steer(self, cmd):
        ctx = self.machine.sessions.get(cmd.sid)
        if not ctx or ctx.engine != _ENGINE:
            raise AcpError(ERR_NOT_STEERABLE, "Gemini 没有可补充的当前回合。")
        async with ctx.query_lock:
            return await self.query(ctx, cmd, steer=True)

    async def query(self, ctx, cmd, *, steer=False, launch_receipt=None):
        def check_steer():
            if (not turn or handle.turn is not turn or ctx.state != "running" or turn.stopping or ctx.pending_asks
                    or len(turn.responses) >= 32 or all(future.done() for _, future in turn.responses)):
                raise AcpError(ERR_NOT_STEERABLE, "Gemini 当前回合无法接收补充，可排队到下一轮。")

        def check_idle():
            if self.admission_paused() or ctx.sdk is not handle or handle.turn is not None or ctx.state != "idle":
                raise AcpError(ERR_BUSY, "Gemini 正在处理消息，可将新消息排队到下一轮。")

        claim = None
        try:
            self.validate_input(cmd)
            if self.admission_paused():
                raise AcpError(ERR_BUSY, "Gemini 服务正在切换，请稍后重试。")
            if self.auth_lock.locked():
                raise AcpError(ERR_BUSY, "Gemini 正在登录，完成后可以发送。")
            handle = ctx.sdk
            if not handle.connected:
                raise AcpError("offline", "本机 agy 已断开，请重新打开 Gemini 会话。")
            self.trusted_workspace(ctx.cwd)
            if handle.history_params is not None:
                async with self.setup_lock:
                    await self.load_resumed_history(handle)
            if not os.path.isdir(ctx.cwd):
                if ctx.state == "idle":
                    await self.sync_directory_control(ctx)
                raise AcpError("directory", _MISSING_DIRECTORY)
            if ctx.write_state != "writable":
                raise AcpError("offline", "请重新打开 Gemini 会话后继续。")
            if handle.tools and not handle.tools.workspace_matches():
                raise AcpError("directory", "Gemini 工作目录已更换，请重新打开会话后继续。")
            blocked = handle.unrestored_controls.keys() & {"model", "mode"}
            if blocked:
                labels = "、".join(_CONTROL_LABELS[category] for category in CONTROL_CATEGORIES if category in blocked)
                raise AcpError("controls_restore", f"Gemini 无法恢复原来的{labels}，请重新选择后再发送。")
            if (await asyncio.to_thread(self.metadata.get, ctx.key)).hidden:
                raise AcpError("hidden", "请恢复显示此 Gemini 会话后继续。")
            turn = handle.turn
            if steer:
                check_steer()
            else:
                check_idle()
            blocks = self.prompt_blocks(handle.client, cmd)
            identity, fresh, _ = await self.mutation_intent(
                cmd, "steer" if steer else "prompt", [ctx.key, blocks])
            if not fresh:
                raise self.uncertain()
            claim = identity
            response = await handle.client.submit("session/prompt", {
                "sessionId": handle.native_id, "prompt": blocks,
            }, guard=check_steer if steer else check_idle)
        except AcpError as exc:
            if claim and not exc.unknown:
                with suppress(AcpError):
                    await asyncio.to_thread(self.mutations.discard_unsent, claim)
            if launch_receipt and not launch_receipt.done():
                launch_receipt.set_result(exc.unknown)
            return await self.error(cmd, exc)

        # The request is committed locally. Pause narrative callbacks until
        # the user echo and its binding precede the first native chunk.
        handle.input_ready.clear()
        if launch_receipt and not launch_receipt.done():
            launch_receipt.set_result(True)
        if turn is None:
            messages = self.machine._session_messages
            outbound = messages is not None and messages.owns(cmd.msg_id)
            turn = handle.turn = PromptTurn(delivery_msg_id=cmd.msg_id if outbound else None)
        turn.responses.append((cmd.msg_id, response))
        turn.changed.set()
        response.add_done_callback(lambda _future: turn.changed.set())
        try:
            if not steer:
                ctx.interrupt_event.clear()
                await self.machine._set_state(ctx, "running", msg_id=cmd.msg_id)
                await self.send(SessionActivity(engine=_ENGINE, session_id=ctx.key, state="running"))
            fields = dict(msg_id=cmd.msg_id, prompt=cmd.prompt, images=cmd.images,
                          files=[{"filename": item["filename"]} for item in cmd.files or []] or None)
            if steer:
                for frame in handle.transcript.complete("steered"):
                    if frame.type == "assistant_msg_end":
                        await self.machine._emit(ctx, frame)
            user = UserMsg(**fields)
            handle.transcript.start(user)
            ctx.active_msg_id = cmd.msg_id
            await self.machine._emit(ctx, TurnSteered(
                turn_id=cmd.msg_id, preserve_open_tools=True, **fields) if steer else user)
            await self.emit(ctx, TurnBinding(msg_id=cmd.msg_id, turn_id=cmd.msg_id))
        finally:
            handle.input_ready.set()
            if not steer:
                ctx.turn_task = asyncio.create_task(self.wait_turn(ctx, handle, turn), name=f"agy-turn-{ctx.key}")
        if cmd.files:
            await self.machine._artifacts.uploads(ctx, cmd.msg_id, cmd.files)
        return None

    async def wait_turn(self, ctx, handle, turn):
        subtype, failed = "success", False
        try:
            while True:
                turn.changed.clear()
                async with ctx.query_lock:
                    if all(response.done() for _, response in turn.responses):
                        break
                await turn.changed.wait()
            for i, (turn_id, response) in enumerate(turn.responses):
                try:
                    result = response.result()
                    reason = result.get("stopReason")
                    if usage := prompt_usage(result.get("usage")):
                        await self.emit(ctx, TurnUsage(turn_id=turn_id, usage=usage))
                    messages = self.machine._session_messages
                    if (i == 0 and messages is not None and turn.delivery_msg_id
                            and reason in {"end_turn", "max_tokens", "max_turn_requests", "refusal"}):
                        messages.accepted(turn.delivery_msg_id)
                    if reason == "cancelled":
                        subtype = "interrupted"
                    elif reason in {"max_tokens", "max_turn_requests", "refusal"}:
                        subtype, failed = "error", True
                except AcpError as exc:
                    subtype, failed = "error", True
                    await self.machine._emit(ctx, Error(code="gemini_" + exc.code, message=str(exc),
                                                        msg_id=ctx.active_msg_id))
            async with handle.stream_lock:
                await self.finish_turn(ctx, handle, turn, subtype, failed)
        finally:
            if handle.turn is turn:
                await self.release_turn(ctx, handle, turn)

    async def wait_followup(self, ctx, handle, turn):
        # The native Stop hook supplies the terminal for unsolicited output.
        # Leave a short interval for the hook process to exit and buffered ACP
        # frames to arrive. Silence by itself never settles the turn.
        notified = False
        try:
            while True:
                async with handle.stream_lock:
                    turn.changed.clear()
                    if not handle.connected:
                        subtype = "interrupted" if turn.force_requested else "error"
                        await self.finish_turn(ctx, handle, turn, subtype, subtype == "error")
                        return
                    pending = turn.interactions or ctx.pending_asks or handle.transcript.foreground_tools_pending()
                    wait = None
                    if not pending and not turn.stopping and turn.native_stop:
                        stopped_at, reason = turn.native_stop
                        wait = max(stopped_at, turn.activity_at) + _FOLLOWUP_STOP_SETTLE - time.monotonic()
                        if wait <= 0:
                            if reason in {"NO_TOOL_CALL", "MODEL_STOP"}:
                                subtype = "background_followup"
                            elif reason in {"CANCELLED", "CANCELED", "INTERRUPTED", "USER_INTERRUPTED"}:
                                subtype = "interrupted"
                            else:
                                subtype = "error"
                            await self.finish_turn(ctx, handle, turn, subtype, subtype == "error")
                            return
                    elif not pending and not notified and not turn.stopping:
                        wait = _FOLLOWUP_NOTICE_AFTER - (time.monotonic() - turn.activity_at)
                    if wait is not None and wait <= 0:
                        notified = True
                        wait = None
                        try:
                            await self.machine._emit(ctx, StateEvent(
                                state="running", msg_id=ctx.active_msg_id,
                                detail="Gemini 尚未确认续写结束，可点停止继续处理排队消息",
                            ))
                        except Exception as exc:
                            log.warning("Gemini follow-up status unavailable", sid=ctx.key,
                                        error_type=type(exc).__name__)
                with suppress(TimeoutError):
                    await asyncio.wait_for(turn.changed.wait(), wait)
        finally:
            if handle.turn is turn:
                await self.release_turn(ctx, handle, turn)

    async def finish_turn(self, ctx, handle, turn, subtype, failed):
        try:
            await self.machine._cancel_pending_asks(ctx)
            active_tasks = len(handle.transcript.background)
            frames = handle.transcript.complete(
                subtype, duration_ms=int((time.monotonic() - turn.started) * 1000), is_error=failed,
                keep_background=handle.connected)
            if len(handle.transcript.background) != active_tasks:
                await self.machine._emit(ctx, self.machine._background_process_sync(ctx))
            for frame in frames:
                await self.machine._emit(ctx, frame)
        finally:
            await self.release_turn(ctx, handle, turn)

    async def release_turn(self, ctx, handle, turn):
        ctx.active_msg_id = None
        ctx.active_turn_binding = None
        ctx.turn_task = None
        ctx.interrupt_event.clear()
        handle.turn = None
        try:
            if self.machine._is_resident_context(ctx):
                if not handle.connected:
                    await self.publish_disconnect(ctx, handle, interrupted=turn.force_requested)
                await self.machine._set_state(ctx, "idle")
                await self.send(SessionActivity(engine=_ENGINE, session_id=ctx.key, state="idle"))
                await self.send(SessionListInvalidated(engine=_ENGINE))
        finally:
            turn.finished.set()

    async def handle_interrupt(self, cmd):
        ctx = self.machine._ctx_for(cmd.sid)
        if not ctx or ctx.engine != _ENGINE:
            raise AcpError("session", "没有可停止的 Gemini 会话。")
        async with ctx.query_lock:
            handle = ctx.sdk
            turn = handle.turn
            force = bool(turn and turn.stopping and ctx.state == "draining")
            if not turn or (turn.stopping and not force):
                return
            _, fresh, _ = await self.mutation_intent(cmd, "cancel", [ctx.key])
            if not fresh:
                return
            if turn.autonomous:
                await self.stop_followup(ctx, handle, turn)
                return
            if all(response.done() for _, response in turn.responses):
                return
            if force:
                await self.force_stop(handle, turn)
            else:
                turn.stopping = True
                ctx.interrupt_event.set()
                try:
                    await handle.client.notify("session/cancel", {"sessionId": handle.native_id})
                except AcpError as exc:
                    if handle.connected:
                        await self.machine._set_state(ctx, "draining")
                        raise AcpError("cancel_pending", "停止请求尚未确认。可以再次点击停止来恢复会话。") from exc
                    turn.stopping = False
                    ctx.interrupt_event.clear()
                    raise
                await self.machine._set_state(ctx, "interrupting")
                await self.machine._cancel_pending_asks(ctx)
                turn.touch()
        try:
            await asyncio.wait_for(turn.finished.wait(), _CANCEL_TIMEOUT)
        except TimeoutError:
            if not turn.finished.is_set():
                await self.machine._set_state(ctx, "draining")
                raise AcpError("cancel_pending", "Gemini 尚未确认停止。可以再次点击停止来恢复会话。") from None
        if force and self.machine._is_resident_context(ctx):
            await self.context(ctx.key)

    async def stop_followup(self, ctx, handle, turn):
        # Keep the query lock until the old native subscription is closed and
        # the session is reopened. A notification-only cancel has no receipt.
        turn.stopping = True
        ctx.interrupt_event.set()
        await self.machine._set_state(ctx, "interrupting")
        await self.machine._cancel_pending_asks(ctx)
        try:
            await self.force_stop(handle, turn)
            await asyncio.wait_for(turn.finished.wait(), _CANCEL_TIMEOUT)
        except AcpError:
            if not turn.finished.is_set():
                await self.machine._set_state(ctx, "draining")
            raise
        except TimeoutError:
            if not turn.finished.is_set():
                await self.machine._set_state(ctx, "draining")
            raise AcpError("cancel_pending", "Gemini 尚未确认停止。可以再次点击停止来恢复会话。") from None
        if self.machine._is_resident_context(ctx):
            await self.context(ctx.key)

    async def force_stop(self, handle, turn):
        """Called with the target query lock held to fence newer prompts."""
        client = handle.client
        turn.force_requested = True
        if "close" in (client.capabilities.get("sessionCapabilities") or {}):
            with suppress(AcpError, TimeoutError):
                async with asyncio.timeout(_CLOSE_TIMEOUT):
                    await client.call("session/close", {"sessionId": handle.native_id})
                handle.attached = False
                if handle.tools:
                    await handle.tools.close()
                for _, response in turn.responses:
                    if not response.done():
                        response.set_result({"stopReason": "cancelled"})
                turn.changed.set()
                return
        # An unresponsive shared process can be restarted once no other
        # running Gemini turn would be stopped without an explicit request.
        async with self.setup_lock, self.connect_lock, client.write_lock:
            if not turn.autonomous and all(response.done() for _, response in turn.responses):
                return
            if any(other.engine == _ENGINE and other.sdk.client is client and other.sdk is not handle
                   and (other.sdk.transcript.background
                        or ((active := other.sdk.turn) is not None and not active.force_requested
                            and (active.autonomous or any(not response.done() for _, response in active.responses))))
                   for other in self.machine.sessions.values()):
                raise AcpError("cancel_pending", "其他 Gemini 会话仍在运行。可分别强制停止卡住的会话，再恢复共享引擎。")
            await client.close()
            await self.close_tools(client)
            for ctx in self.machine.sessions.values():
                if ctx.engine == _ENGINE and ctx.sdk.client is client and ctx.sdk.turn:
                    ctx.sdk.turn.changed.set()
            if self.client is client:
                self.client = None

    async def history_source(self, sid):
        async with self.setup_lock:
            ctx = self.machine.sessions.get(sid)
            if ctx and ctx.engine == _ENGINE:
                await self.load_resumed_history(ctx.sdk)
                return ctx.sdk, ctx
            cached = self.cached_history.get(sid)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1], None
            handle = await self.open_native(await self.session_cwd(sid), sid)
            await handle.disconnect()
            self.cached_history[sid] = (time.monotonic(), handle)
            self.cached_history.move_to_end(sid)
            if len(self.cached_history) > 2:
                self.cached_history.popitem(last=False)
            return handle, None

    async def handle_get_history(self, cmd, *, publish=True):
        handle, ctx = await self.history_source(cmd.session_id)
        keys = list(handle.transcript.groups)
        if cmd.before is not None and cmd.before not in keys:
            raise AcpError("history_changed", "Gemini 历史已更新，请重新打开会话。")
        stop = keys.index(cmd.before) if cmd.before else len(keys)
        start = max(0, stop - min(cmd.limit or 8, 32))
        if cmd.detail == "full":
            # Full compatibility pages still fit the relay's frame budget.
            while start < stop - 1 and sum(handle.transcript.sizes[key] for key in keys[start:stop]) > 6 * 1024 * 1024:
                start += 1
        selected = keys[start:stop]
        events = [event for key in selected for event in handle.transcript.groups[key]]
        turns = list(materialize_history_turns(events, include_live_detail=True,
                                               preserve_steered_tools=True)) if cmd.detail == "summary" else []
        for row in turns:
            row.pop("forkPointId", None)
            row.pop("checkpointId", None)
        if cmd.before is None:
            handle.build_seq += 1
        result = History(
            sid=cmd.session_id, session_id=cmd.session_id, to=cmd.client_id,
            revision=handle.revision, generation=self.machine.instance_id,
            build_seq=handle.build_seq, live_seq=ctx.seq if ctx else 0,
            in_progress=bool(ctx and ctx.state != "idle"),
            before=cmd.before, detail=cmd.detail, has_more=start > 0,
            oldest_id=selected[0] if selected else None, newest_id=selected[-1] if selected else None,
            events=events if cmd.detail == "full" else [], turns=turns,
            control=self.machine._session_control(ctx) if ctx else None,
        )
        return await self.send(result) if publish else result

    async def handle_get_turn_detail(self, cmd):
        from cc_remote.wrapper.machine import _turn_detail_page

        handle, _ = await self.history_source(cmd.session_id)
        events = handle.transcript.groups.get(cmd.turn_id)
        if events is None or cmd.revision not in {None, handle.revision}:
            raise AcpError("detail_changed", "Gemini 详情已更新，请刷新会话。")
        page, more, older, later, newer = _turn_detail_page(
            events, before=cmd.before, limit=cmd.limit, max_bytes=4 * 1024 * 1024)
        if cmd.before and not page:
            raise AcpError("detail_changed", "Gemini 详情已更新，请刷新会话。")
        return await self.send(TurnDetail(
            session_id=cmd.session_id, sid=cmd.session_id, turn_id=cmd.turn_id, to=cmd.client_id,
            revision=handle.revision, events=page, has_more=more, oldest_cursor=older,
            has_newer=later, newer_cursor=newer, before=cmd.before,
        ))

    async def history_image(self, cmd, revision, *, publish=True, **fields):
        result = HistoryImage(
            session_id=cmd.session_id, sid=cmd.session_id, turn_id=cmd.turn_id,
            image_id=cmd.image_id, variant=cmd.variant, revision=revision,
            request_id=cmd.request_id, to=cmd.client_id, **fields,
        )
        return await self.send(result) if publish else result

    async def handle_get_history_image(self, cmd, *, publish=True):
        handle, _ = await self.history_source(cmd.session_id)
        if cmd.revision not in {None, handle.revision}:
            raise AcpError("image_changed", "Gemini 历史已更新，请刷新会话。")
        events = handle.transcript.groups.get(cmd.turn_id) or []
        for index, image in enumerate(events[0].get("images", []) if events else []):
            if cmd.image_id == history_image_id(cmd.turn_id, index):
                return await self.history_image(cmd, handle.revision, publish=publish, **image)
        raise AcpError("image_unavailable", "原生 Gemini 历史没有提供此图片的数据。")

    async def on_request(self, method, identity, params):
        if method == "session/request_permission":
            cancel = {"outcome": {"outcome": "cancelled"}}
            request = self.permission
        elif method == "elicitation/create":
            cancel = {"action": "cancel"}
            if params.get("mode") != "form":
                return cancel
            request = self.elicitation
        else:
            return None
        sid = wire_session_id(params.get("sessionId"))
        ctx = self.machine.sessions.get(sid)
        if ctx is None or ctx.engine != _ENGINE:
            return cancel
        handle = ctx.sdk
        await handle.input_ready.wait()
        async with handle.stream_lock:
            if (ctx.sdk is not handle or not self.machine._is_resident_context(ctx)
                    or not handle.connected or ctx.write_state != "writable"
                    or (self.opening and self.opening.native_id == handle.native_id)):
                return cancel
            if handle.turn is None and ctx.state == "idle":
                await self.start_followup(ctx, handle)
            turn = handle.turn
            if turn is None or turn.stopping or ctx.state != "running":
                return cancel
        with turn.interaction():
            return await request(ctx, identity, params)

    async def permission(self, ctx, identity, params):
        tool = params.get("toolCall")
        tool = tool if isinstance(tool, dict) else {}
        tools = ctx.sdk.tools
        direct_question = tools is not None and tools.owns_question(tool.get("name"))
        labels = {}
        options = []
        for option in records(params.get("options")):
            option_id = text(option.get("optionId"), 256)
            kind = text(option.get("kind"), 32)
            if not option_id or kind not in {"allow_once", "allow_always", "reject_once", "reject_always"}:
                continue
            if direct_question and kind == "allow_once":
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
            label = text(option.get("name"), 480) or kind
            if label in labels:
                label = f"{label} ({len(options) + 1})"
            labels[label] = option_id
            options.append({"label": label})
            if len(options) >= ASK_OPTION_MAX_COUNT:
                break
        if not options:
            return {"outcome": {"outcome": "cancelled"}}
        if len(options) == 1:
            options.append({"label": "取消本次请求"})
        title = text(tool.get("title"), 8192) or "Gemini 请求运行工具。"
        content = tool.get("content")
        raw_input = tool.get("rawInput")
        raw_input = raw_input if isinstance(raw_input, dict) else {}
        command = text(raw_input.get("CommandLine") or raw_input.get("command") or raw_input.get("cmd"), 4096)
        paths = [item.get("path") for item in islice(records(content), 64) if item.get("type") == "diff"]
        paths.extend(item.get("path") for item in islice(records(tool.get("locations")), 32))
        if not paths:
            paths.append(raw_input.get("Path") or raw_input.get("file_path") or raw_input.get("path"))
        targets = dict.fromkeys(os.path.normpath(os.path.join(ctx.cwd, path)) for path in paths
                                if isinstance(path, str) and 0 < len(path) <= 4096
                                and not any(char in path for char in "\0\n\r"))
        details = [item for item in (*targets, command) if item and item not in title]
        title = "\n\n".join([title, *details])[:ASK_QUESTION_MAX_CHARS]
        patch = native_diff(content)
        if len(patch) > 64 * 1024:
            notice = "\n… 差异预览已截断"
            patch = patch[:64 * 1024 - len(notice)] + notice
        elif not patch and any(item.get("type") == "diff" for item in records(content)):
            patch = "此编辑未能生成差异预览。"
        try:
            answers = await self.machine._on_ask_batch(ctx, [AskQuestion(
                question=title, options=options, header="Gemini 工具权限",
            )], ask_id=event_id("agy-approval", ctx.sdk.revision, identity),
                timeout=float("inf"), dismiss_actions=["cancel"], approval_diff=patch or None)
            answer = answers[0]
        except AskUnavailable:
            answer = None
        if isinstance(answer, str) and answer in labels:
            return {"outcome": {"outcome": "selected", "optionId": labels[answer]}}
        return {"outcome": {"outcome": "cancelled"}}

    async def elicitation(self, ctx, identity, params):
        # The shared questionnaire supports string choices and free text.
        # Unsupported schemas receive a native decline instead of guessed data.
        schema = params.get("requestedSchema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or not 1 <= len(properties) <= ASK_QUESTION_MAX_COUNT:
            return {"action": "decline"}
        questions = []
        message = text(params.get("message"), 8192) or "Gemini 有一个问题。"
        for name, item in properties.items():
            if not isinstance(item, dict) or item.get("type") != "string" or item.get("format"):
                return {"action": "decline"}
            choices = item.get("enum")
            if choices is not None and (not isinstance(choices, list)
                    or not 2 <= len(choices) <= ASK_OPTION_MAX_COUNT
                    or not all(isinstance(value, str) and 0 < len(value) <= 512 for value in choices)):
                return {"action": "decline"}
            label = text(item.get("title"), 512) or name[:512]
            description = text(item.get("description"), 2048)
            questions.append(AskQuestion(
                header=label, question="\n\n".join(filter(None, (message, description))),
                options=[{"label": value} for value in choices or ()], allow_text=choices is None,
            ))
        try:
            answers = await self.machine._on_ask_batch(
                ctx, questions, ask_id=event_id("agy-question", ctx.sdk.revision, identity),
                timeout=float("inf"), dismiss_actions=["decline", "cancel"])
        except AskDeclined:
            return {"action": "decline"}
        except AskUnavailable:
            return {"action": "cancel"}
        for item, answer in zip(properties.values(), answers):
            if not isinstance(answer, str) or not item.get("minLength", 0) <= len(answer) <= item.get("maxLength", 4096):
                return {"action": "decline"}
        return {"action": "accept", "content": dict(zip(properties, answers))}
