"""Authoritative Claude permission state across reconnect and runtime creation."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import AssistantMessage, TextBlock

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    ContextReport,
    ERR_INTERNAL,
    GetContext,
    GetModels,
    Hello,
    Model,
    NewSession,
    OpenBtw,
    SetModel,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.sdk import CLAUDE_DEFAULT_MODEL, SdkHandle
from cc_remote.wrapper.claude_controls import ClaudeControls, ClaudeControlStore
from cc_remote.workspaces import WorkStores
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

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_messages(self):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - make this an async generator
            yield None


def test_context_recovery_barrier_releases_the_old_generation_lock():
    async def go():
        handle = SdkHandle(WrapperConfig())
        old_route_lock = handle._message_route_lock

        async with handle.context_recovery_barrier() as quiescent:
            assert quiescent is True
            assert old_route_lock.locked()
            # connect() installs a new generation lock before the barrier exits.
            handle._message_route_lock = asyncio.Lock()

        assert old_route_lock.locked() is False
        assert handle._message_route_lock.locked() is False

    asyncio.run(go())


def test_context_recovery_barrier_fails_closed_behind_message_router():
    async def go():
        handle = SdkHandle(WrapperConfig())
        await handle._message_route_lock.acquire()
        try:
            async with handle.context_recovery_barrier() as quiescent:
                assert quiescent is False
        finally:
            handle._message_route_lock.release()

    asyncio.run(go())


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
        assert "autocompact" not in (
            _FakeClaudeClient.created[-1].options.extra_args or {})

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
        assert "allow-dangerously-skip-permissions" in (
            _FakeClaudeClient.created[-1].options.extra_args or {})
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8"]

        # A terminal-owned append may also change the session model. External
        # reload must let resume recover that value instead of forcing our cache.
        await handle.force_reconnect(
            None, "/tmp", reason="external transcript change",
            preserve_model=False)
        assert [client.options.model for client in _FakeClaudeClient.created] == [
            None, "claude-opus-4-8", None]
        # Reconnects deliberately skip the optional context RPC.  The resumed
        # stream will restore the authoritative model on its next native
        # announcement instead of blocking readiness on a metadata read.
        assert handle.model is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_curated_model_stays_pinned_across_context_echo_and_reconnect(
    monkeypatch,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")

        await handle.set_model("claude-fable-5-1")
        first = _FakeClaudeClient.created[-1]
        assert first.model_calls == ["claude-fable-5-1[1m]"]
        assert handle.model == "claude-fable-5-1[1m]"

        # Native /context may echo only the base alias. It must not erase the
        # long-context selection used by the next child generation.
        handle._record_context_usage(
            {"model": "claude-fable-5-1", "totalTokens": 10},
            update_model=True,
        )
        assert handle.model == "claude-fable-5-1[1m]"

        await handle.force_reconnect(None, "/tmp", reason="test reconnect")
        replacement = _FakeClaudeClient.created[-1]
        assert replacement.options.model == "claude-fable-5-1[1m]"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_context_observation_does_not_promote_native_base_model():
    handle = SdkHandle(WrapperConfig())

    handle._record_context_usage(
        {"model": "claude-fable-5-1", "totalTokens": 10},
        update_model=True,
    )

    assert handle.model == "claude-fable-5-1"


def test_claude_context_report_never_leaks_a_proxy_upstream_id():
    """The chip must not show a gateway's raw id when nothing selected it.

    ``_claude_context_model``'s contract is "a user-facing Claude alias, never
    a proxy upstream id". With no Remote-owned selection in force, the /context
    reading is an *observation* and may not supply one; a session that selected
    the id itself still reports it.
    """
    def context_model(sdk_model, announced, usage_model):
        ctx = SimpleNamespace(sdk=SimpleNamespace(model=sdk_model),
                              announced_model=announced)
        return machine_module.WrapperMachine._claude_context_model(
            ctx, {"model": usage_model})

    # No selection anywhere: the upstream name must not become the answer.
    assert context_model(None, None, "glm-5.2") is None
    assert context_model(None, None, "openai/gpt-5") is None
    # A Remote-owned selection is a real answer, provider-native or not.
    assert context_model("glm-5.2", None, "glm-5.2") == "glm-5.2"
    assert context_model(None, "glm-5.2", "glm-5.2") == "glm-5.2"
    # A Claude-branded observation is still adopted, including Vertex forms.
    assert context_model(None, None, "claude-opus-4-6") == "claude-opus-4-6"
    assert context_model(None, None, "claude-sonnet-4-5@20250929") == (
        "claude-sonnet-4-5@20250929")


def test_same_claude_model_selection_compares_through_the_pins():
    """Agreement is decided on normalized ids, and "no selection" agrees with
    nothing -- so a reading can never be mistaken for an established choice.

    Both reconciliation sites share this predicate; comparing raw strings would
    call a curated id and its pinned ``[1m]`` form different when they are the
    same selection.
    """
    same = sdk_module.same_claude_model_selection

    # A curated id and its pinned spelling are one selection.
    assert same("claude-fable-5-1", "claude-fable-5-1[1m]")
    assert same("claude-fable-5-1[1m]", "claude-fable-5-1")
    # Surrounding space is never part of the identity.
    assert same("  claude-fable-5-1  ", "claude-fable-5-1[1m]")
    # Case folds through the pin table, which is keyed lowercase.
    assert same("opus", CLAUDE_DEFAULT_MODEL)
    assert same("OPUS", CLAUDE_DEFAULT_MODEL)
    assert same("CLAUDE-FABLE-5-1", "claude-fable-5-1[1m]")
    # An unpinned id keeps its casing -- only the pin lookup folds. A mixed-case
    # selection therefore reads as *disagreeing* with a lowercase observation of
    # the same id, which is the conservative direction: the selection is kept.
    assert not same("Claude-Opus-4-6", "claude-opus-4-6")
    # Genuinely different selections, including provider-native ids.
    assert not same("claude-opus-4-6", "claude-3-7-sonnet@20250219")
    assert not same("glm-5.2", "claude-opus-4-6")
    assert not same("glm-5.2", "openai/gpt-5")
    # Nothing established: never agreement, whichever side is missing.
    assert not same(None, "claude-opus-4-6")
    assert not same("claude-opus-4-6", None)
    assert not same(None, None)


def test_claude_context_upstream_id_never_overwrites_explicit_selection():
    """Requirement: transcript/context metadata cannot replace a selection.

    A gateway reports its own upstream id in context usage even though the
    Claude alias the user picked is what was actually requested. That reading
    must not become this session's model -- and must not suppress the rest of
    the reading, which the auto-compact/chip paths depend on.
    """
    for selection in ["claude-opus-4-6", "claude-opus-4-6[1m]"]:
        handle = SdkHandle(WrapperConfig())
        handle.model = selection

        handle._record_context_usage(
            {"model": "glm-5.2", "totalTokens": 10,
             "autoCompactThreshold": 500_000, "rawMaxTokens": 1_000_000},
            update_model=True,
        )

        assert handle.model == selection
        # The same reading still lands its non-model fields.
        assert handle.effective_auto_compact_threshold_tokens == 500_000
        assert handle.raw_context_max_tokens == 1_000_000


def test_claude_context_work_baseline_survives_upstream_id_observation():
    """The Work baseline is captured even when the model reading is refused."""
    handle = SdkHandle(WrapperConfig())
    handle.work_mode = True
    handle.model = "claude-opus-4-6"

    handle._record_context_usage(
        {"model": "glm-5.2", "totalTokens": 4_242},
        update_model=True,
        capture_work_baseline=True,
    )

    assert handle.model == "claude-opus-4-6"
    assert handle.work_context_baseline_tokens == 4_242


def test_provider_native_selection_persists_and_survives_reconnect(
    monkeypatch, tmp_path,
):
    """Requirement 1: an explicit provider-native model is accepted, handed
    to Claude Code, and preserved across a reconnect."""

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")

        await handle.set_model("glm-5.2")
        assert _FakeClaudeClient.created[-1].model_calls == ["glm-5.2"]
        assert handle.model == "glm-5.2"

        await handle.force_reconnect(None, "/tmp", reason="test reconnect")
        assert _FakeClaudeClient.created[-1].options.model == "glm-5.2"
        await handle.disconnect()

    asyncio.run(go())


def test_provider_native_selection_roundtrips_the_private_store(tmp_path):
    """Requirement 1: the durable record keeps the provider-native id, so a
    cold resume restores it rather than falling back to a Claude default."""
    store = ClaudeControlStore(tmp_path)
    saved = store.update(
        "11111111-1111-4111-8111-111111111111",
        model="glm-5.2",
        effort="max",
        permission_mode="bypassPermissions",
    )

    assert saved.model == "glm-5.2"
    # A fresh store instance reading the same directory -- i.e. a cold resume
    # in a new wrapper process -- must recover the same selection.
    assert ClaudeControlStore(tmp_path).get(
        "11111111-1111-4111-8111-111111111111").model == "glm-5.2"


def test_failed_model_switch_keeps_the_prior_selection(monkeypatch):
    """Requirement 4: a rejected switch reports failure and does not damage
    the selection already in force."""
    machine, transport = _mk_machine()

    class RejectingHandle:
        is_claude_broker = False
        control_plane_failed = False
        model = "claude-mythos-5"
        effort = "max"
        permission_mode = "bypassPermissions"

        async def set_model(self, _model):
            raise RuntimeError("provider rejected the model id")

    async def go():
        ctx = _mk_ctx("claude-bad-model", "claude-bad-model")
        ctx.engine = "claude"
        ctx.sdk = RejectingHandle()
        ctx.announced_model = ctx.sdk.model
        machine.sessions[ctx.key] = ctx

        async def control_ready(*_args, **_kwargs):
            return None

        machine._runtime_control_preflight = control_ready

        result = await machine._handle_set_model(SetModel(
            sid=ctx.key, model="glm-5.2"))

        assert getattr(result, "code", None) == ERR_INTERNAL
        # The rejected switch must not have damaged what was already in force.
        assert ctx.sdk.model == "claude-mythos-5"
        assert not any(getattr(event, "type", None) == "model"
                       for event in transport.sent)
        assert any(getattr(event, "code", None) == ERR_INTERNAL
                   for event in transport.sent)

    asyncio.run(go())


def test_claude_work_launch_does_not_promote_native_base_model(monkeypatch):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        handle.work_mode = True
        handle.model = "claude-fable-5-1"

        await handle.connect(
            cwd="/tmp", model_override=handle.model,
            _suppress_context_probe=True,
        )

        assert _FakeClaudeClient.created[-1].options.model == (
            "claude-fable-5-1")
        await handle.disconnect()

    asyncio.run(go())


def test_claude_model_switch_invalidates_serialized_context_generation(
    monkeypatch,
):
    class BlockingContext(_FakeClaudeClient):
        block_context = False
        context_started: asyncio.Event
        release_context: asyncio.Event

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            if self.block_context:
                assert timeout == 15.0
                self.context_started.set()
                await self.release_context.wait()
            else:
                assert timeout == 5.0
            return {
                "model": "claude-mythos-5",
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "autoCompactThreshold": 400_000,
                "rawMaxTokens": 500_000,
                "categories": [],
            }

    async def go():
        BlockingContext.created = []
        BlockingContext.context_started = asyncio.Event()
        BlockingContext.release_context = asyncio.Event()
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", BlockingContext)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        client = BlockingContext.created[-1]
        assert handle.cached_context_usage()["maxTokens"] == 500_000
        assert handle.effective_auto_compact_threshold_tokens == 400_000
        assert handle.raw_context_max_tokens == 500_000

        handle.remember_recent_context_usage({"totalTokens": 126_000})
        client.block_context = True
        context_task = asyncio.create_task(handle.get_context_usage())
        await asyncio.wait_for(BlockingContext.context_started.wait(), timeout=1)
        model_task = asyncio.create_task(
            handle.set_model("claude-opus-5[1m]"))
        await asyncio.sleep(0)
        assert client.model_calls == []

        BlockingContext.release_context.set()
        await asyncio.gather(context_task, model_task)
        assert client.model_calls == ["claude-opus-5[1m]"]
        assert handle.model == "claude-opus-5[1m]"
        assert handle.cached_context_usage() is None
        assert handle.cached_recent_context_usage() is None
        assert handle.effective_auto_compact_threshold_tokens is None
        assert handle.raw_context_max_tokens is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_model_event_cannot_overtake_old_context_report():
    class ContextModelHandle:
        is_claude_broker = False
        control_plane_failed = False

        def __init__(self):
            self.model = "claude-mythos-5"

        def cached_context_usage(self):
            return {
                "model": self.model,
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "categories": [],
            }

        def cached_recent_context_usage(self):
            return None

        async def set_model(self, model):
            self.model = model

    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-model-order", "claude-model-order")
        ctx.engine = "claude"
        ctx.sdk = ContextModelHandle()
        ctx.announced_model = ctx.sdk.model
        machine.sessions[ctx.key] = ctx

        async def control_ready(*_args, **_kwargs):
            return None

        machine._runtime_control_preflight = control_ready
        context_emit_started = asyncio.Event()
        release_context_emit = asyncio.Event()
        original_emit = machine._emit

        async def gated_emit(target_ctx, event):
            if isinstance(event, ContextReport):
                context_emit_started.set()
                await release_context_emit.wait()
            return await original_emit(target_ctx, event)

        machine._emit = gated_emit
        context_task = asyncio.create_task(machine._handle_get_context(
            GetContext(sid=ctx.key, refresh=False)))
        await asyncio.wait_for(context_emit_started.wait(), timeout=1)

        model_task = asyncio.create_task(machine._handle_set_model(SetModel(
            sid=ctx.key, model="claude-fable-5-1")))
        await asyncio.sleep(0)
        assert model_task.done() is False
        assert ctx.sdk.model == "claude-mythos-5"

        release_context_emit.set()
        await asyncio.gather(context_task, model_task)
        projected = [
            event for event in transport.sent
            if isinstance(event, (ContextReport, Model))
        ]
        assert [type(event) for event in projected] == [ContextReport, Model]
        assert projected[0].model == "claude-mythos-5"
        assert projected[1].model == "claude-fable-5-1[1m]"

    asyncio.run(go())


def test_claude_sdk_passes_bounded_autocompact_as_a_spawn_option(monkeypatch):
    class ContextClient(_FakeClaudeClient):
        async def get_context_usage(self):
            return {
                "model": self.options.model or "claude-mythos-5",
                "autoCompactThreshold": 250_000,
                "rawMaxTokens": 1_000_000,
            }

    async def go():
        ContextClient.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextClient)
        handle = SdkHandle(WrapperConfig())
        handle.set_auto_compact("custom", 250_000)

        await handle.connect(cwd="/tmp")

        first = ContextClient.created[-1]
        assert first.options.extra_args["autocompact"] == "250000"
        assert handle.applied_auto_compact_mode == "custom"
        assert handle.applied_auto_compact_threshold_tokens == 250_000
        assert handle.effective_auto_compact_threshold_tokens == 250_000
        assert handle.raw_context_max_tokens == 1_000_000

        handle.set_auto_compact("auto")
        assert handle.applied_auto_compact_mode == "custom"
        await handle.force_reconnect(
            None, "/tmp", reason="apply autocompact",
            apply_pending_auto_compact=True)
        assert ContextClient.created[-1].options.extra_args[
            "autocompact"] == "auto"
        assert handle.applied_auto_compact_mode == "auto"

        handle.set_auto_compact("inherit")
        await handle.force_reconnect(
            None, "/tmp", reason="inherit autocompact",
            apply_pending_auto_compact=True)
        assert "autocompact" not in (
            ContextClient.created[-1].options.extra_args or {})
        assert handle.applied_auto_compact_mode == "inherit"
        await handle.disconnect()

    asyncio.run(go())


def test_unrelated_reconnect_does_not_apply_a_pending_smaller_window(
    monkeypatch,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        handle.set_auto_compact("custom", 200_000)

        await handle.force_reconnect(
            None, "/tmp", reason="effort change")

        replacement = _FakeClaudeClient.created[-1]
        assert "autocompact" not in (replacement.options.extra_args or {})
        assert handle.auto_compact_threshold_tokens == 200_000
        assert handle.applied_auto_compact_mode == "inherit"
        assert handle.applied_auto_compact_threshold_tokens is None
        await handle.disconnect()

    asyncio.run(go())


def test_reconnect_uses_immutable_spawn_snapshot_without_losing_newer_choice(
    monkeypatch,
):
    class BlockingDisconnect(_FakeClaudeClient):
        disconnect_started = asyncio.Event()
        release_disconnect = asyncio.Event()

        async def disconnect(self):
            if self is self.created[0]:
                self.disconnect_started.set()
                await self.release_disconnect.wait()
            await super().disconnect()

    async def go():
        BlockingDisconnect.created = []
        BlockingDisconnect.disconnect_started = asyncio.Event()
        BlockingDisconnect.release_disconnect = asyncio.Event()
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", BlockingDisconnect)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        handle.set_auto_compact("custom", 200_000)

        reconnect = asyncio.create_task(handle.force_reconnect(
            None, "/tmp", reason="unrelated reconnect",
        ))
        await asyncio.wait_for(
            BlockingDisconnect.disconnect_started.wait(), timeout=1)

        # This command belongs to the next generation. It must neither alter the
        # argv already selected above nor be overwritten when reconnect returns.
        handle.set_auto_compact("custom", 300_000)
        handle.effort = "high"
        BlockingDisconnect.release_disconnect.set()
        await reconnect

        replacement = BlockingDisconnect.created[-1]
        assert "autocompact" not in (replacement.options.extra_args or {})
        assert replacement.options.effort == "max"
        assert handle.applied_auto_compact_mode == "inherit"
        assert handle.applied_auto_compact_threshold_tokens is None
        assert handle.applied_effort == "max"
        assert handle.auto_compact_threshold_tokens == 300_000
        assert handle.effort == "high"
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


def test_resumed_claude_connect_skips_the_startup_context_probe(
    monkeypatch,
):
    class ProbeTimeout(_FakeClaudeClient):
        probes = 0

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            type(self).probes += 1
            raise Exception("Control request timeout: get_context_usage")

    async def go():
        ProbeTimeout.created = []
        ProbeTimeout.probes = 0
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ProbeTimeout)
        handle = SdkHandle(WrapperConfig())

        await handle.connect(
            resume_id="11111111-1111-4111-8111-111111111111",
            cwd="/tmp",
        )

        assert ProbeTimeout.probes == 0
        assert len(ProbeTimeout.created) == 1
        assert ProbeTimeout.created[0].disconnected is False
        assert handle.client is ProbeTimeout.created[0]
        assert handle.control_plane_failed is False
        assert handle.context_probe_suppressed is False

        await handle.query("继续")
        assert ProbeTimeout.created[0].prompt == "继续"
        await handle.disconnect()

    asyncio.run(go())


def test_fresh_claude_probe_timeout_replaces_generation_once(monkeypatch):
    class ProbeTimeout(_FakeClaudeClient):
        probes = 0

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            assert timeout == 5.0
            type(self).probes += 1
            raise Exception("Control request timeout: get_context_usage")

    async def go():
        ProbeTimeout.created = []
        ProbeTimeout.probes = 0
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ProbeTimeout)
        handle = SdkHandle(WrapperConfig())

        await handle.connect(cwd="/tmp")

        assert ProbeTimeout.probes == 1
        assert len(ProbeTimeout.created) == 2
        assert ProbeTimeout.created[0].disconnected is True
        assert handle.client is ProbeTimeout.created[1]
        assert handle.control_plane_failed is False
        assert handle.context_probe_suppressed is False

        await handle.query("fresh child is ready")
        assert ProbeTimeout.created[1].prompt == "fresh child is ready"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_context_timeout_poisoning_is_generation_scoped(monkeypatch):
    class ContextTimeout(_FakeClaudeClient):
        fail_context = False

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            if self.fail_context:
                assert timeout == 15.0
                raise Exception("Control request timeout: get_context_usage")
            assert timeout in {5.0, 15.0}
            return {"model": "claude-mythos-5", "totalTokens": 123}

    async def go():
        ContextTimeout.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextTimeout)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        first = ContextTimeout.created[-1]
        handle._observe_recent_context_usage(AssistantMessage(
            content=[TextBlock(text="done")],
            model="claude-current",
            usage={
                "input_tokens": 80,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 10,
                "output_tokens": 3,
            },
        ))
        assert handle.cached_recent_context_usage() == {"totalTokens": 98}
        handle._observe_recent_context_usage(AssistantMessage(
            content=[TextBlock(text="child")],
            model="claude-child",
            parent_tool_use_id="agent-tool",
            usage={"input_tokens": 900, "output_tokens": 9},
        ))
        assert handle.cached_recent_context_usage()["totalTokens"] == 98
        handle._record_context_usage({"model": "claude-incomplete"})
        assert handle.cached_recent_context_usage()["totalTokens"] == 98
        first.fail_context = True

        with pytest.raises(Exception, match="Control request timeout"):
            await handle.get_context_usage()
        assert handle.control_plane_failed is True
        assert handle.context_probe_suppressed is False
        with pytest.raises(RuntimeError, match="control plane is unhealthy"):
            await handle.query("must not reach the poisoned child")
        assert not hasattr(first, "prompt")

        await handle.force_reconnect(None, "/tmp", reason="control plane failure")
        replacement = ContextTimeout.created[-1]
        assert replacement is not first
        assert handle.control_plane_failed is False
        assert handle.context_probe_suppressed is False
        assert handle.cached_recent_context_usage() == {"totalTokens": 98}
        assert (await handle.get_context_usage())["totalTokens"] == 123
        assert handle.cached_recent_context_usage() is None
        handle.effective_auto_compact_threshold_tokens = 400_000
        handle.raw_context_max_tokens = 1_000_000
        handle.invalidate_context_usage_cache()
        assert handle.cached_context_usage() is None
        assert handle.cached_recent_context_usage() is None
        assert handle.effective_auto_compact_threshold_tokens is None
        assert handle.raw_context_max_tokens is None
        await handle.query("safe after replacement")
        assert replacement.prompt == "safe after replacement"
        await handle.disconnect()

    asyncio.run(go())


def test_autocompact_reconnect_drops_previous_context_generation(
    monkeypatch,
):
    class ContextTimeout(_FakeClaudeClient):
        fail_context = False

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            if self.fail_context:
                assert timeout == 15.0
                raise Exception("Control request timeout: get_context_usage")
            assert timeout == 5.0
            return {
                "model": "claude-mythos-5[1m]",
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "autoCompactThreshold": 500_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

    async def go():
        ContextTimeout.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextTimeout)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        first = ContextTimeout.created[-1]
        assert handle.cached_context_usage()["maxTokens"] == 500_000
        assert handle.effective_auto_compact_threshold_tokens == 500_000

        first.fail_context = True
        with pytest.raises(Exception, match="Control request timeout"):
            await handle.get_context_usage()

        handle.set_auto_compact("custom", 400_000)
        await handle.force_reconnect(
            None, "/tmp", reason="autocompact setting change",
            apply_pending_auto_compact=True)

        replacement = ContextTimeout.created[-1]
        assert replacement is not first
        assert replacement.options.extra_args["autocompact"] == "400000"
        assert handle.applied_auto_compact_mode == "custom"
        assert handle.applied_auto_compact_threshold_tokens == 400_000
        assert handle.context_probe_suppressed is False
        assert handle.cached_context_usage() is None
        assert handle.effective_auto_compact_threshold_tokens is None
        assert handle.raw_context_max_tokens is None
        await handle.disconnect()

    asyncio.run(go())


def test_claude_context_read_serializes_query_acceptance(monkeypatch):
    class BlockingContext(_FakeClaudeClient):
        block_context = False
        context_started: asyncio.Event
        release_context: asyncio.Event

        async def _send_control_request(self, request, timeout):
            assert request == {"subtype": "get_context_usage"}
            if self.block_context:
                assert timeout == 15.0
                self.context_started.set()
                await self.release_context.wait()
            else:
                assert timeout == 5.0
            return {"model": "claude-mythos-5", "totalTokens": 123}

    async def go():
        BlockingContext.created = []
        BlockingContext.context_started = asyncio.Event()
        BlockingContext.release_context = asyncio.Event()
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", BlockingContext)
        handle = SdkHandle(WrapperConfig())
        await handle.connect(cwd="/tmp")
        client = BlockingContext.created[-1]
        client.block_context = True

        context_task = asyncio.create_task(handle.get_context_usage())
        await asyncio.wait_for(
            BlockingContext.context_started.wait(), timeout=1)
        query_task = asyncio.create_task(handle.query("after context"))
        await asyncio.sleep(0)
        assert not hasattr(client, "prompt")

        BlockingContext.release_context.set()
        await asyncio.gather(context_task, query_task)
        assert client.prompt == "after context"
        await handle.disconnect()

    asyncio.run(go())


def test_claude_work_captures_pre_turn_context_baseline_only_once(
    monkeypatch,
):
    class ContextClient(_FakeClaudeClient):
        totals = iter((1_234, 9_999, 8_888, 777))

        async def get_context_usage(self):
            return {
                "model": self.options.model or "claude-mythos-5",
                "totalTokens": next(self.totals),
            }

    async def go():
        ContextClient.created = []
        ContextClient.totals = iter((1_234, 9_999, 8_888, 777))
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", ContextClient)
        handle = SdkHandle(WrapperConfig())
        handle.work_mode = True
        handle.work_settings_path = "/tmp/cc-remote-work-policy.json"

        await handle.connect(cwd="/tmp")
        assert handle.work_context_baseline_tokens == 1_234

        # Runtime reconnects must not redefine the fixed engine baseline from a
        # later conversation state.
        await handle.force_reconnect(None, "/tmp", reason="baseline regression")
        assert handle.work_context_baseline_tokens == 1_234
        assert len(ContextClient.created) == 2
        await handle.disconnect()

        # A migrated Work session has no trustworthy pre-history baseline.
        # Resume must not relabel its entire existing conversation as fixed
        # engine overhead.
        resumed = SdkHandle(WrapperConfig())
        resumed.work_mode = True
        resumed.work_settings_path = "/tmp/cc-remote-work-policy.json"
        await resumed.connect(resume_id="existing-session", cwd="/tmp")
        assert resumed.work_context_baseline_tokens is None
        await resumed.disconnect()

        code = SdkHandle(WrapperConfig())
        await code.connect(cwd="/tmp")
        assert code.work_context_baseline_tokens is None
        await code.disconnect()

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
        (project / ".claude" / "settings.local.json").write_text(
            '{"model":"claude-mythos-5[1m]",'
            '"env":{"ANTHROPIC_MODEL":"claude-settings-env-model"}}')
        assert machine._claude_configured_model(
            str(project)) == "claude-settings-env-model"

        monkeypatch.setenv("ANTHROPIC_MODEL", "default")
        (project / ".claude" / "settings.local.json").write_text(
            '{"model":"default"}')
        assert machine._claude_configured_model(str(project)) is None
        fallback_model, fallback_effort = (
            await machine._claude_new_session_defaults(str(project)))
        assert fallback_model == CLAUDE_DEFAULT_MODEL
        assert fallback_effort == "max"

        monkeypatch.delenv("ANTHROPIC_MODEL")
        for configured, expected in (
            ("opus", CLAUDE_DEFAULT_MODEL),
            ("opus[1m]", CLAUDE_DEFAULT_MODEL),
            ("claude-opus-5", CLAUDE_DEFAULT_MODEL),
            (CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL),
            ("claude-fable-5-1", "claude-fable-5-1[1m]"),
            ("claude-mythos-5-1", "claude-mythos-5-1[1m]"),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("provider-custom-model", "provider-custom-model"),
        ):
            (project / ".claude" / "settings.local.json").write_text(
                f'{{"model":"{configured}"}}')
            resolved, _ = await machine._claude_new_session_defaults(
                str(project))
            assert resolved == expected

        managed = tmp_path / "managed-settings.json"
        managed.write_text('{"model":"claude-managed-model"}')
        monkeypatch.setattr(
            machine_module.WrapperMachine, "_claude_managed_settings_paths",
            staticmethod(lambda: [str(managed)]))
        assert machine._claude_configured_model(
            str(project)) == "claude-managed-model"

    asyncio.run(go())


def test_claude_multi_profile_default_ignores_project_and_ambient_models(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "profile"
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".claude").mkdir()
    profile.mkdir()
    profile_settings = profile / "settings.json"
    project_settings = project / ".claude" / "settings.json"
    local_settings = project / ".claude" / "settings.local.json"
    profile_settings.write_text(
        '{"model":"profile-model"}', encoding="utf-8")
    project_settings.write_text(
        '{"model":"project-model"}', encoding="utf-8")
    local_settings.write_text(
        '{"env":{"ANTHROPIC_MODEL":"local-env-model"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("ANTHROPIC_MODEL", "ambient-model")
    monkeypatch.setattr(
        machine_module.WrapperMachine,
        "_claude_managed_settings_paths",
        staticmethod(lambda: []),
    )

    assert machine_module.WrapperMachine._claude_configured_model(
        str(project),
        config_dir=str(profile),
        isolate_account_env=True,
    ) == "profile-model"

    profile_settings.write_text("{}", encoding="utf-8")
    assert machine_module.WrapperMachine._claude_configured_model(
        str(project),
        config_dir=str(profile),
        isolate_account_env=True,
    ) is None


def test_fresh_claude_spawn_applies_the_resolved_default_model(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    project.mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(
        machine_module.WrapperMachine, "_claude_managed_settings_paths",
        staticmethod(lambda: []))

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            resume_id=None, cwd=str(project), engine="claude")

        assert ctx is not None
        assert ctx.sdk.model == CLAUDE_DEFAULT_MODEL
        assert _FakeClaudeClient.created[-1].model_calls == [
            CLAUDE_DEFAULT_MODEL]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_explicit_fresh_claude_model_wins_without_reading_default(
    monkeypatch,
    tmp_path,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("an explicit model must bypass default lookup")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=None,
            cwd=str(tmp_path),
            engine="claude",
            model="claude-sonnet-5",
        )

        assert ctx is not None
        assert ctx.sdk.model == "claude-sonnet-5"
        assert _FakeClaudeClient.created[-1].model_calls == [
            "claude-sonnet-5"]
        await ctx.sdk.disconnect()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("opus", CLAUDE_DEFAULT_MODEL),
        ("opus[1m]", CLAUDE_DEFAULT_MODEL),
        ("claude-opus-5", CLAUDE_DEFAULT_MODEL),
        (CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL),
        ("claude-fable-5-1", "claude-fable-5-1[1m]"),
        ("claude-mythos-5-1", "claude-mythos-5-1[1m]"),
        ("claude-sonnet-5", "claude-sonnet-5"),
        ("provider-custom-model", "provider-custom-model"),
    ],
)
def test_explicit_new_session_normalizes_curated_1m_models(
    monkeypatch,
    tmp_path,
    requested,
    expected,
):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, _ = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def forbidden_default(_cwd):
            raise AssertionError("an explicit model must bypass default lookup")

        machine._claude_new_session_defaults = forbidden_default
        await machine._handle_new_session(NewSession(
            request_id="explicit-alias",
            cwd=str(tmp_path),
            model=requested,
        ))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model == expected
        assert _FakeClaudeClient.created[-1].model_calls == [expected]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_implicit_claude_default_failure_reports_probed_provider_model(
    monkeypatch,
    tmp_path,
):
    class RejectingModelClient(_FakeClaudeClient):
        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("provider rejected curated model")

    async def go():
        RejectingModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", RejectingModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setattr(
            machine_module.WrapperMachine,
            "_claude_managed_settings_paths",
            staticmethod(lambda: []),
        )
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="implicit-default", cwd=str(tmp_path)))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model == "claude-mythos-5"
        assert ctx.announced_model == "claude-mythos-5"
        assert [
            event.model for event in transport.sent if event.type == "model"
        ] == ["claude-mythos-5"]
        assert RejectingModelClient.created[-1].model_calls == [
            CLAUDE_DEFAULT_MODEL]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_implicit_claude_default_and_probe_failure_emit_no_fake_model(
    monkeypatch,
    tmp_path,
):
    class UnreportedModelClient(_FakeClaudeClient):
        async def _send_control_request(self, request, timeout):
            raise RuntimeError("model probe unavailable")

        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("provider rejected curated model")

    async def go():
        UnreportedModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", UnreportedModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setattr(
            machine_module.WrapperMachine,
            "_claude_managed_settings_paths",
            staticmethod(lambda: []),
        )
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="implicit-unreported", cwd=str(tmp_path)))

        assert len(machine.sessions) == 1
        ctx = next(iter(machine.sessions.values()))
        assert ctx.sdk.model is None
        assert ctx.announced_model is None
        assert not [event for event in transport.sent
                    if event.type == "model"]
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_explicit_claude_model_failure_is_one_correlated_create_error(
    monkeypatch,
    tmp_path,
):
    class RejectingModelClient(_FakeClaudeClient):
        async def set_model(self, model):
            self.model_calls.append(model)
            raise RuntimeError("explicit model rejected")

    async def go():
        RejectingModelClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", RejectingModelClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        machine, transport = _mk_machine()
        machine._load_history = lambda *_args: asyncio.sleep(0)

        await machine._handle_new_session(NewSession(
            request_id="explicit-model",
            client_id="browser-one",
            cwd=str(tmp_path),
            model="claude-sonnet-5",
        ))

        assert machine.sessions == {}
        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert error.type == "error"
        assert error.to == "browser-one"
        assert error.request_id == "explicit-model"
        assert error.sid is None
        assert RejectingModelClient.created[-1].disconnected is True

    asyncio.run(go())


def test_claude_code_resume_without_override_never_reads_fresh_default(
    monkeypatch,
    tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        machine, _ = _mk_machine()
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        monkeypatch.setattr(
            machine_module,
            "last_completed_assistant_controls",
            lambda *_args, **_kwargs: ClaudeControls(),
        )

        async def forbidden_default(_cwd):
            raise AssertionError("resume must not resolve a fresh default")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=session_id, engine="claude", space="code")

        assert ctx is not None
        assert _FakeClaudeClient.created[-1].model_calls == []
        assert _FakeClaudeClient.created[-1].options.model is None
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_claude_code_resume_recovers_native_curated_model_before_history(
    monkeypatch,
    tmp_path,
):
    session_id = "11111111-1111-4111-8111-333333333333"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        recovered = []

        def native_controls(native_id, **kwargs):
            recovered.append((native_id, kwargs))
            return ClaudeControls(model="claude-fable-5-1")

        monkeypatch.setattr(
            machine_module,
            "last_completed_assistant_controls",
            native_controls,
        )
        machine, _ = _mk_machine()
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            resume_id=session_id, engine="claude", space="code")

        assert ctx is not None
        assert recovered[0][0] == session_id
        assert recovered[0][1]["directory"] == str(tmp_path)
        assert _FakeClaudeClient.created[-1].model_calls == [
            "claude-fable-5-1[1m]",
        ]
        assert ctx.sdk.model == "claude-fable-5-1[1m]"
        assert machine._claude_controls.get(session_id).model == (
            "claude-fable-5-1[1m]"
        )
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_claude_code_resume_migrates_saved_curated_model_to_native_1m_marker(
    monkeypatch,
    tmp_path,
):
    session_id = "11111111-1111-4111-8111-222222222222"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        machine, _ = _mk_machine()
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        async def saved_controls(_sid):
            return ClaudeControls(model="claude-fable-5-1")

        machine._load_claude_session_controls = saved_controls
        ctx = await machine._spawn(
            resume_id=session_id, engine="claude", space="code")

        assert ctx is not None
        assert _FakeClaudeClient.created[-1].model_calls == [
            "claude-fable-5-1[1m]",
        ]
        assert ctx.sdk.model == "claude-fable-5-1[1m]"
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_claude_work_resume_never_applies_code_fresh_default(
    monkeypatch,
    tmp_path,
):
    session_id = "22222222-2222-4222-8222-222222222222"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=record.cwd))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        machine, _ = _mk_machine()
        machine._work = WorkStores(
            tmp_path / "work-claude", tmp_path / "work-codex")
        store = machine._work.for_engine("claude")
        record = store.create_session()
        store.bind_session(record.work_id, session_id)
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        def forbidden_native_controls(*_args, **_kwargs):
            raise AssertionError(
                "Work resume must not import Code transcript controls")

        monkeypatch.setattr(
            machine_module,
            "last_completed_assistant_controls",
            forbidden_native_controls,
        )

        async def forbidden_default(_cwd):
            raise AssertionError("Work resume must preserve its native model")

        machine._claude_new_session_defaults = forbidden_default
        ctx = await machine._spawn(
            resume_id=session_id,
            engine="claude",
            space="work",
            work_id=record.work_id,
        )

        assert ctx is not None
        assert _FakeClaudeClient.created[-1].model_calls == []
        assert _FakeClaudeClient.created[-1].options.model is None
        await ctx.sdk.disconnect()

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


def test_cold_claude_resume_restores_private_remote_controls(
    monkeypatch, tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)

        machine, _ = _mk_machine()
        machine._claude_controls.update(
            session_id,
            model="claude-opus-4-6[1m]",
            effort="high",
            permission_mode="plan",
        )
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            session_id, engine="claude", space="code")

        assert ctx is not None
        assert ctx.sdk.model == "claude-opus-4-6[1m]"
        assert ctx.sdk.effort == "high"
        assert ctx.sdk.applied_effort == "high"
        assert ctx.sdk.permission_mode == "plan"
        client = _FakeClaudeClient.created[-1]
        assert client.options.permission_mode == "plan"
        assert client.options.effort == "high"
        assert client.model_calls == ["claude-opus-4-6[1m]"]
        assert machine._claude_controls.get(session_id) == ClaudeControls(
            model="claude-opus-4-6[1m]",
            effort="high",
            permission_mode="plan",
            applied_auto_compact_mode="inherit",
            applied_auto_compact_threshold_tokens=None,
        )
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_cold_resume_keeps_pending_lower_window_out_of_launch(
    monkeypatch, tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)

        machine, _ = _mk_machine()
        machine._claude_controls.update(
            session_id,
            model=None,
            effort=None,
            permission_mode=None,
            auto_compact_mode="custom",
            auto_compact_threshold_tokens=300_000,
            applied_auto_compact_mode="custom",
            applied_auto_compact_threshold_tokens=800_000,
        )
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            session_id, engine="claude", space="code")

        assert ctx is not None
        client = _FakeClaudeClient.created[-1]
        assert client.options.extra_args["autocompact"] == "800000"
        assert ctx.sdk.auto_compact_threshold_tokens == 300_000
        assert ctx.sdk.applied_auto_compact_threshold_tokens == 800_000
        assert machine._claude_auto_compact_event(ctx).pending is True
        await ctx.sdk.disconnect()

    asyncio.run(go())


def test_cold_resume_without_control_record_uses_native_autocompact(
    monkeypatch, tmp_path,
):
    session_id = "11111111-1111-4111-8111-111111111111"

    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(
            sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        monkeypatch.setattr(
            SdkHandle, "preflight", staticmethod(lambda _path: None))
        monkeypatch.setattr(
            SdkHandle, "refresh_goal", lambda *_args: asyncio.sleep(0))
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd=str(tmp_path)))
        monkeypatch.setattr(
            machine_module, "save_session_id", lambda *_args: None)

        machine, _ = _mk_machine()
        machine._watch_session = lambda _sid: None
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            session_id, engine="claude", space="code")

        assert ctx is not None
        client = _FakeClaudeClient.created[-1]
        assert "autocompact" not in (client.options.extra_args or {})
        assert ctx.sdk.auto_compact_mode == "inherit"
        assert ctx.sdk.auto_compact_threshold_tokens is None
        assert ctx.sdk.applied_auto_compact_mode == "inherit"
        assert ctx.sdk.applied_auto_compact_threshold_tokens is None
        event = machine._claude_auto_compact_event(ctx)
        assert event.pending is False
        assert event.phase == "stable"
        saved = machine._claude_controls.get(session_id)
        assert saved.auto_compact_mode == "inherit"
        assert saved.auto_compact_threshold_tokens is None
        assert saved.applied_auto_compact_mode == "inherit"
        assert saved.applied_auto_compact_threshold_tokens is None
        await ctx.sdk.disconnect()

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
            "session_focus", "background_process_sync", "session_control",
            "completion_state", "perm",
            "model", "effort", "auto_compact", "state"]
        assert result[1].items == []
        assert result[4].mode == "default"
        assert result[5].model == "claude-sonnet-5"
        assert result[6].effort == "high"
        assert result[7].mode == "inherit"
        assert result[8].state == "idle"

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
            self.connected_effort = None

        async def connect(self, **_kwargs):
            self.connected_permission = self.permission_mode
            self.connected_effort = self.effort

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
        assert fork.sdk.connected_effort == "xhigh"

    asyncio.run(go())


def test_claude_btw_launches_with_parent_applied_autocompact(monkeypatch):
    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg):
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.auto_compact_mode = "custom"
            self.auto_compact_threshold_tokens = 500_000
            self.connected_auto_compact = None

        def set_auto_compact(self, mode, threshold):
            self.auto_compact_mode = mode
            self.auto_compact_threshold_tokens = threshold

        async def connect(self, **_kwargs):
            self.connected_auto_compact = (
                self.auto_compact_mode,
                self.auto_compact_threshold_tokens,
            )

        async def disconnect(self):
            return None

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)
        machine, _ = _mk_machine()
        parent = _mk_ctx("parent-1", "parent-1")
        parent.sdk = SimpleNamespace(
            permission_mode="plan",
            auto_compact_mode="custom",
            auto_compact_threshold_tokens=300_000,
            applied_auto_compact_mode="custom",
            applied_auto_compact_threshold_tokens=800_000,
        )
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        assert fork.sdk.connected_auto_compact == ("custom", 800_000)

    asyncio.run(go())


def test_claude_work_btw_reuses_registered_policy_and_work_identity(monkeypatch):
    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg):
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.work_mode = False
            self.work_settings_path = None
            self.connected = None

        async def connect(self, **kwargs):
            self.connected = kwargs

        async def disconnect(self):
            return None

    async def go():
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)
        machine, _ = _mk_machine()
        store = machine._work.for_engine("claude")
        record = store.create_session()
        store.bind_session(record.work_id, "parent-work")
        parent = _mk_ctx("parent-work", "parent-work")
        parent.cwd = record.cwd
        parent.space = "work"
        parent.work_id = record.work_id
        parent.sdk = SimpleNamespace(permission_mode="bypassPermissions")
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        assert fork.space == "work"
        assert fork.work_id == record.work_id
        assert fork.sdk.work_mode is True
        assert fork.sdk.permission_mode == "acceptEdits"
        assert fork.sdk.work_settings_path.endswith(f"{record.work_id}.json")
        assert fork.sdk.connected == {
            "resume_id": "parent-work", "cwd": record.cwd, "fork": True,
        }

    asyncio.run(go())


def test_claude_profile_work_btw_keeps_config_and_policy_account(
    monkeypatch, tmp_path,
):
    created = []

    class FakeHandle:
        @staticmethod
        def preflight(_path):
            return None

        def __init__(self, _cfg, **kwargs):
            self.init = kwargs
            self.permission_mode = "bypassPermissions"
            self.effort = "max"
            self.work_mode = False
            self.work_settings_path = None
            self.connected = None
            created.append(self)

        async def connect(self, **kwargs):
            self.connected = kwargs

        async def disconnect(self):
            return None

    async def go():
        personal = tmp_path / "personal"
        company = tmp_path / "company"
        personal.mkdir()
        company.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal))
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.claude_profiles_json = json.dumps({
            "personal": {
                "label": "Personal",
                "config_dir": str(personal),
                "default": True,
            },
            "company": {
                "label": "Company",
                "config_dir": str(company),
            },
        })
        machine, transport = _mk_machine()
        machine = machine_module.WrapperMachine(cfg, transport)
        monkeypatch.setattr(machine_module, "SdkHandle", FakeHandle)

        store = machine._work.for_engine("claude")
        record = store.create_session(claude_profile_id="company")
        store.bind_session(
            record.work_id, "parent-work", claude_profile_id="company")
        observed_policy_roots = []
        real_policy = store.ensure_claude_policy

        def tracked_policy(work_record, *, claude_config_dir=None):
            observed_policy_roots.append(claude_config_dir)
            return real_policy(
                work_record, claude_config_dir=claude_config_dir)

        monkeypatch.setattr(store, "ensure_claude_policy", tracked_policy)
        parent = _mk_ctx("company@parent-work", "parent-work")
        parent.claude_profile_id = "company"
        parent.cwd = record.cwd
        parent.space = "work"
        parent.work_id = record.work_id
        parent.sdk = SimpleNamespace(permission_mode="bypassPermissions")
        machine.sessions[parent.key] = parent

        fork = await machine._spawn_btw(
            parent, owner_client_id="client-1")

        handle = created[-1]
        assert handle.init == {
            "claude_config_dir": str(company.resolve()),
            "isolate_account_env": True,
        }
        assert observed_policy_roots == [str(company.resolve())]
        assert fork.claude_profile_id == "company"
        assert fork.sdk.permission_mode == "acceptEdits"

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
            fork.owner_client_id = owner_client_id
            machine.sessions[fork.key] = fork
            return fork

        machine._spawn_btw = spawn
        result = await machine._handle_open_btw(OpenBtw(
            sid="parent-1",
            request_id="request-1",
            client_id="client-1",
        ))

        assert [event.type for event in result] == [
            "btw_opened", "snapshot", "auto_compact", "perm"]
        perm = result[-1]
        assert perm.mode == "plan"
        assert perm.sid == "btw-1" and perm.to == "client-1"
        assert transport.sent[-1] is perm

    asyncio.run(go())
