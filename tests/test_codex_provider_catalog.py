"""Provider-specific discovery never borrows a different thread's capabilities."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import ERR_AUTH, GetModels
from cc_remote.wrapper import codex_models as models
from cc_remote.wrapper import codex_sessions, machine as machine_module
from cc_remote.wrapper.codex_handle import CodexHandle
from tests.test_codex_controls import _Cfg
from tests.test_codex_profiles import _context, _machine


@pytest.mark.asyncio
async def test_same_model_id_keeps_provider_capabilities_and_failure_cache_separate(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(models, "_profile_cache", {})
    monkeypatch.setattr(models, "_inflight", {})
    failed = set()
    calls = []

    async def query(home, *, model_provider):
        calls.append((home, model_provider))
        if model_provider in failed:
            raise RuntimeError("discovery unavailable")
        return [{
            "id": "shared-model",
            "supportedReasoningEfforts": [{"reasoningEffort": model_provider}],
            "defaultReasoningEffort": model_provider,
            "serviceTiers": [{"id": "priority"}] if model_provider == "openai" else [],
        }]

    monkeypatch.setattr(models, "_rpc_model_list", query)
    home = str(tmp_path)
    first = await models.codex_catalog(codex_home=home, model_provider="openai")
    second = await models.codex_catalog(codex_home=home, model_provider="local")
    assert first[0]["efforts"] == ["openai"]
    assert second[0]["efforts"] == ["local"]
    assert second[0]["service_tiers"] == []
    assert await models.clamp_effort(
        "shared-model", "local", codex_home=home, model_provider="openai",
    ) == "openai"
    assert models.resolve_service_tier("shared-model", "fast", first) == "priority"
    with pytest.raises(ValueError):
        models.resolve_service_tier("shared-model", "fast", second)
    failed.update({"openai", "unknown"})
    assert await models.codex_catalog(True, codex_home=home, model_provider="openai") == first
    assert await models.codex_catalog(True, codex_home=home, model_provider="unknown") == []
    assert calls == [(home, provider) for provider in ("openai", "local", "openai", "unknown")]


@pytest.mark.asyncio
async def test_default_catalog_follows_native_selection_without_forcing_a_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_profile_cache", {})
    monkeypatch.setattr(models, "_inflight", {})
    calls = []

    async def query(home, *, model_provider):
        calls.append(model_provider)
        # Native config may select a managed provider or profile which a local
        # root-only config reader cannot infer. Preserve its resolution.
        return [{"id": "native-default" if model_provider is None else model_provider}]

    monkeypatch.setattr(models, "_rpc_model_list", query)
    home = str(tmp_path)
    (tmp_path / "config.toml").write_text('model_provider = "openai"\n')
    default = await models.codex_catalog(codex_home=home)
    explicit = await models.codex_catalog(codex_home=home, model_provider="openai")
    assert default[0]["id"] == "native-default" and explicit[0]["id"] == "openai"
    (tmp_path / "config.toml").write_text('model_provider = "local"\n')
    await models.codex_catalog(codex_home=home)
    assert calls == [None, "openai", None]


@pytest.mark.asyncio
async def test_provider_refreshes_coalesce_without_blocking_other_providers(monkeypatch):
    monkeypatch.setattr(models, "_profile_cache", {})
    monkeypatch.setattr(models, "_inflight", {})
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def query(_home, *, model_provider):
        calls.append(model_provider)
        if model_provider == "slow":
            started.set()
            await release.wait()
        return [{"id": model_provider}]

    monkeypatch.setattr(models, "_rpc_model_list", query)
    first = asyncio.create_task(models.codex_catalog(True, model_provider="slow"))
    await started.wait()
    second = asyncio.create_task(models.codex_catalog(True, model_provider="slow"))
    fast = await asyncio.wait_for(models.codex_catalog(True, model_provider="fast"), 1)
    assert fast[0]["id"] == "fast"
    release.set()
    assert await first == await second
    assert calls == ["slow", "fast"]


@pytest.mark.parametrize("provider,expected", [
    ("custom", "custom"), (None, "openai"), ("bad\nprovider", None),
])
def test_provider_reads_exact_native_rollout_metadata(tmp_path, provider, expected):
    root = tmp_path / "sessions"
    root.mkdir()
    payload = {"id": "thread-1"}
    if provider is not None:
        payload["model_provider"] = provider
    (root / "rollout-thread-1.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n")
    assert codex_sessions.codex_session_provider("thread-1", codex_home=tmp_path) == expected
    assert codex_sessions.codex_session_provider("thread-2", codex_home=tmp_path) is None


@pytest.mark.parametrize("nested", [False, True])
def test_handle_retains_native_provider_across_partial_settings(nested):
    handle = CodexHandle(_Cfg())
    receipt = {"modelProvider": "custom"}
    handle._apply_thread_settings({"thread": receipt} if nested else receipt)
    handle._apply_thread_settings({"model": "shared-model"})
    assert handle.model_provider == "custom"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["live", "rollout", "read"])
async def test_session_picker_resolves_exact_account_and_provider(monkeypatch, tmp_path, source):
    machine, transport = _machine(tmp_path)
    ctx = _context("stack@thread", "thread", "stack")
    ctx.sdk = SimpleNamespace(model_provider="custom" if source == "live" else None)
    machine.sessions[ctx.key] = ctx
    def metadata(*_args, **_kwargs):
        return "custom" if source == "rollout" else None

    monkeypatch.setattr(machine_module, "codex_session_provider", metadata)
    native = AsyncMock(return_value={"thread": {"id": "thread", "modelProvider": "custom"}})
    monkeypatch.setattr(machine, "_codex_rpc_for_profile", native)
    catalog = AsyncMock(return_value=[{
        "id": "custom-model", "is_default": True,
        "default_effort": "adaptive", "efforts": ["adaptive"],
    }])
    monkeypatch.setattr(machine_module, "codex_catalog", catalog)

    result = await machine._handle_get_models(GetModels(
        sid=ctx.key, engine="codex", codex_profile_id="stack", client_id="browser"))
    assert result.sid == ctx.key and result.codex_profile_id == "stack"
    assert result.default_model == "custom-model" and result.default_effort == "adaptive"
    assert result.to == "browser" and transport.sent == [result]
    catalog.assert_awaited_once_with(
        force=True, codex_home=str((tmp_path / "stack").resolve()), model_provider="custom")
    if source == "read":
        assert native.call_args.args[0].id == "stack"
        assert native.call_args.args[1:] == ("thread/read", {"threadId": "thread", "includeTurns": False})
    else:
        native.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_or_mismatched_session_provider_cannot_borrow_default(monkeypatch, tmp_path):
    machine, _transport = _machine(tmp_path)
    monkeypatch.setattr(machine_module, "codex_session_provider", lambda *_args, **_kwargs: None)
    read = AsyncMock(return_value={"thread": {"id": "different", "modelProvider": "openai"}})
    monkeypatch.setattr(machine, "_codex_rpc_for_profile", read)
    catalog = AsyncMock()
    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    result = await machine._handle_get_models(GetModels(
        sid="stack@thread", engine="codex", codex_profile_id="stack"))
    assert result.sid == "stack@thread" and result.models == [] and result.error
    invalid = await machine._handle_get_models(GetModels(
        sid="stack@thread", engine="codex", codex_profile_id="primary"))
    assert invalid.code == ERR_AUTH
    catalog.assert_not_called()
    assert read.await_count == 1


@pytest.mark.asyncio
async def test_model_and_effort_validation_use_session_provider(monkeypatch, tmp_path):
    machine, _transport = _machine(tmp_path)
    ctx = _context("stack@thread", "thread", "stack")
    ctx.sdk = SimpleNamespace(
        model="shared", model_provider="custom", effort=None, set_effort=AsyncMock())
    catalog = AsyncMock(return_value=[{
        "id": "custom-default", "is_default": True, "efforts": ["adaptive"],
    }])
    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(machine_module, "codex_current_provider", lambda **_kwargs: "openai")
    assert await machine._resolve_codex_profile_model(
        machine._codex_profile("stack"), "custom-unlisted", model_provider="custom",
    ) == ("custom-unlisted", False)
    clamp = AsyncMock(return_value="adaptive")
    monkeypatch.setattr(machine_module, "clamp_effort", clamp)
    assert await machine._apply_codex_effort(ctx, "max") == "adaptive"
    assert catalog.call_args.kwargs["model_provider"] == "custom"
    assert clamp.call_args.kwargs["model_provider"] == "custom"
    ctx.sdk.set_effort.assert_awaited_once_with("adaptive")


@pytest.mark.asyncio
async def test_speed_and_model_changes_validate_native_provider(monkeypatch):
    from cc_remote.wrapper import codex_handle as handle_module

    handle = CodexHandle(_Cfg())
    handle.model, handle.thread_id, handle.model_provider = "shared", "thread", "custom"
    catalog = AsyncMock(return_value=[{
        "id": "shared", "service_tiers": [{"id": "priority"}],
    }])
    monkeypatch.setattr(handle_module, "codex_catalog", catalog)
    handle._request = AsyncMock(return_value={})
    await handle.set_service_tier("fast")
    assert catalog.call_args.kwargs["model_provider"] == "custom"
    assert handle.service_tier == "priority"
    await handle.set_model("other")
    assert handle.service_tier is None
    assert catalog.call_args.kwargs["model_provider"] == "custom"

    async def provider_changed(**_kwargs):
        handle.model_provider = "openai"
        return catalog.return_value

    monkeypatch.setattr(handle_module, "codex_catalog", provider_changed)
    calls = handle._request.await_count
    with pytest.raises(ValueError, match="已变化"):
        await handle.set_service_tier("fast")
    assert handle._request.await_count == calls
