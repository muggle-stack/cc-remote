"""Zero-token tests for reliable client commands and wrapper deduplication."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from cc_remote.protocol import (
    BtwOpened,
    CloseBtw,
    CommandAck,
    Error,
    GetHistory,
    Hello,
    ListSessions,
    OpenBtw,
    Perm,
    Ping,
    Query,
    Snapshot,
    SessionList,
    SwitchSession,
    Takeover,
    TakeoverState,
    UserMsg,
    deserialize,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.relay.pairing import RelayHub
from tests.test_multisession import _mk_ctx, _mk_machine


def test_command_envelope_and_routed_ack_roundtrip():
    command = Query(
        prompt="hello",
        msg_id="msg-1",
        cmd_id="cmd-1",
        client_id="client-1",
    )
    assert deserialize(serialize(command)) == command
    takeover = Takeover(
        sid="session-1", cmd_id="takeover-1", client_id="client-1")
    assert deserialize(serialize(takeover)) == takeover
    takeover_state = TakeoverState(
        sid="session-1", pending=True, message="waiting")
    assert deserialize(serialize(takeover_state)) == takeover_state
    with pytest.raises(ValidationError):
        Takeover(sid="session-1")
    ack = CommandAck(
        cmd_id="cmd-1",
        client_id="client-1",
        to="client-1",
    )
    assert deserialize(serialize(ack)) == ack
    with pytest.raises(ValidationError):
        CommandAck(cmd_id="cmd-1", client_id="client-1")
    with pytest.raises(ValidationError):
        Hello(role="client", client_id="client-1", cmd_id="not-reliable")
    with pytest.raises(ValidationError):
        Ping(n=1, cmd_id="not-reliable")


def test_open_btw_request_id_roundtrip_is_required_on_both_frames():
    command = OpenBtw(
        sid="parent-1",
        request_id="btw-request-1",
        cmd_id="btw-command-1",
        client_id="client-1",
    )
    assert deserialize(serialize(command)) == command
    opened = BtwOpened(
        request_id="btw-request-1",
        btw_sid="btw-1",
        parent_sid="parent-1",
        engine="claude",
        to="client-1",
    )
    assert deserialize(serialize(opened)) == opened
    with pytest.raises(ValidationError):
        OpenBtw(sid="parent-1")
    with pytest.raises(ValidationError):
        BtwOpened(btw_sid="btw-1", parent_sid="parent-1", engine="claude")


def test_wrapper_deduplicates_processed_command_and_resends_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = Query(
            prompt="hello", msg_id="msg-1",
            cmd_id="cmd-1", client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["cmd-1"]
        acks = [msg for msg in transport.sent if isinstance(msg, CommandAck)]
        assert len(acks) == 2
        assert all(
            ack.cmd_id == "cmd-1"
            and ack.client_id == "client-1"
            and ack.to == "client-1"
            for ack in acks
        )

    asyncio.run(run())


def test_takeover_duplicate_is_at_most_once_and_only_resends_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = Takeover(
            sid="session-1", cmd_id="takeover-1", client_id="client-1")
        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["takeover-1"]
        assert [msg.type for msg in transport.sent] == [
            "command_ack", "command_ack"]

    asyncio.run(run())


def test_duplicate_safe_read_reexecutes_handler_before_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = GetHistory(
            session_id="session-1",
            cmd_id="read-1",
            client_id="client-1",
        )
        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["read-1", "read-1"]
        assert len([
            msg for msg in transport.sent if isinstance(msg, CommandAck)
        ]) == 2

    asyncio.run(run())


def test_wrapper_does_not_ack_or_remember_a_crashed_handler():
    async def run():
        machine, transport = _mk_machine()
        calls = 0

        async def boom(_cmd):
            nonlocal calls
            calls += 1
            raise RuntimeError("handler crashed")

        machine._handle = boom
        command = Query(
            prompt="hello", msg_id="msg-1",
            cmd_id="cmd-1", client_id="client-1",
        )
        for _ in range(2):
            with pytest.raises(RuntimeError, match="handler crashed"):
                await machine._process_command(command)

        assert calls == 2
        assert not [msg for msg in transport.sent if isinstance(msg, CommandAck)]

    asyncio.run(run())


def test_wrapper_dedupe_cache_is_bounded_per_client():
    async def run():
        machine, _ = _mk_machine()
        machine.COMMAND_IDS_PER_CLIENT = 2
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        for cmd_id in ("one", "two", "three", "one"):
            await machine._process_command(Query(
                prompt=cmd_id,
                msg_id=f"msg-{cmd_id}",
                cmd_id=cmd_id,
                client_id="client-1",
            ))

        # "one" was evicted after "three", so it is processed again; the cache
        # itself remains at the configured hard bound.
        assert handled == ["one", "two", "three", "one"]
        assert len(machine._processed_commands["client-1"]) == 2

    asyncio.run(run())


def test_business_rejection_is_acknowledged_after_error():
    async def run():
        machine, transport = _mk_machine()
        await machine._process_command(Query(
            sid="missing-session",
            prompt="hello",
            msg_id="msg-1",
            cmd_id="cmd-1",
            client_id="client-1",
        ))
        command = Query(
            sid="missing-session",
            prompt="hello",
            msg_id="msg-2",
            cmd_id="cmd-2",
            client_id="client-1",
        )
        await machine._process_command(command)
        await machine._process_command(command)
        assert [msg.type for msg in transport.sent] == [
            "error", "command_ack", "error", "command_ack",
            "error", "command_ack",
        ]
        replayed = transport.sent[-2]
        assert replayed.type == "error"
        assert replayed.msg_id == "msg-2"
        assert replayed.to == "client-1"

    asyncio.run(run())


def test_open_btw_missing_parent_error_is_correlated_targeted_and_replayed():
    async def run():
        machine, transport = _mk_machine()
        command = OpenBtw(
            sid="missing-parent",
            request_id="btw-request-1",
            cmd_id="btw-command-1",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert [message.type for message in transport.sent] == [
            "error", "command_ack", "error", "command_ack"]
        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 2
        assert all(
            message.request_id == "btw-request-1"
            and message.to == "client-1"
            and message.sid == "missing-parent"
            for message in errors
        )

    asyncio.run(run())


def test_ownerless_open_btw_fails_closed_without_broadcast():
    async def run():
        machine, transport = _mk_machine()
        await machine._handle(OpenBtw(
            sid="parent-1", request_id="ownerless-request"))
        assert transport.sent == []
        assert machine.sessions == {}

    asyncio.run(run())


def test_open_btw_spawn_rejection_is_correlated_and_cached():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("tmp-parent", session_id=None)
        machine.sessions[parent.key] = parent
        machine.focused_sid = parent.key
        command = OpenBtw(
            sid=parent.key,
            request_id="btw-request-spawn-fail",
            cmd_id="btw-command-spawn-fail",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 2
        assert all(
            message.request_id == "btw-request-spawn-fail"
            and message.to == "client-1"
            and message.sid == parent.key
            for message in errors
        )
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 2

    asyncio.run(run())


def test_open_btw_success_response_is_correlated_and_replayed_without_refork():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent-1", session_id="parent-1")
        fork = _mk_ctx("btw-fork-1", session_id=None)
        fork.key = "btw-fork-1"
        fork.btw = True
        fork.parent_sid = parent.session_id
        machine.sessions[parent.key] = parent
        machine.focused_sid = parent.key
        spawn_calls = 0

        async def fake_spawn(_parent, owner_client_id=None):
            nonlocal spawn_calls
            spawn_calls += 1
            assert owner_client_id == "client-1"
            return fork

        machine._spawn_btw = fake_spawn
        command = OpenBtw(
            sid=parent.key,
            request_id="btw-request-success",
            cmd_id="btw-command-success",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert spawn_calls == 1
        opened = [message for message in transport.sent
                  if isinstance(message, BtwOpened)]
        assert len(opened) == 2
        assert all(
            message.request_id == "btw-request-success"
            and message.to == "client-1"
            and message.btw_sid == fork.key
            for message in opened
        )
        snapshots = [message for message in transport.sent
                     if isinstance(message, Snapshot)]
        assert len(snapshots) == 2
        assert all(
            message.sid == fork.key
            and message.to == "client-1"
            and message.generation == machine.instance_id
            for message in snapshots
        )
        permissions = [message for message in transport.sent
                       if isinstance(message, Perm)]
        assert len(permissions) == 2
        assert all(
            message.sid == fork.key
            and message.to == "client-1"
            and message.mode == "bypassPermissions"
            for message in permissions
        )
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 2

    asyncio.run(run())


def test_btw_live_frames_are_routed_and_buffered_for_owner_only():
    async def run():
        machine, transport = _mk_machine()
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"

        await machine._emit(
            fork, UserMsg(msg_id="private-msg", prompt="private prompt"))

        sent = transport.sent[-1]
        assert sent.sid == "btw-private"
        assert sent.to == "owner-client"
        buffered = list(fork.buffer._buf)
        assert len(buffered) == 1
        assert buffered[0][1].to == "owner-client"

    asyncio.run(run())


def test_nonowner_cannot_query_close_or_focus_btw_runtime():
    async def run():
        machine, transport = _mk_machine()
        normal = _mk_ctx("normal", session_id="normal")
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.parent_sid = "normal"
        fork.owner_client_id = "owner-client"
        machine.sessions = {normal.key: normal, fork.key: fork}
        machine.focused_sid = normal.key

        commands = [
            Query(
                sid=fork.key, prompt="steal", msg_id="private-query",
                cmd_id="query-command", client_id="other-client",
            ),
            CloseBtw(
                sid=fork.key, cmd_id="close-command",
                client_id="other-client",
            ),
            SwitchSession(
                session_id=fork.key, cmd_id="switch-command",
                client_id="other-client",
            ),
        ]
        for command in commands:
            await machine._process_command(command)

        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 3
        assert all(
            message.code == "auth"
            and message.sid == fork.key
            and message.to == "other-client"
            for message in errors
        )
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 3
        assert machine.sessions[fork.key] is fork
        assert fork.state == "idle" and fork.turn_task is None
        assert machine.focused_sid == normal.key

    asyncio.run(run())


def test_claude_btw_real_id_is_hidden_and_cannot_be_cold_resumed(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        real_id = "11111111-2222-4333-8444-555555555555"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        fork.btw_real_id = real_id
        machine.sessions[fork.key] = fork
        machine._private_btw_sessions[real_id] = {
            "cwd": fork.cwd, "created_at": 1.0,
        }

        def info(session_id):
            return SimpleNamespace(
                session_id=session_id, summary=None, custom_title=None,
                last_modified=None, first_prompt=None, git_branch=None,
                cwd=fork.cwd, tag=None,
            )

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda limit=200: [info(real_id), info("normal-session")],
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="owner-client"))
        listing = next(message for message in transport.sent
                       if isinstance(message, SessionList))
        assert [row.session_id for row in listing.sessions] == ["normal-session"]

        spawned = []

        async def forbidden_spawn(*_args, **_kwargs):
            spawned.append(True)
            raise AssertionError("private fork must not be resumed")

        machine._spawn = forbidden_spawn
        # Both a non-owner and the original owner are denied when they address
        # the internal real transcript id rather than the stable btw-* key.
        for index, client in enumerate(("other-client", "owner-client"), 1):
            await machine._process_command(SwitchSession(
                session_id=real_id,
                cmd_id=f"switch-private-{index}",
                client_id=client,
            ))

        # The tombstone remains authoritative after the live ctx is gone too.
        machine.sessions.clear()
        await machine._process_command(SwitchSession(
            session_id=real_id,
            cmd_id="switch-private-cold",
            client_id="owner-client",
        ))
        assert not spawned
        denied = [message for message in transport.sent
                  if isinstance(message, Error)
                  and message.sid in {fork.key, real_id}]
        assert len(denied) == 3
        assert all(message.code == "auth" for message in denied)

    asyncio.run(run())


def test_claude_session_list_is_withheld_until_btw_real_id_is_tombstoned(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        fork = _mk_ctx("btw-pending", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        machine.sessions[fork.key] = fork

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("unsafe session scan must not run")),
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="requester-client"))

        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert isinstance(error, Error) and error.code == "busy"
        assert error.to == "requester-client"

    asyncio.run(run())


def test_claude_btw_tombstone_survives_restart_until_delete_succeeds(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        real_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"

        await machine._capture_session_id(fork, real_id)
        assert real_id in machine._private_btw_sessions
        assert machine._private_btw_file().exists()

        restarted = machine.__class__(machine.cfg, transport)
        assert real_id in restarted._private_btw_sessions

        def fail_delete(*_args, **_kwargs):
            raise PermissionError("still private")

        monkeypatch.setattr(machine_module, "delete_session", fail_delete)
        await restarted._cleanup_private_btw_sessions()
        assert real_id in restarted._private_btw_sessions

        monkeypatch.setattr(machine_module, "delete_session",
                            lambda *_args, **_kwargs: None)
        await restarted._cleanup_private_btw_sessions()
        assert real_id not in restarted._private_btw_sessions

    asyncio.run(run())


def test_corrupt_private_btw_state_refuses_fail_open_startup():
    machine, transport = _mk_machine()
    machine._private_btw_file().write_text("not-json")

    with pytest.raises(RuntimeError, match="refusing fail-open"):
        machine.__class__(machine.cfg, transport)


def test_btw_capture_persistence_failure_terminates_and_deletes_fork(monkeypatch):
    class Sdk:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    async def run():
        machine, _ = _mk_machine()
        real_id = "99999999-8888-4777-8666-555555555555"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        fork.sdk = Sdk()
        machine.sessions[fork.key] = fork

        monkeypatch.setattr(
            machine, "_persist_private_btw_sessions",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("disk denied")),
        )
        deleted = []
        monkeypatch.setattr(
            machine_module, "delete_session",
            lambda sid, directory=None: deleted.append((sid, directory)),
        )

        with pytest.raises(RuntimeError, match="fork terminated"):
            await machine._capture_session_id(fork, real_id)

        assert fork.sdk.disconnected is True
        assert fork.key not in machine.sessions
        assert fork.btw_real_id == real_id
        assert real_id not in machine._private_btw_sessions
        assert deleted == [(real_id, fork.cwd)]

    asyncio.run(run())


def test_btw_capture_keeps_live_guard_when_persist_and_delete_both_fail(
        monkeypatch):
    class Sdk:
        async def disconnect(self):
            return None

    async def run():
        machine, _ = _mk_machine()
        real_id = "12345678-1234-4234-8234-123456789abc"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        fork.sdk = Sdk()
        machine.sessions[fork.key] = fork

        monkeypatch.setattr(
            machine, "_persist_private_btw_sessions",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("disk denied")),
        )
        monkeypatch.setattr(
            machine_module, "delete_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                PermissionError("delete denied")),
        )

        with pytest.raises(RuntimeError, match="fork terminated"):
            await machine._capture_session_id(fork, real_id)

        assert fork.key not in machine.sessions
        assert real_id in machine._private_btw_sessions

        def info(session_id):
            return SimpleNamespace(
                session_id=session_id, summary=None, custom_title=None,
                last_modified=None, first_prompt=None, git_branch=None,
                cwd=fork.cwd, tag=None,
            )

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda limit=200: [info(real_id), info("normal-session")],
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="owner-client"))
        listing = next(message for message in machine.transport.sent
                       if isinstance(message, SessionList))
        assert [row.session_id for row in listing.sessions] == ["normal-session"]

    asyncio.run(run())


def test_relay_overwrites_spoofed_command_client_id_from_bound_hello():
    class ClientWs:
        def __init__(self, frames):
            self.frames = iter(frames)

        async def receive_text(self):
            try:
                return next(self.frames)
            except StopIteration as exc:
                raise WebSocketDisconnect() from exc

        async def send_text(self, _raw):
            return None

        async def close(self, code=1000, reason=""):
            return None

    class WrapperWs:
        def __init__(self):
            self.frames = []

        async def send_text(self, raw):
            self.frames.append(deserialize(raw))

    async def run():
        hub = RelayHub(SimpleNamespace(
            client_queue_cap=4, client_queue_bytes=4096))
        wrapper = WrapperWs()
        hub._wrapper_ws = wrapper
        client = ClientWs([
            serialize(Hello(role="client", client_id="bound-client")),
            serialize(Query(
                prompt="hello",
                msg_id="msg-1",
                cmd_id="cmd-1",
                client_id="spoofed-client",
            )),
        ])
        await hub.serve_client(client)

        forwarded = next(msg for msg in wrapper.frames if msg.type == "query")
        assert forwarded.client_id == "bound-client"
        assert forwarded.cmd_id == "cmd-1"

    asyncio.run(run())


def test_relay_routes_command_ack_only_to_originating_client():
    class Conn:
        def __init__(self):
            self.messages = []

        async def send(self, msg):
            self.messages.append(msg)

    async def run():
        hub = RelayHub(SimpleNamespace())
        origin, other = Conn(), Conn()
        hub._clients = {"origin": origin, "other": other}
        await hub._on_wrapper_msg(CommandAck(
            cmd_id="cmd-1", client_id="origin", to="origin"))
        assert [msg.cmd_id for msg in origin.messages] == ["cmd-1"]
        assert other.messages == []

    asyncio.run(run())
