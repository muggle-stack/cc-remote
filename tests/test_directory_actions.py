"""Directory confirmation freezes identities, not a truncated browser list."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.protocol import PrepareDirectoryAction, RunDirectoryAction, Error, ERR_BUSY
from cc_remote.wrapper import directory_actions as module


class Machine:
    def __init__(self, tmp_path):
        self.rows = {"primary": {}, "second": {}}
        self._codex_profiles = self._claude_profiles = [
            SimpleNamespace(id=key, label=key, home=tmp_path / key, config_dir=tmp_path / key)
            for key in self.rows]
        self._codex_profile_migration_ok = self._claude_profile_migration_ok = True
        self._codex_controls = SimpleNamespace(cwd_overrides=lambda: self.overrides)
        self.overrides = {}
        self._private_btw_sessions = set()
        self.work = {}
        self._work = SimpleNamespace(for_engine=lambda _: SimpleNamespace(
            records_by_profile_session=lambda: self.work,
            get_by_session=lambda sid, **kwargs: self.work.get((next(iter(kwargs.values())), sid))))
        self._watch = {}
        self.contexts = {}
        self.external = set()
        self.calls = []
        self.sent = []
        self.transport = SimpleNamespace(send=self.send)
        self.failures = {}
        self.after_action = None
        self.locks = {}
        self.refreshes = 0
        self.invalidations = []
        self.focused_sid = None

    def _resolve_session_alias(self, sid):
        return sid

    async def send(self, value):
        self.sent.append(value)

    def add(self, sid, *, profile="primary", cwd="/project", archived=False):
        self.rows[profile][sid] = {"session_id": sid, "cwd": cwd, "tag": "archived" if archived else None}
        return f"{profile}@{sid}"

    def _codex_home(self, profile):
        return str(profile.home)

    def _codex_target(self, sid):
        profile, native = sid.split("@")
        return next(value for value in self._codex_profiles if value.id == profile), native

    _claude_target = _codex_target

    @staticmethod
    def _codex_wire_sid(profile, sid):
        return f"{profile.id}@{sid}"

    _claude_wire_sid = _codex_wire_sid

    def _filter_private_codex_btw_rows(self, rows):
        return [row for row in rows if row["session_id"] not in self._private_btw_sessions]

    def _claude_catalog_list_sessions(self, profile, **kwargs):
        assert kwargs["include_worktrees"] is False
        return [SimpleNamespace(**row) for row in self.rows[profile.id].values()
                if row["cwd"] == kwargs["directory"]][:kwargs["limit"]]

    def _claude_catalog_session_info(self, profile, sid):
        row = self.rows[profile.id].get(sid)
        return SimpleNamespace(**row) if row else None

    def _bg_blocked_session_ids(self, _):
        return set()

    def _ctx_by_sid(self, sid):
        return self.contexts.get(sid)

    def _session_delete_busy(self, ctx):
        return ctx.busy

    def _claude_has_background_work(self, ctx):
        return ctx.background

    def _is_external(self, sid):
        return sid in self.external

    def _conversation_tree_mutation_lock(self, engine, sid):
        return self.locks.setdefault((engine, sid.split("@")[0]), asyncio.Lock())

    async def _handle_archive_session_locked(self, cmd):
        return await self.mutate(cmd, "archive")

    async def _handle_delete_session_locked(self, cmd):
        return await self.mutate(cmd, "delete")

    async def mutate(self, cmd, action):
        assert module.defer_session_lists()
        self.calls.append((action, cmd.session_id))
        if cmd.session_id in self.failures:
            return Error(code=self.failures[cmd.session_id], message="not applied")
        profile, sid = cmd.session_id.split("@")
        if action == "delete":
            del self.rows[profile][sid]
        else:
            self.rows[profile][sid]["tag"] = "archived"
        if self.after_action:
            self.after_action()

    def _invalidate_codex_session_catalog(self):
        pass

    async def _list_codex_sessions(self, cmd):
        assert not module.defer_session_lists()
        self.refreshes += 1

    _handle_list_sessions = _list_codex_sessions

    async def _invalidate_session_list(self, engine, space):
        assert not module.defer_session_lists()
        self.invalidations.append((engine, space))


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    machine = Machine(tmp_path)

    async def listing(cwd, *, codex_home, limit):
        return [dict(row) for row in machine.rows[Path(codex_home).name].values() if row["cwd"] == cwd]

    def exact(ids, *, codex_home):
        return [dict(machine.rows[Path(codex_home).name][sid]) for sid in ids
                if sid in machine.rows[Path(codex_home).name]]

    monkeypatch.setattr(module, "list_codex_directory_sessions", listing)
    monkeypatch.setattr(module, "codex_exact_catalog_rows", exact)
    async def no_external(*args):
        return set()
    monkeypatch.setattr(module.DirectoryActions, "_claude_external_busy", no_external)
    return machine, module.DirectoryActions(machine)


def prepare(**kwargs):
    return PrepareDirectoryAction(client_id="browser", cmd_id="prepare", engine="codex",
                                  cwd="/project", action="delete", **kwargs)


def run(plan, **kwargs):
    return RunDirectoryAction(client_id="browser", cmd_id="run", plan_id=plan.plan_id, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_complete_directory_account_scope_and_private_work_exclusion(fixture, engine):
    machine, manager = fixture
    for index in range(220):
        machine.add(f"thread-{index}")
    machine.add("old", archived=True)
    machine.add("thread-0", profile="second")
    machine.add("outside", cwd="/other")
    private = machine.add("btw")
    machine._private_btw_sessions.add(private)
    machine.work[("primary", "work")] = object()
    machine.add("work")
    cmd = prepare(profile_id="primary").model_copy(update={"engine": engine})
    plan = await manager.prepare(cmd)
    assert plan.total == plan.eligible == 221 and plan.archived == 1
    result = await manager.run(run(plan))
    assert result.done and result.succeeded == 221 and result.failed == 0
    assert machine.refreshes == 1
    assert machine.invalidations == [(engine, "code")]
    assert set(machine.rows["primary"]) == {"outside", "btw", "work"}
    assert set(machine.rows["second"]) == {"thread-0"}


@pytest.mark.asyncio
async def test_snapshot_excludes_new_moved_and_newly_busy_sessions(fixture):
    machine, manager = fixture
    machine.add("first")
    machine.add("moved")
    busy = machine.add("busy")
    plan = await manager.prepare(prepare())
    machine.rows["primary"]["moved"]["cwd"] = "/other"
    machine.external.add(busy)
    machine.add("new-after-confirmation")
    result = await manager.run(run(plan))
    assert (result.succeeded, result.skipped, result.failed) == (1, 2, 0)
    assert machine.calls == [("delete", "primary@first")]
    assert "new-after-confirmation" in machine.rows["primary"]
    # Repeated confirmation with a new command id reports the original result.
    retry = await manager.run(run(plan).model_copy(update={"cmd_id": "retry"}))
    assert retry.succeeded == 1 and retry.request_id == "retry" and len(machine.calls) == 1


@pytest.mark.asyncio
async def test_running_queued_and_background_sessions_are_retained(fixture):
    machine, manager = fixture
    for sid, busy, background in [("working", True, False), ("background", False, True)]:
        wire = machine.add(sid)
        machine.contexts[wire] = SimpleNamespace(busy=busy, background=background,
            codex_background_processes={"task": object()} if background else {}, btw=False, cwd="/project")
    machine.add("idle")
    machine.add("archived", archived=True)
    plan = await manager.prepare(prepare().model_copy(update={"action": "archive"}))
    assert plan.eligible == 1 and plan.skipped == 2 and plan.total == 3
    result = await manager.run(run(plan))
    assert result.succeeded == 1 and machine.calls == [("archive", "primary@idle")]


@pytest.mark.asyncio
async def test_profile_collision_migration_override_and_partial_failure(fixture):
    machine, manager = fixture
    moved = machine.add("same", cwd="/old")
    machine.overrides[moved] = "/project"
    machine.add("same", profile="second")
    machine.add("running-now")
    machine.failures["primary@running-now"] = ERR_BUSY
    machine.failures["second@same"] = "internal"
    plan = await manager.prepare(prepare())
    assert plan.total == 3
    result = await manager.run(run(plan))
    assert (result.succeeded, result.skipped, result.failed) == (1, 1, 1)
    assert result.errors == ["not applied"]


@pytest.mark.asyncio
async def test_owner_expiry_and_failed_inventory_never_mutate(fixture, monkeypatch):
    machine, manager = fixture
    machine.add("first")
    plan = await manager.prepare(prepare())
    assert isinstance(await manager.run(run(plan).model_copy(update={"client_id": "other"})), Error)
    manager.plans[plan.plan_id].expires = 0
    assert isinstance(await manager.run(run(plan)), Error)
    assert isinstance(await module.DirectoryActions(machine).run(run(plan)), Error)
    async def unavailable(*a, **k):
        raise RuntimeError("partial catalog")
    monkeypatch.setattr(module, "list_codex_directory_sessions", unavailable)
    assert isinstance(await manager.prepare(prepare()), Error)
    assert machine.calls == []


@pytest.mark.asyncio
async def test_claude_reservation_blocks_new_resume_and_holds_query_and_queue(fixture):
    machine, manager = fixture
    sid = machine.add("idle")
    ctx = SimpleNamespace(query_lock=asyncio.Lock(), queued_query_lock=asyncio.Lock())
    machine.contexts[sid] = ctx
    entry = module.Entry(sid, "idle", "primary", False)
    async with manager._reserve("claude", entry):
        assert ctx.query_lock.locked() and ctx.queued_query_lock.locked()
        command = SimpleNamespace(type="switch_session", session_id=sid, cmd_id="switch", client_id="browser")
        error = await manager.reject_conflict(command)
        assert error.code == ERR_BUSY and error.request_id == "switch"
        command.session_id = "primary@another"
        assert await manager.reject_conflict(command) is None
    assert not ctx.query_lock.locked() and not ctx.queued_query_lock.locked() and not manager.mutating


@pytest.mark.asyncio
async def test_inflight_session_command_is_skipped_before_native_mutation(fixture):
    machine, manager = fixture
    sid = machine.add("idle")
    machine._command_scheduler = SimpleNamespace(owns_target=lambda target: target == sid)
    plan = await manager.prepare(prepare())
    assert plan.skipped == 1 and plan.eligible == 0
    assert (await manager.run(run(plan))).succeeded == 0
    assert machine.calls == []


@pytest.mark.asyncio
async def test_claude_native_holder_scan_is_account_scoped_and_fails_closed(tmp_path, monkeypatch):
    machine = Machine(tmp_path)
    machine.sessions = {}
    machine._claude_continue_bindings = {}
    machine._claude_continue_candidates = {}
    machine._latest_claude_session_for_cwd = lambda *args: None
    machine._claude_transcript_path = lambda profile, sid: profile.config_dir / f"{sid}.jsonl"
    observed = {}
    def scan(paths, cwds, **kwargs):
        observed.update(kwargs)
        assert set(paths) == {"primary@same", "second@same"}
        return SimpleNamespace(complete=False, holders={})
    monkeypatch.setattr(module, "claude_session_holders", scan)
    entries = [module.Entry(f"{profile}@same", "same", profile, False) for profile in machine.rows]
    busy = await module.DirectoryActions(machine)._claude_external_busy(entries, "/project")
    assert busy == {entry.sid for entry in entries}
    assert observed["native_session_ids"] == {entry.sid: "same" for entry in entries}
    assert len(set(observed["config_dirs"].values())) == 2
