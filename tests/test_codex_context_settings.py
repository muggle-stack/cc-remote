import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cc_remote.protocol import SetCodexContext
from cc_remote.wrapper.codex_context_settings import NativeContextSettings, model_context_bounds
from cc_remote.wrapper.codex_controls import CodexControlStore


@pytest.fixture
def catalog(tmp_path):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "fixture", "context_window": 10000,
        "max_context_window": 20000, "effective_context_window_percent": 95,
    }]}))
    return tmp_path


def test_bounds_and_profiles(catalog, tmp_path):
    bounds = model_context_bounds("fixture", str(catalog))
    assert bounds.limit == 19000
    assert bounds.window_for(12000) == 12632
    with pytest.raises(ValueError):
        bounds.window_for(19001)
    assert model_context_bounds("unknown", str(catalog)) is None
    assert model_context_bounds("fixture", str(tmp_path / "other-account")) is None
    for value in (True, 0, -1, 100000001, 1.1):
        with pytest.raises(ValidationError):
            SetCodexContext(threshold_tokens=value)


def test_preferences_survive_restart_other_controls_and_reset(catalog):
    store = CodexControlStore(catalog)
    store.set_context("profile@one", 12000, 12632)
    store.update("profile@one", approval_policy="on-request", permission_profile="workspace", web_search="cached")
    store.set_cwd_override("profile@one", str(catalog))
    restored = CodexControlStore(catalog)
    assert restored.get("profile@one").context_threshold_tokens == 12000
    assert restored.get("profile@two").context_threshold_tokens is None
    assert restored.get("other@one").context_threshold_tokens is None
    restored.set_context("profile@one", None, None)
    default = CodexControlStore(catalog).get("profile@one")
    assert default.context_settings_set
    assert default.context_threshold_tokens is None
    assert default.approval_policy == "on-request"


def handle_for(catalog):
    return SimpleNamespace(
        model="fixture", codex_home=str(catalog), thread_id="one", turn_active=False,
        turn_start_pending=False, work_mode=False, _ephemeral_thread_id=None,
        _pending_server_request_ids=set(), last_goal=None,
        _request=AsyncMock(return_value={"thread": {"status": {"type": "idle"}}}),
        list_loaded_thread_ids=AsyncMock(return_value=()), force_reconnect=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_apply_waits_for_terminal_and_unsubscribes_before_resume(catalog):
    settings = NativeContextSettings()
    handle = handle_for(catalog)
    await settings.select(handle, 12000)
    async def resume(*args, **kwargs):
        settings.applied_threshold = settings.threshold
        settings.pending = False
    handle.force_reconnect.side_effect = resume
    handle.turn_active = True
    assert not await settings.apply(handle)
    handle._request.assert_not_called()
    handle.turn_active = False
    assert await settings.apply(handle)
    assert [call.args[0] for call in handle._request.call_args_list] == ["thread/read", "thread/unsubscribe"]
    handle.force_reconnect.assert_awaited_once()
    assert settings.applied_threshold == 12000 and not settings.pending
    assert settings.config() == {"model_context_window": 12632,
        "model_auto_compact_token_limit": 12000, "model_auto_compact_token_limit_scope": "total"}


@pytest.mark.asyncio
async def test_other_client_keeps_setting_pending_and_our_subscription_restored(catalog):
    settings = NativeContextSettings()
    handle = handle_for(catalog)
    handle.list_loaded_thread_ids.return_value = ("one", "other")
    await settings.select(handle, 12000)
    assert not await settings.apply(handle)
    assert settings.pending and settings.applied_threshold is None
    assert "其他客户端" in settings.error
    handle.force_reconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_and_smaller_models_never_apply_saved_override(catalog):
    settings = NativeContextSettings()
    handle = handle_for(catalog)
    await settings.select(handle, 12000)
    handle.model = "unknown"
    assert await settings.validated_config(handle) == {}
    assert not await settings.apply(handle)
    handle._request.assert_not_called()
    assert settings.pending
    with pytest.raises(ValueError):
        await settings.select(handle, 12000)


@pytest.mark.asyncio
async def test_pending_reset_cannot_bypass_applied_model_window(catalog):
    from cc_remote.wrapper.codex_handle import CodexHandle
    settings = NativeContextSettings()
    settings.applied_threshold, settings.applied_window = 12000, 30000
    settings.pending = True
    handle = SimpleNamespace(context_settings=settings, codex_home=str(catalog),
                             _update_thread_settings=AsyncMock())
    with pytest.raises(ValueError, match="恢复默认"):
        await CodexHandle.set_model(handle, "fixture")
    handle._update_thread_settings.assert_not_called()


@pytest.mark.asyncio
async def test_resume_rebuild_receipt_is_exact_thread_scoped():
    from cc_remote.wrapper.codex_handle import CodexHandle
    from tests.test_codex_controls import _Cfg
    handle = CodexHandle(_Cfg(), daemon_mode="never")
    handle.thread_id = "target"
    handle._context_resume_thread_id = "target"
    await handle._dispatch({"method": "thread/status/changed", "params": {
        "threadId": "sibling", "status": {"type": "notLoaded"}}})
    assert not handle.context_settings.reloaded
    await handle._dispatch({"method": "thread/status/changed", "params": {
        "threadId": "target", "status": {"type": "notLoaded"}}})
    assert handle.context_settings.reloaded


@pytest.mark.asyncio
async def test_applied_capacity_survives_old_rollout_and_yields_to_live_usage(catalog, monkeypatch):
    from cc_remote.wrapper import codex_handle as module
    from tests.test_codex_controls import _Cfg

    handle = module.CodexHandle(_Cfg(), codex_home=str(catalog), daemon_mode="never")
    handle.model, handle.thread_id = "fixture", "target"
    old = {"last": {"totalTokens": 7000}, "modelContextWindow": 9500}
    monkeypatch.setattr(module, "recover_codex_context_usage", lambda *a, **k: old)
    await handle.context_settings.select(handle, 12000)
    # Saving alone keeps the current native capacity.
    assert (await handle.get_context_usage())["context_window"] == 9500
    handle.last_token_usage = None
    handle._rollout_context_recovery_attempted = False
    await handle.context_settings.confirm_applied(handle)
    handle.context_window = handle.context_settings.applied_effective_window
    usage = await handle.get_context_usage()
    assert (usage["used_tokens"], usage["context_window"]) == (7000, 12000)
    assert usage["raw"] == old
    await handle._dispatch({"method": "thread/tokenUsage/updated", "params": {
        "threadId": "target", "tokenUsage": {
            "last": {"totalTokens": 7100}, "modelContextWindow": 11900}}})
    usage = await handle.get_context_usage()
    assert (usage["used_tokens"], usage["context_window"]) == (7100, 11900)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,expected", [(None, 9500), (15000, 14250), (30000, 19000)])
async def test_reset_capacity_reads_native_inheritance(catalog, configured, expected):
    settings, handle = NativeContextSettings(), handle_for(catalog)
    handle.cwd = str(catalog)
    settings.applied_window = 20000
    handle._request.return_value = {"config": {"model_context_window": configured}}
    await settings.select(handle, None)
    await settings.confirm_applied(handle)
    assert settings.applied_effective_window == expected
    assert settings.applied_threshold is None and not settings.pending
    handle._request.assert_awaited_once_with("config/read", {
        "cwd": str(catalog), "includeLayers": False})


@pytest.mark.asyncio
async def test_unreadable_reset_capacity_does_not_reuse_old_rollout(catalog, monkeypatch):
    from cc_remote.wrapper import codex_handle as module
    from tests.test_codex_controls import _Cfg

    handle = module.CodexHandle(_Cfg(), codex_home=str(catalog), daemon_mode="never")
    handle.model, handle.thread_id = "fixture", "target"
    handle._request = AsyncMock(side_effect=RuntimeError("unavailable"))
    await handle.context_settings.confirm_applied(handle)
    monkeypatch.setattr(module, "recover_codex_context_usage", lambda *a, **k: {
        "last": {"totalTokens": 7000}, "modelContextWindow": 19000})
    usage = await handle.get_context_usage()
    assert usage["used_tokens"] == 7000 and usage["context_window"] == 0


@pytest.mark.asyncio
async def test_applied_setting_refreshes_context_without_a_model_turn(catalog):
    from cc_remote.wrapper.machine import WrapperMachine

    settings, handle = NativeContextSettings(), handle_for(catalog)
    handle.context_settings = settings
    settings.pending = True
    settings.apply = AsyncMock(return_value=True)
    machine = SimpleNamespace(_handle_get_context_locked=AsyncMock(), _publish_codex_context=AsyncMock())
    ctx = SimpleNamespace(engine="codex", sdk=handle)
    await WrapperMachine._apply_codex_context(machine, ctx)
    machine._handle_get_context_locked.assert_awaited_once_with(ctx, None)
    machine._publish_codex_context.assert_not_called()
