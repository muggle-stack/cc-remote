import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cc_remote.protocol import SetCodexContext
from cc_remote.wrapper.codex_context_settings import ContextBounds, NativeContextSettings, model_context_bounds
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
            SetCodexContext(max_context_tokens=value)
    with pytest.raises(ValidationError):
        SetCodexContext(threshold_tokens=12000)


def test_smaller_usable_window_remains_a_hard_bound():
    bounds = ContextBounds(10000, 20000, 80)
    assert bounds.limit == 16000
    assert bounds.window_for(12000) == 15000
    assert bounds.compact_limit(15000) == 11400
    with pytest.raises(ValueError):
        bounds.window_for(16001)


def test_preferences_survive_restart_other_controls_and_reset(catalog):
    store = CodexControlStore(catalog)
    store.set_context("profile@one", 12000, 12632)
    store.update("profile@one", approval_policy="on-request", permission_profile="workspace", web_search="cached")
    store.set_cwd_override("profile@one", str(catalog))
    restored = CodexControlStore(catalog)
    assert restored.get("profile@one").context_max_tokens == 12000
    assert restored.get("profile@two").context_max_tokens is None
    assert restored.get("other@one").context_max_tokens is None
    restored.set_context("profile@one", None, None)
    default = CodexControlStore(catalog).get("profile@one")
    assert default.context_settings_set
    assert default.context_max_tokens is None
    assert default.approval_policy == "on-request"


def handle_for(catalog):
    return SimpleNamespace(
        model="fixture", codex_home=str(catalog), thread_id="one", turn_active=False,
        turn_start_pending=False, work_mode=False, _ephemeral_thread_id=None,
        _pending_server_request_ids=set(), last_goal=None,
        _request=AsyncMock(return_value={"thread": {"status": {"type": "idle"}}}),
        list_loaded_thread_ids=AsyncMock(return_value=()), force_reconnect=AsyncMock(),
    )


@pytest.fixture
def astra_catalog(tmp_path):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "gpt-6-astra", "context_window": 272000,
        "max_context_window": 872000, "effective_context_window_percent": 95,
    }]}))
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity,window,threshold", [
    (300000, 315790, 284211), (400000, 421053, 378947),
    (828400, 872000, 784800), (200000, 210527, 189474),
])
async def test_requested_capacity_is_exact_and_compaction_is_near_95_percent(
    astra_catalog, capacity, window, threshold,
):
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    await settings.select(handle, capacity)
    config = await settings.validated_config(handle)
    assert config["model_context_window"] == window
    # Codex 0.154.0 ModelInfo::auto_compact_token_limit clamps the request
    # to 90% of its raw window, independently of the 95% usable capacity.
    native_limit = min(config["model_auto_compact_token_limit"],
                       config["model_context_window"] * 9 // 10)
    assert native_limit == threshold
    await settings.confirm_applied(handle)
    assert settings.applied_threshold == threshold
    assert settings.applied_effective_window == capacity
    assert 0.94 < settings.applied_threshold / capacity <= 0.95


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_window", [315790, 333334, 444445])
async def test_saved_300k_capacity_is_restored_only_at_safe_reload(astra_catalog, saved_window):
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    settings.restore(300000, saved_window, selected=True)
    handle.turn_active = True
    assert not await settings.apply(handle)
    handle._request.assert_not_called()
    handle.force_reconnect.assert_not_called()

    async def resume(*args, **kwargs):
        config = await settings.validated_config(handle)
        assert config["model_context_window"] == 315790
        assert config["model_auto_compact_token_limit"] == 284211
        await settings.confirm_applied(handle)

    handle.turn_active = False
    handle.force_reconnect.side_effect = resume
    assert await settings.apply(handle)
    assert settings.max_tokens == settings.applied_effective_window == 300000
    assert settings.threshold == settings.applied_threshold == 284211
    assert settings.window == 315790 and not settings.pending


@pytest.mark.asyncio
async def test_legacy_setting_migrates_capacity_without_changing_the_users_number(astra_catalog):
    path = astra_catalog / "codex-session-controls.json"
    path.write_text(json.dumps({"version": 1, "sessions": {
        "primary@one": {"context_threshold_tokens": 300000,
                        "context_window_tokens": 333334, "context_settings_set": True},
    }}))
    path.chmod(0o600)
    controls = CodexControlStore(astra_catalog).get("primary@one")
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    settings.restore(controls.context_max_tokens, controls.context_window_tokens,
                     controls.context_settings_set)
    assert settings.config() == {}  # Do not send the old threshold as capacity.
    await settings.validated_config(handle)
    await settings.confirm_applied(handle)
    assert settings.max_tokens == 300000
    assert settings.applied_threshold == 284211
    assert settings.applied_effective_window == 300000


@pytest.mark.asyncio
async def test_saved_capacity_above_model_max_stays_pending(astra_catalog):
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    settings.restore(828401, 872001, selected=True)
    assert await settings.validated_config(handle) == {}
    assert not await settings.apply(handle)
    assert settings.pending and settings.max_tokens == 828401
    assert "828,400" in settings.error
    handle._request.assert_not_called()
    with pytest.raises(ValueError, match="828,400"):
        await settings.select(handle, 828401)


@pytest.mark.asyncio
async def test_lowering_capacity_also_lowers_window_despite_old_usage(astra_catalog):
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    await settings.select(handle, 400000)
    await settings.confirm_applied(handle)
    handle.context_window = 400000
    await settings.select(handle, 300000)
    assert settings.window == 315790 and settings.max_tokens == 300000
    assert settings.threshold == 284211
    assert settings.applied_effective_window == 400000 and settings.pending
    await settings.confirm_applied(handle)
    assert settings.applied_effective_window == 300000


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_capacity_command_saves_session_limit_and_preserves_live_configuration(
    astra_catalog, monkeypatch, save_fails,
):
    from cc_remote.wrapper.machine import WrapperMachine

    store = CodexControlStore(astra_catalog)
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model, handle.context_settings = "gpt-6-astra", settings
    await settings.select(handle, 400000)
    await settings.confirm_applied(handle)
    if save_fails:
        def fail(*args):
            raise OSError("storage unavailable")
        monkeypatch.setattr(store, "set_context", fail)
    ctx = SimpleNamespace(sdk=handle, engine="codex", space="code", btw=False,
                          state="running", query_lock=asyncio.Lock())
    machine = SimpleNamespace(
        _ctx_for=lambda sid: ctx, _ctx_wire_sid=lambda ctx: "primary@one",
        _runtime_control_preflight=AsyncMock(return_value=None),
        _codex_controls=store, _emit=AsyncMock(), _apply_codex_context=AsyncMock(),
    )
    machine._publish_codex_context = lambda ctx: WrapperMachine._publish_codex_context(machine, ctx)
    result = await WrapperMachine._handle_set_codex_context(
        machine, SetCodexContext(sid="primary@one", max_context_tokens=300000))
    assert result.applied_max_context_tokens == 400000
    assert result.applied_threshold_tokens == 378947
    assert result.limit_tokens == 828400
    if save_fails:
        assert (settings.max_tokens, settings.window, settings.threshold) == (400000, 421053, 378947)
        assert result.error == "storage unavailable" and not result.pending
        assert store.get("primary@one").context_max_tokens is None
    else:
        assert result.max_context_tokens == 300000 and result.pending
        assert store.get("primary@one").context_max_tokens == 300000
        assert store.get("primary@one").context_window_tokens == 315790
        assert store.get("other@one").context_max_tokens is None
    machine._apply_codex_context.assert_not_called()
    handle.force_reconnect.assert_not_called()


@pytest.mark.asyncio
async def test_apply_waits_for_terminal_and_unsubscribes_before_resume(catalog):
    settings = NativeContextSettings()
    handle = handle_for(catalog)
    await settings.select(handle, 12000)
    async def resume(*args, **kwargs):
        await settings.confirm_applied(handle)
    handle.force_reconnect.side_effect = resume
    handle.turn_active = True
    assert not await settings.apply(handle)
    handle._request.assert_not_called()
    handle.turn_active = False
    assert await settings.apply(handle)
    assert [call.args[0] for call in handle._request.call_args_list] == ["thread/read", "thread/unsubscribe"]
    handle.force_reconnect.assert_awaited_once()
    assert settings.applied_threshold == 11368 and not settings.pending
    assert settings.config() == {"model_context_window": 12632,
        "model_auto_compact_token_limit": 11368, "model_auto_compact_token_limit_scope": "total"}


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
@pytest.mark.parametrize("scenario", [
    "concurrent_subscriber", "early_receipt", "late_receipt", "sibling_receipt",
    "native_reload", "cold_then_reload", "new_thread", "private_resume",
])
async def test_context_connect_confirms_only_native_reload_or_new_thread(
    monkeypatch, astra_catalog, scenario,
):
    from cc_remote.wrapper import codex_handle as module
    from tests.test_codex_controls import _Cfg

    manager = SimpleNamespace(
        strict_shared_affinity=True, mode="auto", invalidate=lambda: None,
        proxy_args=AsyncMock(return_value=["/unused/codex", "app-server", "proxy"]),
    )
    handle = module.CodexHandle(_Cfg(), codex_home=str(astra_catalog),
                               daemon_mode="off" if scenario == "private_resume" else "auto",
                               daemon_manager=manager)
    handle.model = "gpt-6-astra"
    settings = handle.context_settings
    settings.applied_model = handle.model
    settings.applied_effective_window = 258400
    await settings.select(handle, 300000)
    methods = []
    resumes = 0

    async def status(thread_id="target"):
        await handle._dispatch({"method": "thread/status/changed", "params": {
            "threadId": thread_id, "status": {"type": "notLoaded"}}})

    async def open_process(_argv, _bin, *, daemon_proxy):
        handle.proc = SimpleNamespace(returncode=None)
        handle._using_daemon_proxy = daemon_proxy
        handle._dead = False

    async def send(frame):
        nonlocal resumes
        method = frame["method"]
        methods.append(method)
        if method == "initialized":
            return
        if method == "initialize":
            if scenario == "early_receipt":
                await status()
            result = {"userAgent": "codex_cli_rs/0.154.0 (fixture)"}
        elif method == "thread/loaded/list":
            # The old check sees an unloaded snapshot; a sibling subscribes
            # immediately afterward and resume retains that native config.
            result = {"data": [], "nextCursor": None}
        elif method in {"thread/resume", "thread/start"}:
            resumes += 1
            assert frame["params"]["config"]["model_context_window"] == 315790
            if scenario == "native_reload" or resumes > 1:
                await status()
            elif scenario == "sibling_receipt":
                await status("other")
            result = {"thread": {"id": "target"}, "model": handle.model}
        elif method == "thread/read":
            result = {"thread": {"status": {"type": "idle"}}}
        elif method == "thread/unsubscribe":
            result = {}
        else:
            raise AssertionError(method)
        await handle._dispatch({"id": frame["id"], "result": result})
        if method == "thread/resume" and scenario == "late_receipt":
            # The reader can see a sibling's later reload before our awaiting
            # connect coroutine resumes. It must already have closed the fence.
            await status()

    async def reconnect(sid, **_kwargs):
        handle.proc = None
        await handle.connect(resume_id=sid, cwd=str(astra_catalog), preserve_controls=True)

    monkeypatch.setattr(module, "_resolve_codex_bin", lambda: "/unused/codex")
    monkeypatch.setattr(module, "_newer_private_core_for_oversized_resume", lambda *_a: None)
    monkeypatch.setattr(module, "_oversized_desktop_openai_resume_requires_http", lambda *_a: False)
    monkeypatch.setattr(module, "_profile_codex_env", lambda *_a: {})
    handle._open_process, handle._send = open_process, send
    handle._update_thread_settings = AsyncMock()
    handle.force_reconnect = reconnect
    await handle.connect(resume_id=None if scenario == "new_thread" else "target",
                         cwd=str(astra_catalog))
    confirmed = scenario in {"native_reload", "new_thread", "private_resume"}
    assert settings.pending is not confirmed
    assert handle.context_window == (300000 if confirmed else 258400)
    assert settings.applied_effective_window == handle.context_window
    assert handle._context_resume_request_id is None
    assert handle._context_resume_thread_id is None
    if scenario == "cold_then_reload":
        assert await settings.apply(handle)
        assert not settings.pending and handle.context_window == 300000
        assert resumes == 2
    assert not any(method.startswith("turn/") for method in methods)
    handle.proc = None


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
    machine = SimpleNamespace(_handle_get_context_locked=AsyncMock(), _publish_codex_context=AsyncMock(),
                              _persist_codex_session_controls=AsyncMock())
    ctx = SimpleNamespace(engine="codex", sdk=handle)
    await WrapperMachine._apply_codex_context(machine, ctx)
    machine._handle_get_context_locked.assert_awaited_once_with(ctx, None)
    machine._persist_codex_session_controls.assert_awaited_once_with(ctx)
    machine._publish_codex_context.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,newer_capacity,expected", [
    (False, None, 315790), (True, None, 333334), (False, 400000, 421053),
])
async def test_repaired_window_persists_only_after_apply_without_overwriting_newer_choice(
    astra_catalog, pending, newer_capacity, expected,
):
    from cc_remote.wrapper.machine import WrapperMachine

    store = CodexControlStore(astra_catalog)
    store.set_context("primary@one", newer_capacity or 300000,
                      421053 if newer_capacity else 333334)
    store.set_context("other@one", 300000, 315790)
    settings, handle = NativeContextSettings(), handle_for(astra_catalog)
    handle.model = "gpt-6-astra"
    handle.context_settings = settings
    settings.restore(300000, 333334, selected=True)
    await settings.validated_config(handle)
    if not pending:
        await settings.confirm_applied(handle)
    machine = SimpleNamespace(_codex_controls=store, _ctx_wire_sid=lambda ctx: "primary@one")
    ctx = SimpleNamespace(engine="codex", space="code", session_id="one", sdk=handle)
    await WrapperMachine._persist_codex_session_controls(machine, ctx)
    reopened = CodexControlStore(astra_catalog)
    saved = reopened.get("primary@one")
    assert saved.context_max_tokens == (newer_capacity or 300000)
    assert saved.context_window_tokens == expected
    assert reopened.get("other@one").context_window_tokens == 315790
