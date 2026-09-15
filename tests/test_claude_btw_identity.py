"""Private Claude forks must not block catalogs before their first prompt."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from cc_remote.protocol import CloseBtw, ListSessions, SessionList
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.fixture
def setup(monkeypatch):
    machine, transport = _mk_machine()
    handles = []
    catalog = []
    deleted = []

    class Handle(SdkHandle):
        @staticmethod
        def preflight(_path):
            pass

        async def connect(self, resume_id=None, cwd=None, fork=False):
            self.launch = self._options(resume_id, cwd, fork=fork)
            handles.append(self)
            # Model a native transcript visible as soon as connect starts,
            # before _spawn_btw has inserted the context into the pool.
            catalog.append(SimpleNamespace(
                session_id=self.launch.session_id, summary="private", cwd=cwd,
                first_prompt="private prompt", last_modified=1,
                git_branch=None, tag=None,
            ))
            assert self.launch.session_id in machine._load_private_btw_sessions()
            listed = await machine._handle_list_sessions(ListSessions(
                client_id="other-client", cmd_id="during-connect"))
            assert isinstance(listed, SessionList)
            assert listed.sessions == []

        async def disconnect(self):
            pass

    monkeypatch.setattr(machine_module, "SdkHandle", Handle)
    monkeypatch.setattr(machine, "_claude_catalog_list_sessions",
                        lambda *_args, **_kwargs: list(catalog))
    monkeypatch.setattr(machine, "_bg_blocked_session_ids", lambda *_args: set())
    monkeypatch.setattr(machine, "_claude_catalog_delete_session",
                        lambda _profile, sid, **_kwargs: deleted.append(sid))
    parent = _mk_ctx("parent", "parent")
    parent.sdk = SimpleNamespace(permission_mode="plan")
    machine.sessions[parent.key] = parent
    machine.focused_sid = parent.key
    return machine, transport, parent, handles, deleted


def test_empty_btw_is_private_without_blocking_owner_or_other_client(setup):
    async def run():
        machine, transport, parent, handles, _deleted = setup
        fork = await machine._spawn_btw(parent, owner_client_id="owner")
        reserved = fork.btw_reserved_id
        assert str(UUID(reserved)) == reserved
        assert handles[0].launch.session_id == reserved
        assert fork.btw_real_id is None
        assert fork.session_id is None
        assert machine.focused_sid == parent.key
        for client in ("owner", "other-client"):
            listed = await machine._handle_list_sessions(ListSessions(
                client_id=client, cmd_id=client))
            assert isinstance(listed, SessionList)
            assert listed.sessions == []
        assert all(event.type != "error" for event in transport.sent)
        # Reserved is not yet resumable. A settings reconnect must repeat the
        # fork with its original reserved UUID until a native init captures it.
        assert machine._claude_reconnect_identity(fork) == (parent.session_id, True)
        assert fork.sdk._options(parent.session_id, fork=True).session_id == reserved
        await machine._capture_session_id(fork, reserved)
        assert machine._claude_reconnect_identity(fork) == (reserved, False)
        assert fork.sdk._options(reserved).session_id is None
        assert fork.key.startswith("btw-")
        assert parent.session_id == "parent"

    asyncio.run(run())


@pytest.mark.parametrize("captured", [False, True])
def test_close_cleans_reserved_identity_even_without_a_first_turn(setup, captured):
    async def run():
        machine, _transport, parent, _handles, deleted = setup
        fork = await machine._spawn_btw(parent, owner_client_id="owner")
        reserved = fork.btw_reserved_id
        if captured:
            await machine._capture_session_id(fork, reserved)
        await machine._handle_close_btw(CloseBtw(sid=fork.key, client_id="owner"))
        assert deleted == [reserved]
        assert reserved not in machine._load_private_btw_sessions()
        assert parent.key in machine.sessions

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("disconnect_failed", [False, True])
def test_failed_fork_retains_privacy_until_native_writer_is_stopped(
    setup, monkeypatch, cancelled, disconnect_failed,
):
    async def run():
        machine, _transport, parent, handles, deleted = setup
        handle_type = machine_module.SdkHandle
        connect = handle_type.connect

        async def fail_connect(self, **kwargs):
            await connect(self, **kwargs)
            if cancelled:
                raise asyncio.CancelledError()
            raise RuntimeError("fixture connect failure")

        async def fail_disconnect(self):
            raise RuntimeError("fixture disconnect failure")

        monkeypatch.setattr(handle_type, "connect", fail_connect)
        if disconnect_failed:
            monkeypatch.setattr(handle_type, "disconnect", fail_disconnect)
        error = asyncio.CancelledError if cancelled else machine_module._BtwSpawnFailure
        with pytest.raises(error):
            await machine._spawn_btw(parent, owner_client_id="owner")
        reserved = handles[0].launch.session_id
        assert deleted == [reserved]
        assert (reserved in machine._load_private_btw_sessions()) is disconnect_failed
        assert list(machine.sessions) == [parent.key]

    asyncio.run(run())


def test_failed_private_registration_never_starts_a_native_fork(setup, monkeypatch):
    async def run():
        machine, _transport, parent, handles, deleted = setup

        def fail(*_args):
            raise RuntimeError("fixture storage failure")

        monkeypatch.setattr(machine, "_remember_private_btw", fail)
        with pytest.raises(machine_module._BtwSpawnFailure):
            await machine._spawn_btw(parent, owner_client_id="owner")
        assert handles == []
        assert deleted == []
        assert list(machine.sessions) == [parent.key]

    asyncio.run(run())


def test_reservation_is_not_counted_twice_against_private_fork_cap(setup, monkeypatch):
    async def run():
        machine, _transport, parent, _handles, _deleted = setup
        monkeypatch.setattr(machine, "PRIVATE_BTW_CAP", 2)
        first = await machine._spawn_btw(parent, owner_client_id="owner")
        second = await machine._spawn_btw(parent, owner_client_id="owner")
        assert first.btw_reserved_id != second.btw_reserved_id
        with pytest.raises(machine_module._BtwSpawnFailure):
            await machine._spawn_btw(parent, owner_client_id="owner")

    asyncio.run(run())
