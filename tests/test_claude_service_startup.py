"""Slow native startup must isolate sessions without creating duplicate writers."""

import asyncio

import pytest

from cc_remote.claude_service.server import Service
from cc_remote.claude_service import server as service_module
from cc_remote.claude_service.wire import ControllerLeaseConflict
from tests.test_claude_service import FakeClient


def params(sid):
    return {"metadata": {"session_id": sid, "space": "code", "cwd": f"/{sid}"},
            "options": {"cwd": f"/{sid}"}}


@pytest.mark.asyncio
async def test_slow_start_does_not_block_other_session_open_or_attach(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def connect(self):
            if self.options.cwd == "/slow":
                started.set()
                await release.wait()

    service = Service(tmp_path, factory=Client)
    owner = object()
    existing = await service.dispatch(owner, "existing", "open", params("existing"))
    slow = asyncio.create_task(service.dispatch(owner, "slow", "open", params("slow")))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        attached = await asyncio.wait_for(service.dispatch(
            owner, "attach", "open", params("existing")), 0.5)
        fast = await asyncio.wait_for(service.dispatch(owner, "fast", "open", params("fast")), 0.5)
        assert attached["attached"] and attached["id"] == existing["id"]
        assert not fast["attached"] and not slow.done()
        await service.dispatch(owner, "query", "query", {
            "session": existing["id"], "prompt": "accepted once", "turn": {"id": "turn"}})
        assert service.sessions[existing["id"]].client.prompts == ["accepted once"]
    finally:
        release.set()
        await asyncio.gather(slow, return_exceptions=True)
        for session in service.sessions.values():
            await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_owner", [False, True])
async def test_concurrent_same_identity_starts_once_and_preserves_lease(tmp_path, other_owner):
    started, release = asyncio.Event(), asyncio.Event()
    clients = []

    class Client(FakeClient):
        async def connect(self):
            clients.append(self)
            started.set()
            await release.wait()

    service = Service(tmp_path, factory=Client)
    owner = object()
    first = asyncio.create_task(service.dispatch(owner, "one", "open", params("same")))
    await asyncio.wait_for(started.wait(), 0.5)
    second = asyncio.create_task(service.dispatch(
        object() if other_owner else owner, "two", "open", params("same")))
    try:
        await asyncio.sleep(0)
        assert not second.done() and len(clients) == 1
        release.set()
        opened = await asyncio.wait_for(first, 0.5)
        if other_owner:
            with pytest.raises(ControllerLeaseConflict):
                await asyncio.wait_for(second, 0.5)
        else:
            attached = await asyncio.wait_for(second, 0.5)
            assert attached["id"] == opened["id"] and attached["attached"]
        assert len(clients) == 1
        assert service.sessions[opened["id"]].controller is owner
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        for session in service.sessions.values():
            await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_failed_start_keeps_identity_reserved_until_cleanup_and_wakes_joiners(tmp_path, failure):
    started, release_start, closing, release_close = (asyncio.Event() for _ in range(4))
    clients = []

    class Client(FakeClient):
        async def connect(self):
            clients.append(self)
            if self.options.cwd == "/broken":
                started.set()
                await release_start.wait()
                raise RuntimeError("deliberate startup failure")

        async def disconnect(self):
            if self.options.cwd == "/broken":
                closing.set()
                await release_close.wait()
            await super().disconnect()

    service = Service(tmp_path, factory=Client)
    owner = object()
    first = asyncio.create_task(service.dispatch(owner, "one", "open", params("broken")))
    await asyncio.wait_for(started.wait(), 0.5)
    if failure == "cancel":
        first.cancel()
    else:
        release_start.set()
    await asyncio.wait_for(closing.wait(), 0.5)
    joining = asyncio.create_task(service.dispatch(owner, "two", "open", params("broken")))
    try:
        fast = await asyncio.wait_for(service.dispatch(owner, "fast", "open", params("fast")), 0.5)
        await asyncio.sleep(0)
        assert not joining.done()
        assert len(clients) == 2 and len(service.sessions) == 2
        release_close.set()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            await first
        with pytest.raises(RuntimeError, match="no longer available"):
            await asyncio.wait_for(joining, 0.5)
        assert list(service.sessions) == [fast["id"]]
        assert len(clients) == 2 and clients[0].closed
        assert {path.stem for path in tmp_path.glob("*.sqlite3")} == {fast["id"]}
        assert not service._opening
    finally:
        release_start.set()
        release_close.set()
        await asyncio.gather(first, joining, return_exceptions=True)
        for session in service.sessions.values():
            await session.close()


@pytest.mark.asyncio
async def test_cancelling_startup_joiner_does_not_cancel_creator(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def connect(self):
            started.set()
            await release.wait()

    service = Service(tmp_path, factory=Client)
    owner = object()
    first = asyncio.create_task(service.dispatch(owner, "one", "open", params("same")))
    await asyncio.wait_for(started.wait(), 0.5)
    joining = asyncio.create_task(service.dispatch(owner, "two", "open", params("same")))
    try:
        await asyncio.sleep(0)
        joining.cancel()
        with pytest.raises(asyncio.CancelledError):
            await joining
        assert not first.done() and len(service.sessions) == 1
        release.set()
        opened = await asyncio.wait_for(first, 0.5)
        session = service.sessions[opened["id"]]
        assert session.controller is owner and not session.client.closed
    finally:
        release.set()
        await asyncio.gather(first, joining, return_exceptions=True)
        for session in service.sessions.values():
            await session.close()


@pytest.mark.asyncio
async def test_starting_session_counts_toward_capacity_and_checks_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_SESSIONS", 1)
    started, release = asyncio.Event(), asyncio.Event()

    class Client(FakeClient):
        async def connect(self):
            started.set()
            await release.wait()

    service = Service(tmp_path, factory=Client)
    owner = object()
    first = asyncio.create_task(service.dispatch(owner, "one", "open", params("same")))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        with pytest.raises(RuntimeError, match="capacity"):
            await asyncio.wait_for(service.dispatch(owner, "two", "open", params("other")), 0.5)
        mismatched = params("same")
        mismatched["metadata"]["cwd"] = "/different"
        with pytest.raises(PermissionError, match="identity mismatch"):
            await asyncio.wait_for(service.dispatch(owner, "bad", "open", mismatched), 0.5)
        assert not first.done() and len(service.sessions) == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        for session in service.sessions.values():
            await session.close()
