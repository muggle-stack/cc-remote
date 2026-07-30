from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import cc_remote.wrapper.machine as machine_module
import pytest
from cc_remote.protocol import (
    ArtifactInvalidated,
    DirList,
    ERR_AUTH,
    ERR_BUSY,
    ERR_INTERNAL,
    ERR_INVALID_CWD,
    MigrateSession,
    PROTOCOL_VERSION,
    SessionMigrated,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper.codex_handle import CodexHandle
from tests.test_codex_external import _RunTurnSdk
from tests.test_multisession import _mk_ctx, _mk_machine


class _FakeCodexHandle:
    def __init__(self, *, fail_cwd: str | None = None):
        self.fail_cwd = fail_cwd
        self.cwd: str | None = None
        self.calls: list[tuple[str, str]] = []

    async def set_cwd(
        self,
        cwd: str,
        *,
        reason: str = "thread cwd update",
    ) -> str:
        self.calls.append((cwd, reason))
        if cwd == self.fail_cwd:
            raise RuntimeError("cwd update rejected")
        self.cwd = cwd
        return cwd


def _command(cwd: str) -> MigrateSession:
    return MigrateSession(
        session_id="thread-1",
        cwd=cwd,
        request_id="migration-1",
        cmd_id="command-1",
        client_id="client-1",
    )


def _install_idle_codex(machine, cwd: Path, handle):
    ctx = _mk_ctx("thread-1", "thread-1")
    ctx.engine = "codex"
    ctx.space = "code"
    ctx.cwd = str(cwd)
    ctx.sdk = handle
    if isinstance(handle, _FakeCodexHandle):
        handle.cwd = str(cwd)
    machine.sessions = {"thread-1": ctx}
    machine.focused_sid = "thread-1"
    return ctx


def _stub_migration_dependencies(monkeypatch, machine):
    async def preflight(*_args, **_kwargs):
        return None

    async def list_sessions(_cmd):
        return None

    monkeypatch.setattr(machine, "_runtime_control_preflight", preflight)
    monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)


def test_session_migration_protocol_roundtrips_as_control_frames():
    assert PROTOCOL_VERSION == 27
    command = deserialize(serialize(_command("/tmp/new-cwd")))
    assert command.type == "migrate_session"
    assert command.session_id == "thread-1"
    assert command.cwd == "/tmp/new-cwd"

    event = deserialize(serialize(SessionMigrated(
        session_id="thread-1",
        previous_cwd="/tmp/old-cwd",
        cwd="/tmp/new-cwd",
        request_id="migration-1",
    )))
    assert event.type == "session_migrated"
    assert event.previous_cwd == "/tmp/old-cwd"
    assert event.cwd == "/tmp/new-cwd"
    assert is_downstream(event) is False

    invalidated = deserialize(serialize(ArtifactInvalidated(
        session_id="thread-1",
        reason="session_migration",
    )))
    assert invalidated.type == "artifact_invalidated"
    assert invalidated.reason == "session_migration"
    assert is_downstream(invalidated) is True


def test_directory_listing_echoes_its_command_id(tmp_path):
    async def run():
        (tmp_path / "child").mkdir()
        machine, transport = _mk_machine()
        command = SimpleNamespace(
            path=str(tmp_path),
            cmd_id="browse-command-1",
            client_id="client-1",
        )

        result = await machine._handle_list_dir(command)

        assert isinstance(result, DirList)
        assert result.request_id == "browse-command-1"
        assert result.to == "client-1"
        assert result.path == str(tmp_path)
        assert result in transport.sent

    asyncio.run(run())


def test_idle_codex_session_migrates_without_focus_or_queue_loss(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, transport = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        ctx.codex_checkpoint = False
        ctx.preview_write_candidates["tool-1"] = ("/tmp/outside",)
        ctx.preview_external_paths["/tmp/outside"] = None
        queued = object()
        ctx.queued_queries.append(queued)
        scheduled = []
        _stub_migration_dependencies(monkeypatch, machine)
        monkeypatch.setattr(
            machine,
            "_schedule_query_queue_drain",
            lambda target: scheduled.append(target),
        )

        result = await machine._command_router.dispatch(
            _command(str(new_cwd)))

        assert isinstance(result, SessionMigrated)
        assert result.session_id == "thread-1"
        assert result.previous_cwd == str(old_cwd)
        assert result.cwd == str(new_cwd)
        assert handle.calls == [(
            str(new_cwd), "session cwd migration")]
        assert ctx.cwd == str(new_cwd)
        assert ctx.needs_reload is False
        assert ctx.codex_checkpoint is None
        assert ctx.preview_write_candidates == {}
        assert ctx.preview_external_paths == {}
        assert ctx.queued_queries == [queued]
        assert (
            machine._codex_controls.get("thread-1").cwd_override
            == str(new_cwd)
        )
        assert scheduled == [ctx]
        assert machine.focused_sid == "thread-1"
        assert not any(message.type == "session_focus"
                       for message in transport.sent)
        invalidated = [
            message for message in transport.sent
            if isinstance(message, ArtifactInvalidated)
        ]
        assert len(invalidated) == 1
        assert invalidated[0].session_id == "thread-1"
        assert invalidated[0].reason == "session_migration"
        assert invalidated[0].sid == "thread-1"
        assert invalidated[0].seq == 1
        migrated = [
            message for message in transport.sent
            if message.type == "session_migrated"
        ]
        assert migrated == [result]
        assert migrated[0].sid == "thread-1"
        assert migrated[0].to is None
        assert transport.sent.index(invalidated[0]) < transport.sent.index(result)
        replay = ctx.buffer.replay_from(
            0,
            cc_session_id=ctx.session_id,
            state=ctx.state,
        )
        assert invalidated[0] in replay

    asyncio.run(run())


def test_loaded_shared_thread_uses_confirmed_migrated_cwd_on_next_turn(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        cfg = SimpleNamespace(
            cc_cwd=str(old_cwd),
            tool_result_max=8000,
        )
        handle = CodexHandle(cfg, cwd=str(old_cwd), daemon_mode="off")
        handle.proc = SimpleNamespace(returncode=None)
        handle.thread_id = "thread-1"
        handle._using_daemon_proxy = True
        # Simulate thread/resume rejoining a daemon-loaded thread and reporting
        # its old effective cwd despite receiving the new resume override.
        handle._apply_thread_settings({"cwd": str(old_cwd)})
        handle._reader = asyncio.create_task(asyncio.Event().wait())
        effective_cwd = str(old_cwd)
        requests = []
        turn_cwds = []

        async def request(method, params=None):
            nonlocal effective_cwd
            requests.append((method, params))
            if method == "thread/settings/update":
                effective_cwd = params["cwd"]
                await handle._dispatch({
                    "method": "thread/settings/updated",
                    "params": {
                        "threadId": "thread-1",
                        "threadSettings": {"cwd": effective_cwd},
                    },
                })
                return {}
            if method == "turn/start":
                turn_cwds.append(effective_cwd)
                return {"turn": {"id": "turn-after-migration"}}
            raise AssertionError(method)

        handle._request = request
        machine, _ = _mk_machine()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        ctx.codex_checkpoint = False
        _stub_migration_dependencies(monkeypatch, machine)

        try:
            result = await machine._handle_migrate_session(
                _command(str(new_cwd)))
            assert isinstance(result, SessionMigrated)
            assert handle.cwd == str(new_cwd)
            assert requests[0] == ("thread/settings/update", {
                "threadId": "thread-1",
                "cwd": str(new_cwd),
            })

            await handle.query("run in the migrated repository")

            assert turn_cwds == [str(new_cwd)]
            assert requests[-1][0] == "turn/start"
            assert "cwd" not in requests[-1][1]
        finally:
            handle._reader.cancel()
            await asyncio.gather(handle._reader, return_exceptions=True)
            handle._reader = None

    asyncio.run(run())


def test_shared_migration_rejects_active_private_codex_app(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, transport = _mk_machine()
        handle = _FakeCodexHandle()
        handle.shared_daemon_affinity = True
        handle.using_daemon_proxy = True
        ctx = _install_idle_codex(machine, old_cwd, handle)
        ctx.control_mode = "desktop"
        ctx.write_state = "read_only"
        ctx.control_reason = "Codex App 正在运行此会话"
        generation_checks = []
        ownership_probes = []

        async def ensure_generation(target, *, reason):
            generation_checks.append((target, reason))
            return True

        async def private_app_active(sid):
            ownership_probes.append(sid)
            return True

        monkeypatch.setattr(
            machine, "_ensure_codex_daemon_generation", ensure_generation)
        monkeypatch.setattr(
            machine, "_prime_codex_ownership", private_app_active)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_BUSY
        assert result.request_id == "migration-1"
        assert result.sid == "thread-1"
        assert result.to == "client-1"
        assert "Codex App" in result.message
        assert generation_checks == [(
            ctx,
            "runtime control preflight: 迁移工作目录",
        )]
        assert ownership_probes == ["thread-1"]
        assert handle.calls == []
        assert handle.cwd == str(old_cwd)
        assert ctx.cwd == str(old_cwd)
        assert machine._codex_controls.get(
            "thread-1").cwd_override is None
        assert result in transport.sent
        assert not any(
            isinstance(message, SessionMigrated)
            for message in transport.sent
        )

    asyncio.run(run())


def test_migration_preserves_native_reload_before_next_remote_turn(
    monkeypatch, tmp_path,
):
    class TurnSdk(_RunTurnSdk):
        def __init__(self):
            super().__init__()
            self.reconnect_options = []

        async def force_reconnect(self, **kwargs):
            self.reconnects += 1
            self.reconnect_options.append(kwargs)

    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        ctx.needs_reload = True
        ctx.codex_checkpoint = False
        _stub_migration_dependencies(monkeypatch, machine)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert isinstance(result, SessionMigrated)
        assert ctx.cwd == str(new_cwd)
        assert ctx.needs_reload is True

        turn_sdk = TurnSdk()
        ctx.sdk = turn_sdk
        ctx.state = "running"
        ctx.active_msg_id = "post-migration-turn"
        ctx.codex_checkpoint = False
        refreshed = []

        async def refresh(target):
            refreshed.append(target)

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_refresh_codex_collaboration_mode", refresh)
        monkeypatch.setattr(
            machine, "_prime_codex_ownership", no_external_owner)

        await machine._run_turn(ctx, "continue after migration")

        assert refreshed == [ctx]
        assert turn_sdk.reconnect_options == [{
            "resume_id": "thread-1",
            "cwd": str(new_cwd),
            "reason": "external transcript change",
        }]
        assert turn_sdk.queries == 1
        assert ctx.needs_reload is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_cwd_update_rejects_mismatched_authoritative_snapshot(tmp_path):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        handle = CodexHandle(
            SimpleNamespace(cc_cwd=str(old_cwd), tool_result_max=8000),
            cwd=str(old_cwd),
            daemon_mode="off",
        )
        handle.proc = SimpleNamespace(returncode=None)
        handle.thread_id = "thread-1"
        handle._apply_thread_settings({"cwd": str(old_cwd)})
        handle._reader = asyncio.create_task(asyncio.Event().wait())

        async def request(method, params=None):
            assert (method, params) == ("thread/settings/update", {
                "threadId": "thread-1",
                "cwd": str(new_cwd),
            })
            await handle._dispatch({
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "thread-1",
                    "threadSettings": {"cwd": str(old_cwd)},
                },
            })
            return {}

        handle._request = request
        try:
            with pytest.raises(
                RuntimeError,
                match="did not confirm the requested cwd",
            ):
                await handle.set_cwd(str(new_cwd))
            assert handle.cwd == str(old_cwd)
        finally:
            handle._reader.cancel()
            await asyncio.gather(handle._reader, return_exceptions=True)
            handle._reader = None

    asyncio.run(run())


def test_same_directory_migration_is_idempotent(monkeypatch, tmp_path):
    async def run():
        cwd = tmp_path / "repo"
        cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        result = await machine._handle_migrate_session(_command(str(cwd)))

        assert isinstance(result, SessionMigrated)
        assert result.previous_cwd == result.cwd == str(cwd)
        assert handle.calls == []
        assert ctx.cwd == str(cwd)
        assert not any(
            isinstance(message, ArtifactInvalidated)
            for message in machine.transport.sent
        )

    asyncio.run(run())


def test_cold_session_is_loaded_and_migrated_without_changing_focus(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        visible = _mk_ctx("visible", "visible")
        machine.sessions = {"visible": visible}
        machine.focused_sid = "visible"
        handle = _FakeCodexHandle()
        cold = _mk_ctx("thread-1", "thread-1")
        cold.engine = "codex"
        cold.space = "code"
        cold.cwd = str(old_cwd)
        cold.sdk = handle
        spawn_calls = []

        async def is_codex(_sid):
            return True

        async def spawn(**kwargs):
            spawn_calls.append(kwargs)
            machine.sessions["thread-1"] = cold
            return cold

        _stub_migration_dependencies(monkeypatch, machine)
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_spawn", spawn)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert isinstance(result, SessionMigrated)
        assert spawn_calls == [{
            "resume_id": "thread-1",
            "engine": "codex",
            "space": "code",
            "raise_on_failure": True,
        }]
        assert cold.cwd == str(new_cwd)
        assert handle.calls == [(
            str(new_cwd), "session cwd migration")]
        assert machine.focused_sid == "visible"

    asyncio.run(run())


def test_busy_session_rejects_migration_before_reconnect(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        ctx.state = "running"
        _stub_migration_dependencies(monkeypatch, machine)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_BUSY
        assert result.request_id == "migration-1"
        assert handle.calls == []
        assert ctx.cwd == str(old_cwd)

    asyncio.run(run())


def test_failed_migration_restores_original_directory(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle(fail_cwd=str(new_cwd))
        ctx = _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_INTERNAL
        assert result.request_id == "migration-1"
        assert handle.calls == [
            (str(new_cwd), "session cwd migration"),
            (str(old_cwd), "session cwd migration rollback"),
        ]
        assert ctx.cwd == str(old_cwd)
        assert ctx.needs_reload is False

    asyncio.run(run())


def test_migration_rolls_back_when_cwd_override_cannot_be_persisted(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        def fail_persist(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(
            machine._codex_controls, "set_cwd_override", fail_persist)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_INTERNAL
        assert handle.calls == [
            (str(new_cwd), "session cwd migration"),
            (
                str(old_cwd),
                "session cwd migration persistence rollback",
            ),
        ]
        assert ctx.cwd == str(old_cwd)
        assert ctx.needs_reload is False

    asyncio.run(run())


def test_migration_restores_override_after_a_post_replace_failure(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)
        store = machine._codex_controls
        real_persist = store._persist
        injected = False

        def persist_then_fail(sessions):
            nonlocal injected
            real_persist(sessions)
            cwd = sessions.get("thread-1", {}).get("cwd_override")
            if not injected and cwd == str(new_cwd):
                injected = True
                raise OSError("directory fsync failed after replace")

        monkeypatch.setattr(store, "_persist", persist_then_fail)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_INTERNAL
        assert "已恢复原工作目录" in result.message
        assert handle.calls == [
            (str(new_cwd), "session cwd migration"),
            (
                str(old_cwd),
                "session cwd migration persistence rollback",
            ),
        ]
        assert ctx.cwd == str(old_cwd)
        assert ctx.needs_reload is False
        assert store.get("thread-1").cwd_override is None
        reloaded = type(store)(store.path.parent)
        assert reloaded.get("thread-1").cwd_override is None

    asyncio.run(run())


def test_migration_does_not_claim_rollback_when_override_cleanup_fails(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        ctx = _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        def fail_persist(*_args, **_kwargs):
            raise OSError("persist failed")

        def fail_cleanup(*_args, **_kwargs):
            raise OSError("cleanup failed")

        monkeypatch.setattr(
            machine._codex_controls, "set_cwd_override", fail_persist)
        monkeypatch.setattr(
            machine._codex_controls,
            "restore_cwd_override_after_failed_set",
            fail_cleanup,
        )

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_INTERNAL
        assert "原目录状态未完全恢复" in result.message
        assert "已恢复原工作目录" not in result.message
        assert handle.cwd == str(old_cwd)
        assert ctx.needs_reload is True

    asyncio.run(run())


def test_migration_rejects_profile_disallowed_in_target_directory(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        new_cwd = tmp_path / "new"
        old_cwd.mkdir()
        new_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        handle.permission_profile = ":workspace"
        ctx = _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        async def profiles(cwd):
            assert cwd == str(new_cwd)
            return [{
                "id": ":workspace",
                "description": "Workspace",
                "allowed": False,
            }]

        monkeypatch.setattr(
            machine_module, "codex_permission_profiles", profiles)

        result = await machine._handle_migrate_session(
            _command(str(new_cwd)))

        assert result.code == ERR_AUTH
        assert handle.calls == []
        assert ctx.cwd == str(old_cwd)
        assert machine._codex_controls.get("thread-1").cwd_override is None

    asyncio.run(run())


def test_codex_session_list_overlays_durable_migrated_cwd(
    monkeypatch, tmp_path,
):
    async def run():
        migrated = tmp_path / "migrated"
        migrated.mkdir()
        machine, _ = _mk_machine()
        machine._codex_controls.set_cwd_override(
            "thread-1", str(migrated))
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", lambda _raw: None)

        event = await machine._send_codex_session_list(_command(str(migrated)), [{
            "session_id": "thread-1",
            "summary": "Migrated",
            "first_prompt": None,
            "cwd": "/native/original",
            "last_modified": "1",
            "git_branch": None,
            "tag": None,
            "status": "idle",
            "forked_from_id": None,
        }])

        assert event.sessions[0].cwd == str(migrated)

    asyncio.run(run())


def test_codex_session_list_discards_a_missing_cwd_override(
    monkeypatch, tmp_path,
):
    async def run():
        native = tmp_path / "native"
        native.mkdir()
        missing = tmp_path / "deleted-migration"
        machine, _ = _mk_machine()
        machine._codex_controls.set_cwd_override(
            "thread-1", str(missing))
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", lambda _raw: None)

        event = await machine._send_codex_session_list(
            SimpleNamespace(space="code", client_id="client-1"),
            [{
                "session_id": "thread-1",
                "summary": "Migrated",
                "first_prompt": None,
                "cwd": str(native),
                "last_modified": "1",
                "git_branch": None,
                "tag": None,
                "status": "idle",
                "forked_from_id": None,
            }],
        )

        assert event.sessions[0].cwd == str(native)
        assert machine._codex_controls.get(
            "thread-1").cwd_override is None

    asyncio.run(run())


def test_restart_clears_missing_override_before_runtime_and_sidebar_resume(
    monkeypatch, tmp_path,
):
    class FakeCodexHandle:
        resumed_cwd = None

        def __init__(
            self,
            _cfg,
            cwd=None,
            daemon_mode=None,
            daemon_manager=None,
        ):
            self.cwd = cwd
            self.daemon_mode = daemon_mode
            self.daemon_manager = daemon_manager
            self.thread_id = None
            self.proc = SimpleNamespace(returncode=None)
            self.model = "gpt-test"
            self.effort = "high"
            self.applied_effort = "high"
            self._approval = "never"
            self.approval_policy = "never"
            self.permission_profile = None
            self.web_search = "cached"
            self.web_search_override = None
            self.collaboration_mode = "default"
            self.service_tier = None
            self.shared_daemon_affinity = False
            self.using_daemon_proxy = False
            self.last_thread_status = None
            self.connect_calls = []
            self.cwd_calls = []

        @property
        def approval(self):
            return self._approval

        @approval.setter
        def approval(self, value):
            self._approval = value
            self.approval_policy = value

        async def connect(self, **kwargs):
            self.connect_calls.append(kwargs)
            self.thread_id = kwargs["resume_id"]
            self.cwd = self.resumed_cwd or kwargs["cwd"]

        async def set_cwd(self, cwd, *, reason="thread cwd update"):
            self.cwd_calls.append((cwd, reason))
            self.cwd = cwd
            return cwd

        async def disconnect(self):
            self.proc = None

    async def run():
        thread_id = "thread-missing-migrated-cwd"
        native_cwd = tmp_path / "native"
        default_cwd = tmp_path / "default"
        missing_cwd = tmp_path / "deleted-migration"
        native_cwd.mkdir()
        default_cwd.mkdir()
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(default_cwd)
        machine._codex_controls.set_cwd_override(
            thread_id, str(missing_cwd))
        FakeCodexHandle.resumed_cwd = str(missing_cwd)

        monkeypatch.setattr(machine_module, "CodexHandle", FakeCodexHandle)
        monkeypatch.setattr(
            machine_module,
            "codex_session_cwd",
            lambda _thread_id: str(native_cwd),
        )
        monkeypatch.setattr(
            machine_module,
            "codex_session_settings",
            lambda *_args: {},
        )
        monkeypatch.setattr(
            machine, "_watch_session", lambda _sid: None)
        monkeypatch.setattr(
            machine,
            "_prime_codex_ownership",
            lambda _sid: asyncio.sleep(0, result=False),
        )
        monkeypatch.setattr(
            machine,
            "_load_history",
            lambda *_args: asyncio.sleep(0),
        )
        monkeypatch.setattr(
            machine, "_prime_codex_sidebar_watches", lambda _raw: None)

        ctx = await machine._spawn(
            resume_id=thread_id,
            engine="codex",
            space="code",
        )

        assert ctx is not None
        assert ctx.cwd == str(native_cwd)
        assert ctx.sdk.connect_calls == [{
            "resume_id": thread_id,
            "cwd": str(native_cwd),
            "preserve_controls": False,
        }]
        assert ctx.sdk.cwd_calls == [(
            str(native_cwd),
            "resume cwd reconciliation",
        )]
        assert machine._codex_controls.get(thread_id).cwd_override is None

        event = await machine._send_codex_session_list(
            SimpleNamespace(space="code", client_id="client-1"),
            [{
                "session_id": thread_id,
                "summary": "Migrated",
                "first_prompt": None,
                "cwd": str(native_cwd),
                "last_modified": "1",
                "git_branch": None,
                "tag": None,
                "status": "idle",
                "forked_from_id": None,
            }],
        )

        assert event.sessions[0].cwd == ctx.cwd == str(native_cwd)

    asyncio.run(run())


def test_migration_rejects_relative_or_missing_directory(
    monkeypatch, tmp_path,
):
    async def run():
        old_cwd = tmp_path / "old"
        old_cwd.mkdir()
        machine, _ = _mk_machine()
        handle = _FakeCodexHandle()
        _install_idle_codex(machine, old_cwd, handle)
        _stub_migration_dependencies(monkeypatch, machine)

        relative = await machine._handle_migrate_session(
            _command("relative/path"))
        missing = await machine._handle_migrate_session(
            _command(str(tmp_path / "missing")))

        assert relative.code == ERR_INVALID_CWD
        assert missing.code == ERR_INVALID_CWD
        assert handle.calls == []

    asyncio.run(run())
