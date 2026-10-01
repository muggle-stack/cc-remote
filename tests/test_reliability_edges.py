"""Zero-token regressions for cursor replay, routed lists, and create recovery."""
from __future__ import annotations

import asyncio
import threading

from cc_remote.protocol import (
    AssistantMsgStart,
    AskUser,
    AskUserSync,
    BtwClosed,
    BtwSync,
    CommandAck,
    Delta,
    Hello,
    ListSessions,
    NewSession,
    ReplayEnd,
    ReplayStart,
    SyncBtw,
    SessionFocus,
    SessionList,
    SessionListInvalidated,
    StateEvent,
    TurnBinding,
    TurnEnd,
    TurnResult,
    TurnSteered,
    UserMsg,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import ActiveTurnBinding, PendingAskState
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
            "replay_start", "delta", "turn_end", "replay_end",
            "ask_user_sync", "background_process_sync", "session_control", "query_queue",
            "completion_state", "perm", "auto_compact",
        ]
        assert all(msg.to == "client-1" for msg in replay_frames)
        assert all(msg.sid == "s-replay" for msg in replay_frames)
        assert all(msg.route_id == "route-1" for msg in replay_frames)
        snapshot = next(msg for msg in transport.sent
                        if msg.type == "snapshot" and msg.sid == "s-new")
        assert snapshot.to == "client-1"
        assert snapshot.route_id == "route-1"
        assert transport.sent[-1].type == "auto_compact"
        assert transport.sent[-1].sid == "s-new"
        # Routed copies must not contaminate the shared ring event.
        assert delta.to is None and delta.route_id is None
        assert end.to is None and end.route_id is None

    asyncio.run(run())


def test_client_hello_does_not_retarget_another_clients_ring_frame():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("s-private", "s-private")
        private = Delta(message_id="a1", text="owner only", to="owner")
        public = Delta(message_id="a1", text="everyone")
        _buffer(ctx, private, public)
        machine.sessions = {ctx.key: ctx}

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="other",
            cursors={"s-private": 0},
            generations={"s-private": machine.instance_id},
        ))
        deltas = [message for message in transport.sent
                  if isinstance(message, Delta)]
        assert [message.text for message in deltas] == ["everyone"]
        assert deltas[0].to == "other"

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="owner",
            cursors={"s-private": 0},
            generations={"s-private": machine.instance_id},
        ))
        deltas = [message for message in transport.sent
                  if isinstance(message, Delta)]
        assert [message.text for message in deltas] == [
            "owner only", "everyone",
        ]
        assert all(message.to == "owner" for message in deltas)
        assert private.to == "owner"

    asyncio.run(run())


def test_client_hello_restores_btw_catalog_then_hydrates_only_selected_chat():
    async def run():
        machine, transport = _mk_machine()
        first = _mk_ctx("btw-first")
        first.engine = "codex"
        first.btw = True
        first.parent_sid = "parent"
        first.owner_client_id = "owner"
        first.btw_created_at = 1.0
        first.btw_announced = True
        second = _mk_ctx("btw-second")
        second.engine = "codex"
        second.btw = True
        second.parent_sid = "parent"
        second.owner_client_id = "owner"
        second.btw_created_at = 2.0
        second.btw_announced = True
        _buffer(
            first,
            UserMsg(msg_id="btw-user", prompt="remember this side chat"),
            Delta(message_id="btw-answer", text="restored answer"),
            TurnEnd(result=TurnResult(
                subtype="success", duration_ms=10, is_error=False)),
        )
        machine.sessions = {first.key: first, second.key: second}
        machine._btw_revision = 2

        await machine._handle_client_hello(Hello(
            role="client", client_id="owner", route_id="route-owner"))

        sync = transport.sent[0]
        assert isinstance(sync, BtwSync)
        assert sync.revision == 2
        assert [item.btw_sid for item in sync.sessions] == [
            "btw-first", "btw-second",
        ]
        assert all(item.parent_sid == "parent" for item in sync.sessions)
        first_frames = [event for event in transport.sent
                        if event.sid == "btw-first"]
        assert first_frames == []
        assert all(event.to == "owner" for event in transport.sent)
        assert all(event.route_id == "route-owner" for event in transport.sent)

        transport.sent.clear()
        await machine._handle_sync_btw(SyncBtw(
            sid="btw-first",
            cursor=0,
            client_id="owner-tab-2",
            owner_id="owner",
        ))
        assert [event.type for event in transport.sent[:6]] == [
            "replay_start", "user_msg", "delta", "turn_end", "replay_end",
            "snapshot",
        ]
        assert all(event.to == "owner-tab-2" for event in transport.sent)
        assert all(event.owner_id == "owner" for event in transport.sent)

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client", client_id="other", route_id="route-other"))

        assert len(transport.sent) == 1
        assert isinstance(transport.sent[0], BtwSync)
        assert transport.sent[0].sessions == []
        assert transport.sent[0].to == "other"

    asyncio.run(run())


def test_btw_sync_restores_pending_question_to_another_tab_of_same_owner():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("btw-question")
        ctx.engine = "claude"
        ctx.btw = True
        ctx.parent_sid = "parent"
        ctx.owner_client_id = "account-owner"
        ctx.btw_announced = True
        question = AskUser(
            ask_id="ask-private",
            question="继续吗？",
            options=[{"label": "继续"}, {"label": "停止"}],
        )
        loop = asyncio.get_running_loop()
        ctx.pending_asks[question.ask_id] = PendingAskState(
            event=question,
            future=loop.create_future(),
            labels=frozenset({"继续", "停止"}),
            allow_text=False,
            multi_select=False,
            created_at=loop.time(),
            deadline=loop.time() + 60,
        )
        machine.sessions = {ctx.key: ctx}

        await machine._handle_sync_btw(SyncBtw(
            sid=ctx.key,
            client_id="second-tab",
            owner_id="account-owner",
        ))

        sync_index = next(
            index for index, event in enumerate(transport.sent)
            if isinstance(event, AskUserSync)
        )
        restored = transport.sent[sync_index + 1]
        assert isinstance(restored, AskUser)
        assert restored.ask_id == "ask-private"
        assert restored.to == "second-tab"
        assert restored.owner_id == "account-owner"
        assert question.seq is None and question.to is None

    asyncio.run(run())


def test_btw_replay_uses_newest_bounded_suffix_and_advances_cursor():
    ring = RingBuffer(max_events=100, max_bytes=100_000)
    for seq in range(1, 7):
        message = Delta(
            seq=seq,
            message_id=f"message-{seq}",
            text=str(seq) * 360,
        )
        ring.append(message)

    frames = ring.replay_from_bounded(
        0,
        max_bytes=1024,
        max_events=100,
        generation="wrapper-generation",
    )

    assert isinstance(frames[0], ReplayStart)
    assert isinstance(frames[-1], ReplayEnd)
    assert frames[0].truncated is True
    assert frames[0].to_seq == frames[-1].to_seq == ring.tail_seq == 6
    payload = frames[1:-1]
    assert payload
    assert [event.seq for event in payload] == list(
        range(payload[0].seq, 7))
    assert frames[0].from_seq == payload[0].seq
    assert sum(ring._size(event) for event in payload) <= 1024


def test_btw_replay_uses_newest_event_bounded_suffix():
    ring = RingBuffer(max_events=100, max_bytes=1_000_000)
    for seq in range(1, 21):
        ring.append(StateEvent(seq=seq, state="running"))

    frames = ring.replay_from_bounded(
        0,
        max_bytes=1_000_000,
        max_events=6,
        generation="wrapper-generation",
    )

    assert isinstance(frames[0], ReplayStart)
    assert isinstance(frames[-1], ReplayEnd)
    assert frames[0].truncated is True
    assert frames[0].from_seq == 15
    assert frames[0].to_seq == frames[-1].to_seq == 20
    assert [event.seq for event in frames[1:-1]] == list(range(15, 21))


def test_close_btw_removes_only_the_exact_fork_and_publishes_catalog_revision():
    class Disconnectable:
        async def disconnect(self):
            return None

    async def run():
        machine, transport = _mk_machine()
        first = _mk_ctx("btw-first")
        first.engine = "codex"
        first.sdk = Disconnectable()
        first.btw = True
        first.parent_sid = "parent"
        first.owner_client_id = "owner"
        first.btw_created_at = 1.0
        first.btw_announced = True
        second = _mk_ctx("btw-second")
        second.engine = "codex"
        second.sdk = Disconnectable()
        second.btw = True
        second.parent_sid = "parent"
        second.owner_client_id = "owner"
        second.btw_created_at = 2.0
        second.btw_announced = True
        machine.sessions = {first.key: first, second.key: second}
        machine._btw_revision = 2

        result = await machine._handle_close_btw(
            type("Close", (), {"sid": first.key})())

        assert isinstance(result, BtwClosed)
        assert result.btw_sid == "btw-first"
        assert result.parent_sid == "parent"
        assert result.revision == 3
        assert result.to is None
        assert result.owner_id == "owner"
        assert list(machine.sessions) == ["btw-second"]
        assert transport.sent[0] is result

    asyncio.run(run())


def test_client_hello_reseeds_binding_before_tail_after_cursor_passed_owner():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("s-replay", "s-replay")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "item-51"
        machine.sessions = {"s-replay": ctx}

        await machine._emit_locked(
            ctx, UserMsg(msg_id="item-51", prompt="continue the task"))
        await machine._emit_locked(ctx, TurnBinding(
            msg_id="item-51", turn_id="native-turn"))
        await machine._emit_locked(ctx, AssistantMsgStart(
            message_id="msg-after-compact", channel="commentary"))
        await machine._emit_locked(ctx, Delta(
            message_id="msg-after-compact", channel="commentary",
            text="one live tail"))
        original_tail_seq = ctx.buffer.tail_seq
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="client-1",
            route_id="route-1",
            cursors={"s-replay": 2},
            generations={"s-replay": machine.instance_id},
        ))

        assert transport.sent[0].type == "btw_sync"
        replay = transport.sent[1:8]
        assert [event.type for event in replay] == [
            "replay_start", "turn_binding", "assistant_msg_start", "delta",
            "replay_end", "ask_user_sync", "session_control",
        ]
        reseed = replay[1]
        assert reseed.msg_id == "item-51"
        assert reseed.turn_id == "native-turn"
        assert reseed.seq is None
        assert reseed.to == "client-1" and reseed.route_id == "route-1"
        assert ctx.buffer.tail_seq == original_tail_seq
        assert [event.type for _, event in ctx.buffer._buf].count(
            "turn_binding") == 1

    asyncio.run(run())


def test_client_hello_does_not_prebind_frames_before_latest_steer():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("s-steer", "s-steer")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "first-user"
        machine.sessions = {"s-steer": ctx}

        await machine._emit_locked(
            ctx, UserMsg(msg_id="first-user", prompt="first"))
        await machine._emit_locked(ctx, TurnBinding(
            msg_id="first-user", turn_id="native-turn"))
        await machine._emit_locked(ctx, AssistantMsgStart(
            message_id="before-steer", channel="commentary"))
        ctx.active_msg_id = "second-user"
        await machine._emit_locked(ctx, TurnSteered(
            msg_id="second-user", turn_id="native-turn", prompt="second"))
        await machine._emit_locked(ctx, AssistantMsgStart(
            message_id="after-steer", channel="commentary"))
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="client-1",
            cursors={"s-steer": 2},
            generations={"s-steer": machine.instance_id},
        ))

        narrative = [event for event in transport.sent
                     if event.type in {
                         "turn_binding", "turn_steered", "assistant_msg_start",
                     }]
        assert [(event.type, getattr(event, "msg_id", None),
                 getattr(event, "message_id", None)) for event in narrative] == [
            ("assistant_msg_start", None, "before-steer"),
            ("turn_steered", "second-user", None),
            ("assistant_msg_start", None, "after-steer"),
        ]

        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client",
            client_id="client-2",
            cursors={"s-steer": 4},
            generations={"s-steer": machine.instance_id},
        ))
        narrative = [event for event in transport.sent
                     if event.type in {
                         "turn_binding", "turn_steered", "assistant_msg_start",
                     }]
        assert [(event.type, getattr(event, "msg_id", None),
                 getattr(event, "message_id", None)) for event in narrative] == [
            ("turn_binding", "second-user", None),
            ("assistant_msg_start", None, "after-steer"),
        ]

    asyncio.run(run())


def test_client_hello_preseeds_proven_current_suffix_after_binding_eviction():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("s-truncated", "s-truncated")
        ctx.buffer = RingBuffer(2, 10_000_000)
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "current-user"
        machine.sessions = {"s-truncated": ctx}

        await machine._emit_locked(
            ctx, UserMsg(msg_id="current-user", prompt="current"))
        await machine._emit_locked(ctx, TurnBinding(
            msg_id="current-user", turn_id="native-turn"))
        await machine._emit_locked(ctx, AssistantMsgStart(
            message_id="tail-message", channel="commentary"))
        await machine._emit_locked(ctx, Delta(
            message_id="tail-message", channel="commentary", text="tail"))
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client",
            client_id="client-1",
            cursors={"s-truncated": 0},
            generations={"s-truncated": machine.instance_id},
        ))

        reseed = next(index for index, event in enumerate(transport.sent)
                      if isinstance(event, TurnBinding))
        assert transport.sent[0].type == "btw_sync"
        assert isinstance(transport.sent[1], ReplayStart)
        assert transport.sent[1].truncated is True
        # The retained head is strictly newer than the evicted binding. Every
        # replayed narrative frame is therefore a proven suffix of that exact
        # logical turn and must see the owner seed before it is reduced.
        assert transport.sent[1].from_seq > ctx.active_turn_binding.seq
        assert reseed == 2
        assert transport.sent[reseed].seq is None

    asyncio.run(run())


def test_client_hello_keeps_unproven_replay_prefix_before_owner_seed():
    machine, _transport = _mk_machine()
    ctx = _mk_ctx("s-ambiguous", "s-ambiguous")
    ctx.engine = "codex"
    ctx.state = "running"
    ctx.active_turn_binding = ActiveTurnBinding(
        msg_id="current-user",
        turn_id="current-native-turn",
        seq=50,
        generation=machine.instance_id,
    )
    frames = [
        ReplayStart(
            from_seq=40, to_seq=41, truncated=True,
            generation=machine.instance_id,
        ),
        AssistantMsgStart(
            seq=40, message_id="unproven-prefix", channel="commentary"),
        ReplayEnd(to_seq=41, truncated=True),
    ]

    seeded = machine._reseed_active_binding_for_hello(
        ctx, frames, cursor=0, same_generation=True)

    assert [frame.type for frame in seeded] == [
        "replay_start", "assistant_msg_start", "replay_end", "turn_binding",
    ]
    assert seeded[-1].seq is None


def test_fresh_hello_reseeds_owner_when_current_boundary_left_ring():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("s-fresh", "s-fresh")
        ctx.buffer = RingBuffer(2, 10_000_000)
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "current-user"
        machine.sessions = {"s-fresh": ctx}

        await machine._emit_locked(
            ctx, UserMsg(msg_id="current-user", prompt="current"))
        await machine._emit_locked(ctx, TurnBinding(
            msg_id="current-user", turn_id="native-turn"))
        await machine._emit_locked(ctx, AssistantMsgStart(
            message_id="tail-message", channel="commentary"))
        await machine._emit_locked(ctx, Delta(
            message_id="tail-message", channel="commentary", text="tail"))
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client", client_id="client-1"))

        assert [event.type for event in transport.sent[:8]] == [
            "btw_sync", "snapshot", "replay_start", "turn_binding",
            "assistant_msg_start", "delta", "replay_end", "ask_user_sync",
        ]
        assert transport.sent[2].truncated is True
        assert transport.sent[3].seq is None
        assert transport.sent[5].text == "tail"

        await machine._emit_locked(ctx, TurnEnd(
            turn_id="native-turn",
            result=TurnResult(
                subtype="success", duration_ms=1, is_error=False)))
        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client", client_id="client-2"))
        assert not any(isinstance(event, TurnBinding)
                       for event in transport.sent)

    asyncio.run(run())


def test_session_lists_echo_engine_and_are_unicast_to_each_requester(monkeypatch):
    caller_thread = threading.get_ident()
    calls = []

    def list_claude_sessions(*, limit):
        calls.append((threading.get_ident(), limit))
        return []

    codex_calls = []

    async def list_codex_sessions(_limit):
        codex_calls.append(_limit)
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
        await machine._handle_list_sessions(ListSessions(
            engine="codex", space="work", client_id="client-codex-work"))

        lists = [msg for msg in transport.sent if isinstance(msg, SessionList)]
        assert [(msg.engine, msg.to) for msg in lists] == [
            ("claude", "client-claude"),
            ("codex", "client-codex"),
            ("codex", "client-codex-work"),
        ]
        assert lists[-1].space == "work"
        assert codex_calls == [200]
        # The wrapper applies the cap after filtering native metadata stubs.
        assert len(calls) == 1 and calls[0][1] is None
        assert calls[0][0] != caller_thread

    asyncio.run(run())


def test_session_list_invalidation_is_typed_and_never_replayed():
    event = SessionListInvalidated(engine="codex", space="work")
    decoded = deserialize(serialize(event))

    assert isinstance(decoded, SessionListInvalidated)
    assert decoded.engine == "codex"
    assert decoded.space == "work"
    assert not is_downstream(decoded)


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
            "snapshot", "session_focus", "auto_compact", "perm",
            "command_ack",
        ]
        replayed_snapshot, replayed_focus, auto_compact, permission, _ = (
            transport.sent)
        assert replayed_snapshot.sid == "tmp-created"
        assert replayed_snapshot.generation == machine.instance_id
        assert replayed_snapshot.state == "running"
        assert isinstance(replayed_focus, SessionFocus)
        assert replayed_focus.session_id == "tmp-created"
        assert replayed_focus.request_id == "create-request"
        assert replayed_focus.to == "client-1"
        assert auto_compact.mode == "inherit"
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
            "snapshot", "session_rekey", "session_focus", "auto_compact",
            "perm", "command_ack",
        ]
        snapshot, rekey, focus, auto_compact, permission, _ = transport.sent
        assert snapshot.sid == "tmp-created"
        assert snapshot.cc_session_id == "real-session"
        assert rekey.old_key == "tmp-created"
        assert rekey.session_id == "real-session"
        assert rekey.to == "client-1"
        assert focus.session_id == "real-session"
        assert focus.sid == "real-session"
        assert focus.to == "client-1"
        assert auto_compact.sid == "real-session"
        assert auto_compact.mode == "inherit"
        assert permission.sid == "real-session"
        assert permission.mode == "bypassPermissions"

    asyncio.run(run())
