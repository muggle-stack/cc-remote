"""Work native permissions across live changes, cold resume and failed writes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import Error, Perm, PermissionProfile, SetPerm, SetPermissionProfile
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_codex_controls import _control_ctx
from tests.test_multisession import _mk_machine


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "auto", "bypassPermissions"])
def test_claude_work_retains_native_permission_switching_and_isolated_context(mode):
    machine, _ = _mk_machine()
    handle = SdkHandle(machine.cfg)
    handle.work_mode = True
    handle.work_settings_path = "/tmp/work-policy.json"
    handle.permission_mode = mode
    options = handle._options(None, "/tmp/work")
    assert options.permission_mode == mode
    assert options.setting_sources == []
    assert options.settings == "/tmp/work-policy.json"
    assert "safe-mode" in options.extra_args
    if mode != "bypassPermissions":
        assert "allow-dangerously-skip-permissions" in options.extra_args


def test_codex_work_permission_changes_follow_native_ack_and_keep_failed_selection():
    async def run():
        machine, _ = _mk_machine()
        handle = CodexHandle(machine.cfg, work_mode=True)
        handle.thread_id = "work-thread"
        handle._cwd = "/tmp/work"
        calls = []
        fail = False

        async def request(method, params=None):
            calls.append((method, params))
            if method == "permissionProfile/list":
                return {"data": [
                    {"id": "cc_remote_work", "allowed": True},
                    {"id": ":danger-full-access", "allowed": True},
                    {"id": ":forbidden", "allowed": False},
                ]}
            if fail:
                raise RuntimeError("native rejection")
            return {}

        handle._request = request
        await handle.set_permission_mode("on-request")
        assert handle.approval_policy == "on-request"
        await handle.set_permission_profile(":danger-full-access")
        assert handle.permission_profile == ":danger-full-access"
        before = len(calls)
        with pytest.raises(ValueError, match="unavailable"):
            await handle.set_permission_profile(":forbidden")
        assert len(calls) == before + 1  # catalog only, no settings mutation
        fail = True
        with pytest.raises(RuntimeError, match="native rejection"):
            await handle.set_permission_mode("never")
        assert handle.approval_policy == "on-request"
        assert handle.permission_profile == ":danger-full-access"

    asyncio.run(run())


def test_work_failed_permission_controls_do_not_publish_or_persist_success():
    async def run():
        machine, transport = _mk_machine()
        ctx = _control_ctx("work-thread", "codex")
        ctx.space = "work"
        machine.sessions[ctx.key] = ctx
        ctx.sdk.fail_perm = True
        ctx.sdk.fail_profile = True
        for command, handler in [
            (SetPerm(sid=ctx.key, mode="on-request"), machine._handle_set_perm),
            (SetPermissionProfile(sid=ctx.key, profile=":danger-full-access"),
             machine._handle_set_permission_profile),
        ]:
            assert isinstance(await handler(command), Error)
        assert not any(isinstance(event, (Perm, PermissionProfile)) for event in transport.sent)
        assert machine._codex_controls.get(ctx.key).permission_profile is None
        assert ctx.sdk.permission_profile == ":workspace"

    asyncio.run(run())


@pytest.mark.parametrize("selected", [False, True])
def test_codex_work_creation_and_cold_resume_keep_permission_choices(monkeypatch, tmp_path, selected):
    class Handle:
        def __init__(self, cfg, **kwargs):
            self.thread_id = None
            self._cwd = kwargs["cwd"]
            self.work_mode = kwargs["work_mode"]
            self.model = "test-model"
            self.effort = self.applied_effort = "high"
            self._approval = "never"
            self.approval_policy = "never"
            self.permission_profile = "cc_remote_work"
            self.web_search = "cached"
            self.web_search_override = None
            self.collaboration_mode = "default"
            self.service_tier = None
            self.proc = SimpleNamespace(returncode=None)
            self.using_daemon_proxy = self.shared_daemon_affinity = False
            self.connect_calls = []

        @property
        def approval(self):
            return self._approval

        @approval.setter
        def approval(self, value):
            self._approval = self.approval_policy = value

        async def connect(self, **kwargs):
            self.connect_calls.append((kwargs, self.approval, self.permission_profile, self.web_search))
            self.thread_id = kwargs.get("resume_id") or "work-permission-thread"

        async def disconnect(self):
            self.proc = None

    async def run():
        machine, transport = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        monkeypatch.setattr(machine_module, "CodexHandle", Handle)
        monkeypatch.setattr(machine_module, "clamp_effort", AsyncMock(return_value="high"))
        monkeypatch.setattr(machine_module, "codex_catalog", AsyncMock(return_value=[]))
        catalog = AsyncMock(return_value=[{"id": ":danger-full-access", "allowed": True}])
        monkeypatch.setattr(machine_module, "codex_permission_profiles", catalog)
        monkeypatch.setattr(machine_module, "codex_session_settings", lambda *a, **k: {})
        machine._watch_session = AsyncMock()
        machine._prime_codex_ownership = AsyncMock(return_value=False)
        machine._load_history = AsyncMock()
        machine._resolve_codex_session_effort = AsyncMock()
        machine._stamp_codex_daemon_epoch = AsyncMock()
        store = machine._work.for_engine("codex")
        profile = machine._codex_profiles.default.id
        record = store.create_session(codex_profile_id=profile)
        monkeypatch.setattr(machine_module, "codex_session_cwd", lambda *a, **k: record.cwd)
        choices = dict(permission_mode="on-request", permission_profile=":danger-full-access", web_search="live") if selected else {}
        expected = ("on-request", ":danger-full-access", "live") if selected else ("never", "cc_remote_work", "cached")
        ctx = await machine._spawn(resume_id=None, cwd=record.cwd, engine="codex", space="work", work_id=record.work_id, **choices)
        assert ctx is not None, transport.sent
        assert ctx.sdk.connect_calls[0][1:] == expected
        assert ctx.cwd == record.cwd
        sid = ctx.key
        await machine._persist_codex_session_controls(ctx)
        assert machine._codex_controls.get(sid).permission_profile == expected[1]
        machine.sessions.pop(sid)
        machine.focused_sid = None
        resumed = await machine._spawn(resume_id=ctx.session_id, engine="codex", space="work", work_id=record.work_id)
        assert resumed is not None, transport.sent
        assert resumed.sdk.connect_calls[0][1:] == expected
        assert resumed.sdk.connect_calls[0][0]["preserve_controls"] is True
        assert resumed.sdk.connect_calls[0][0].get("preserve_permission_profile", True) is True
        assert resumed.cwd == record.cwd
        if selected:
            assert all(call.args[0] == record.cwd for call in catalog.call_args_list)
        else:
            catalog.assert_not_awaited()

    asyncio.run(run())
