"""Rooms coordinate the existing native session paths and their public events."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import weakref
from uuid import NAMESPACE_URL, uuid5

from cc_remote.attachments import validate_attachments
from cc_remote.collaboration import CollaborationMemberSpec, shared_prompt
from cc_remote.collaboration_store import CollaborationStore
from cc_remote.log import logger
from cc_remote.protocol import (
    AnswerQuestion,
    AssistantMsgStart,
    AssistantMsgEnd,
    CollaborationState,
    Delta,
    Error,
    GetHistory,
    GetModels,
    GetPermissionProfiles,
    Interrupt,
    ListSessions,
    Models,
    PermissionProfiles,
    NewSession,
    Query,
    RenameSession,
    SessionFocus,
    SessionList,
    SwitchSession,
    Steer,
    TurnBinding,
    TurnEnd,
    TurnSteered,
    UserMsg,
)

log = logger("cc_remote.wrapper.collaboration")


@dataclass
class _ProjectionContext:
    key: str
    session_id: str | None
    cwd: str | None
    engine: str
    texts: OrderedDict = field(default_factory=OrderedDict)
    live_context: object | None = None
    live_seq: int | None = None


class Collaboration:
    def __init__(self, machine):
        self.machine = machine
        self.store: CollaborationStore | None = None
        self.database_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.task: asyncio.Task | None = None
        self.workers: dict[str, asyncio.Task] = {}
        self.members: dict[int, tuple[weakref.ReferenceType, str]] = {}
        self.texts: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self.pending_replies: dict[str, set[str]] = {}
        self.recovery_pending: set[str] = set()
        self.projection_locks: dict[str, asyncio.Lock] = {}
        self.wake = asyncio.Event()
        self.closed = False
        self.dirty: set[tuple[str, str]] = set()
        self.bindings: dict[str, str] = {}
        self.reconciling: dict[str, asyncio.Task] = {}
        self.reconcile_after: dict[str, float] = {}
        self.build_seq = 0
        self.active_members: set[str] = set()
        self.title_lock = asyncio.Lock()
        self.title_catalogs: dict[str, tuple[float, dict[str, str]]] = {}

    async def database(self):
        async with self.database_lock:
            if self.store is None:
                self.store = await asyncio.to_thread(
                    CollaborationStore, self.machine.cfg.state_dir
                )
        return self.store

    async def start(self):
        async with self.start_lock:
            try:
                await self._start()
            except Exception as exc:
                log.warning(
                    "collaboration storage unavailable", error_type=type(exc).__name__
                )

    async def _start(self):
        if self.task is not None:
            return
        path = Path(self.machine.cfg.state_dir) / "collaboration.sqlite3"
        if path.exists():
            store = await self.database()
            for delivery in await asyncio.to_thread(store.deliveries, active=True):
                if delivery["status"] == "preparing":
                    await asyncio.to_thread(
                        store.set_delivery, delivery["id"], "queued"
                    )
                elif delivery["status"] == "submitting":
                    await asyncio.to_thread(
                        store.set_delivery,
                        delivery["id"],
                        "unknown",
                        error="上次提交结果待确认，请查看原生会话。",
                    )
            await self.restore_observers()
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="collaboration")

    async def close(self):
        self.closed = True
        tasks = (
            list(self.workers.values())
            + list(self.reconciling.values())
            + ([self.task] if self.task else [])
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def busy_count(self):
        return sum(not task.done() for task in self.workers.values())

    def pins(self, ctx):
        entry = self.members.get(id(ctx))
        return bool(
            entry
            and entry[0]() is ctx
            and (entry[1] in self.workers or entry[1] in self.active_members)
        )

    def watch(self, ctx, member_id):
        key = id(ctx)
        self.members[key] = (
            weakref.ref(ctx, lambda _ref: self.members.pop(key, None)),
            member_id,
        )

    def prepare_member(self, spec: CollaborationMemberSpec, *, check_cwd=True):
        options = spec.session_options()
        NewSession(**options)
        if spec.engine == "claude":
            profile = self.machine._claude_profile(spec.profile_id)
            profile_id, path = profile.id, str(profile.config_dir)
        elif spec.engine == "codex":
            profile = self.machine._codex_profile(spec.profile_id)
            profile_id, path = profile.id, str(profile.home)
        else:
            profile_id = "default"
            connection = self.machine.cfg.dsh_connection_file
            if not connection:
                raise ValueError("请先连接本机 DSH。")
            path = str(Path(connection).resolve())
        cwd = Path(spec.cwd or self.machine.cfg.cc_cwd).expanduser()
        if spec.cwd and check_cwd:
            if not cwd.is_absolute() or not cwd.is_dir():
                raise ValueError("工作目录须为设备上已存在的绝对目录。")
        cwd = cwd.resolve()
        options["cwd"] = str(cwd)
        values = spec.model_dump()
        values["profile_id"] = profile_id
        values["cwd"] = options.get("cwd")
        title = {"claude": "Claude", "codex": "Codex", "dsh": "DSH"}[spec.engine]
        values["name"] = (spec.name.strip() or f"{title} · {profile_id}")[:80]
        account_key = hashlib.sha256(
            f"{spec.engine}\0{profile_id}\0{path}".encode()
        ).hexdigest()
        return values, account_key

    def profiles(self):
        return {
            "claude": self.machine._claude_profiles.public_profiles(),
            "codex": self.machine._codex_profiles.public_profiles(),
            "dsh": (
                [{"id": "default", "label": "default"}]
                if self.machine.cfg.dsh_connection_file
                else []
            ),
        }

    async def snapshot(self, owner, room_id=None, *, before=None):
        store = await self.database()
        data = {
            "rooms": await asyncio.to_thread(store.rooms, owner),
            "profiles": self.profiles(),
        }
        if room_id:
            room = await asyncio.to_thread(store.room, owner, room_id)
            await self.sync_native_titles(room)
            data["room"] = await asyncio.to_thread(store.public_room, owner, room_id)
            messages = await asyncio.to_thread(
                store.messages, owner, room_id, before=before
            )
            # Preserve the full reply in storage; the native view provides its complete text.
            for message in messages:
                message["truncated"] = len(message["text"]) > 64 * 1024
                message["text"] = message["text"][: 64 * 1024]
            while len(json.dumps(messages).encode()) > 2 * 1024 * 1024:
                messages.pop(0)
            data["messages"] = messages
            data["before"] = before
            data["has_more"] = bool(messages and messages[0]["seq"] > 1)
            data["deliveries"] = await asyncio.to_thread(
                store.public_deliveries, room_id
            )
            questions = []
            for member in data["room"]["members"]:
                ctx = self.machine._ctx_for(member["sid"]) if member["sid"] else None
                if ctx:
                    questions.extend(
                        {
                            "member_id": member["id"],
                            "sid": ctx.key,
                            "question": pending.event.model_dump(mode="json"),
                        }
                        for pending in ctx.pending_asks.values()
                    )
            data["questions"] = questions
        return data

    async def publish(
        self, owner, room_id=None, *, request_id=None, to=None, before=None, error=None
    ):
        self.build_seq += 1
        version = self.build_seq
        data = await self.snapshot(owner, room_id, before=before)
        data.update(generation=self.machine.instance_id, version=version)
        frame = CollaborationState(
            owner_id=owner,
            to=to,
            room_id=room_id,
            request_id=request_id,
            data=data,
            error=error,
        )
        await self.machine.transport.send(frame)
        return frame

    async def handle(self, cmd):
        owner = cmd.owner_id
        if not owner:
            frame = CollaborationState(
                to=cmd.client_id, request_id=cmd.cmd_id, error="请重新登录后打开协作。"
            )
            await self.machine.transport.send(frame)
            return frame
        try:
            store = await self.database()
            if self.task is None:
                await self.start()
            if cmd.operation == "catalog":
                return await self.catalog(cmd)
            if cmd.operation == "sessions":
                sessions = await self.session_candidates(cmd.member, cmd.cmd_id)
                frame = CollaborationState(
                    owner_id=owner, to=cmd.client_id, request_id=cmd.cmd_id,
                    data={"sessions": sessions, "sessions_spec": cmd.member.model_dump()},
                )
                await self.machine.transport.send(frame)
                return frame
            room_id = cmd.room_id
            body = cmd.model_dump(exclude={
                "v", "ts", "seq", "to", "route_id", "owner_id", "client_id", "cmd_id",
            }, exclude_none=True)
            if cmd.operation == "send" and validate_attachments(cmd.images, cmd.files):
                raise ValueError("附件不符合要求，请调整后重试。")
            if cmd.operation == "reconcile":
                room = await asyncio.to_thread(store.room, owner, room_id)
                for member in room["members"]:
                    self.schedule_reconcile(member)
            elif cmd.operation == "answer":
                room = await asyncio.to_thread(store.room, owner, room_id)
                member = next(
                    (item for item in room["members"] if item["id"] == cmd.member_id),
                    None,
                )
                ctx = (
                    self.machine._ctx_for(member["sid"])
                    if member and member["sid"]
                    else None
                )
                pending = ctx.pending_asks.get(cmd.ask_id) if ctx else None
                if pending is None:
                    raise ValueError("该问题已关闭，请刷新当前状态。")
                result = await self.machine._handle_answer_question(
                    AnswerQuestion(
                        sid=ctx.key,
                        ask_id=cmd.ask_id,
                        answer=cmd.answer,
                        answers=cmd.answers,
                        client_id=pending.target or cmd.client_id,
                        owner_id=owner,
                        cmd_id=cmd.cmd_id,
                    )
                )
                if isinstance(result, Error):
                    raise ValueError(result.message)
            elif cmd.operation == "stop":
                member, fresh = await asyncio.to_thread(
                    store.stop_pending, owner, cmd.cmd_id, room_id, cmd.member_id
                )
                if fresh and member["sid"]:
                    ctx = self.machine._ctx_for(member["sid"])
                    if ctx is None and member["spec"]["engine"] == "dsh":
                        _values, account = self.prepare_member(
                            CollaborationMemberSpec.model_validate(member["spec"]),
                            check_cwd=False,
                        )
                        if account != member["account_key"]:
                            raise ValueError("参与者账号配置已变化，请核对账号后再停止。")
                        await self.machine._dsh.connection()
                        ctx = await self.machine._dsh.ctx(member["sid"])
                        self.watch(ctx, member["id"])
                    if ctx and ctx.state == "idle" and member["id"] in self.workers:
                        async with ctx.query_lock:
                            pass
                    rows = await asyncio.to_thread(
                        store.deliveries, room_id, active=True
                    )
                    stopping = any(
                        row["member_id"] == member["id"] and row["status"] == "stopping"
                        for row in rows
                    )
                    if ctx and ctx.state != "idle" and stopping:
                        stop = Interrupt(
                            sid=ctx.key,
                            client_id=cmd.client_id,
                            cmd_id=cmd.cmd_id,
                            owner_id=owner,
                        )
                        if member["spec"]["engine"] == "dsh":
                            await self.machine._dsh.handle_interrupt(stop)
                        else:
                            await self.machine._handle_interrupt(stop)
            elif cmd.operation == "rename_member":
                async with self.title_lock:
                    member = await asyncio.to_thread(
                        store.rename_target, owner, cmd.cmd_id, body
                    )
                    if member is not None:
                        if member["native_sid"]:
                            await self.rename_native(member, cmd.name.strip(), cmd.cmd_id)
                        await asyncio.to_thread(
                            store.mutate,
                            owner,
                            cmd.cmd_id,
                            cmd.operation,
                            body,
                            native_title_confirmed=bool(member["native_sid"]),
                        )
            elif cmd.operation not in {"list", "read"}:
                specs = (
                    cmd.members
                    if cmd.operation == "create"
                    else ([cmd.member] if cmd.operation == "add_member" else [])
                )
                prepared = [await self.prepare_binding(spec, cmd.cmd_id) for spec in specs]
                room_id = await asyncio.to_thread(
                    store.mutate,
                    owner,
                    cmd.cmd_id,
                    cmd.operation,
                    body,
                    prepared_members=prepared,
                )
            frame = await self.publish(
                owner,
                room_id,
                request_id=cmd.cmd_id,
                to=cmd.client_id,
                before=cmd.before,
            )
            if cmd.operation not in {"list", "read"}:
                self.dirty.add((owner, room_id))
                self.wake.set()
            return frame
        except (ValueError, OSError) as exc:
            error = (
                str(exc)
                if isinstance(exc, ValueError)
                else "聊天室数据暂时无法访问，请稍后重试。"
            )
        except Exception:
            log.exception("collaboration command failed", operation=cmd.operation)
            error = "操作暂时未完成，请查看设备连接后重试。"
        frame = CollaborationState(
            owner_id=owner,
            to=cmd.client_id,
            request_id=cmd.cmd_id,
            room_id=cmd.room_id,
            error=error[:4096],
        )
        await self.machine.transport.send(frame)
        return frame

    async def native_session_list(self, engine, request_id):
        query = ListSessions(
            engine=engine,
            space="code",
            cmd_id=request_id,
            client_id="collab-sessions-"
            + hashlib.sha256(request_id.encode()).hexdigest()[:32],
        )
        result = (
            await self.machine._dsh.dispatch(query)
            if engine == "dsh"
            else await self.machine._handle_list_sessions(query)
        )
        if not isinstance(result, SessionList):
            raise ValueError(
                result.message
                if isinstance(result, Error)
                else "会话列表暂不可读取，请重试。"
            )
        return result

    async def sync_native_titles(self, room):
        async with self.title_lock:
            # A queued snapshot may predate a confirmed rename or ID capture.
            current = await asyncio.to_thread(
                self.store.room, room["owner"], room["id"]
            )
            members = [
                member for member in current["members"]
                if member["sid"] and member["native_sid"]
            ]
            for engine in {member["spec"]["engine"] for member in members}:
                try:
                    now = asyncio.get_running_loop().time()
                    cached = self.title_catalogs.get(engine)
                    if cached is None or cached[0] <= now:
                        result = await asyncio.wait_for(
                            self.native_session_list(
                                engine,
                                f"collab-titles-{uuid5(NAMESPACE_URL, room['id'])}",
                            ),
                            timeout=3,
                        )
                        names = {
                            row.session_id: (row.summary or row.first_prompt or "").strip()[:80]
                            for row in result.sessions if row.space == "code"
                        }
                        cached = (now + 3, names)
                        self.title_catalogs[engine] = cached
                    for member in members:
                        if member["spec"]["engine"] != engine:
                            continue
                        try:
                            _values, account = self.prepare_member(
                                CollaborationMemberSpec.model_validate(member["spec"]),
                                check_cwd=False,
                            )
                            if account != member["account_key"]:
                                continue
                            if member.get("pending_title"):
                                title_hash = hashlib.sha256(
                                    member["pending_title"].encode()
                                ).hexdigest()[:16]
                                title = await self.rename_native(
                                    member,
                                    member["pending_title"],
                                    f"collab-title-{member['id']}-{title_hash}",
                                )
                                await asyncio.to_thread(
                                    self.store.sync_native_title,
                                    member["id"],
                                    title,
                                    clear_pending=True,
                                )
                            elif cached[1].get(member["sid"]):
                                await asyncio.to_thread(
                                    self.store.sync_native_title,
                                    member["id"],
                                    cached[1][member["sid"]],
                                )
                        except Exception as exc:
                            log.debug(
                                "collaboration member title unavailable",
                                member_id=member["id"],
                                error_type=type(exc).__name__,
                            )
                except Exception as exc:
                    log.debug(
                        "native collaboration titles unavailable",
                        engine=engine,
                        error_type=type(exc).__name__,
                    )

    async def rename_native(self, member, name, request_id):
        spec = CollaborationMemberSpec.model_validate(member["spec"])
        _values, account = self.prepare_member(spec, check_cwd=False)
        if account != member["account_key"]:
            raise ValueError("参与者账号配置已变化，请核对账号后再重命名。")
        command = RenameSession(
            session_id=member["sid"],
            title=name,
            engine=spec.engine,
            space="code",
            cmd_id=request_id,
            client_id="collab-rename-"
            + hashlib.sha256(request_id.encode()).hexdigest()[:32],
        )
        result = (
            await self.machine._dsh.dispatch(command)
            if spec.engine == "dsh"
            else await self.machine._handle_rename_session(command)
        )
        outcomes = result if isinstance(result, tuple) else (result,)
        error = next((item for item in outcomes if isinstance(item, Error)), None)
        self.title_catalogs.pop(spec.engine, None)
        if error:
            raise ValueError(error.message)
        listing = next((item for item in outcomes if isinstance(item, SessionList)), None)
        if listing is None:
            listing = await self.native_session_list(spec.engine, request_id)
        row = next((item for item in listing.sessions if item.session_id == member["sid"]), None)
        title = (row.summary or row.first_prompt or "").strip() if row else ""
        if title != name:
            raise ValueError("原生会话名称尚未确认，请稍后重试。")
        return title

    async def session_candidates(self, spec, request_id):
        prepared, account = self.prepare_member(spec, check_cwd=False)
        result = await self.native_session_list(spec.engine, request_id)
        store = await self.database()
        bound = await asyncio.to_thread(store.bound_sessions, account)
        sessions = []
        for row in result.sessions:
            if row.space != "code" or row.tag == "archived" or not row.cwd or not Path(row.cwd).is_absolute():
                continue
            if row.session_id.startswith("tmp-"):
                continue
            if spec.engine == "claude":
                profile, native_sid = self.machine._claude_target(row.session_id)
                if profile.id != prepared["profile_id"]:
                    continue
            elif spec.engine == "codex":
                profile, native_sid = self.machine._codex_target(row.session_id)
                if profile.id != prepared["profile_id"]:
                    continue
            else:
                from cc_remote.wrapper.dsh_client import native_session_id

                native_sid = native_session_id(row.session_id)
            sessions.append({
                "sid": row.session_id, "native_sid": native_sid,
                "name": ((row.summary or row.first_prompt or "").strip() or native_sid)[:80],
                "cwd": row.cwd, "state": row.state,
                "unavailable": native_sid in bound,
            })
        return sessions

    async def prepare_binding(self, spec, request_id):
        values, account = self.prepare_member(spec, check_cwd=not spec.session_id)
        if not spec.session_id:
            return values, account, None, spec.name.strip() or None
        sessions = await self.session_candidates(spec, request_id)
        binding = next((row for row in sessions if row["sid"] == spec.session_id), None)
        if binding is None:
            raise ValueError("所选会话不属于该账号的 Code 空间，请刷新后重试。")
        values["cwd"] = binding["cwd"]
        values["name"] = binding["name"]
        return values, account, binding

    async def catalog(self, cmd):
        spec = cmd.member
        prepared, _account = self.prepare_member(spec)
        target = (
            "collab-catalog-" + hashlib.sha256(cmd.cmd_id.encode()).hexdigest()[:32]
        )
        query = GetModels(
            engine=spec.engine,
            cwd=prepared["cwd"],
            client_id=target,
            claude_profile_id=spec.profile_id if spec.engine == "claude" else None,
            codex_profile_id=spec.profile_id if spec.engine == "codex" else None,
        )
        result = (
            await self.machine._dsh.dispatch(query)
            if spec.engine == "dsh"
            else await self.machine._handle_get_models(query)
        )
        if not isinstance(result, Models):
            raise ValueError(
                result.message
                if isinstance(result, Error)
                else "原生模型列表暂时无法读取。"
            )
        data = {
            "catalog": result.model_dump(mode="json"),
            "catalog_spec": spec.model_dump(),
        }
        if spec.engine == "codex":
            permissions = await self.machine._handle_get_permission_profiles(
                GetPermissionProfiles(
                    cwd=prepared["cwd"],
                    codex_profile_id=spec.profile_id,
                    client_id=target,
                )
            )
            if isinstance(permissions, PermissionProfiles):
                data["permission_profiles"] = permissions.model_dump(mode="json")
        frame = CollaborationState(
            owner_id=cmd.owner_id, to=cmd.client_id, request_id=cmd.cmd_id, data=data
        )
        await self.machine.transport.send(frame)
        return frame

    async def run(self):
        while not self.closed:
            try:
                if self.store:
                    rows = await asyncio.to_thread(self.store.deliveries, active=True)
                    self.active_members = {
                        row["member_id"] for row in rows if row["status"] != "unknown"
                    }
                    if self.machine._ui_updates.guarded():
                        await asyncio.sleep(1)
                        continue
                    for row in rows:
                        if (
                            row["status"] != "queued"
                            or row["member_id"] in self.workers
                        ):
                            continue
                        if any(
                            other["member_id"] == row["member_id"]
                            and other["status"]
                            in {"preparing", "submitting", "stopping", "unknown"}
                            for other in rows
                        ):
                            continue
                        member_id = row["member_id"]
                        task = asyncio.create_task(
                            self.deliver(row), name=f"collaboration-{member_id}"
                        )
                        self.workers[member_id] = task
                        task.add_done_callback(
                            lambda done, key=member_id: self.worker_done(key, done)
                        )
                    for member_id in {
                        row["member_id"] for row in rows if row["status"] != "queued"
                    }:
                        if (
                            member_id not in self.workers
                            and asyncio.get_running_loop().time()
                            >= self.reconcile_after.get(member_id, 0)
                        ):
                            member = await asyncio.to_thread(
                                self.store.member_by_id, member_id
                            )
                            self.schedule_reconcile(member)
                    dirty, self.dirty = self.dirty, set()
                    for owner, room_id in dirty:
                        await self.publish(owner, room_id)
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("collaboration reconciliation failed")
                await asyncio.sleep(1)

    def worker_done(self, member_id, task):
        self.workers.pop(member_id, None)
        if not task.cancelled() and task.exception():
            log.error(
                "collaboration delivery failed",
                error_type=type(task.exception()).__name__,
            )
        self.wake.set()

    async def context(self, member, delivery):
        if member["sid"] and self.machine._ctx_for(member["sid"]):
            return await self._context(member, delivery)
        future = asyncio.get_running_loop().create_future()
        values = dict(client_id=f"collaboration-{member['id']}", cmd_id=delivery["id"])
        command = (
            SwitchSession(
                session_id=member["sid"], engine=member["spec"]["engine"], **values
            )
            if member["sid"]
            else NewSession(engine=member["spec"]["engine"], **values)
        )

        async def prepare(_command):
            if future.cancelled():
                return
            try:
                ctx = await self._context(member, delivery)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(ctx)

        # Reuse the normal pool-admission lane for creates and cold resumes.
        # Actual inputs retain their independent per-session query/steer locks.
        await self.machine._command_scheduler.submit(command, handler=prepare)
        return await future

    async def _context(self, member, delivery):
        spec = CollaborationMemberSpec.model_validate(member["spec"])
        _values, account = self.prepare_member(spec, check_cwd=not member["sid"])
        if account != member["account_key"]:
            raise ValueError("参与者账号配置已变化，请重新添加参与者并核对账号。")
        if spec.engine == "dsh":
            await self.machine._dsh.connection()
        ctx = self.machine._ctx_for(member["sid"]) if member["sid"] else None
        if ctx is not None and (
            ctx.engine != spec.engine or ctx.space != "code" or ctx.btw
        ):
            raise ValueError("会话与当前参与者的 Harness 或空间不匹配。")
        if ctx is None:
            options = spec.session_options()
            options["cwd"] = member["cwd"] or spec.cwd or str(self.machine.cfg.cc_cwd)
            if member["sid"]:
                options = {
                    key: value
                    for key, value in options.items()
                    if key in {"engine", "cwd", "claude_profile_id", "codex_profile_id"}
                }
                if not member["native_sid"]:
                    raise ValueError(
                        "原生会话身份待确认，请查看对应 Harness 后重新添加参与者。"
                    )
                if spec.engine == "dsh":
                    ctx = await self.machine._dsh.ctx(member["sid"])
                    await self.machine._dsh.activate(ctx)
                else:
                    ctx = await self.machine._spawn(
                        resume_id=member["native_sid"], raise_on_failure=True, **options
                    )
            else:
                await asyncio.to_thread(
                    self.store.set_delivery, delivery["id"], "preparing"
                )
                cmd = NewSession(
                    **options,
                    request_id=delivery["id"],
                    cmd_id=delivery["id"],
                    client_id=f"collaboration-{member['id']}",
                    owner_id=self.store.owner(member["room_id"]),
                )
                result = (
                    await self.machine._dsh.handle_new_session(cmd, focus=False)
                    if spec.engine == "dsh"
                    else await self.machine._handle_new_session(
                        cmd, _focus=False, _persist_defaults=False
                    )
                )
                frames = result if isinstance(result, (tuple, list)) else (result,)
                error = next((item for item in frames if isinstance(item, Error)), None)
                if error:
                    raise ValueError(error.message)
                focus = next(
                    (item for item in frames if isinstance(item, SessionFocus)), None
                )
                ctx = self.machine._ctx_for(focus.session_id) if focus else None
                if ctx is None:
                    raise ValueError("原生会话创建结果待确认，请查看对应 Harness。")
        self.watch(ctx, member["id"])
        await self.bind(member["id"], ctx)
        return ctx

    async def bind(self, member_id, ctx):
        sid = self.machine._ctx_wire_sid(ctx) or ctx.key
        self.bindings[sid] = member_id
        await asyncio.to_thread(
            self.store.bind, member_id, sid, self.native_id(ctx), ctx.cwd
        )

    @staticmethod
    def native_id(ctx):
        if ctx.engine == "dsh":
            return getattr(getattr(ctx, "sdk", None), "native_id", ctx.session_id)
        return ctx.session_id

    async def deliver(self, delivery):
        store = self.store
        member = await asyncio.to_thread(store.member_by_id, delivery["member_id"])
        owner = await asyncio.to_thread(store.owner, delivery["room_id"])
        attempted = False
        try:
            if not await asyncio.to_thread(store.claim, delivery["id"]):
                return
            self.active_members.add(member["id"])
            if member["removed"]:
                await asyncio.to_thread(store.set_delivery, delivery["id"], "cancelled")
                return
            ctx = await self.context(member, delivery)
            if ctx.state in {"interrupting", "draining"} or ctx.pending_asks:
                await asyncio.to_thread(
                    store.set_delivery,
                    delivery["id"],
                    "queued",
                    error="等待原生会话处理当前交互。",
                )
                return
            if getattr(ctx, "write_state", "writable") != "writable":
                await asyncio.to_thread(
                    store.set_delivery,
                    delivery["id"],
                    "queued",
                    error="等待原生会话恢复可写状态。",
                )
                return
            mode = "steer" if ctx.state == "running" else "query"
            delivery = await asyncio.to_thread(store.submit, delivery["id"], mode)
            if delivery is None:
                return
            room = await asyncio.to_thread(store.room, owner, member["room_id"])
            messages, current = await asyncio.to_thread(store.context, delivery)
            prompt = shared_prompt(room, member, messages, current)
            payload = await asyncio.to_thread(store.payload, delivery["message_id"])
            values = dict(
                sid=ctx.key,
                prompt=prompt,
                msg_id=delivery["id"],
                cmd_id=delivery["id"],
                client_id=f"collaboration-{member['id']}",
                owner_id=owner,
                **payload,
            )
            if mode == "steer" and ctx.engine == "claude":
                native_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        json.dumps([ctx.key, values["client_id"], delivery["id"]]),
                    )
                )
                await asyncio.to_thread(store.expect_input, delivery["id"], native_id)
            if not await asyncio.to_thread(store.can_submit, delivery["id"]):
                return
            attempted = True
            if mode == "steer":
                cmd = Steer(**values)
                result = (
                    await self.machine._dsh.handle_steer(cmd)
                    if ctx.engine == "dsh"
                    else await self.machine._handle_steer(cmd)
                )
            else:
                cmd = Query(**values)
                result = await self.machine._handle_query(cmd)
            latest = await asyncio.to_thread(store.delivery, delivery["id"])
            if latest and latest["status"] in {
                "running",
                "succeeded",
                "cancelled",
                "failed",
            }:
                pass
            elif isinstance(result, Error):
                unknown = getattr(result, "_outcome_unknown", False) or result.code in {
                    "steer_outcome_unknown",
                    "mutation_unknown",
                }
                status = "unknown" if unknown else "failed"
                if result.code in {"busy", "not_steerable", "dsh_busy"}:
                    status = "queued"
                if latest and latest["status"] == "stopping" and not unknown:
                    status = "cancelled"
                await asyncio.to_thread(
                    store.set_delivery, delivery["id"], status, error=result.message
                )
            elif latest and latest["status"] == "submitting":
                await asyncio.to_thread(store.set_delivery, delivery["id"], "accepted")
        except asyncio.CancelledError:
            await asyncio.shield(
                asyncio.to_thread(
                    store.set_delivery,
                    delivery["id"],
                    "unknown" if attempted else "queued",
                    error="投递中断，等待核对原生会话。",
                )
            )
            raise
        except Exception as exc:
            unknown = attempted or bool(getattr(exc, "outcome_unknown", False))
            await asyncio.to_thread(
                store.set_delivery,
                delivery["id"],
                "unknown" if unknown else "failed",
                error=str(exc)[:1024]
                if isinstance(exc, ValueError)
                else "请查看原生会话与设备连接，核对本次输入状态。",
            )
            log.warning(
                "collaboration input unresolved",
                run_id=delivery["id"],
                error_type=type(exc).__name__,
            )
        finally:
            self.dirty.add((owner, member["room_id"]))

    async def restore_observers(self):
        rows = await asyncio.to_thread(self.store.deliveries, active=True)
        pending = {row["member_id"] for row in rows}
        # After a restart, a terminal can replay before its missing text. Keep
        # these durable active rows until authoritative history repairs them.
        self.recovery_pending.update(
            row["id"] for row in rows if row["status"] != "queued"
        )
        for member in await asyncio.to_thread(self.store.bound_members):
            self.bindings[member["sid"]] = member["id"]
            ctx = self.machine._ctx_for(member["sid"])
            if ctx:
                self.watch(ctx, member["id"])
            if member["id"] in pending:
                self.schedule_reconcile(member)

    def schedule_reconcile(self, member):
        if not member or not member["sid"] or member["id"] in self.reconciling:
            return
        member_id = member["id"]
        task = asyncio.create_task(
            self.reconcile(member), name=f"room-reconcile-{member_id}"
        )
        self.reconciling[member_id] = task
        self.reconcile_after[member_id] = asyncio.get_running_loop().time() + 30
        task.add_done_callback(lambda _task: self.reconciling.pop(member_id, None))

    async def reconcile(self, member):
        """Repair the room from native history without resubmitting an input."""
        try:
            _spec, account = self.prepare_member(
                CollaborationMemberSpec.model_validate(member["spec"]), check_cwd=False
            )
            if account != member["account_key"]:
                return
            rows = await asyncio.to_thread(
                self.store.deliveries, member["room_id"], active=True
            )
            pending = [
                row
                for row in rows
                if row["member_id"] == member["id"] and row["status"] != "queued"
            ]
            if not pending:
                return
            sid = member["sid"]
            ctx = self.machine._ctx_for(sid)
            if ctx:
                await self.bind(member["id"], ctx)
                member = await asyncio.to_thread(self.store.member_by_id, member["id"])
                sid = member["sid"]
            else:
                resolved = self.machine._resolve_session_alias(sid) or sid
                if resolved != sid:
                    native = (
                        self.machine._codex_target(resolved)[1]
                        if member["spec"]["engine"] == "codex"
                        else self.machine._claude_target(resolved)[1]
                    )
                    await asyncio.to_thread(
                        self.store.bind, member["id"], resolved, native, member["cwd"]
                    )
                    member = await asyncio.to_thread(
                        self.store.member_by_id, member["id"]
                    )
                    sid = resolved
            projection = _ProjectionContext(
                sid, member["native_sid"], member["cwd"], member["spec"]["engine"],
                live_context=ctx, live_seq=ctx.seq if ctx else None,
            )
            self.watch(projection, member["id"])
            if projection.engine == "dsh":
                await self.machine._dsh.connection()

            async def read_page(before):
                command = GetHistory(
                    session_id=sid, detail="full", limit=16,
                    cwd=member["cwd"], before=before,
                )
                if projection.engine == "dsh":
                    history = await self.machine._dsh.handle_get_history(command, publish=False)
                else:
                    history = await self.machine._history_page_singleflight(command, sid)
                if history.session_id != sid:
                    raise ValueError("原生会话历史身份不匹配。")
                return history

            identities = self.delivery_identities(pending)
            remaining = {row["id"] for row in pending}
            cursors = []
            before = None
            revision = None
            while True:
                if not self.projection_current(projection):
                    return
                history = await read_page(before)
                if not history.authoritative or history.error:
                    return
                if projection.live_seq is not None and history.live_seq is not None:
                    projection.live_seq = min(projection.live_seq, history.live_seq)
                if revision is None:
                    revision = history.revision
                elif history.revision != revision:
                    return
                cursors.append(before)
                for event in history.events:
                    for key in ("msg_id", "client_msg_id", "turn_id", "checkpoint_id"):
                        remaining.difference_update(identities.get(event.get(key), ()))
                if not remaining or not history.has_more:
                    break
                older = history.oldest_id
                if not older or older in cursors:
                    break
                before = older

            # Retain only cursors while scanning, then replay oldest first.
            # This also lets an older user echo bind a newer terminal page.
            loaded_before = before
            for cursor in reversed(cursors):
                if not self.projection_current(projection):
                    return
                if cursor != loaded_before:
                    history = await read_page(cursor)
                    loaded_before = cursor
                if projection.live_seq is not None and history.live_seq is not None:
                    projection.live_seq = min(projection.live_seq, history.live_seq)
                if (not history.authoritative or history.error
                        or history.revision != revision):
                    return
                if await self.replay_history(projection, history, identities) is False:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "room native reconciliation deferred",
                member_id=member["id"],
                error_type=type(exc).__name__,
            )

    @staticmethod
    def delivery_identities(pending):
        identities: dict[str, set[str]] = {}
        for row in pending:
            # A known native owner wins over the business input ID, which
            # may also identify a completed predecessor of a follow-up.
            keys = (row["native_turn"],) if row["native_turn"] else (
                row["id"], row["native_input_id"],
            )
            for identity in keys:
                if identity:
                    identities.setdefault(identity, set()).add(row["id"])
        return identities

    def projection_current(self, projection):
        live = self.machine._ctx_for(projection.key)
        return live is projection.live_context and (
            live is None or projection.live_seq is None or live.seq <= projection.live_seq
        )

    async def replay_history(self, projection, history, identities):
        events = history.events
        if any(event.get("type") == "error" for event in events):
            events = await self.machine._history_recovery_events(history, projection.engine)
            if events is None:
                return False
        native_owners = {
            event["msg_id"]: event["turn_id"] for event in events
            if event.get("type") == "turn_binding"
        }
        matching = set()
        event_types = {
            cls.model_fields["type"].default: cls
            for cls in (
                UserMsg,
                TurnBinding,
                TurnSteered,
                AssistantMsgStart,
                Delta,
                AssistantMsgEnd,
                TurnEnd,
            )
        }
        last_user = max(
            (
                index
                for index, item in enumerate(events)
                if item.get("type") == "user_msg"
            ),
            default=-1,
        )
        for index, event in enumerate(events):
            kind = event.get("type")
            if kind in {"user_msg", "turn_steered", "turn_binding"}:
                keys = {event.get("msg_id"), event.get("client_msg_id")}
                keys.add(native_owners.get(event.get("msg_id")))
                if kind != "user_msg":
                    keys.add(event.get("turn_id"))
                matching = set()
                for key in keys:
                    matching.update(identities.get(key, ()))
                owner = event.get("turn_id") or event.get("msg_id")
                if matching and owner:
                    identities.setdefault(owner, set()).update(matching)
            # Claude terminal turn_id is an assistant fork point. Its
            # checkpoint_id identifies the owning native user turn.
            owner = (
                event.get("checkpoint_id")
                if kind == "turn_end" and projection.engine == "claude"
                else event.get("turn_id")
            )
            scope = identities.get(owner, set()) if owner else matching
            if not scope or kind not in event_types:
                continue
            if kind == "turn_end" and history.in_progress and index > last_user:
                continue
            if await self.observe(
                projection, event_types[kind].model_validate(event),
                delivery_ids=frozenset(scope),
            ) is False:
                return False
            if (
                kind == "turn_end"
                and event.get("result", {}).get("subtype") != "steered"
            ):
                matching = set()

    async def observe(self, ctx, frame, *, delivery_ids=None):
        entry = self.members.get(id(ctx))
        if self.store and entry is None and ctx.key in self.bindings:
            self.watch(ctx, self.bindings[ctx.key])
            entry = self.members.get(id(ctx))
        if not self.store or not entry or entry[0]() is not ctx:
            return
        member_id = entry[1]
        async with self.projection_locks.setdefault(member_id, asyncio.Lock()):
            if isinstance(ctx, _ProjectionContext):
                if not self.projection_current(ctx):
                    return False
            # Serialize projection writes with native consumption. Live frames
            # can advance seq during I/O, then their observation runs next.
            await self._observe(ctx, frame, member_id, delivery_ids=delivery_ids)

    async def _observe(self, ctx, frame, member_id, *, delivery_ids=None):
        kind = frame.type
        texts = ctx.texts if isinstance(ctx, _ProjectionContext) else self.texts
        if kind in {"assistant_msg_start", "delta", "assistant_msg_end"}:
            key = (member_id, frame.message_id)
            if getattr(frame, "channel", "unknown") == "thinking":
                return
            if kind == "assistant_msg_start":
                texts[key] = {"text": "", "channel": frame.channel}
            item = texts.setdefault(key, {"text": "", "channel": "unknown"})
            texts.move_to_end(key)
            channel = getattr(frame, "channel", "unknown")
            if channel != "unknown":
                item["channel"] = channel
            if kind == "assistant_msg_end":
                if frame.canonical_message_id:
                    item["canonical_id"] = frame.canonical_message_id
                if frame.delivery:
                    item["delivery"] = frame.delivery
                if frame.questions is not None:
                    item["has_questions"] = bool(frame.questions)
            if kind == "delta" and item["channel"] != "thinking":
                text = frame.text if frame.replace else item["text"] + frame.text
                item["text"] = text if isinstance(ctx, _ProjectionContext) else text[: 2 * 1024 * 1024]
            while len(texts) > 32:
                texts.popitem(last=False)
            if kind != "assistant_msg_end":
                return
        if kind not in {
            "assistant_msg_end",
            "turn_end",
            "turn_binding",
            "turn_steered",
            "user_msg",
            "ask_user",
            "ask_user_closed",
            "session_rekey",
            "state",
            "snapshot",
            "session_migrated",
            "error",
        }:
            return
        member = await asyncio.to_thread(self.store.member_by_id, member_id)
        owner = await asyncio.to_thread(self.store.owner, member["room_id"])
        if (
            kind in {"session_rekey", "session_migrated"}
            or member["cwd"] != ctx.cwd
            or member["native_sid"] != self.native_id(ctx)
        ):
            await self.bind(member_id, ctx)
        if kind == "state" and getattr(frame, "continuation", False):
            await asyncio.to_thread(self.store.resume_source, member_id, [frame.msg_id])
        if (
            kind == "turn_binding"
            and frame.autonomous
            and frame.continuation == "subagent"
        ):
            await asyncio.to_thread(
                self.store.resume_source, member_id, autonomous=True, native_turn=frame.turn_id
            )
        rows = await asyncio.to_thread(
            self.store.deliveries, member["room_id"], active=True
        )
        active = [
            row for row in rows
            if row["member_id"] == member_id
            and (delivery_ids is None or row["id"] in delivery_ids)
        ]
        if kind == "error":
            for row in active:
                if frame.msg_id == row["id"] and row["status"] in {
                    "submitting",
                    "accepted",
                }:
                    status = "unknown"
                    if frame.code in {"busy", "not_running", "bad_prompt"}:
                        status = "queued" if frame.code == "busy" else "failed"
                    if frame.code == "not_steerable":
                        status = "cancelled"
                    await asyncio.to_thread(
                        self.store.set_delivery, row["id"], status, error=frame.message
                    )
        if kind in {"user_msg", "turn_steered", "turn_binding"}:
            identities = {
                getattr(frame, name, None) for name in ("msg_id", "client_msg_id")
            } - {None}
            for row in active:
                if (
                    row["id"] in identities
                    or row["native_input_id"] in identities
                    or row["native_turn"]
                    and row["native_turn"] in identities
                ):
                    await asyncio.to_thread(
                        self.store.consume,
                        row["id"],
                        getattr(frame, "turn_id", None) or frame.msg_id,
                    )
            if kind == "user_msg":
                known = await asyncio.to_thread(
                    self.store.deliveries, member["room_id"]
                )
                matched = [
                    row
                    for row in known
                    if (
                        row["member_id"] == member_id
                        and identities
                        & {row["id"], row["native_turn"], row["native_input_id"]}
                    )
                ]
                if not matched or all(
                    row["status"] in {"failed", "cancelled"} for row in matched
                ):
                    await asyncio.to_thread(self.store.clear_source, member_id)
        running = [row for row in active if row["status"] in {"running", "stopping"}]
        owners = (
            {frame.checkpoint_id, frame._changes_turn_id}
            if kind == "turn_end" and ctx.engine == "claude"
            else {getattr(frame, "turn_id", None)}
        ) - {None}
        if kind in {"assistant_msg_end", "turn_end"} and owners:
            running = [
                row for row in running
                if owners & {row["id"], row["native_turn"], row["native_input_id"]}
            ]
        if kind == "assistant_msg_end" and running:
            key = (member_id, frame.message_id)
            item = texts.get(key, {})
            public_reply = (
                item.get("has_questions", False)
                if item.get("delivery") == "async"
                else item.get("channel") == "final"
            )
            if public_reply and item["text"].strip():
                message_id = item.get("canonical_id") or frame.message_id
                source = f"{ctx.engine}:{member_id}:{message_id}"
                # Mark before the write. If it raises, live terminals must leave
                # these rows active so native history can recover the reply.
                for row in running:
                    self.pending_replies.setdefault(row["id"], set()).add(source)
                await asyncio.to_thread(
                    self.store.publish,
                    member_id,
                    source,
                    item["text"],
                    [row["message_id"] for row in running],
                )
                for row in running:
                    pending = self.pending_replies.get(row["id"])
                    if pending is not None:
                        pending.discard(source)
                        if not pending:
                            self.pending_replies.pop(row["id"], None)
        if kind == "turn_end" and frame.result.subtype != "steered":
            status = (
                "cancelled"
                if frame.result.subtype == "interrupted"
                or getattr(frame.result, "interrupted", False)
                or (
                    frame.result.subtype == "error_during_execution"
                    and any(row["status"] == "stopping" for row in running)
                )
                else "failed"
                if frame.result.is_error
                else "succeeded"
            )
            completed = {
                row["id"] for row in running
                if delivery_ids is not None
                or (row["id"] not in self.pending_replies
                    and row["id"] not in self.recovery_pending)
            }
            await asyncio.to_thread(
                self.store.finish,
                member_id,
                status,
                frame.result.subtype if status == "failed" else None,
                delivery_ids=completed,
            )
            for delivery_id in completed:
                self.pending_replies.pop(delivery_id, None)
                self.recovery_pending.discard(delivery_id)
        if kind in {
            "assistant_msg_end",
            "turn_end",
            "turn_steered",
            "user_msg",
            "ask_user",
            "ask_user_closed",
            "session_rekey",
            "state",
            "snapshot",
            "session_migrated",
            "error",
        }:
            self.dirty.add((owner, member["room_id"]))
            self.wake.set()
