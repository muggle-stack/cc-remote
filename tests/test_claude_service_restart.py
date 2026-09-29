"""A new service generation can resume a transcript, never an accepted input."""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from cc_remote.claude_service import client as client_module
from cc_remote.claude_service.client import RemoteClient, service_owner_exited
from cc_remote.config import WrapperConfig
from cc_remote.protocol import ContextReport, Error, GetContext, SetModel, TurnEnd
from cc_remote.wrapper import process_scan
from cc_remote.wrapper.process_scan import ProcessIdentity
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk
from tests.test_claude_live_context import SUMMARY
from tests.test_claude_service import FakeClient, environment
from tests.test_claude_service_delivery import wait_until


@asynccontextmanager
async def restarted_service(monkeypatch):
    # Same PID with a new start time also covers PID reuse. No real daemon or
    # model is touched: only the local Unix transport and service are real.
    generation = [1]
    def identity(pid):
        return ProcessIdentity(pid, generation[0])
    monkeypatch.setattr(process_scan, "process_identity", identity)
    controls = []

    async def control(client, request, timeout):
        controls.append(request)
        return dict(SUMMARY) if request["subtype"] == "get_context_usage" else {}

    monkeypatch.setattr(FakeClient, "_send_control_request", control, raising=False)
    async with environment() as (service, _attach):
        sdk = SdkHandle(WrapperConfig(
            claude_service_socket=str(service.directory / "service.sock")))
        machine, transport, ctx = _machine_with_sdk(sdk)
        sdk.service_metadata = {
            "profile_root": str(service.directory / "profile"),
            "session_id": SESSION_ID, "cwd": ctx.cwd, "space": "code",
        }
        await sdk.connect(resume_id=SESSION_ID, cwd=ctx.cwd)
        first = sdk.client
        old_worker = service.sessions[first.id]
        # It is deliberately not completed. Restart recovery must not try to
        # finish it by submitting its prompt again to the new service.
        first.next_turn = {"id": "old-input", "prompt": "accepted before restart"}
        await first.query("accepted before restart")
        await first.detach()
        await old_worker.close()
        service.sessions.clear()
        generation[0] += 1
        await wait_until(lambda: sdk.message_pump_failed)
        try:
            yield sdk, machine, transport, ctx, service, old_worker, controls
        finally:
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
async def test_model_switch_after_service_restart_resumes_exact_session(monkeypatch):
    async with restarted_service(monkeypatch) as (
        sdk, machine, _, ctx, service, old, controls,
    ):
        assert sdk.service_restart_required
        reply = await machine._handle_set_model(SetModel(sid=SESSION_ID, model="sonnet-5.5"))
        assert not isinstance(reply, Error)
        assert sdk.model == "sonnet-5.5"
        assert controls == [{"subtype": "set_model", "model": "sonnet-5.5"}]
        worker = service.sessions[sdk.client.id]
        assert worker.id != old.id
        assert sdk.service_metadata["service_id"] == worker.id
        assert worker.client.options.resume == SESSION_ID
        assert worker.client.options.cwd == ctx.cwd
        assert worker.metadata["profile_root"] == old.metadata["profile_root"]
        assert old.client.prompts == ["accepted before restart"]
        assert worker.client.prompts == []
        assert not sdk.service_restart_required


@pytest.mark.asyncio
async def test_context_refresh_recovers_but_cached_read_does_not(monkeypatch):
    async with restarted_service(monkeypatch) as (
        sdk, machine, _, ctx, service, old, controls,
    ):
        sdk.remember_recent_context_usage(dict(SUMMARY))
        await machine._handle_get_context(GetContext(sid=SESSION_ID))
        assert not service.sessions and controls == []
        report = await machine._handle_get_context(GetContext(sid=SESSION_ID, refresh=True))
        assert isinstance(report, ContextReport) and report.source == "control"
        assert report.total_tokens == SUMMARY["totalTokens"]
        assert service.sessions[sdk.client.id].client.prompts == []
        assert controls == [{"subtype": "get_context_usage", "detail": "summary"}]
        assert old.client.prompts == ["accepted before restart"]


@pytest.mark.asyncio
async def test_new_query_recovers_after_service_died_during_ack(monkeypatch):
    async with restarted_service(monkeypatch) as (
        sdk, machine, transport, ctx, service, old, _,
    ):
        sdk._service_delivery_error = ConnectionError("old service died during ACK")
        ctx.active_msg_id = "new-input"
        ctx.state = "running"

        async def query(client, prompt):
            client.prompts.append(prompt)
            await client.queue.put({
                "type": "result", "subtype": "success", "duration_ms": 20,
                "duration_api_ms": 19, "is_error": False, "num_turns": 1,
                "session_id": SESSION_ID,
            })

        monkeypatch.setattr(FakeClient, "query", query)
        await asyncio.wait_for(machine._run_turn(ctx, "new explicit question"), 3)
        worker = service.sessions[sdk.client.id]
        assert worker.client.prompts == ["new explicit question"]
        assert old.client.prompts == ["accepted before restart"]
        assert len([frame for frame in transport.sent if isinstance(frame, TurnEnd)]) == 1
        assert not [frame for frame in transport.sent if isinstance(frame, Error)]
        assert worker.turn is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["active", "external", "callback"])
async def test_model_recovery_yields_to_active_or_external_work(monkeypatch, blocked):
    async with restarted_service(monkeypatch) as (
        sdk, machine, _, ctx, service, old, controls,
    ):
        if blocked == "active":
            ctx.state = "running"
        elif blocked == "external":
            async def external(_sid):
                return True
            machine._prime_claude_ownership = external
        else:
            sdk._background_callbacks_pending = 1
        reply = await machine._handle_set_model(SetModel(sid=SESSION_ID, model="sonnet-5.5"))
        assert isinstance(reply, Error)
        assert not service.sessions and controls == []
        assert sdk.service_metadata["service_id"] == old.id


@pytest.mark.asyncio
async def test_unavailable_service_keeps_restart_identity_for_later_retry(monkeypatch):
    async with restarted_service(monkeypatch) as (
        sdk, machine, _, ctx, service, old, controls,
    ):
        connect = client_module.Connection.connect

        async def unavailable(_self):
            raise ConnectionRefusedError("service is starting")

        monkeypatch.setattr(client_module.Connection, "connect", unavailable)
        reply = await machine._handle_set_model(SetModel(sid=SESSION_ID, model="sonnet-5.5"))
        assert isinstance(reply, Error)
        assert sdk.service_restart_required
        assert sdk.service_metadata["service_id"] == old.id
        assert not service.sessions and controls == []
        monkeypatch.setattr(client_module.Connection, "connect", connect)
        reply = await machine._handle_set_model(SetModel(sid=SESSION_ID, model="sonnet-5.5"))
        assert not isinstance(reply, Error)
        assert service.sessions[sdk.client.id].client.prompts == []


@pytest.mark.asyncio
async def test_restart_does_not_take_over_a_replacement_worker(monkeypatch):
    async with restarted_service(monkeypatch) as (
        sdk, machine, _, ctx, service, old, controls,
    ):
        another = RemoteClient(sdk.client.connection.socket_path, options=sdk.client.options,
                               metadata={**old.metadata, "service_id": None})
        try:
            await another.connect()
            another.next_turn = {"id": "another-input", "prompt": "another controller"}
            await another.query("another controller")
            await another.detach()
            reply = await machine._handle_set_model(SetModel(sid=SESSION_ID, model="sonnet-5.5"))
            assert isinstance(reply, Error)
            assert list(service.sessions) == [another.id]
            worker = service.sessions[another.id]
            assert worker.turn["id"] == "another-input"
            assert worker.client.prompts == ["another controller"]
            assert not worker.client.closed and not controls
        finally:
            await another.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["live", "unknown", "unreadable"])
async def test_missing_worker_is_not_recreated_without_proven_restart(monkeypatch, owner):
    async with environment() as (service, attach):
        first = await attach()
        first.options.resume = "native-session"
        identity = first.owner_identity if owner != "unknown" else None
        if owner == "unreadable":
            monkeypatch.setattr(process_scan, "process_identity", lambda _pid: None)
        client = RemoteClient(first.connection.socket_path, options=first.options,
                              metadata={**first.metadata, "service_id": "missing-worker"},
                              previous_owner_identity=identity)
        try:
            with pytest.raises(RuntimeError, match="KeyError"):
                await client.connect()
            assert list(service.sessions) == [first.id]
            assert service.sessions[first.id].client.prompts == []
        finally:
            await client.detach()


@pytest.mark.parametrize("probe", ["alive", "denied", "absent", "reused"])
def test_service_owner_exit_proof_distinguishes_unknown_pid(monkeypatch, probe):
    expected = ProcessIdentity(os.getpid(), 1)
    monkeypatch.setattr(process_scan, "process_identity", lambda pid: (
        ProcessIdentity(pid, 2) if probe == "reused" else None))

    def kill(pid, signal):
        assert pid == expected.pid and signal == 0
        if probe == "denied":
            raise PermissionError()
        if probe == "absent":
            raise ProcessLookupError()

    monkeypatch.setattr(client_module.os, "kill", kill)
    assert service_owner_exited(expected) == (probe in {"absent", "reused"})
