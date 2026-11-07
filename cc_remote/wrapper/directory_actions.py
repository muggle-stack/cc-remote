"""Bounded Code directory operations over a confirmed native catalog snapshot.

Plans are private to one client and wrapper generation. An interrupted/restarted
operation can be inspected, but never implicitly replayed against a new catalog.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
from pathlib import Path
import time
import uuid

from cc_remote.protocol import (
    ArchiveSession, DeleteSession, DirectoryActionPlan, DirectoryActionProgress,
    Error, ERR_BUSY, ERR_INTERNAL, ListSessions,
)
from cc_remote.wrapper.codex_sessions import (
    CODEX_EXACT_CATALOG_MAX_IDS, codex_exact_catalog_rows, list_codex_directory_sessions,
)
from cc_remote.wrapper.claude_external import claude_session_holders
from cc_remote.log import logger

log = logger("cc_remote.wrapper.directory_actions")

MAX_SESSIONS = 1000
PLAN_TTL = 120
_deferred_owner = ContextVar("directory_action_owner", default=None)


def defer_session_lists() -> bool:
    return _deferred_owner.get() is asyncio.current_task()


@contextmanager
def _defer_lists():
    token = _deferred_owner.set(asyncio.current_task())
    try:
        yield
    finally:
        _deferred_owner.reset(token)


@dataclass(frozen=True)
class Entry:
    sid: str
    native_sid: str
    profile_id: str
    archived: bool


@dataclass
class Plan:
    owner: str
    response: DirectoryActionPlan
    entries: tuple[Entry, ...]
    expires: float
    started: bool = False
    progress: DirectoryActionProgress | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DirectoryActions:
    def __init__(self, machine):
        self.machine = machine
        self.plans: dict[str, Plan] = {}
        self._prepare_lock = asyncio.Lock()
        self.mutating: set[str] = set()

    async def reject_conflict(self, cmd):
        if cmd.type not in {"query", "steer", "switch_session", "reconnect", "rollback",
                            "fork_session", "fork_session_worktree", "migrate_session",
                            "cancel_queued_query", "update_queued_query", "reorder_queued_queries"}:
            return None
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None) or self.machine.focused_sid
        sid = self.machine._resolve_session_alias(sid) or sid
        if sid not in self.mutating:
            return None
        error = Error(code=ERR_BUSY, message="此会话正在归档或删除，请刷新列表后重试。",
                      request_id=cmd.cmd_id, to=cmd.client_id, sid=sid)
        await self.machine.transport.send(error)
        return error

    @asynccontextmanager
    async def _reserve(self, engine, entry):
        # Native Codex mutations already lock the entire descendant tree.
        # Claude SDK catalog operations need the resident query/queue boundary
        # as well as rejection of a new cold resume while the file is removed.
        self.mutating.add(entry.sid)
        try:
            async with AsyncExitStack() as stack:
                ctx = self.machine._ctx_by_sid(entry.sid)
                if engine == "claude" and ctx is not None:
                    await stack.enter_async_context(ctx.query_lock)
                    await stack.enter_async_context(ctx.queued_query_lock)
                yield
        finally:
            self.mutating.discard(entry.sid)

    async def _error(self, cmd, message):
        result = Error(code=ERR_INTERNAL, message=message[:512],
                       request_id=cmd.cmd_id, to=cmd.client_id)
        await self.machine.transport.send(result)
        return result

    def _profiles(self, engine, profile_id=None):
        profiles = self.machine._codex_profiles if engine == "codex" else self.machine._claude_profiles
        selected = [profile for profile in profiles if profile_id is None or profile.id == profile_id]
        if not selected:
            raise ValueError("账号已移除，请重新选择目录。")
        return selected

    def _cwd(self, engine, sid, native_cwd):
        ctx = self.machine._ctx_by_sid(sid)
        if ctx:
            return ctx.cwd
        if engine == "codex" and self.machine._codex_controls is not None:
            return self.machine._codex_controls.cwd_overrides().get(sid, native_cwd)
        return native_cwd

    async def _inventory(self, cmd):
        machine = self.machine
        if not getattr(machine, f"_{cmd.engine}_profile_migration_ok", False):
            raise ValueError("账号归属迁移未完成")
        records = await asyncio.to_thread(machine._work.for_engine(cmd.engine).records_by_profile_session)
        result = {}
        profiles = self._profiles(cmd.engine, cmd.profile_id)
        for profile in profiles:
            if cmd.engine == "codex":
                home = machine._codex_home(profile)
                rows = await list_codex_directory_sessions(cmd.cwd, codex_home=home, limit=MAX_SESSIONS)
                # Accepted in-place migrations can precede the next native
                # catalog write. Include only exact, still-existing identities.
                overrides = machine._codex_controls.cwd_overrides() if machine._codex_controls else {}
                extra = []
                for sid, cwd in overrides.items():
                    try:
                        owner, native = machine._codex_target(sid)
                    except ValueError:
                        continue
                    if owner.id == profile.id and cwd == cmd.cwd:
                        extra.append(native)
                for start in range(0, len(extra), CODEX_EXACT_CATALOG_MAX_IDS):
                    exact = await asyncio.to_thread(codex_exact_catalog_rows,
                        extra[start:start + CODEX_EXACT_CATALOG_MAX_IDS], codex_home=home)
                    if exact is None:
                        raise ValueError("目录迁移状态暂不可用，请稍后重试。")
                    rows.extend(exact)
                rows = machine._filter_private_codex_btw_rows([
                    {**row, "session_id": machine._codex_wire_sid(profile, row["session_id"]),
                     "native_session_id": row["session_id"], "codex_profile_id": profile.id}
                    for row in rows])
            else:
                infos = await asyncio.to_thread(machine._claude_catalog_list_sessions, profile,
                    limit=MAX_SESSIONS + 1, directory=cmd.cwd, include_worktrees=False)
                if len(infos) > MAX_SESSIONS:
                    raise ValueError("目录会话过多，请按账号分别操作。")
                blocked = await asyncio.to_thread(machine._bg_blocked_session_ids, profile.config_dir)
                rows = [{"session_id": machine._claude_wire_sid(profile, info.session_id),
                         "native_session_id": info.session_id, "cwd": info.cwd,
                         "tag": getattr(info, "tag", None)} for info in infos
                        if info.session_id not in blocked]
            for row in rows:
                sid, native = row["session_id"], row["native_session_id"]
                if ((profile.id, native) in records or sid in machine._private_btw_sessions
                        or self._cwd(cmd.engine, sid, row.get("cwd")) != cmd.cwd):
                    continue
                result[sid] = Entry(sid, native, profile.id, row.get("tag") == "archived")
            if len(result) > MAX_SESSIONS:
                raise ValueError("目录会话过多，请按账号分别操作。")
        return list(result.values()), profiles

    async def _claude_external_busy(self, entries, cwd):
        """One account-aware native holder scan for the entire confirmation."""
        if not entries:
            return set()
        machine = self.machine
        paths, roots, natives = {}, {}, {}
        for entry in entries:
            profile = self._profiles("claude", entry.profile_id)[0]
            path = await asyncio.to_thread(machine._claude_transcript_path, profile, entry.native_sid)
            if path is None:
                continue
            paths[entry.sid] = os.fspath(path)
            roots[entry.sid] = str(profile.config_dir)
            natives[entry.sid] = entry.native_sid
        owners = {identity for ctx in machine.sessions.values() if ctx.engine == "claude"
                  and (identity := getattr(getattr(ctx.sdk, "client", None), "owner_identity", None)) is not None}
        scan = await asyncio.to_thread(claude_session_holders, paths, {sid: cwd for sid in paths},
            wrapper_pid=os.getpid(), config_dirs=roots, native_session_ids=natives,
            default_config_dir=str(Path.home() / ".claude"), owner_identities=owners,
            continue_bindings=machine._claude_continue_bindings,
            continue_candidates=machine._claude_continue_candidates,
            continue_resolver=machine._latest_claude_session_for_cwd)
        if not scan.complete:
            return {entry.sid for entry in entries}
        return {sid for sid, holders in scan.holders.items() if holders} | {
            entry.sid for entry in entries if entry.sid not in paths}

    async def _busy(self, engine, entry):
        machine = self.machine
        scheduler = getattr(machine, "_command_scheduler", None)
        if scheduler is not None and scheduler.owns_target(entry.sid):
            return True
        ctx = machine._ctx_by_sid(entry.sid)
        if ctx and (machine._session_delete_busy(ctx) or ctx.btw
                    or (engine == "claude" and machine._claude_has_background_work(ctx))
                    or (engine == "codex" and ctx.codex_background_processes)):
            return True
        if machine._is_external(entry.sid):
            return True
        watch = machine._watch.get(entry.sid, {})
        return bool(watch.get("active_external_turns") or watch.get("desktop_active"))

    async def prepare(self, cmd):
        if not cmd.client_id or not cmd.cmd_id or not os.path.isabs(cmd.cwd) or "\0" in cmd.cwd:
            return await self._error(cmd, "目录或请求范围无效。")
        async with self._prepare_lock:
            now = time.monotonic()
            self.plans = {key: plan for key, plan in self.plans.items()
                          if plan.lock.locked() or plan.expires > now}
            if len(self.plans) >= 32:
                return await self._error(cmd, "目录操作过多，请稍后重试。")
            try:
                rows, profiles = await self._inventory(cmd)
                selected = [entry for entry in rows if cmd.action == "delete" or not entry.archived]
                external = await self._claude_external_busy(selected, cmd.cwd) if cmd.engine == "claude" else set()
                eligible = []
                for entry in selected:
                    if entry.sid not in external and not await self._busy(cmd.engine, entry):
                        eligible.append(entry)
                response = DirectoryActionPlan(request_id=cmd.cmd_id, plan_id=uuid.uuid4().hex,
                    engine=cmd.engine, cwd=cmd.cwd, action=cmd.action,
                    profiles=[profile.label for profile in profiles], total=len(selected),
                    eligible=len(eligible), archived=sum(entry.archived for entry in eligible),
                    skipped=len(selected) - len(eligible), expires_at=time.time() + PLAN_TTL, to=cmd.client_id)
                self.plans[response.plan_id] = Plan(cmd.client_id, response, tuple(eligible), time.monotonic() + PLAN_TTL)
                await self.machine.transport.send(response)
                return response
            except Exception as exc:
                log.warning("directory inventory failed", engine=cmd.engine, error_type=type(exc).__name__)
                return await self._error(cmd, "无法完整读取目录会话，请刷新后重试。")

    async def _still_in_scope(self, plan, entry):
        machine, scope = self.machine, plan.response
        profile = self._profiles(scope.engine, entry.profile_id)[0]
        record = await asyncio.to_thread(machine._work.for_engine(scope.engine).get_by_session,
            entry.native_sid, **{f"{scope.engine}_profile_id": profile.id})
        if record is not None or entry.sid in machine._private_btw_sessions:
            return False
        if scope.engine == "codex":
            rows = await asyncio.to_thread(codex_exact_catalog_rows, [entry.native_sid],
                                          codex_home=machine._codex_home(profile))
            if rows is None:
                raise ValueError("无法确认会话当前目录")
            if not rows:
                return False
            cwd, archived = rows[0].get("cwd"), rows[0].get("tag") == "archived"
        else:
            info = await asyncio.to_thread(machine._claude_catalog_session_info, profile, entry.native_sid)
            if info is None:
                return False
            cwd, archived = info.cwd, getattr(info, "tag", None) == "archived"
            if entry.sid in await self._claude_external_busy([entry], cwd):
                return False
        return (self._cwd(scope.engine, entry.sid, cwd) == scope.cwd
                and (scope.action == "delete" or not archived)
                and not await self._busy(scope.engine, entry))

    async def run(self, cmd):
        plan = self.plans.get(cmd.plan_id)
        if plan is None or plan.owner != cmd.client_id or not cmd.cmd_id:
            return await self._error(cmd, "目录操作确认已失效，请重新打开。")
        async with plan.lock:
            if plan.started:
                # A different retry id never replays partially applied native mutations.
                if plan.progress:
                    result = plan.progress.model_copy(update={"request_id": cmd.cmd_id})
                    await self.machine.transport.send(result)
                    return result
                return await self._error(cmd, "目录操作结果待确认，请刷新列表。")
            if time.monotonic() > plan.expires:
                return await self._error(cmd, "目录清单已过期，请重新确认。")
            plan.started = True
            plan.expires = time.monotonic() + 600
            scope = plan.response
            progress = DirectoryActionProgress(request_id=cmd.cmd_id, plan_id=cmd.plan_id,
                engine=scope.engine, total=scope.eligible, to=cmd.client_id)
            plan.progress = progress
            await self.machine.transport.send(progress.model_copy(deep=True))
            machine = self.machine
            try:
                with _defer_lists():
                    for index, entry in enumerate(plan.entries):
                        try:
                            async with machine._conversation_tree_mutation_lock(scope.engine, entry.sid), self._reserve(scope.engine, entry):
                                if not await self._still_in_scope(plan, entry):
                                    progress.skipped += 1
                                else:
                                    # Child errors are collected here, not broadcast as unrelated
                                    # chat banners. The native journals still get stable identities.
                                    fields = dict(session_id=entry.sid, engine=scope.engine,
                                        client_id=f"directory-{cmd.plan_id}", cmd_id=f"dir-{cmd.plan_id}-{index}")
                                    if scope.action == "archive":
                                        result = await machine._handle_archive_session_locked(ArchiveSession(archived=True, **fields))
                                    else:
                                        result = await machine._handle_delete_session_locked(DeleteSession(**fields))
                                    values = result if isinstance(result, tuple) else (result,)
                                    error = next((item for item in values if isinstance(item, Error)), None)
                                    if error and error.code == ERR_BUSY:
                                        progress.skipped += 1
                                    elif error:
                                        progress.failed += 1
                                        if len(progress.errors) < 5:
                                            progress.errors.append(error.message[:512])
                                    else:
                                        progress.succeeded += 1
                        except Exception:
                            progress.failed += 1
                            if len(progress.errors) < 5:
                                progress.errors.append("部分会话未完成操作，请刷新列表核查。")
                        await machine.transport.send(progress.model_copy(deep=True))
            except asyncio.CancelledError:
                progress.failed += progress.total - progress.succeeded - progress.skipped - progress.failed
                if len(progress.errors) < 5:
                    progress.errors.append("操作中断，尚未确认的结果请刷新列表核查。")
                raise
            finally:
                progress.done = True
                await machine.transport.send(progress.model_copy(deep=True))
                if scope.engine == "codex":
                    machine._invalidate_codex_session_catalog()
                    await machine._list_codex_sessions(cmd)
                else:
                    await machine._handle_list_sessions(ListSessions(engine=scope.engine,
                        client_id=cmd.client_id, cmd_id=cmd.cmd_id))
                await machine._invalidate_session_list(scope.engine, "code")
            return progress
