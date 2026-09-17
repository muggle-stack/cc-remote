"""Isolate service-session recovery failures without weakening identity checks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from cc_remote.claude_service.client import RemoteClient
from cc_remote.wrapper import claude_service
from tests.test_claude_service import environment, released


def recovery_machine(profile, spawn, *, socket="current", drain_socket=""):
    return SimpleNamespace(
        cfg=SimpleNamespace(claude_service_socket=socket,
                            claude_service_drain_socket=drain_socket),
        _claude_profile=lambda profile_id: profile,
        _spawn=spawn,
    )


def session_item(profile, session_id):
    return {"id": "worker-" + session_id, "metadata": {
        "profile_id": profile.id, "profile_root": str(profile.config_dir),
        "session_id": session_id, "cwd": str(profile.config_dir), "space": "code",
    }}


@pytest.mark.parametrize("failure", ["none", "exception"])
def test_one_failed_session_does_not_strand_other_accepted_turns(failure, monkeypatch):
    async def go():
        warnings = []
        monkeypatch.setattr(claude_service, "log", SimpleNamespace(
            warning=lambda message, **fields: warnings.append((message, fields))), raising=False)

        async def allow(*args):
            return PermissionResultAllow()

        async with environment() as (service, attach):
            profile = SimpleNamespace(id="primary", config_dir=service.directory / "profile")
            originals = {}
            workers = {}
            for name in ("healthy-before", "broken", "healthy-after"):
                client = await attach(session_id=name, permission=allow)
                await client.call("metadata", {"value": {
                    "profile_id": profile.id, "profile_root": str(profile.config_dir),
                    "cwd": str(service.directory),
                }})
                client.next_turn = {"id": "original-" + name}
                await client.query("accepted once: " + name)
                originals[name] = client
                workers[name] = service.sessions[client.id]
                await client.detach()
                await released(workers[name])

            attempted = []
            recovered = []
            permissions = [asyncio.create_task(workers[name].client.options.can_use_tool(
                "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id=name)))
                for name in ("healthy-before", "healthy-after")]

            async def spawn(**kwargs):
                name = kwargs["resume_id"]
                attempted.append(name)
                assert kwargs["_service_recovering"] is True
                assert kwargs["_service_worker_id"] == workers[name].id
                if name == "broken":
                    if failure == "exception":
                        raise OSError("private provider details")
                    return None
                client = RemoteClient(kwargs["_service_socket"],
                                      options=originals[name].options,
                                      metadata=workers[name].metadata.copy())
                recovered.append(client)
                await client.connect()
                client.ready.set()
                return SimpleNamespace(sdk=SimpleNamespace(client=client))

            machine = recovery_machine(profile, spawn,
                                       socket=originals["broken"].connection.socket_path)
            try:
                await claude_service.restore(machine)
                assert attempted == ["healthy-before", "broken", "healthy-after"]
                answers = await asyncio.wait_for(asyncio.gather(*permissions), 2)
                assert all(isinstance(answer, PermissionResultAllow) for answer in answers)
                assert workers["healthy-before"].controller is not None
                assert workers["healthy-after"].controller is not None
                assert workers["broken"].controller is None
                for name, worker in workers.items():
                    assert worker.client.prompts == ["accepted once: " + name]
                    assert worker.turn["id"] == "original-" + name
                    assert not worker.client.closed
                assert len(warnings) == 1
                assert warnings[0][1]["service_id"] == workers["broken"].id
                assert warnings[0][1]["error_type"] == (
                    "OSError" if failure == "exception" else "RuntimeError")
                assert "private provider details" not in repr(warnings)
            finally:
                for client in recovered:
                    await client.detach()
                for task in permissions:
                    task.cancel()
                await asyncio.gather(*permissions, return_exceptions=True)

    asyncio.run(go())


def test_duplicate_scan_finishes_before_attaching_any_session(tmp_path, monkeypatch):
    async def go():
        profile = SimpleNamespace(id="primary", config_dir=tmp_path)
        duplicate = session_item(profile, "duplicate")
        listings = {
            "drain": [session_item(profile, "healthy"), duplicate],
            "current": [{**duplicate, "id": "other-worker"}],
        }
        listed = []

        async def list_sessions(socket):
            listed.append(socket)
            return listings[socket]

        monkeypatch.setattr(claude_service, "_list_sessions", list_sessions)
        spawn = AsyncMock()
        machine = recovery_machine(profile, spawn, drain_socket="drain")
        with pytest.raises(RuntimeError, match="exists in both SDK services"):
            await claude_service.restore(machine)
        assert listed == ["drain", "current"]
        spawn.assert_not_awaited()

    asyncio.run(go())


@pytest.mark.parametrize("unavailable", ["primary", "drain"])
def test_unavailable_service_does_not_strand_other_services_turns(unavailable, monkeypatch):
    async def go():
        warnings = []
        monkeypatch.setattr(claude_service, "log", SimpleNamespace(
            warning=lambda message, **fields: warnings.append((message, fields))))

        async def allow(*args):
            return PermissionResultAllow()

        async with environment() as (service, attach):
            profile = SimpleNamespace(id="primary", config_dir=service.directory / "profile")
            original = await attach(permission=allow)
            await original.call("metadata", {"value": {
                "profile_id": profile.id, "profile_root": str(profile.config_dir),
                "cwd": str(service.directory),
            }})
            original.next_turn = {"id": "original-turn"}
            await original.query("accepted once")
            worker = service.sessions[original.id]
            await original.detach()
            await released(worker)
            permission = asyncio.create_task(worker.client.options.can_use_tool(
                "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id="tool")))
            recovered = []

            async def spawn(**kwargs):
                assert kwargs["_service_recovering"] is True
                assert kwargs["_service_worker_id"] == worker.id
                assert kwargs["_service_socket"] == original.connection.socket_path
                client = RemoteClient(kwargs["_service_socket"], options=original.options,
                                      metadata=worker.metadata.copy())
                recovered.append(client)
                await client.connect()
                client.ready.set()
                return SimpleNamespace(sdk=SimpleNamespace(client=client))

            sockets = {"primary": original.connection.socket_path,
                       "drain": original.connection.socket_path}
            sockets[unavailable] = str(service.directory / "missing.sock")
            machine = recovery_machine(profile, spawn, socket=sockets["primary"],
                                       drain_socket=sockets["drain"])
            try:
                await claude_service.restore(machine)
                assert isinstance(await asyncio.wait_for(permission, 2), PermissionResultAllow)
                assert len(recovered) == 1 and worker.controller is not None
                assert worker.client.prompts == ["accepted once"]
                assert worker.turn["id"] == "original-turn" and not worker.client.closed
                assert len(warnings) == 1
                assert warnings[0][1] == {
                    "service_role": unavailable, "error_type": "FileNotFoundError"}
            finally:
                for client in recovered:
                    await client.detach()
                permission.cancel()
                await asyncio.gather(permission, return_exceptions=True)

    asyncio.run(go())


def test_all_unavailable_services_do_not_spawn_a_replacement(tmp_path, monkeypatch):
    async def go():
        profile = SimpleNamespace(id="primary", config_dir=tmp_path)
        listing = AsyncMock(side_effect=ConnectionError("private socket details"))
        monkeypatch.setattr(claude_service, "_list_sessions", listing)
        spawn = AsyncMock()
        await claude_service.restore(recovery_machine(profile, spawn, drain_socket="drain"))
        assert [call.args[0] for call in listing.await_args_list] == ["drain", "current"]
        spawn.assert_not_awaited()

    asyncio.run(go())


def test_service_listing_cancellation_stops_recovery(tmp_path, monkeypatch):
    async def go():
        profile = SimpleNamespace(id="primary", config_dir=tmp_path)
        listing = AsyncMock(side_effect=asyncio.CancelledError())
        monkeypatch.setattr(claude_service, "_list_sessions", listing)
        spawn = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await claude_service.restore(recovery_machine(profile, spawn, drain_socket="drain"))
        assert listing.await_count == 1
        spawn.assert_not_awaited()

    asyncio.run(go())


def test_session_recovery_cancellation_stops_the_restore_loop(tmp_path, monkeypatch):
    async def go():
        profile = SimpleNamespace(id="primary", config_dir=tmp_path)
        monkeypatch.setattr(claude_service, "_list_sessions", AsyncMock(return_value=[
            session_item(profile, "first"), session_item(profile, "second"),
        ]))
        spawn = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await claude_service.restore(recovery_machine(profile, spawn))
        assert spawn.await_count == 1

    asyncio.run(go())
