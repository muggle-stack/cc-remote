"""Recover through a closing controller lease without taking over live work."""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from cc_remote.claude_service import client as client_module
from cc_remote.claude_service.client import Connection, RemoteClient, options_payload
from cc_remote.wrapper import claude_service
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_service import environment, released
from tests.test_claude_service_recovery import recovery_machine


def replacement(first, worker, **metadata):
    return RemoteClient(first.connection.socket_path, options=first.options,
                        metadata={**worker.metadata, "service_id": worker.id, **metadata})


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_service", [False, True])
async def test_restore_retries_until_old_connection_finishes_cleanup(monkeypatch, legacy_service):
    async def allow(*args):
        return PermissionResultAllow()

    async with environment() as (service, attach):
        first = await attach(permission=allow)
        profile = SimpleNamespace(id="primary", config_dir=service.directory / "profile")
        await first.call("metadata", {"value": {
            "profile_id": profile.id, "profile_root": str(profile.config_dir),
            "cwd": str(service.directory),
        }})
        first.next_turn = {"id": "original-turn"}
        await first.query("accepted once")
        worker = service.sessions[first.id]
        old_owner = worker.controller
        waiting = asyncio.Event()
        closing = asyncio.Event()
        release_close = asyncio.Event()
        conflict = asyncio.Event()
        opens = []
        original_dispatch = service.dispatch

        async def dispatch(owner, request_id, method, params):
            if method == "hold_connection_cleanup":
                waiting.set()
                try:
                    await asyncio.Future()
                finally:
                    closing.set()
                    await release_close.wait()
            if method == "open":
                opens.append(params["session"])
                if worker.controller is old_owner:
                    conflict.set()
            try:
                value = await original_dispatch(owner, request_id, method, params)
            except RuntimeError as exc:
                if legacy_service and "already has a controller" in str(exc):
                    # Older immutable services expose only RuntimeError's name.
                    raise RuntimeError("legacy controller conflict") from None
                raise
            if legacy_service and method == "hello":
                value.pop("strict_controller_leases", None)
            return value

        monkeypatch.setattr(service, "dispatch", dispatch)
        waiter = asyncio.create_task(first.call("hold_connection_cleanup"))
        recovered = []
        permission = None
        restore = None

        async def spawn(**kwargs):
            assert kwargs["_service_recovering"] is True
            assert kwargs["_service_worker_id"] == worker.id
            client = replacement(first, worker)
            recovered.append(client)
            await client.connect()
            client.ready.set()
            return SimpleNamespace(sdk=SimpleNamespace(client=client))

        try:
            await asyncio.wait_for(waiting.wait(), 2)
            await first.detach()
            await asyncio.wait_for(closing.wait(), 2)
            assert worker.controller is old_owner
            permission = asyncio.create_task(worker.client.options.can_use_tool(
                "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id="tool")))
            restore = asyncio.create_task(claude_service.restore(recovery_machine(
                profile, spawn, socket=first.connection.socket_path)))
            await asyncio.wait_for(conflict.wait(), 2)
            release_close.set()
            await asyncio.wait_for(restore, 2)
            assert opens == [worker.id, worker.id]
            assert len(recovered) == 1 and recovered[0].id == worker.id
            assert worker.controller is not None and worker.controller is not old_owner
            assert isinstance(await asyncio.wait_for(permission, 2), PermissionResultAllow)
            assert worker.client.prompts == ["accepted once"]
            assert worker.turn["id"] == "original-turn"
            assert not worker.client.closed and worker.client.interrupts == 0
        finally:
            release_close.set()
            if restore is not None:
                restore.cancel()
                await asyncio.gather(restore, return_exceptions=True)
            for client in recovered:
                await client.detach()
            if permission is not None:
                permission.cancel()
                await asyncio.gather(permission, return_exceptions=True)
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_live_controller_is_not_replaced_when_retry_budget_expires(monkeypatch):
    monkeypatch.setattr(client_module, "CONTROLLER_LEASE_WAIT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(client_module, "CONTROLLER_LEASE_RETRY_DELAY", 0.01, raising=False)
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "original-turn"}
        await first.query("accepted once")
        worker = service.sessions[first.id]
        old_owner = worker.controller
        second = replacement(first, worker)
        opens = []
        original_dispatch = service.dispatch

        async def dispatch(owner, request_id, method, params):
            if method == "open":
                opens.append(params["session"])
            return await original_dispatch(owner, request_id, method, params)

        monkeypatch.setattr(service, "dispatch", dispatch)
        try:
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second.connect(), 1)
            assert asyncio.get_running_loop().time() - started < 0.5
            assert len(opens) >= 2 and set(opens) == {worker.id}
            assert second.connection.task.done() and second.callback_task is None
            assert worker.controller is old_owner
            assert len(service.sessions) == 1 and not worker.client.closed
            assert worker.client.prompts == ["accepted once"]
            await first.call("metadata", {"value": {"still_connected": True}})
            assert worker.metadata["still_connected"]
        finally:
            await second.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "missing", "other_error", "legacy_missing"])
async def test_recovery_does_not_retry_or_spawn_on_other_open_failures(monkeypatch, failure):
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        await first.detach()
        await released(worker)
        override = ({"cwd": "/different"} if failure == "identity" else
                    {"service_id": "missing-worker"} if failure == "missing" else {})
        second = replacement(first, worker, **override)
        opens = []
        original_dispatch = service.dispatch

        async def dispatch(owner, request_id, method, params):
            if method == "open":
                opens.append(params)
                if failure in {"other_error", "legacy_missing"}:
                    raise RuntimeError("a non-lease service failure")
            value = await original_dispatch(owner, request_id, method, params)
            if failure == "legacy_missing":
                if method == "hello":
                    value.pop("strict_controller_leases", None)
                elif method == "list":
                    return []
            return value

        monkeypatch.setattr(service, "dispatch", dispatch)
        try:
            with pytest.raises(RuntimeError):
                await second.connect()
            assert len(opens) == 1
            assert list(service.sessions) == [worker.id]
            assert worker.controller is None and worker.client.prompts == []
            assert second.connection.task.done()
        finally:
            await second.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_fails", [False, True])
async def test_explicit_reconnect_retires_worker_identity_only_after_confirmed_close(
    monkeypatch, close_fails,
):
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        sdk = SdkHandle(SimpleNamespace())
        sdk.client = first
        sdk.service_metadata = {**worker.metadata, "service_id": worker.id}
        if close_fails:
            original_dispatch = service.dispatch

            async def dispatch(owner, request_id, method, params):
                if method == "close":
                    raise RuntimeError("close failed")
                return await original_dispatch(owner, request_id, method, params)

            monkeypatch.setattr(service, "dispatch", dispatch)
            with pytest.raises(RuntimeError):
                await sdk.disconnect()
            assert sdk.service_metadata["service_id"] == worker.id
            assert not worker.client.closed
        else:
            await sdk.disconnect()
            assert "service_id" not in sdk.service_metadata
            assert worker.client.closed
            second = RemoteClient(first.connection.socket_path, options=first.options,
                                  metadata=sdk.service_metadata.copy())
            try:
                await second.connect()
                assert second.id != worker.id
                assert service.sessions[second.id].client.prompts == []
                assert list(service.sessions) == [second.id]
            finally:
                await second.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
async def test_unknown_open_response_is_not_retried(monkeypatch, failure):
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "original-turn"}
        await first.query("accepted once")
        worker = service.sessions[first.id]
        await first.detach()
        await released(worker)
        second = replacement(first, worker)
        original_call = second.connection.call
        opens = []

        async def lost_response(method, *args, **kwargs):
            value = await original_call(method, *args, **kwargs)
            if method == "open":
                opens.append(value["id"])
                raise failure("open acknowledgement lost")
            return value

        monkeypatch.setattr(second.connection, "call", lost_response)
        try:
            with pytest.raises(failure):
                await second.connect()
            await released(worker)
            assert opens == [worker.id]
            assert not worker.client.closed and worker.client.prompts == ["accepted once"]
            assert second.connection.task.done() and second.callback_task is None
        finally:
            await second.detach()


@pytest.mark.asyncio
async def test_legacy_controller_can_reconnect_after_deliberate_close():
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        await first.disconnect()
        connection = Connection(first.connection.socket_path)
        await connection.connect()
        try:
            # Old controllers did not clear their stale worker hint after close
            # or negotiate strict_session. Preserve that established contract.
            description = await connection.call("open", {
                "session": worker.id, "metadata": worker.metadata.copy(),
                "options": options_payload(first.options),
            })
            assert description["id"] != worker.id
            assert list(service.sessions) == [description["id"]]
            assert service.sessions[description["id"]].client.prompts == []
        finally:
            await connection.disconnect()


@pytest.mark.asyncio
async def test_cancellation_during_lease_wait_closes_only_replacement_connection(monkeypatch):
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        old_owner = worker.controller
        second = replacement(first, worker)
        conflict = asyncio.Event()
        original_dispatch = service.dispatch

        async def dispatch(owner, request_id, method, params):
            if method == "open":
                conflict.set()
            return await original_dispatch(owner, request_id, method, params)

        monkeypatch.setattr(service, "dispatch", dispatch)
        connecting = asyncio.create_task(second.connect())
        try:
            await asyncio.wait_for(conflict.wait(), 2)
            connecting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await connecting
            assert second.connection.task.done() and second.callback_task is None
            assert worker.controller is old_owner and not worker.client.closed
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            await second.detach()
