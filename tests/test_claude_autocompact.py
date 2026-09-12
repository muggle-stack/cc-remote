"""Claude per-session automatic-compaction lifecycle regressions."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from cc_remote.protocol import (
    CompactSession,
    ContextReport,
    Delta,
    Error,
    GetContext,
    Query,
    ProcessEvent,
    SetAutoCompact,
    ToolResult,
    ToolUse,
    TurnEnd,
    UserMsg,
)
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.claude_errors import (
    classify_provider_request_too_large,
    is_empty_system_content_error,
    is_provider_request_too_large,
)
from cc_remote.wrapper.stream import StreamTranslator
from tests.test_multisession import _mk_ctx, _mk_machine


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def test_nested_provider_413_is_classified_without_retrying_other_errors():
    outer = RuntimeError("Claude request failed")
    outer.__cause__ = RuntimeError(
        "输入Tokens数量(1249708)超过系统限制(1000000)")
    assert classify_provider_request_too_large(outer) == "context"
    assert is_provider_request_too_large(outer) is True
    assert classify_provider_request_too_large(
        RuntimeError("payload too large"),
    ) == "request"
    assert classify_provider_request_too_large(
        RuntimeError("request failed"), status_code=413,
    ) == "request"
    assert is_provider_request_too_large(RuntimeError("temporary 503")) is False


def test_nested_context_overflow_takes_precedence_over_outer_generic_413():
    outer = RuntimeError("status_code=413, payload too large")
    outer.__cause__ = RuntimeError(
        "input token count exceeds the maximum allowed tokens")
    assert classify_provider_request_too_large(outer) == "context"


def test_empty_system_content_400_is_not_mislabeled_as_context_overflow():
    message = "API Error: 400 messages.1: system content must contain at least one block"
    assert is_empty_system_content_error(message)
    assert is_empty_system_content_error("system content must contain at least one block", 400)
    assert not is_empty_system_content_error("API Error: 400 invalid token")
    assert classify_provider_request_too_large(message) is None


class _AutoCompactSdk:
    model = "claude-mythos-5[1m]"
    effort = "max"
    applied_effort = "max"
    permission_mode = "bypassPermissions"
    is_claude_broker = False

    def __init__(
        self,
        *,
        fail_first_reconnect: bool = False,
        context_total: int | None = 0,
    ):
        self.auto_compact_mode = "inherit"
        self.auto_compact_threshold_tokens = None
        self.applied_auto_compact_mode = "inherit"
        self.applied_auto_compact_threshold_tokens = None
        self.fail_first_reconnect = fail_first_reconnect
        self.context_total = context_total
        self.reconnects: list[tuple[str, int | None, dict]] = []
        self.disconnected = 0

    def cached_recent_context_usage(self):
        if self.context_total is None:
            return None
        return {"totalTokens": self.context_total}

    def set_auto_compact(
        self, mode: str, threshold_tokens: int | None = None,
    ) -> None:
        self.auto_compact_mode = mode
        self.auto_compact_threshold_tokens = threshold_tokens

    async def force_reconnect(self, **kwargs) -> None:
        launch = kwargs.get("launch_auto_compact")
        if launch is None:
            launch = (
                self.auto_compact_mode,
                self.auto_compact_threshold_tokens,
            )
        self.reconnects.append((
            launch[0],
            launch[1],
            kwargs,
        ))
        if self.fail_first_reconnect and len(self.reconnects) == 1:
            raise RuntimeError("new launch failed")
        self.applied_auto_compact_mode = launch[0]
        self.applied_auto_compact_threshold_tokens = launch[1]

    async def disconnect(self) -> None:
        self.disconnected += 1

    def observe_goal_message(self, _message, _thread_id):
        return False, None


def _machine_with_sdk(sdk: object):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(SESSION_ID, SESSION_ID)
    ctx.engine = "claude"
    ctx.sdk = sdk
    machine.sessions[ctx.key] = ctx

    async def ready(_ctx, **_kwargs):
        return None

    async def no_external_owner(_sid):
        return False

    machine._runtime_control_preflight = ready
    machine._prime_claude_ownership = no_external_owner
    return machine, transport, ctx


def test_claude_reconnect_identity_preserves_private_btw_context():
    machine, _transport = _mk_machine()
    parent = _mk_ctx("parent-key", "parent-session")
    machine.sessions[parent.key] = parent
    btw = _mk_ctx("btw-key")
    btw.engine = "claude"
    btw.btw = True
    btw.parent_sid = parent.key

    assert machine._claude_reconnect_identity(btw) == (
        "parent-session", True)

    btw.btw_real_id = "btw-native-session"
    assert machine._claude_reconnect_identity(btw) == (
        "btw-native-session", False)


def test_idle_autocompact_change_reconnects_and_persists_exact_session():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=250_000,
        ))

        assert event.type == "auto_compact"
        assert event.pending is False
        assert event.mode == event.applied_mode == "custom"
        assert event.threshold_tokens == event.applied_threshold_tokens == 250_000
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 250_000),
        ]
        assert sdk.reconnects[0][2] == {
            "resume_id": SESSION_ID,
            "cwd": ctx.cwd,
            "reason": "autocompact setting change",
            "fork": False,
            "apply_pending_auto_compact": True,
        }
        saved = machine._claude_controls.get(SESSION_ID)
        assert saved.auto_compact_mode == "custom"
        assert saved.auto_compact_threshold_tokens == 250_000

    asyncio.run(run())


def test_applied_autocompact_publishes_new_generation_cached_context():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0
            self.context_usage = {
                "totalTokens": 125_000,
                "maxTokens": 500_000,
                "percentage": 25.0,
                "model": self.model,
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 500_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

        async def force_reconnect(self, **kwargs) -> None:
            await super().force_reconnect(**kwargs)
            self.context_usage = {
                **self.context_usage,
                "maxTokens": 400_000,
                "percentage": 31.25,
                "autoCompactThreshold": 400_000,
            }

        def cached_context_usage(self):
            return dict(self.context_usage)

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("autocompact apply must reuse the startup cache")

    async def run():
        sdk = ContextSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))

        assert event.pending is False
        published = [
            item for item in transport.sent
            if item.type in {"auto_compact", "context_report"}
        ]
        assert [item.type for item in published[-2:]] == [
            "auto_compact", "context_report",
        ]
        report = published[-1]
        assert report.max_tokens == 400_000
        assert report.auto_compact_threshold_tokens == 400_000
        assert report.raw_max_tokens == 1_000_000
        assert sdk.context_calls == 0

    asyncio.run(run())


def test_applied_autocompact_without_cache_reports_unavailable():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = True

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("cached-only publish must not receive a probe")

    async def run():
        sdk = ContextSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))

        assert event.pending is False
        published = [
            item for item in transport.sent
            if item.type in {"auto_compact", "context_report"}
        ]
        assert [item.type for item in published[-2:]] == [
            "auto_compact", "context_report",
        ]
        report = published[-1]
        assert report.available is False
        assert report.total_tokens == 0
        assert report.max_tokens == 0
        assert sdk.context_calls == 0

    asyncio.run(run())


def test_busy_autocompact_change_waits_for_real_terminal_boundary():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="auto",
        ))

        assert pending.pending is True
        assert sdk.reconnects == []
        assert ctx.state == "running"

        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)

        assert ctx.state == "idle"
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("auto", None),
        ]
        final = machine._claude_auto_compact_event(ctx)
        assert final.pending is False
        assert final.applied_mode == "auto"

    asyncio.run(run())


def test_ambiguous_stream_failure_does_not_apply_pending_autocompact():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=400_000,
        ))
        assert pending.pending is True

        # The generic stream-failure path has no ResultMessage proof.  It may
        # unlock the UI according to the established recovery contract, but it
        # must not disconnect a child which could still be executing upstream.
        await machine._set_idle_after_managed_turn(ctx)

        assert ctx.state == "idle"
        assert sdk.reconnects == []
        assert machine._claude_auto_compact_event(ctx).pending is True

    asyncio.run(run())


def test_running_agent_defers_idle_reconnect_until_agent_finishes():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        run_state = SimpleNamespace(status="running")
        ctx.claude_agents = SimpleNamespace(runs={"agent-1": run_state})

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))

        assert pending.pending is True
        assert sdk.reconnects == []

        machine._schedule_pending_claude_auto_compact(ctx)
        assert ctx.auto_compact_apply_task is None

        run_state.status = "succeeded"
        machine._schedule_pending_claude_auto_compact(ctx)
        task = ctx.auto_compact_apply_task
        assert task is not None
        await asyncio.wait_for(asyncio.shield(task), timeout=1)

        assert sdk.applied_auto_compact_mode == "custom"
        assert sdk.applied_auto_compact_threshold_tokens == 500_000
        assert machine._claude_auto_compact_event(ctx).pending is False

    asyncio.run(run())


def test_failed_change_rolls_back_live_child_but_keeps_desired_value_pending():
    async def run():
        sdk = _AutoCompactSdk(fail_first_reconnect=True)
        machine, _transport, ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=200_000,
        ))

        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 200_000),
            ("inherit", None),
        ]
        assert sdk.auto_compact_mode == "custom"
        assert sdk.auto_compact_threshold_tokens == 200_000
        assert sdk.applied_auto_compact_mode == "inherit"
        assert event.pending is True
        assert event.phase == "blocked"
        assert event.error and "恢复上一次可用设置" in event.error
        saved = machine._claude_controls.get(SESSION_ID)
        assert saved.auto_compact_mode == "custom"
        assert saved.auto_compact_threshold_tokens == 200_000
        assert ctx.state == "idle"

    asyncio.run(run())


class _CompactingAutoCompactSdk(_AutoCompactSdk):
    def __init__(self, *, context_total: int | None = 600_000, boundary=True):
        super().__init__(context_total=context_total)
        self.auto_compact_mode = "custom"
        self.auto_compact_threshold_tokens = 800_000
        self.applied_auto_compact_mode = "custom"
        self.applied_auto_compact_threshold_tokens = 800_000
        self.boundary = boundary
        self.queries: list[str] = []
        self.next_turn_id = None
        self.context_invalidations = 0

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        if self.boundary:
            yield SystemMessage(
                subtype="compact_boundary",
                data={
                    "type": "system",
                    "subtype": "compact_boundary",
                    "uuid": "compact-boundary",
                    "timestamp": "2026-09-03T01:02:03.000Z",
                    "compactMetadata": {
                        "trigger": "manual",
                        "preTokens": 600_000,
                        "postTokens": 8_000,
                        "durationMs": 25,
                    },
                },
            )
        yield ResultMessage(
            subtype="success",
            duration_ms=25,
            duration_api_ms=20,
            is_error=False,
            num_turns=1,
            session_id=SESSION_ID,
        )

    def release_background_messages(self):
        return None

    def invalidate_context_usage_cache(self):
        self.context_invalidations += 1
        self.context_total = 8_000


def test_lowering_window_compacts_before_reconnecting_with_new_threshold():
    async def run():
        sdk = _CompactingAutoCompactSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))

        assert sdk.queries == ["/compact"]
        assert sdk.context_invalidations == 1
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 500_000),
        ]
        assert event.pending is False
        assert event.phase == "stable"
        compactions = [
            item for item in transport.sent
            if isinstance(item, ProcessEvent) and item.kind == "compaction"
        ]
        assert len(compactions) == 1
        assert compactions[0].item_id == "compact-boundary"

    asyncio.run(run())


def test_manual_compact_does_not_repeat_background_compact_while_waiting():
    async def run():
        sdk = _CompactingAutoCompactSdk(context_total=8_000)
        sdk.auto_compact_threshold_tokens = 800_000
        machine, _transport, ctx = _machine_with_sdk(sdk)
        context_resolved = asyncio.Event()

        async def resolve_context(_cmd, _action):
            context_resolved.set()
            return ctx

        machine._claude_code_context = resolve_context
        await ctx.query_lock.acquire()
        keep_background_active = asyncio.Event()
        ctx.auto_compact_apply_started_revision = 0
        background = asyncio.create_task(keep_background_active.wait())
        ctx.auto_compact_apply_task = background
        # The scheduler has already finished the native compact but is still
        # reconnecting before it can release query_lock.
        ctx.claude_compaction_revision = 1
        command = asyncio.create_task(machine._handle_compact_session(
            CompactSession(session_id=SESSION_ID, engine="claude"),
        ))
        await asyncio.wait_for(context_resolved.wait(), timeout=1)
        await asyncio.sleep(0)

        ctx.query_lock.release()
        result = await command

        assert sdk.queries == []
        assert result.title == "上下文压缩完成"
        assert "后台维护" in result.message
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)

    asyncio.run(run())


def test_lowering_uses_recent_turn_usage_over_stale_context_cache():
    class StaleControlCacheSdk(_CompactingAutoCompactSdk):
        def cached_context_usage(self):
            return {"totalTokens": 250_000}

        def cached_recent_context_usage(self):
            return {"totalTokens": 350_000}

    async def run():
        sdk = StaleControlCacheSdk(context_total=350_000)
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=300_000,
        ))

        assert sdk.queries == ["/compact"]
        assert sdk.reconnects[0][0:2] == ("custom", 300_000)
        assert event.pending is False

    asyncio.run(run())


def test_lowering_never_reconnects_without_a_real_compact_boundary():
    async def run():
        sdk = _CompactingAutoCompactSdk(boundary=False)
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))

        assert sdk.queries == ["/compact"]
        assert sdk.context_invalidations == 0
        assert sdk.reconnects == []
        assert event.pending is True
        assert event.phase == "blocked"
        assert event.applied_threshold_tokens == 800_000

    asyncio.run(run())


def test_unknown_auto_target_compacts_nontrivial_context_before_reconnect():
    async def run():
        sdk = _CompactingAutoCompactSdk(context_total=200_000)
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="auto",
        ))

        assert sdk.queries == ["/compact"]
        assert sdk.reconnects[0][0:2] == ("auto", None)
        assert event.pending is False
        assert event.applied_mode == "auto"

    asyncio.run(run())


def test_unknown_legacy_window_compacts_once_before_adopting_default():
    async def run():
        sdk = _CompactingAutoCompactSdk(context_total=None)
        sdk.auto_compact_mode = "custom"
        sdk.auto_compact_threshold_tokens = 500_000
        sdk.applied_auto_compact_mode = "inherit"
        sdk.applied_auto_compact_threshold_tokens = None
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        event, applied = await machine._apply_pending_claude_auto_compact(
            _ctx, reason="legacy default migration",
        )

        assert sdk.queries == ["/compact"]
        assert sdk.reconnects[0][0:2] == ("custom", 500_000)
        assert applied is True
        assert event.pending is False

    asyncio.run(run())


def test_external_growth_reloads_under_applied_window_before_lowering():
    class ReloadingSdk(_CompactingAutoCompactSdk):
        def invalidate_context_usage_cache(self):
            self.context_total = None

    async def run():
        sdk = ReloadingSdk(context_total=100_000)
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.needs_reload = True

        event = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))

        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 800_000),
            ("custom", 500_000),
        ]
        assert sdk.reconnects[0][2]["preserve_model"] is True
        assert sdk.reconnects[1][2]["apply_pending_auto_compact"] is True
        assert sdk.queries == ["/compact"]
        assert event.pending is False
        assert ctx.needs_reload is False

    asyncio.run(run())


def test_persistent_fork_inherits_desired_and_applied_windows_separately():
    async def run():
        sdk = _AutoCompactSdk()
        sdk.auto_compact_mode = "custom"
        sdk.auto_compact_threshold_tokens = 300_000
        sdk.applied_auto_compact_mode = "custom"
        sdk.applied_auto_compact_threshold_tokens = 800_000
        machine, _transport, ctx = _machine_with_sdk(sdk)

        controls = await machine._claude_fork_control_snapshot(
            SESSION_ID, ctx.cwd, ctx)
        assert controls["auto_compact_threshold_tokens"] == 300_000
        assert controls["applied_auto_compact_threshold_tokens"] == 800_000

        child_id = "22222222-2222-4222-8222-222222222222"
        await machine._inherit_claude_fork_controls(child_id, controls)
        child = machine._claude_controls.get(child_id)
        assert child.auto_compact_threshold_tokens == 300_000
        assert child.applied_auto_compact_threshold_tokens == 800_000
        assert "auto_compact_compaction_done" not in child.as_dict()

    asyncio.run(run())


def test_broker_owned_autocompact_is_observed_but_never_hot_switched():
    async def run():
        broker = SimpleNamespace(
            is_claude_broker=True,
            auto_compact_mode="custom",
            auto_compact_threshold_tokens=300_000,
            applied_auto_compact_mode="custom",
            applied_auto_compact_threshold_tokens=300_000,
            permission_mode="default",
        )
        machine, _transport, _ctx = _machine_with_sdk(broker)

        result = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="auto",
            cmd_id="command-1",
            client_id="client-1",
        ))

        event, error = result
        assert event.mutable is False
        assert event.mode == event.applied_mode == "custom"
        assert event.threshold_tokens == 300_000
        assert error.code == "auth"
        assert broker.auto_compact_mode == "custom"

    asyncio.run(run())


def test_context_report_keeps_effective_threshold_separate_from_raw_window():
    class ContextSdk(_AutoCompactSdk):
        async def get_context_usage(self):
            return {
                "totalTokens": 123_456,
                "maxTokens": 200_000,
                "percentage": 61.728,
                "model": "claude-mythos-5[1m]",
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 200_000,
                "rawMaxTokens": 1_000_000,
                "categories": [],
            }

    async def run():
        machine, _transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True))

        assert report.max_tokens == 200_000
        assert report.auto_compact_threshold_tokens == 200_000
        assert report.raw_max_tokens == 1_000_000
        assert report.is_auto_compact_enabled is True
        assert report.source == "control"

    asyncio.run(run())


def test_automatic_claude_context_report_uses_cache_without_control_rpc():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return {
                "totalTokens": 321,
                "maxTokens": 1_000,
                "percentage": 32.1,
                "model": self.model,
                "categories": [],
            }

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("running Claude must not receive context RPC")

    async def run():
        sdk = ContextSdk()
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        report = await machine._handle_get_context(GetContext(sid=SESSION_ID))

        assert report.total_tokens == 321
        assert report.percentage == 32.1
        assert report.source == "cached_control"
        assert sdk.context_calls == 0

    asyncio.run(run())


def test_automatic_claude_context_without_cache_reports_unavailable():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = True

        def cached_context_usage(self):
            return None

        async def get_context_usage(self):
            raise AssertionError("automatic read must not receive context RPC")

    async def run():
        machine, _transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(sid=SESSION_ID))

        assert report.available is False
        assert report.total_tokens == 0
        assert report.max_tokens == 0

    asyncio.run(run())


def test_busy_or_queued_claude_context_refresh_is_deferred():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False

        async def get_context_usage(self):
            raise AssertionError("busy Claude must not receive context RPC")

    async def run():
        machine, transport, ctx = _machine_with_sdk(ContextSdk())
        ctx.state = "running"

        running = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-running",
            client_id="browser-one",
        ))

        assert isinstance(running, Error)
        assert running.code == "busy"
        assert running.request_id == "context-running"
        assert running.to == "browser-one"

        ctx.state = "idle"
        ctx.queued_queries.append(object())
        queued = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-queued",
            client_id="browser-one",
        ))

        assert isinstance(queued, Error)
        assert queued.code == "busy"
        assert queued.request_id == "context-queued"
        assert queued.to == "browser-one"
        assert transport.sent[-2:] == [running, queued]

    asyncio.run(run())


def test_context_control_timeout_preserves_cache_but_returns_error():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return {
                "totalTokens": 80_000,
                "maxTokens": 500_000,
                "percentage": 16.0,
                "model": "claude-old-model",
                "isAutoCompactEnabled": True,
                "autoCompactThreshold": 400_000,
                "rawMaxTokens": 1_000_000,
                "categories": [{"name": "old", "tokens": 80_000}],
            }

        def cached_recent_context_usage(self):
            return {
                "totalTokens": 88_259,
            }

        async def get_context_usage(self):
            self.context_calls += 1
            raise TimeoutError("control request timed out")

    async def run():
        sdk = ContextSdk()
        machine, transport, _ctx = _machine_with_sdk(sdk)

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-command",
            client_id="browser-one",
        ))

        assert isinstance(report, Error)
        assert report.code == "internal"
        assert report.request_id == "context-command"
        assert report.to == "browser-one"
        assert transport.sent[-1] == report
        assert not any(
            isinstance(item, ContextReport) for item in transport.sent
        )
        assert sdk.context_calls == 1
        assert sdk.cached_context_usage()["totalTokens"] == 80_000
        assert sdk.cached_recent_context_usage()["totalTokens"] == 88_259

    asyncio.run(run())


def test_context_control_timeout_without_cache_reports_error():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def cached_context_usage(self):
            return None

        def cached_recent_context_usage(self):
            return None

        async def get_context_usage(self):
            raise TimeoutError("control request timed out")

    async def run():
        machine, transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-command",
            client_id="browser-one",
        ))

        assert isinstance(report, Error)
        assert report.code == "internal"
        assert report.request_id == "context-command"
        assert report.to == "browser-one"
        assert transport.sent[-1] == report
        assert not any(
            isinstance(item, ContextReport) for item in transport.sent)

    asyncio.run(run())


def test_malformed_context_control_response_is_not_reported_as_success():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def cached_context_usage(self):
            return {"model": "glm-5.2"}

        def cached_recent_context_usage(self):
            return {"totalTokens": 123, "model": "glm-5.2"}

        async def get_context_usage(self):
            return {"model": "glm-5.2"}

    async def run():
        machine, transport, _ctx = _machine_with_sdk(ContextSdk())

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-malformed",
            client_id="browser-one",
        ))

        assert isinstance(report, Error)
        assert report.code == "internal"
        assert report.request_id == "context-malformed"
        assert report.to == "browser-one"
        assert not any(
            isinstance(item, ContextReport) for item in transport.sent)

    asyncio.run(run())


def test_poisoned_context_generation_reconnects_once_then_can_refresh():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False
        context_probe_suppressed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return None

        def cached_recent_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            if self.context_calls == 1:
                self.control_plane_failed = True
                raise Exception(
                    "Control request timeout: get_context_usage")
            return {
                "totalTokens": 250_000,
                "maxTokens": 500_000,
                "percentage": 50.0,
                "model": self.model,
                "categories": [],
            }

        async def force_reconnect(self, **kwargs) -> None:
            await super().force_reconnect(**kwargs)
            self.control_plane_failed = False

    async def run():
        sdk = ContextSdk()
        machine, transport, ctx = _machine_with_sdk(sdk)

        failed = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-timeout",
            client_id="browser-one",
        ))

        assert isinstance(failed, Error)
        assert failed.request_id == "context-timeout"
        assert "安全恢复" in failed.message
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2] == {
            "resume_id": SESSION_ID,
            "cwd": ctx.cwd,
            "reason": "context control timeout",
            "fork": False,
        }

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-retry",
            client_id="browser-one",
        ))

        assert isinstance(report, ContextReport)
        assert report.source == "control"
        assert report.total_tokens == 250_000
        assert report.max_tokens == 500_000
        assert report.request_id == "context-retry"
        assert len(sdk.reconnects) == 1
        assert transport.sent[-2:] == [failed, report]

    asyncio.run(run())


def test_context_timeout_does_not_reconnect_over_autonomous_followup():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        def cached_context_usage(self):
            return None

        def cached_recent_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            if self.context_calls == 1:
                self.started.set()
                await self.release.wait()
                self.control_plane_failed = True
                raise Exception(
                    "Control request timeout: get_context_usage")
            return {
                "totalTokens": 275_000,
                "maxTokens": 500_000,
                "percentage": 55.0,
                "model": self.model,
                "categories": [],
            }

        async def force_reconnect(self, **kwargs) -> None:
            await super().force_reconnect(**kwargs)
            self.control_plane_failed = False

    async def run():
        sdk = ContextSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        request = asyncio.create_task(machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-race",
            client_id="browser-one",
        )))

        await sdk.started.wait()
        # This is the post-Result autonomous continuation which used to be
        # erased by force_reconnect(), leaving state=running with no owner.
        ctx.claude_background_followups["task-one:1"] = "active"
        ctx.state = "running"
        sdk.release.set()

        failed = await request

        assert isinstance(failed, Error)
        assert failed.code == "busy"
        assert sdk.reconnects == []
        assert ctx.state == "running"
        assert ctx.claude_background_followups == {"task-one:1": "active"}

        # The real autonomous Result owns the release to idle. A later exact
        # refresh can then replace the still-poisoned generation once and read
        # the successor without manufacturing a running state.
        ctx.claude_background_followups.clear()
        await machine._settle_claude_lifecycle_if_quiescent(ctx)
        assert ctx.state == "idle"

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-after-followup",
            client_id="browser-one",
        ))

        assert isinstance(report, ContextReport)
        assert report.source == "control"
        assert report.total_tokens == 275_000
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == "context control retry"

    asyncio.run(run())


def test_context_refresh_never_reconnects_external_claude_cli_owner():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = True

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return None

        def cached_recent_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            raise AssertionError("external owner must block the native RPC")

    async def run():
        sdk = ContextSdk()
        machine, _transport, _ctx = _machine_with_sdk(sdk)

        async def external_owner(_sid):
            return True

        machine._prime_claude_ownership = external_owner

        failed = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-external",
            client_id="browser-one",
        ))

        assert isinstance(failed, Error)
        assert failed.code == "internal"
        assert failed.request_id == "context-external"
        assert sdk.context_calls == 0
        assert sdk.reconnects == []
        assert sdk.control_plane_failed is True

    asyncio.run(run())


def test_context_refresh_reloads_terminal_growth_before_native_read():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0

        def cached_context_usage(self):
            return None

        def cached_recent_context_usage(self):
            return None

        async def get_context_usage(self):
            self.context_calls += 1
            return {
                "totalTokens": 300_000,
                "maxTokens": 500_000,
                "percentage": 60.0,
                "model": self.model,
                "categories": [],
            }

    async def run():
        sdk = ContextSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.needs_reload = True

        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID,
            refresh=True,
            cmd_id="context-after-cli",
            client_id="browser-one",
        ))

        assert isinstance(report, ContextReport)
        assert report.total_tokens == 300_000
        assert report.source == "control"
        assert sdk.context_calls == 1
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2] == {
            "resume_id": SESSION_ID,
            "cwd": ctx.cwd,
            "reason": "external transcript change before context",
            "preserve_model": True,
            "fork": False,
        }
        assert ctx.needs_reload is False

    asyncio.run(run())


def test_work_baseline_context_timeout_recovers_generation_once():
    class ContextSdk(_AutoCompactSdk):
        control_plane_failed = False

        def __init__(self):
            super().__init__()
            self.context_calls = 0
            self.work_context_baseline_tokens = None

        async def get_context_usage(self):
            self.context_calls += 1
            if self.context_calls == 1:
                self.control_plane_failed = True
                raise Exception(
                    "Control request timeout: get_context_usage")
            return {"totalTokens": 25_500}

        async def force_reconnect(self, **kwargs) -> None:
            await super().force_reconnect(**kwargs)
            self.control_plane_failed = False

    async def run():
        sdk = ContextSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.space = "work"
        ctx.work_id = "work-one"
        ctx.work_context_baseline_pending = True

        await machine._refresh_pending_claude_work_baseline(ctx)

        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == (
            "Work baseline context timeout")
        assert sdk.control_plane_failed is False
        assert ctx.work_context_baseline_tokens is None

        await machine._refresh_pending_claude_work_baseline(ctx)

        assert len(sdk.reconnects) == 1
        assert sdk.context_calls == 2
        assert sdk.work_context_baseline_tokens == 25_500
        assert ctx.work_context_baseline_tokens == 25_500

    asyncio.run(run())


def test_closing_btw_cancels_an_inflight_autocompact_apply_task():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        machine.sessions.pop(SESSION_ID)
        ctx.key = "btw-autocompact"
        ctx.session_id = None
        ctx.btw = True
        ctx.owner_client_id = "client-1"
        machine.sessions[ctx.key] = ctx
        started = asyncio.Event()

        async def applying():
            started.set()
            await asyncio.Event().wait()

        ctx.auto_compact_apply_task = asyncio.create_task(applying())
        await started.wait()

        await machine._handle_close_btw(SimpleNamespace(sid=ctx.key))

        assert ctx.key not in machine.sessions
        assert ctx.auto_compact_apply_task is None
        assert sdk.disconnected == 1

    asyncio.run(run())


def test_claude_control_persistence_serializes_complete_sdk_snapshots():
    class BlockingStore:
        def __init__(self):
            self.calls = []
            self.first_started = threading.Event()
            self.release_first = threading.Event()

        def update(self, session_id, **values):
            self.calls.append((session_id, values))
            if len(self.calls) == 1:
                self.first_started.set()
                assert self.release_first.wait(timeout=2)

    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        store = BlockingStore()
        machine._claude_controls = store
        machine._claude_broker_enabled = False

        first = asyncio.create_task(
            machine._persist_claude_session_controls(ctx))
        assert await asyncio.to_thread(store.first_started.wait, 1)

        sdk.model = "claude-opus-4-6[1m]"
        sdk.set_auto_compact("custom", 350_000)
        second = asyncio.create_task(
            machine._persist_claude_session_controls(ctx))
        await asyncio.sleep(0.05)
        assert len(store.calls) == 1

        store.release_first.set()
        await asyncio.gather(first, second)
        assert len(store.calls) == 2
        assert store.calls[-1][1]["model"] == "claude-opus-4-6[1m]"
        assert store.calls[-1][1]["auto_compact_mode"] == "custom"
        assert store.calls[-1][1]["auto_compact_threshold_tokens"] == 350_000

    asyncio.run(run())


def test_background_autocompact_waits_for_autonomous_result_boundary():
    async def run():
        sdk = _AutoCompactSdk()
        sdk.set_auto_compact("custom", 450_000)
        machine, _transport, ctx = _machine_with_sdk(sdk)
        scheduled = []
        machine._schedule_pending_claude_auto_compact = scheduled.append

        await machine._on_claude_background_message(
            ctx,
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="task-1",
                status="completed",
                output_file="/private/task-output",
                summary="done",
                uuid="notification-1",
                session_id=SESSION_ID,
                tool_use_id="agent-tool",
            ),
            "origin-turn",
        )
        assert scheduled == []

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        assert scheduled == [ctx]

    asyncio.run(run())


def test_work_background_bash_defers_reconnect_through_autonomous_result():
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.space = "work"
        ctx.claude_agents = None
        ctx.state = "running"

        pending = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=500_000,
        ))
        assert pending.pending is True

        machine._observe_claude_task_lifecycle(ctx, TaskStartedMessage(
            subtype="task_started",
            data={},
            task_id="bash-task",
            description="background shell",
            uuid="task-start",
            session_id=SESSION_ID,
            tool_use_id="bash-tool",
            task_type="local_bash",
        ))
        assert ctx.claude_active_tasks == {"bash-task"}

        # The parent Result is not the end of Bash(run_in_background=true).
        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)
        assert ctx.state == "idle"
        assert sdk.reconnects == []
        assert machine._claude_auto_compact_event(ctx).pending is True

        await machine._on_claude_background_message(
            ctx,
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="bash-task",
                status="completed",
                output_file="/private/task-output",
                summary="done",
                uuid="task-finished",
                session_id=SESSION_ID,
                tool_use_id="bash-tool",
            ),
            "origin-turn",
        )
        assert ctx.claude_active_tasks == set()
        assert ctx.claude_background_followup_pending is True
        assert sdk.reconnects == []

        # Even an explicit idle control change cannot cut off the autonomous
        # response started by the task notification.
        held = await machine._handle_set_auto_compact(SetAutoCompact(
            sid=SESSION_ID,
            mode="custom",
            threshold_tokens=600_000,
        ))
        assert held.pending is True
        assert sdk.reconnects == []

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        for _ in range(20):
            if sdk.reconnects:
                break
            await asyncio.sleep(0)

        assert ctx.claude_background_followup_pending is False
        assert [(mode, threshold) for mode, threshold, _ in sdk.reconnects] == [
            ("custom", 600_000),
        ]
        assert machine._claude_auto_compact_event(ctx).pending is False

    asyncio.run(run())


def test_immediate_query_rejects_autonomous_claude_followup_window():
    async def run():
        machine, transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_background_followup_pending = True

        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="do not steal the autonomous result",
            msg_id="browser-query",
        ))

        assert isinstance(result, Error)
        assert result.code == "busy"
        assert result.msg_id == "browser-query"
        assert ctx.state == "idle"
        assert ctx.turn_task is None
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_query_rechecks_autonomous_followup_after_async_preflight():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())

        async def ownership(_sid):
            ctx.claude_background_followup_pending = True
            return False

        machine._prime_claude_ownership = ownership
        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="follow-up starts during ownership preflight",
            msg_id="racing-query",
        ))

        assert isinstance(result, Error)
        assert result.code == "busy"
        assert result.msg_id == "racing-query"
        assert ctx.state == "idle"
        assert ctx.turn_task is None

    asyncio.run(run())


def test_real_run_turn_final_guard_never_writes_or_reports_crash():
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.engine = "claude"
        sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.effort = "max"
        sdk.applied_effort = "max"
        sdk.set_auto_compact("custom", 500_000)
        sdk.applied_auto_compact_mode = "custom"
        sdk.applied_auto_compact_threshold_tokens = 500_000
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()

        async def no_external_owner(_sid):
            return False

        def autonomous_followup_wins(_ctx):
            _ctx.claude_background_followup_pending = True

        machine._prime_claude_ownership = no_external_owner
        machine._start_claude_client_alias_probe = autonomous_followup_wins
        try:
            result = await machine._handle_query(Query(
                sid=SESSION_ID,
                prompt="must stop at the real SDK boundary",
                msg_id="guarded-browser-query",
            ))
            assert result is None
            turn = ctx.turn_task
            assert turn is not None
            await asyncio.wait_for(turn, timeout=1)

            assert sdk.client.queries == []
            guarded = [
                item for item in transport.sent
                if isinstance(item, Error)
                and item.msg_id == "guarded-browser-query"
            ]
            assert [item.code for item in guarded] == ["busy"]
            assert not any(
                isinstance(item, Error)
                and item.code == "cc_crash"
                and item.msg_id == "guarded-browser-query"
                for item in transport.sent
            )
            assert ctx.state == "running"

            await machine._on_claude_background_message(
                ctx,
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=SESSION_ID,
                ),
                "autonomous-turn",
            )
            assert ctx.state == "idle"
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("failure", "expected_message", "unexpected_message"),
    [
        (
            "status_code=413, input tokens exceed system limit",
            "上游按其 token 口径",
            "较大附件",
        ),
        (
            "status_code=413, 输入Tokens数量(1126006)超过系统限制(1000000)",
            "媒体请求体可能与其不一致",
            "Claude 未能在发送前完成原生自动压缩",
        ),
        (
            "status_code=413, payload too large",
            "较大附件",
            "上游按其 token 口径",
        ),
    ],
)
def test_query_exception_413_is_not_retried_and_explains_known_cause(
    failure, expected_message, unexpected_message,
):
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)
                raise RuntimeError(failure)

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.engine = "claude"
        sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.effort = "max"
        sdk.applied_effort = "max"
        sdk.set_auto_compact("custom", 500_000)
        sdk.applied_auto_compact_mode = "custom"
        sdk.applied_auto_compact_threshold_tokens = 500_000
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()

        async def no_external_owner(_sid):
            return False

        machine._prime_claude_ownership = no_external_owner
        machine._schedule_pending_claude_auto_compact = lambda _ctx: None
        try:
            result = await machine._handle_query(Query(
                sid=SESSION_ID,
                prompt="one attempt only",
                msg_id="provider-413-query",
            ))
            assert result is None
            turn = ctx.turn_task
            assert turn is not None
            await asyncio.wait_for(turn, timeout=1)

            assert sdk.client.queries == ["one attempt only"]
            errors = [
                item for item in transport.sent
                if isinstance(item, Error)
                and item.msg_id == "provider-413-query"
            ]
            assert [item.code for item in errors] == ["bad_prompt"]
            assert "/compact" in errors[0].message
            assert expected_message in errors[0].message
            assert unexpected_message not in errors[0].message
            assert ctx.state == "idle"
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()

    asyncio.run(run())


def test_native_empty_system_result_drains_without_retry_model_change_or_history_write():
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)
                await self.queue.put(UserMessage(content=prompt, uuid=SESSION_ID))
                await self.queue.put(ResultMessage(
                    subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                    is_error=True, num_turns=1, session_id=SESSION_ID, api_error_status=400,
                    result="API Error: 400 messages.1: system content must contain at least one block",
                ))

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        sdk = ctx.sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.model = "claude-fable-5-1[1m]"
        sdk.effort = sdk.applied_effort = "max"
        sdk.set_auto_compact("custom", 400_000)
        sdk.applied_auto_compact_mode = "custom"
        sdk.applied_auto_compact_threshold_tokens = 400_000
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()
        async def no_external_owner(_sid):
            return False
        machine._prime_claude_ownership = no_external_owner
        machine._schedule_pending_claude_auto_compact = lambda _ctx: None
        try:
            await machine._handle_query(Query(sid=SESSION_ID, prompt="only once", msg_id="empty-system-query"))
            await asyncio.wait_for(ctx.turn_task, 2)
            assert sdk.client.queries == ["only once"]
            assert sdk.model == "claude-fable-5-1[1m]"
            assert ctx.state == "idle"
            errors = [event for event in transport.sent if isinstance(event, Error)]
            assert any("系统消息内容为空" in event.message for event in errors)
            assert not any(event.code == "cc_crash" for event in errors)
            assert any(isinstance(event, TurnEnd) and event.result.is_error for event in transport.sent)
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()
    asyncio.run(run())


def test_deferred_query_survives_final_guard_and_retries_after_result():
    async def run():
        class Client:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.queries = []

            async def receive_messages(self):
                while True:
                    yield await self.queue.get()

            async def query(self, prompt):
                self.queries.append(prompt)

        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.engine = "claude"
        sdk = SdkHandle(machine.cfg)
        sdk.client = Client()
        sdk.effort = "max"
        sdk.applied_effort = "max"
        sdk.set_auto_compact("inherit")
        sdk.applied_auto_compact_mode = "inherit"
        sdk.applied_auto_compact_threshold_tokens = None
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk._start_message_pump()

        async def no_external_owner(_sid):
            return False

        first_attempt = True

        def autonomous_followup_wins_once(_ctx):
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                _ctx.claude_background_followup_pending = True

        machine._prime_claude_ownership = no_external_owner
        machine._start_claude_client_alias_probe = (
            autonomous_followup_wins_once
        )
        try:
            queued = Query(
                sid=SESSION_ID,
                prompt="retry this exact queued prompt",
                msg_id="guarded-queued-query",
                delivery="queue",
                cmd_id="queue-guarded-query",
                client_id="browser-client",
            )
            result = await machine._handle_query(queued)
            assert result is None

            for _ in range(100):
                if (
                    ctx.claude_background_followup_pending
                    and ctx.turn_task is None
                    and ctx.queued_query_starting_msg_id is None
                ):
                    break
                await asyncio.sleep(0)

            assert sdk.client.queries == []
            assert [item.msg_id for item in ctx.queued_queries] == [
                "guarded-queued-query"
            ]
            assert not any(
                isinstance(item, UserMsg)
                and item.msg_id == "guarded-queued-query"
                for item in transport.sent
            )
            assert not any(
                isinstance(item, Error)
                and item.msg_id == "guarded-queued-query"
                for item in transport.sent
            )

            await machine._on_claude_background_message(
                ctx,
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=SESSION_ID,
                    origin={"kind": "task-notification"},
                ),
                "autonomous-turn",
            )
            for _ in range(100):
                if sdk.client.queries:
                    break
                await asyncio.sleep(0)

            assert sdk.client.queries == ["retry this exact queued prompt"]
            for _ in range(100):
                if not ctx.queued_queries:
                    break
                await asyncio.sleep(0)
            assert ctx.queued_queries == []
            assert len([
                item for item in transport.sent
                if isinstance(item, UserMsg)
                and item.msg_id == "guarded-queued-query"
            ]) == 1

            await sdk.client.queue.put(UserMessage(
                content="retry this exact queued prompt",
                uuid="native-human-user",
            ))
            await sdk.client.queue.put(ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ))
            turn = ctx.turn_task
            if turn is not None:
                await asyncio.wait_for(turn, timeout=1)
            assert ctx.state == "idle"
        finally:
            sdk.release_background_messages()
            await sdk._stop_message_pump()

    asyncio.run(run())


def test_queued_query_waits_for_autonomous_claude_result_then_starts():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_background_followup_pending = True
        started = asyncio.Event()
        launched = []

        async def launch(_ctx, command, *, launch_receipt=None):
            launched.append(command.msg_id)
            if launch_receipt is not None and not launch_receipt.done():
                launch_receipt.set_result(True)
            started.set()
            return None

        machine._handle_immediate_query = launch
        result = await machine._handle_query(Query(
            sid=SESSION_ID,
            prompt="start after the autonomous result",
            msg_id="queued-query",
            delivery="queue",
            cmd_id="queue-command",
            client_id="browser-client",
        ))
        assert result is None
        for _ in range(10):
            await asyncio.sleep(0)
        assert started.is_set() is False
        assert [item.msg_id for item in ctx.queued_queries] == ["queued-query"]

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        drain = ctx.queued_query_drain_task
        if drain is not None:
            await drain

        assert launched == ["queued-query"]
        assert ctx.claude_background_followup_pending is False
        assert ctx.queued_queries == []

    asyncio.run(run())


@pytest.mark.parametrize("status", ["killed", "completed", "failed"])
def test_terminal_task_update_without_notification_does_not_invent_followup(status):
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        ctx.space = "work"
        ctx.claude_active_tasks.add("bash-task")

        machine._observe_claude_task_lifecycle(
            ctx,
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="bash-task",
                patch={"status": status},
                status=status,
                session_id=SESSION_ID,
                uuid="task-killed",
            ),
            background=True,
        )

        assert ctx.claude_active_tasks == set()
        assert ctx.claude_background_followup_pending is False
        assert machine._claude_has_background_work(ctx) is False

    asyncio.run(run())


@pytest.mark.parametrize("active", [False, True])
def test_stopped_task_only_retires_unstarted_followup(active):
    async def run():
        sdk = _AutoCompactSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        machine._claim_claude_followup_notification(ctx, "task-1")
        if active:
            machine._activate_claude_followup(ctx, UserMessage(
                content="task finished",
                origin={"kind": "task-notification", "taskId": "task-1"},
            ))
        ctx.state = "interrupting"
        await machine._on_claude_background_message(ctx, TaskUpdatedMessage(
            subtype="task_updated", data={}, task_id="task-1",
            patch={"status": "killed"}, status="killed",
        ), "origin-turn")
        assert ctx.claude_background_followup_pending is active
        assert ctx.state == ("interrupting" if active else "idle")
        assert sdk.reconnects == []

    asyncio.run(run())


def test_background_followup_owns_running_state_until_its_result():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_active_tasks.add("bash-task")

        await machine._on_claude_background_message(
            ctx,
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="bash-task",
                status="completed",
                output_file="/private/task-output",
                summary="done",
                uuid="task-finished",
                session_id=SESSION_ID,
                tool_use_id="bash-tool",
            ),
            "origin-turn",
        )

        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )

        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_each_completed_background_task_claims_its_own_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.claude_active_tasks.update({"task-1", "task-2"})

        def notification(task_id: str) -> TaskNotificationMessage:
            return TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id=task_id,
                status="completed",
                output_file=f"/private/{task_id}",
                summary=f"{task_id} done",
                uuid=f"{task_id}-finished",
                session_id=SESSION_ID,
                tool_use_id=f"{task_id}-tool",
            )

        def result(task_id: str) -> ResultMessage:
            return ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin={"kind": "task-notification", "taskId": task_id},
            )

        await machine._on_claude_background_message(
            ctx, notification("task-1"), "origin-turn")
        assert ctx.claude_active_tasks == {"task-2"}
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        # Both completions may be delivered before the first autonomous turn
        # reaches its Result. The first terminal must retire only task-1.
        await machine._on_claude_background_message(
            ctx, notification("task-2"), "origin-turn")
        assert ctx.claude_active_tasks == set()
        assert len(ctx.claude_background_followups) == 2

        await machine._on_claude_background_message(
            ctx, result("task-1"), "task-1-followup")
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        await machine._on_claude_background_message(
            ctx, result("task-2"), "task-2-followup")
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_unrelated_result_cannot_retire_an_autonomous_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followups[
            "task-notification:task:task-1"
        ] = "active"

        def result(origin: dict) -> ResultMessage:
            return ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin=origin,
            )

        # Recovery can route a late human terminal through the background
        # callback. It must not consume an unrelated autonomous claim.
        await machine._on_claude_background_message(
            ctx, result({"kind": "human"}), "human-turn")
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        # An exact but unknown task identity is equally authoritative: never
        # fall back to the first ledger entry and retire task-1 by accident.
        await machine._on_claude_background_message(
            ctx,
            result({"kind": "task-notification", "taskId": "task-2"}),
            "task-2-turn",
        )
        assert ctx.claude_background_followup_pending is True
        assert ctx.state == "running"

        await machine._on_claude_background_message(
            ctx,
            result({"kind": "task-notification", "taskId": "task-1"}),
            "task-1-turn",
        )
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_autonomous_followup_streams_text_and_tools_without_duplicate_turn_end():
    async def run():
        machine, transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        origin = {"kind": "task-notification"}
        assistant_id = "77777777-7777-4777-8777-777777777777"

        await machine._on_claude_background_message(
            ctx,
            UserMessage(
                content="<task-notification>done</task-notification>",
                uuid="66666666-6666-4666-8666-666666666666",
                origin=origin,
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            StreamEvent(
                uuid=assistant_id,
                session_id=SESSION_ID,
                event={
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": "background answer",
                    },
                },
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            AssistantMessage(
                content=[
                    TextBlock(text="background answer"),
                    ToolUseBlock(
                        id="background-read",
                        name="Read",
                        input={"file_path": "README.md"},
                    ),
                ],
                model="claude-test",
                stop_reason="tool_use",
                uuid=assistant_id,
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            UserMessage(
                content=[ToolResultBlock(
                    tool_use_id="background-read",
                    content="contents",
                    is_error=False,
                )],
                parent_tool_use_id="background-read",
            ),
            "origin-turn",
        )
        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
                origin=origin,
            ),
            "origin-turn",
        )

        assert any(
            isinstance(item, Delta) and item.text == "background answer"
            for item in transport.sent
        )
        assert any(
            isinstance(item, ToolUse)
            and item.tool_use_id == "background-read"
            for item in transport.sent
        )
        assert any(
            isinstance(item, ToolResult)
            and item.tool_use_id == "background-read"
            for item in transport.sent
        )
        assert not any(isinstance(item, TurnEnd) for item in transport.sent)
        narrative = [
            item for item in transport.sent
            if isinstance(item, (Delta, ToolUse, ToolResult))
        ]
        assert narrative
        assert all(item.turn_id == "origin-turn" for item in narrative)
        assert all(item.background is True for item in narrative)
        assert ctx.claude_background_translator is None
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())


def test_background_result_projection_failure_still_settles_lifecycle():
    class BrokenProjectionSdk(_AutoCompactSdk):
        def observe_goal_message(self, message, _thread_id):
            if isinstance(message, ResultMessage):
                raise ValueError("malformed goal projection")
            return False, None

    async def run():
        machine, _transport, ctx = _machine_with_sdk(BrokenProjectionSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.claude_background_translator = StreamTranslator(1024)

        with pytest.raises(ValueError, match="malformed goal projection"):
            await machine._on_claude_background_message(
                ctx,
                ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=SESSION_ID,
                    origin={"kind": "task-notification"},
                ),
                "origin-turn",
            )

        assert ctx.claude_background_followup_pending is False
        assert ctx.claude_background_translator is None
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_background_result_and_managed_finalizer_race_still_reaches_idle():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.turn_task = asyncio.current_task()

        # The managed terminal observes the autonomous claim first and correctly
        # leaves the session running.
        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)
        assert ctx.state == "running"

        # Its background Result then retires the last claim while the managed
        # task is still alive, so that callback cannot publish idle either.
        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "origin-turn",
        )
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "running"

        # The real runner finally drops its owner and performs the same idempotent
        # quiescence check. No event ordering may leave a false running latch.
        ctx.turn_task = None
        settled = await machine._settle_claude_lifecycle_if_quiescent(ctx)
        assert settled is True
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_followup_task_id_spellings_share_one_exact_ledger_key():
    machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())

    assert machine._claim_claude_followup_notification(ctx, "task-1") is False
    assert list(ctx.claude_background_followups) == [
        "task-notification:task:task-1",
    ]
    assert machine._activate_claude_followup(ctx, UserMessage(
        content="notification",
        origin={"kind": "task-notification", "task_id": "task-1"},
    )) is False
    assert ctx.claude_background_followups == {
        "task-notification:task:task-1": "active",
    }
    assert machine._retire_claude_followup(ctx, ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=SESSION_ID,
        origin={"kind": "task-notification", "task_id": "task-1"},
    )) is True
    assert ctx.claude_background_followups == {}


def test_followup_ledger_overflow_forces_one_controlled_reconnect(monkeypatch):
    async def run():
        sdk = _AutoCompactSdk()
        machine, transport, ctx = _machine_with_sdk(sdk)
        monkeypatch.setattr(type(machine), "CLAUDE_ACTIVE_TASK_CAP", 1)
        ctx.state = "running"

        machine._observe_claude_task_lifecycle(ctx, TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-1",
            status="completed", output_file="", summary="one",
            uuid="u1", session_id=SESSION_ID, tool_use_id=None,
        ), background=True)
        machine._observe_claude_task_lifecycle(ctx, TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="task-2",
            status="completed", output_file="", summary="two",
            uuid="u2", session_id=SESSION_ID, tool_use_id=None,
        ), background=True)

        recovery = ctx.claude_followup_recovery_task
        assert recovery is not None
        await asyncio.wait_for(recovery, timeout=1)
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == (
            "autonomous follow-up ledger overflow")
        assert ctx.claude_background_followups == {}
        assert ctx.claude_background_followup_overflow is False
        assert ctx.state == "idle"
        assert any(
            isinstance(item, Error) and "后台任务状态过多" in item.message
            for item in transport.sent
        )

    asyncio.run(run())


def test_managed_terminal_does_not_regress_autonomous_interrupt_to_running():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "interrupting"
        ctx.claude_background_followup_pending = True

        await machine._set_idle_after_managed_turn(
            ctx, claude_terminal=True)

        assert ctx.state == "interrupting"
        assert ctx.claude_background_followup_pending is True

    asyncio.run(run())


def test_claude_lifecycle_reset_wakes_queue_waiting_on_followup():
    machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
    ctx.claude_active_tasks.add("task-1")
    ctx.claude_background_followup_pending = True
    ctx.claude_background_followup_nonce = 7
    ctx.queued_query_wakeup.clear()

    machine._reset_claude_task_lifecycle(ctx)

    assert ctx.claude_active_tasks == set()
    assert ctx.claude_background_followup_pending is False
    assert ctx.claude_background_followup_nonce == 0
    assert ctx.queued_query_wakeup.is_set() is True


def test_idle_message_pump_failure_releases_autonomous_followup():
    async def run():
        machine, _transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.queued_query_wakeup.clear()

        await machine._on_claude_message_pump_failure(
            ctx, RuntimeError("reader failed"))

        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"
        assert ctx.queued_query_wakeup.is_set() is True

    asyncio.run(run())


def test_autonomous_followup_interrupt_timeout_reconnects_and_unlocks():
    class InterruptSdk(_AutoCompactSdk):
        def __init__(self):
            super().__init__()
            self.interrupts = 0

        async def interrupt(self):
            self.interrupts += 1

    async def run():
        sdk = InterruptSdk()
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine.cfg.drain_timeout = 0.01
        ctx.state = "running"
        ctx.claude_background_followup_pending = True

        await machine._handle_interrupt(SimpleNamespace(
            sid=SESSION_ID,
            cmd_id="stop-autonomous",
            client_id="browser",
        ))
        watchdog = ctx.claude_autonomous_interrupt_task
        assert watchdog is not None
        await asyncio.wait_for(watchdog, timeout=1)

        assert sdk.interrupts == 1
        assert len(sdk.reconnects) == 1
        assert sdk.reconnects[0][2]["reason"] == (
            "autonomous interrupt drain timeout")
        assert ctx.claude_background_followup_pending is False
        assert ctx.claude_autonomous_interrupt_task is None
        assert ctx.state == "idle"
        assert any(
            isinstance(item, Error) and item.code == "drain_timeout"
            for item in transport.sent
        )

    asyncio.run(run())


def test_autonomous_result_wakes_interrupt_watchdog_without_reconnect():
    class InterruptSdk(_AutoCompactSdk):
        async def interrupt(self):
            return None

    async def run():
        sdk = InterruptSdk()
        machine, _transport, ctx = _machine_with_sdk(sdk)
        machine.cfg.drain_timeout = 1.0
        ctx.state = "running"
        ctx.claude_background_followup_pending = True

        await machine._handle_interrupt(SimpleNamespace(
            sid=SESSION_ID,
            cmd_id="stop-autonomous",
            client_id="browser",
        ))
        watchdog = ctx.claude_autonomous_interrupt_task
        assert watchdog is not None

        await machine._on_claude_background_message(
            ctx,
            ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id=SESSION_ID,
            ),
            "autonomous-turn",
        )
        await asyncio.wait_for(watchdog, timeout=1)

        assert sdk.reconnects == []
        assert ctx.claude_autonomous_interrupt_task is None
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

    asyncio.run(run())
