"""Native model discovery must never submit a prompt or cross account scopes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.wrapper import claude_models as models


def test_native_catalog_keeps_new_models_and_exact_effort_capabilities():
    raw = [
        {"value": "default", "resolvedModel": "claude-opus-5-5",
         "displayName": "Default", "supportedEffortLevels": ["high"]},
        {"value": "opus", "resolvedModel": "claude-opus-5-5",
         "displayName": "Opus", "description": "Opus 5.5 · Native description",
         "supportsEffort": True, "supportedEffortLevels": ["high", "max", "max"]},
        {"value": "haiku", "resolvedModel": "claude-haiku-future",
         "supportsEffort": False, "supportedEffortLevels": ["high"]},
        {"value": "provider-deployment", "displayName": "Company model"},
        None, {"value": "bad\nmodel"},
    ]
    result = models._normalize(raw)
    assert [row["id"] for row in result] == [
        "default", "claude-opus-5-5", "claude-haiku-future", "provider-deployment"]
    assert result[1]["display_name"] == "Opus"
    assert result[0]["is_default"] is True
    assert result[1]["efforts"] == ["high", "max"]
    assert result[2]["efforts"] == result[3]["efforts"] == []
    assert raw[0]["displayName"] == "Default"
    assert models._normalize({}) == []


@pytest.mark.asyncio
async def test_catalog_cache_isolated_by_account_cwd_and_cli_upgrade(monkeypatch, tmp_path):
    models._cache.clear()
    binary = tmp_path / "claude"
    binary.write_text("old")
    monkeypatch.setattr(models, "resolve_claude_cli", lambda _: (str(binary), "configured"))
    now = [1.0]
    monkeypatch.setattr(models.time, "monotonic", lambda: now[0])
    calls = []

    async def read(binary, cwd, root, isolated, user_only):
        calls.append((binary, cwd, root, isolated, user_only))
        return [{"id": f"model-{len(calls)}"}]

    monkeypatch.setattr(models, "_read_catalog", read)
    options = dict(claude_bin=str(binary), cwd=str(tmp_path),
                   config_dir=str(tmp_path / "personal"), isolate_account_env=True)
    first, repeat = await asyncio.gather(
        models.claude_model_catalog(**options), models.claude_model_catalog(**options))
    assert first == repeat == [{"id": "model-1"}]
    assert len(calls) == 1
    assert await models.claude_model_catalog(**{**options, "config_dir": str(tmp_path / "company")}) != first
    assert await models.claude_model_catalog(**{**options, "cwd": str(tmp_path / "project")}) != first
    assert await models.claude_model_catalog(**{**options, "cwd": None}) != first
    assert calls[-1][-1] is True
    binary.write_text("upgraded")
    upgraded = await models.claude_model_catalog(**options)
    assert upgraded != first
    assert len(calls) == 5
    now[0] += models._TTL + 1
    assert await models.claude_model_catalog(**options) != upgraded
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_catalog_failure_preserves_only_same_scope_and_backs_off(monkeypatch, tmp_path):
    models._cache.clear()
    binary = tmp_path / "claude"
    binary.touch()
    monkeypatch.setattr(models, "resolve_claude_cli", lambda _: (str(binary), "configured"))
    read = AsyncMock(return_value=[{"id": "personal-model"}])
    monkeypatch.setattr(models, "_read_catalog", read)
    options = dict(claude_bin=str(binary), config_dir=str(tmp_path / "personal"))
    saved = await models.claude_model_catalog(**options)
    models._cache[next(iter(models._cache))] = (0, saved)
    read.side_effect = RuntimeError("private provider error")
    assert await models.claude_model_catalog(**options) == saved
    assert await models.claude_model_catalog(**{**options, "config_dir": str(tmp_path / "company")}) == []
    assert await models.claude_model_catalog(**options) == saved
    assert read.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "timeout", "cancel"])
async def test_probe_only_initializes_and_always_reaps_child(monkeypatch, tmp_path, outcome):
    writes = []
    proc = SimpleNamespace(
        stdin=SimpleNamespace(write=writes.append, drain=AsyncMock(), close=lambda: None),
        stdout=SimpleNamespace(), returncode=None, terminated=False,
    )

    async def line():
        if outcome == "timeout":
            await asyncio.Event().wait()
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return (json.dumps({"type": "control_response", "response": {
            "subtype": outcome, "request_id": "model-catalog",
            "response": {"models": [{"value": "future-model"}]},
        }}) + "\n").encode()

    proc.stdout.readline = line
    proc.terminate = lambda: setattr(proc, "terminated", True)
    proc.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(models, "_TIMEOUT", 0.01)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-credential")
    monkeypatch.setenv("WRAPPER_TOKEN", "control-credential")
    monkeypatch.setenv("CLAUDECODE", "nested")
    root = str(tmp_path / "profile")
    request = models._read_catalog("/daily/claude", str(tmp_path), root, True, False)
    if outcome == "success":
        assert (await request)[0]["id"] == "future-model"
    else:
        with pytest.raises({"error": RuntimeError, "timeout": TimeoutError,
                            "cancel": asyncio.CancelledError}[outcome]):
            await request
    args, kwargs = spawn.call_args
    assert args[0] == "/daily/claude"
    assert {"--no-session-persistence", "--safe-mode", "--strict-mcp-config"} <= set(args)
    assert args[-2:] == ("--setting-sources", "user")
    assert not {"ANTHROPIC_API_KEY", "WRAPPER_TOKEN", "CLAUDECODE"} & kwargs["env"].keys()
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == root
    assert len(writes) == 1
    assert json.loads(writes[0])["request"] == {"subtype": "initialize"}
    assert proc.terminated and proc.wait.await_count == 1


@pytest.mark.asyncio
async def test_get_models_routes_selected_profile_without_resuming(monkeypatch, tmp_path):
    from cc_remote.protocol import GetModels
    from cc_remote.wrapper import machine as machine_module
    from tests.test_multisession import _mk_machine

    machine, transport = _mk_machine()
    profile = SimpleNamespace(id="company", config_dir=tmp_path / "company")
    monkeypatch.setattr(machine, "_claude_profile", lambda profile_id: profile)
    monkeypatch.setattr(machine, "_claude_config_root", lambda _: str(profile.config_dir))
    machine._claude_profiles_explicit = True
    monkeypatch.setattr(machine, "_claude_new_session_defaults", AsyncMock(return_value=("custom-model", "high")))
    catalog = AsyncMock(return_value=[{"id": "claude-opus-5-5", "efforts": ["high"]}])
    monkeypatch.setattr(machine_module, "claude_model_catalog", catalog)
    await machine._handle_get_models(GetModels(
        engine="claude", cwd=str(tmp_path), claude_profile_id="company", client_id="client"))
    assert catalog.call_args.kwargs == dict(
        claude_bin=machine.cfg.claude_bin, cwd=str(tmp_path),
        config_dir=str(profile.config_dir), isolate_account_env=True)
    event = transport.sent[-1]
    assert event.to == "client" and event.claude_profile_id == "company"
    assert event.models[0]["id"] == "claude-opus-5-5"
    assert not machine.sessions


def test_tui_uses_native_models_and_honors_empty_efforts():
    from cc_remote.tui_settings import SettingsForm

    form = SimpleNamespace(new=False, engine="claude", values={"model": "future"},
                           catalog=lambda _: {"models": [
                               {"id": "future", "display_name": "Future", "efforts": []}]})
    assert SettingsForm.choices(form, "model") == [("Future", "future")]
    assert SettingsForm.choices(form, "effort") == []
