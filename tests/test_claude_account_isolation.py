"""Explicit account isolation is independent of session routing namespaces."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from cc_remote.claude_broker.client import BrokerClientError
from cc_remote.config import WrapperConfig
from cc_remote.wrapper import machine as machine_module, sdk as sdk_module
from cc_remote.wrapper.claude_transport import AccountIsolatedSubprocessCLITransport
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_broker_machine import _BrokerSdk, SESSION_ID
from tests.test_claude_permission_state import _FakeClaudeClient
from tests.test_multisession import _mk_ctx, _StubTransport


@pytest.fixture(params=[
    "implicit", "explicit-single", "explicit-multi", "explicit-contracted",
])
def account_machine(request, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    selected = tmp_path / "selected"
    project = tmp_path / "project"
    legacy.mkdir()
    selected.mkdir()
    (project / ".claude").mkdir(parents=True)
    (selected / "settings.json").write_text('{"model":"account-model"}')
    (project / ".claude" / "settings.local.json").write_text(
        '{"env":{"ANTHROPIC_MODEL":"project-model"}}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(legacy))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("ANTHROPIC_MODEL", "ambient-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-ambient-key")
    monkeypatch.setattr(
        machine_module.WrapperMachine, "_claude_managed_settings_paths",
        staticmethod(lambda: []))
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.cc_cwd = str(project)
    cfg.claude_work_root = tmp_path / "work" / "claude"
    cfg.codex_work_root = tmp_path / "work" / "codex"
    cfg.experimental_claude_broker = False
    cfg.claude_profiles_json = ""
    explicit = request.param != "implicit"
    if explicit:
        profiles = {"selected": {
            "label": "Selected", "config_dir": str(selected), "default": True,
        }}
        if request.param == "explicit-single":
            # A fresh unnamespaced topology must match the native catalog.
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected))
        if request.param == "explicit-contracted":
            cfg.claude_profiles_json = json.dumps({
                **profiles, "legacy": {"label": "Legacy", "config_dir": str(legacy)},
            })
            machine_module.WrapperMachine(cfg, _StubTransport())
        if request.param == "explicit-multi":
            profiles["legacy"] = {"label": "Legacy", "config_dir": str(legacy)}
        cfg.claude_profiles_json = json.dumps(profiles)
    machine = machine_module.WrapperMachine(cfg, _StubTransport())
    return machine, selected, explicit


def test_spawn_reconnect_and_btw_keep_the_selected_account(
    account_machine, monkeypatch,
):
    machine, selected, explicit = account_machine
    clients = []

    class Client(_FakeClaudeClient):
        def __init__(self, options, transport=None):
            super().__init__(options)
            self.transport = transport
            clients.append(self)

    monkeypatch.setattr(sdk_module, "ClaudeSDKClient", Client)
    monkeypatch.setattr(SdkHandle, "preflight", staticmethod(lambda _: None))

    async def run():
        ctx = await machine._spawn(
            resume_id=None, cwd=machine.cfg.cc_cwd, engine="claude")
        assert ctx is not None
        assert ctx.sdk.model == ("account-model" if explicit else "project-model")
        ctx.session_id = SESSION_ID
        await ctx.sdk.force_reconnect(SESSION_ID, ctx.cwd, reason="test")
        btw = await machine._spawn_btw(ctx, owner_client_id="test-client")
        try:
            assert len(clients) == 3
            assert btw.claude_profile_id == ctx.claude_profile_id
            assert clients[-1].options.session_id == btw.btw_reserved_id
            private_sid = machine._claude_wire_sid(
                machine._claude_profile_for_ctx(btw), btw.btw_reserved_id)
            assert private_sid in machine._load_private_btw_sessions()
            for client in clients:
                assert isinstance(client.transport, AccountIsolatedSubprocessCLITransport) is explicit
                assert client.options.setting_sources == (["user"] if explicit else None)
                if explicit:
                    assert client.options.env["CLAUDE_CONFIG_DIR"] == str(selected)
                    assert "ANTHROPIC_API_KEY" not in client.options.env
                    assert client.options.settings is None
            wire_sid = machine._claude_wire_sid(machine._claude_profile(), SESSION_ID)
            assert ("@" in wire_sid) is machine._claude_profiles.is_multi_profile
        finally:
            await btw.sdk.disconnect()
            await ctx.sdk.disconnect()

    asyncio.run(run())


def test_broker_exit_restores_the_selected_account(account_machine, monkeypatch):
    machine, selected, explicit = account_machine
    created = []

    class RestoredSdk:
        def __init__(self, cfg, **kwargs):
            created.append(kwargs)

        async def connect(self, **kwargs):
            pass

        async def disconnect(self):
            pass

    class Broker:
        async def status(self, sid):
            raise BrokerClientError("session_not_found", "fixture broker exited")

    monkeypatch.setattr(machine_module, "SdkHandle", RestoredSdk)

    async def run():
        profile = machine._claude_profile()
        ctx = _mk_ctx(machine._claude_wire_sid(profile, SESSION_ID), SESSION_ID)
        ctx.claude_profile_id = profile.id
        ctx.cwd = machine.cfg.cc_cwd
        ctx.sdk = _BrokerSdk(cwd=ctx.cwd)
        machine.sessions[ctx.key] = ctx
        machine._claude_broker = Broker()
        assert await machine._restore_sdk_after_claude_broker_exit(ctx, ctx.sdk) is True
        assert created == ([{
            "claude_config_dir": str(selected), "isolate_account_env": True,
        }] if explicit else [{}])

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["list", "plugin", "hook"])
def test_capability_operations_use_the_same_account_boundary(
    account_machine, monkeypatch, operation,
):
    machine, selected, explicit = account_machine
    observed = []

    async def discover(*args, **kwargs):
        observed.append(kwargs)
        return [], [], []

    async def mutate(*args, **kwargs):
        observed.append(kwargs)

    monkeypatch.setattr(machine_module, "engine_capabilities", discover)
    monkeypatch.setattr(machine_module, "manage_engine_plugin", mutate)
    monkeypatch.setattr(machine_module, "manage_engine_hook", mutate)
    cmd = SimpleNamespace(
        engine="claude", space="code", cwd=machine.cfg.cc_cwd,
        claude_profile_id=machine._claude_profile().id,
        action="enable", plugin_id="fixture-plugin",
    )
    handler = {
        "list": machine._handle_get_engine_capabilities,
        "plugin": machine._handle_manage_engine_plugin,
        "hook": machine._handle_manage_engine_hook,
    }[operation]
    asyncio.run(handler(cmd))
    assert len(observed) == (1 if operation == "list" else 2)
    for kwargs in observed:
        assert kwargs.get("isolate_claude_account_env", False) is explicit
        assert kwargs.get("claude_config_root") == (str(selected) if explicit else None)
