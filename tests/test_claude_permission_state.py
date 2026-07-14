"""Authoritative Claude permission state across reconnect and runtime creation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import GetModels, Hello, NewSession, OpenBtw
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


class _FakeClaudeClient:
    created = []

    def __init__(self, options):
        self.options = options
        self._query = self
        self.fail_permission = False
        self.disconnected = False
        self.permission_calls = []
        self.model_calls = []
        self.created.append(self)

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True

    async def get_context_usage(self):
        return {"model": self.options.model or "claude-mythos-5"}

    async def _send_control_request(self, request, timeout):
        assert request == {"subtype": "get_context_usage"}
        assert timeout == 5.0
        return await self.get_context_usage()

    async def set_model(self, model):
        self.model_calls.append(model)

    async def set_permission_mode(self, mode):
        self.permission_calls.append(mode)
        if self.fail_permission:
            raise RuntimeError("runtime permission rejected")

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - make this an async generator
            yield None


def test_claude_control_state_survives_sdk_reconnect_and_failed_set(
    monkeypatch,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        assert handle.model == "claude-mythos-5"

        await handle.set_model("claude-opus-4-8")
        assert handle.model == "claude-opus-4-8"

        await handle.set_permission_mode("plan")
        assert handle.permission_mode == "plan"
        first = _FakeClaudeClient.created[-1]
        first.fail_permission = True
        with pytest.raises(RuntimeError, match="runtime permission rejected"):
            await handle.set_permission_mode("acceptEdits")
        assert handle.permission_mode == "plan"

        await handle.force_reconnect(None, "/tmp", reason="test reconnect")
        assert [client.options.permission_mode
                for client in _FakeClaudeClient.created] == [
                    "bypassPermissions", "plan"]
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8"]

        # A terminal-owned append may also change the session model. External
        # reload must let resume recover that value instead of forcing our cache.
        await handle.force_reconnect(
            None, "/tmp", reason="external transcript change",
            preserve_model=False)
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8", None]
        assert handle.model == "claude-mythos-5"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_model_probe_failure_does_not_fail_connect(monkeypatch):
    class ProbeUnavailable(_FakeClaudeClient):
        async def _send_control_request(self, request, timeout):
            assert timeout == 5.0
            raise RuntimeError("control request unavailable")

    async def go():
        ProbeUnavailable.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", ProbeUnavailable)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        assert handle.client is not None
        assert handle.model is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_new_session_defaults_use_settings_without_sdk_probe(
    monkeypatch, tmp_path,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".git").mkdir(parents=True)
    (project / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        '{"model":"claude-haiku-4-5"}')
    (project / ".claude" / "settings.json").write_text(
        '{"model":"claude-sonnet-5"}')
    (project / ".claude" / "settings.local.json").write_text(
        '{"model":"claude-mythos-5[1m]"}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(
        machine_module.WrapperMachine, "_claude_managed_settings_paths",
        staticmethod(lambda: []))

    class ForbiddenProbe:
        def __init__(self, _cfg):
            raise AssertionError("default display must not start Claude CLI")

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", ForbiddenProbe)
        machine, transport = _mk_machine()
        command = GetModels(
            engine="claude", cwd=str(project / "subdir"),
            client_id="client-1")
        (project / "subdir").mkdir()

        await machine._handle_get_models(command)

        assert len(transport.sent) == 1
        assert all(event.models == [] for event in transport.sent)
        assert all(event.default_model == "claude-mythos-5[1m]"
                   for event in transport.sent)
        assert all(event.default_effort == "max"
                   for event in transport.sent)
        assert all(event.cwd == str(project / "subdir")
                   for event in transport.sent)
        assert all(event.to == "client-1" for event in transport.sent)

        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-model")
        assert machine._claude_configured_model(
            str(project)) == "claude-env-model"
        monkeypatch.setenv("ANTHROPIC_MODEL", "default")
        (project / ".claude" / "settings.local.json").write_text(
            '{"model":"default"}')
        assert machine._claude_configured_model(str(project)) is None

        managed = tmp_path / "managed-settings.json"
        managed.write_text('{"model":"claude-managed-model"}')
        monkeypatch.setattr(
            machine_module.WrapperMachine, "_claude_managed_settings_paths",
            staticmethod(lambda: [str(managed)]))
        assert machine._claude_configured_model(
            str(project)) == "claude-managed-model"

    asyncio.run(go())


def test_claude_default_resolution_does_not_block_serial_commands():
    async def go():
        machine, _ = _mk_machine()
        probe_started = asyncio.Event()
        release_probe = asyncio.Event()
        mutation_seen = asyncio.Event()
        probe_calls = 0

        async def process(command):
            nonlocal probe_calls
            if command.type == "get_models":
                probe_calls += 1
                probe_started.set()
                await release_probe.wait()
            else:
                mutation_seen.set()

        machine._process_command = process
        probe = SimpleNamespace(
            type="get_models", client_id="client-1", cmd_id="models-1")
        machine._start_models_command(probe)
        await asyncio.wait_for(probe_started.wait(), timeout=1)

        # A reliable retry coalesces while the original read is still running.
        machine._start_models_command(probe)
        await machine._process_command_safely(SimpleNamespace(
            type="new_session"))
        assert mutation_seen.is_set()
        assert probe_calls == 1

        release_probe.set()
        await asyncio.gather(*machine._models_command_tasks.values())

    asyncio.run(go())


def test_new_claude_session_emits_authoritative_permission():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-new", None)
        ctx.sdk = SimpleNamespace(
            permission_mode="acceptEdits", model="claude-mythos-5",
            effort="max")

        async def spawn(**_kwargs):
            machine.sessions[ctx.key] = ctx
            return ctx

        machine._spawn = spawn
        await machine._handle_new_session(NewSession(request_id="new-1"))

        perms = [event for event in transport.sent if event.type == "perm"]
        assert len(perms) == 1
        assert perms[0].sid == "tmp-new"
        assert perms[0].mode == "acceptEdits"
        assert ctx.announced_perm == "acceptEdits"
        assert [event.model for event in transport.sent
                if event.type == "model"] == ["claude-mythos-5"]
        assert [event.effort for event in transport.sent
                if event.type == "effort"] == ["max"]

    asyncio.run(go())


def test_client_hello_reseeds_claude_permission_authoritatively():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-1", "claude-1")
        ctx.sdk = SimpleNamespace(
            permission_mode="plan", model="claude-opus-4-8", effort="max")
        machine.sessions[ctx.key] = ctx

        await machine._handle_client_hello(Hello(
            role="client", client_id="client-1", route_id="route-1",
            cursors={"claude-1": 999},
            generations={"claude-1": machine.instance_id}))

        perms = [event for event in transport.sent if event.type == "perm"]
        assert len(perms) == 1
        assert perms[0].mode == "plan"
        assert perms[0].sid == "claude-1"
        assert perms[0].to == "client-1"
        assert perms[0].route_id == "route-1"
        models = [event for event in transport.sent if event.type == "model"]
        efforts = [event for event in transport.sent if event.type == "effort"]
        assert [(event.model, event.sid, event.to, event.route_id)
                for event in models] == [
                    ("claude-opus-4-8", "claude-1", "client-1", "route-1")]
        assert [(event.effort, event.sid, event.to, event.route_id)
                for event in efforts] == [
                    ("max", "claude-1", "client-1", "route-1")]

    asyncio.run(go())


def test_switching_to_resident_claude_reseeds_its_actual_permission():
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-1", "claude-1")
        ctx.sdk = SimpleNamespace(
            permission_mode="default", model="claude-sonnet-5", effort="high")
        machine.sessions[ctx.key] = ctx

        result = await machine._handle_switch_session(SimpleNamespace(
            session_id="claude-1", engine="claude"))

        assert [event.type for event in result] == [
            "session_focus", "perm", "model", "effort"]
        assert result[1].mode == "default"
        assert result[2].model == "claude-sonnet-5"
        assert result[3].effort == "high"

    asyncio.run(go())


def test_claude_btw_inherits_parent_permission_before_connect(monkeypatch):
    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg):
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.connected_permission = None

        async def connect(self, **_kwargs):
            self.connected_permission = self.permission_mode

        async def disconnect(self):
            return None

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)
        machine, _ = _mk_machine()
        parent = _mk_ctx("parent-1", "parent-1")
        parent.sdk = SimpleNamespace(permission_mode="plan")
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        assert fork.sdk.permission_mode == "plan"
        assert fork.sdk.connected_permission == "plan"

    asyncio.run(go())


def test_open_btw_emits_its_permission_frame():
    async def go():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent-1", "parent-1")
        fork = _mk_ctx("btw-1", None)
        fork.key = "btw-1"
        fork.btw = True
        fork.sdk = SimpleNamespace(permission_mode="plan")
        machine.sessions[parent.key] = parent

        async def spawn(_parent, owner_client_id=None):
            assert owner_client_id == "client-1"
            return fork

        machine._spawn_btw = spawn
        result = await machine._handle_open_btw(OpenBtw(
            sid="parent-1",
            request_id="request-1",
            client_id="client-1",
        ))

        assert [event.type for event in result] == [
            "btw_opened", "snapshot", "perm"]
        perm = result[-1]
        assert perm.mode == "plan"
        assert perm.sid == "btw-1" and perm.to == "client-1"
        assert transport.sent[-1] is perm

    asyncio.run(go())
