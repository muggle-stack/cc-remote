"""Live context reads must not compete with or restart the active Claude turn."""
from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk.types import AssistantMessage, SystemMessage, TextBlock

from cc_remote.config import WrapperConfig
from cc_remote.protocol import ContextReport, Error, GetContext
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk


SUMMARY = {
    "totalTokens": 242_701, "maxTokens": 500_000,
    "rawMaxTokens": 1_000_000, "autoCompactThreshold": 467_000,
    "percentage": 48.5402, "model": "claude-fable-5-1",
    "isAutoCompactEnabled": True, "categories": [],
}


def assistant(total):
    return AssistantMessage(content=[TextBlock(text="working")],
                            model="claude-fable-5-1", usage={"input_tokens": total})


class SummaryClient:
    def __init__(self, read=None):
        self._query = self
        self.requests = []
        self.read = read

    async def _send_control_request(self, request, timeout):
        self.requests.append((request, timeout))
        if self.read is not None:
            return self.read()
        return dict(SUMMARY)


def test_live_summary_yields_to_a_pending_sdk_control_operation():
    async def go():
        sdk = SdkHandle(WrapperConfig())
        sdk._record_context_usage(dict(SUMMARY))
        client = sdk.client = SummaryClient()
        machine, _, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        async with sdk._control_request_lock:
            report = await asyncio.wait_for(machine._handle_get_context(GetContext(
                sid=SESSION_ID, refresh=True)), 0.5)
        assert report.total_tokens == 242_701
        assert client.requests == []
        assert not sdk.control_plane_failed
    asyncio.run(go())


def test_live_summary_releases_machine_lock_for_the_stream_terminal():
    async def go():
        sdk = SdkHandle(WrapperConfig())
        machine, _, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"

        class StreamDrainingClient(SummaryClient):
            async def _send_control_request(self, request, timeout):
                # A terminal callback must be able to finish draining while the
                # same SDK reader delivers the native control response.
                async with ctx.query_lock:
                    ctx.state = "idle"
                return dict(SUMMARY)

        client = sdk.client = StreamDrainingClient()
        report = await asyncio.wait_for(machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True)), 0.5)
        assert report.max_tokens == 500_000
        assert ctx.state == "idle"
        assert sdk.client is client
    asyncio.run(go())


def test_model_change_while_reacquiring_machine_lock_retires_the_summary():
    async def go():
        sdk = SdkHandle(WrapperConfig())
        machine, _, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        changes = []

        class SwitchingClient(SummaryClient):
            async def _send_control_request(self, request, timeout):
                await ctx.query_lock.acquire()

                async def switch():
                    sdk.invalidate_context_usage_cache()
                    sdk.model = "claude-opus-4-6"
                    sdk._observe_recent_context_usage(assistant(60_000))
                    ctx.query_lock.release()

                # The SDK receives its response first, then the machine waits
                # behind a model change before it can publish the report.
                changes.append(asyncio.create_task(switch()))
                return dict(SUMMARY)

        sdk.client = SwitchingClient()
        report = await asyncio.wait_for(machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True)), 0.5)
        await asyncio.gather(*changes)
        assert report.model == "claude-opus-4-6"
        assert report.total_tokens == 60_000
        assert report.max_tokens == 0
    asyncio.run(go())


def test_running_claude_summary_restores_capacity_then_polls_live_usage():
    async def go():
        sdk = SdkHandle(WrapperConfig())
        client = sdk.client = SummaryClient()
        machine, transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        ctx.turn_task = asyncio.create_task(asyncio.Event().wait())
        try:
            report = await machine._handle_get_context(GetContext(
                sid=SESSION_ID, refresh=True, cmd_id="open"))
            assert isinstance(report, ContextReport)
            assert report.source == "control"
            assert report.request_id == "open"
            assert (report.total_tokens, report.max_tokens) == (242_701, 500_000)
            assert report.raw_max_tokens == 1_000_000
            assert report.auto_compact_threshold_tokens == 467_000
            assert client.requests == [(
                {"subtype": "get_context_usage", "detail": "summary"}, 5.0)]

            sdk._observe_recent_context_usage(assistant(339_685))
            report = await machine._handle_get_context(GetContext(
                sid=SESSION_ID, refresh=False, cmd_id="poll"))
            assert report.source == "recent_turn"
            assert report.total_tokens == 339_685
            assert report.max_tokens == 500_000
            assert report.percentage == pytest.approx(67.937)
            assert len(client.requests) == 1
            assert ctx.state == "running"
            assert sdk.client is client
            assert not ctx.turn_task.done()
            assert all(isinstance(event, ContextReport) for event in transport.sent)
        finally:
            ctx.turn_task.cancel()
            await asyncio.gather(ctx.turn_task, return_exceptions=True)
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["timeout", "unsupported", "malformed"])
def test_running_summary_failure_retains_reading_and_never_restarts(failure):
    async def go():
        sdk = SdkHandle(WrapperConfig())
        sdk._record_context_usage(dict(SUMMARY))
        sdk._observe_recent_context_usage(assistant(339_685))

        def fail():
            if failure == "malformed":
                return {"totalTokens": 0, "maxTokens": 0}
            if failure == "timeout":
                raise Exception("Control request timeout: get_context_usage")
            raise RuntimeError("summary unavailable")

        client = sdk.client = SummaryClient(fail)
        machine, transport, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True, cmd_id="open"))
        assert isinstance(report, ContextReport)
        assert (report.total_tokens, report.max_tokens) == (339_685, 500_000)
        assert report.source == "recent_turn"
        assert sdk.client is client
        assert ctx.state == "running"
        assert sdk.control_plane_failed == (failure == "timeout")
        assert all(isinstance(event, ContextReport) for event in transport.sent)
        cached = await machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=False, cmd_id="poll"))
        assert cached.total_tokens == report.total_tokens
        assert len(client.requests) == 1
    asyncio.run(go())


@pytest.mark.parametrize("boundary", ["new_assistant", "compact", "model"])
def test_live_summary_does_not_overwrite_a_newer_stream_observation(boundary):
    async def go():
        sdk = SdkHandle(WrapperConfig())
        sdk._record_context_usage(dict(SUMMARY))

        def advance():
            if boundary == "compact":
                sdk._observe_context_boundary(SystemMessage(
                    subtype="compact_boundary", data={
                        "type": "system", "subtype": "compact_boundary",
                        "compact_metadata": {"trigger": "auto", "post_tokens": 50_000},
                    }))
            elif boundary == "model":
                sdk.invalidate_context_usage_cache()
                sdk.model = "claude-opus-4-6"
            sdk._observe_recent_context_usage(assistant(60_000))
            return dict(SUMMARY)

        sdk.client = SummaryClient(advance)
        machine, _, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True))
        assert report.total_tokens == 60_000
        assert sdk.cached_recent_context_usage()["totalTokens"] == 60_000
        assert report.categories == []
        if boundary == "model":
            assert report.model == "claude-opus-4-6"
            assert report.max_tokens == 0
        else:
            assert report.max_tokens == 500_000
            sdk._observe_recent_context_usage(assistant(61_000))
            later = await machine._handle_get_context(GetContext(
                sid=SESSION_ID, refresh=False))
            assert (later.total_tokens, later.max_tokens) == (61_000, 500_000)
    asyncio.run(go())


def test_replacement_wrapper_recovers_running_service_context_without_restart():
    from tests.test_claude_service import environment, released

    async def go():
        async with environment() as (service, attach):
            first = await attach()
            first.next_turn = {"id": "existing-turn"}
            await first.query("existing task")
            worker = service.sessions[first.id]
            native = worker.client
            requests = []

            async def read(request, timeout):
                requests.append((request, timeout))
                return dict(SUMMARY)

            native._send_control_request = read
            await first.detach()
            await released(worker)
            replacement = await attach()
            assert replacement.id == first.id
            sdk = SdkHandle(WrapperConfig())
            sdk.client = replacement
            machine, _, ctx = _machine_with_sdk(sdk)
            ctx.state = "running"
            report = await machine._handle_get_context(GetContext(
                sid=SESSION_ID, refresh=True))
            assert report.max_tokens == 500_000
            assert report.total_tokens == 242_701
            assert len(requests) == 1
            assert not native.closed
            assert native.interrupts == 0
            assert native.prompts == ["existing task"]
            assert worker.turn["id"] == "existing-turn"
    asyncio.run(go())


@pytest.mark.parametrize("boundary", ["external", "stale", "poisoned", "broker"])
def test_running_context_cannot_bypass_an_unavailable_writer(boundary):
    async def go():
        sdk = SdkHandle(WrapperConfig())
        client = sdk.client = SummaryClient()
        machine, _, ctx = _machine_with_sdk(sdk)
        ctx.state = "running"
        if boundary == "external":
            ctx.write_state = "read_only"
        elif boundary == "stale":
            ctx.needs_reload = True
        elif boundary == "poisoned":
            sdk.control_plane_failed = True
        else:
            sdk.is_claude_broker = True
        report = await machine._handle_get_context(GetContext(
            sid=SESSION_ID, refresh=True))
        assert isinstance(report, Error)
        assert report.code == "busy"
        assert client.requests == []
        assert sdk.client is client
    asyncio.run(go())
