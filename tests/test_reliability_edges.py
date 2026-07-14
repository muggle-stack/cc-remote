"""Zero-token regressions for cursor replay, routed lists, and create recovery."""
from __future__ import annotations

import asyncio
import threading

from cc_remote.protocol import (
    CommandAck,
    Delta,
    Hello,
    ListSessions,
    NewSession,
    SessionFocus,
    SessionList,
    TurnEnd,
    TurnResult,
    UserMsg,
)
from cc_remote.wrapper import machine as mm
from tests.test_multisession import _mk_ctx, _mk_machine


def _buffer(ctx, *events):
    for event in events:
        event.seq = ctx.next_seq()
        event.sid = ctx.session_id or ctx.key
        ctx.buffer.append(event)


def test_client_hello_replays_only_cursor_sessions_and_routes_every_frame():
    async def run():
        machine, transport = _mk_machine()
        replayed = _mk_ctx("s-replay", "s-replay")
        snapshot_only = _mk_ctx("s-new", "s-new")
        delta = Delta(message_id="a1", text="missed")
        end = TurnEnd(result=TurnResult(
            subtype="success", duration_ms=10, is_error=False))
        _buffer(
            replayed,
            UserMsg(msg_id="u1", prompt="before disconnect"),
            delta,
            end,
        )
        _buffer(snapshot_only, UserMsg(msg_id="u2", prompt="new client"))
        machine.sessions = {"s-replay": replayed, "s-new": snapshot_only}

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="client-1",
            route_id="route-1",
            cursors={"s-replay": 1},
            generations={"s-replay": machine.instance_id},
        ))

        replay_frames = [msg for msg in transport.sent if msg.sid == "s-replay"]
        assert [msg.type for msg in replay_frames] == [
            "replay_start", "delta", "turn_end", "replay_end", "perm",
        ]
        assert all(msg.to == "client-1" for msg in replay_frames)
        assert all(msg.sid == "s-replay" for msg in replay_frames)
        assert all(msg.route_id == "route-1" for msg in replay_frames)
        snapshot = next(msg for msg in transport.sent
                        if msg.type == "snapshot" and msg.sid == "s-new")
        assert snapshot.to == "client-1"
        assert snapshot.route_id == "route-1"
        assert transport.sent[-1].type == "perm"
        assert transport.sent[-1].sid == "s-new"
        # Routed copies must not contaminate the shared ring event.
        assert delta.to is None and delta.route_id is None
        assert end.to is None and end.route_id is None

    asyncio.run(run())


def test_session_lists_echo_engine_and_are_unicast_to_each_requester(monkeypatch):
    caller_thread = threading.get_ident()
    calls = []

    def list_claude_sessions(*, limit):
        calls.append((threading.get_ident(), limit))
        return []

    async def list_codex_sessions(_limit):
        return []

    monkeypatch.setattr(mm, "list_sessions", list_claude_sessions)
    monkeypatch.setattr(mm, "list_codex_sessions", list_codex_sessions)

    async def run():
        machine, transport = _mk_machine()
        machine._bg_blocked_session_ids = lambda: set()
        await machine._handle_list_sessions(ListSessions(
            engine="claude", client_id="client-claude"))
        await machine._handle_list_sessions(ListSessions(
            engine="codex", client_id="client-codex"))

        lists = [msg for msg in transport.sent if isinstance(msg, SessionList)]
        assert [(msg.engine, msg.to) for msg in lists] == [
            ("claude", "client-claude"),
            ("codex", "client-codex"),
        ]
        assert len(calls) == 1 and calls[0][1] == 200
        assert calls[0][0] != caller_thread

    asyncio.run(run())


def test_duplicate_new_session_replays_snapshot_and_focus_without_creating_again():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-created", None)
        spawns = 0

        async def fake_spawn(**_kwargs):
            nonlocal spawns
            spawns += 1
            machine.sessions["tmp-created"] = ctx
            return ctx

        machine._spawn = fake_spawn
        command = NewSession(
            request_id="create-request",
            cmd_id="create-command",
            client_id="client-1",
        )

        await machine._process_command(command)
        first_focus = next(
            msg for msg in transport.sent if isinstance(msg, SessionFocus))
        assert first_focus.to == "client-1"
        transport.sent.clear()  # simulate focus + ACK lost with the relay link
        ctx.state = "running"   # state may advance before the command is retried

        await machine._process_command(command)

        assert spawns == 1
        assert [msg.type for msg in transport.sent] == [
            "snapshot", "session_focus", "perm", "command_ack",
        ]
        replayed_snapshot, replayed_focus, permission, _ = transport.sent
        assert replayed_snapshot.sid == "tmp-created"
        assert replayed_snapshot.generation == machine.instance_id
        assert replayed_snapshot.state == "running"
        assert isinstance(replayed_focus, SessionFocus)
        assert replayed_focus.session_id == "tmp-created"
        assert replayed_focus.request_id == "create-request"
        assert replayed_focus.to == "client-1"
        assert permission.mode == "bypassPermissions"
        assert isinstance(transport.sent[-1], CommandAck)

    asyncio.run(run())


def test_cached_create_response_tracks_temp_to_real_rekey():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-created", None)
        spawns = 0

        async def fake_spawn(**_kwargs):
            nonlocal spawns
            spawns += 1
            machine.sessions["tmp-created"] = ctx
            return ctx

        machine._spawn = fake_spawn
        command = NewSession(
            request_id="create-request",
            cmd_id="create-command",
            client_id="client-1",
        )
        await machine._process_command(command)
        await machine._capture_session_id(ctx, "real-session")
        transport.sent.clear()

        await machine._process_command(command)

        assert spawns == 1
        assert [msg.type for msg in transport.sent] == [
            "snapshot", "session_rekey", "session_focus", "perm", "command_ack",
        ]
        snapshot, rekey, focus, permission, _ = transport.sent
        assert snapshot.sid == "tmp-created"
        assert snapshot.cc_session_id == "real-session"
        assert rekey.old_key == "tmp-created"
        assert rekey.session_id == "real-session"
        assert rekey.to == "client-1"
        assert focus.session_id == "real-session"
        assert focus.sid == "real-session"
        assert focus.to == "client-1"
        assert permission.sid == "real-session"
        assert permission.mode == "bypassPermissions"

    asyncio.run(run())
