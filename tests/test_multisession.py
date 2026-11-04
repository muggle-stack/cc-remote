"""Zero-token unit tests for the multi-session wrapper logic.

No relay/wrapper/cc — these exercise the pure pieces directly:
- protocol: SessionRekey round-trips and is a control frame (not seq'd).
- ringbuffer: rebuild replay wraps the whole buffer in ReplayStart(rebuild=True).
- machine._emit_locked: routes by ctx.key (real sid once known, else temp key)
  so a pre-capture new session never leaks into the focused runtime.
- machine._capture_session_id: re-keys the pool, follows focus ONLY when the
  captured session was the focused one (the focus-steal fix), and emits
  SessionRekey (NOT SessionFocus).

Run: ./.venv/bin/python -m pytest tests/test_multisession.py -q
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    serialize, deserialize, is_downstream,
    Delta, Hello, SessionRekey, StateEvent, UserMsg, ReplayStart, ReplayEnd,
    TurnBinding, TurnEnd, TurnNotificationContext, TurnResult,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.machine import WrapperMachine


class _StubTransport:
    """Captures everything the machine tries to send."""
    def __init__(self):
        self.sent: list = []
        self.on_connected = None

    async def send(self, msg):
        self.sent.append(msg)


def _mk_machine():
    cfg = WrapperConfig()
    state_tmp = tempfile.TemporaryDirectory(prefix="cc-remote-test-")
    cfg.state_dir = Path(state_tmp.name)  # don't touch real state
    cfg.claude_work_root = cfg.state_dir / "work" / "claude"
    cfg.codex_work_root = cfg.state_dir / "work" / "codex"
    tr = _StubTransport()
    machine = WrapperMachine(cfg, tr)
    # Keep the auto-cleanup owner alive exactly as long as the test machine.
    # TemporaryDirectory's finalizer also runs at interpreter shutdown, so a
    # full pytest pass cannot leave thousands of cc-remote-test-* directories.
    machine._test_state_tmp = state_tmp
    return machine, tr


def _mk_ctx(key: str, session_id=None) -> SessionContext:
    # Built inside the running loop so emit_lock binds to the right loop.
    return SessionContext(
        session_id=session_id,
        sdk=object(),                       # unused in these tests
        buffer=RingBuffer(1000, 10_000_000),
        cwd="/tmp/cc-remote-test-cwd",
        key=key,
    )


# ---- protocol ----

def test_session_rekey_roundtrips_and_is_control_frame():
    m = SessionRekey(old_key="tmp-abc", session_id="real-123", cwd="/tmp/x")
    back = deserialize(serialize(m))
    assert back.type == "session_rekey"
    assert back.old_key == "tmp-abc"
    assert back.session_id == "real-123"
    assert back.cwd == "/tmp/x"
    # control frame → never assigned a seq / buffered
    assert is_downstream(m) is False


def test_running_state_progress_roundtrips_without_becoming_an_error():
    event = StateEvent(
        state="running", phase="retrying", detail="HTTP 503，正在重试…",
        msg_id="turn-1")
    back = deserialize(serialize(event))
    assert back.type == "state" and back.state == "running"
    assert back.phase == "retrying" and "503" in back.detail
    assert back.msg_id == "turn-1"
    assert is_downstream(back) is True


# ---- ringbuffer rebuild ----

def test_ringbuffer_rebuild_wraps_full_buffer():
    rb = RingBuffer(1000, 10_000_000)
    for i in range(1, 4):
        u = UserMsg(msg_id=f"m{i}", prompt="hi")
        u.seq = i
        rb.append(u)
    frames = rb.replay_from(0, cc_session_id="s", state="idle", rebuild=True)
    assert isinstance(frames[0], ReplayStart)
    assert frames[0].rebuild is True and frames[0].truncated is False
    assert isinstance(frames[-1], ReplayEnd)
    body = [f for f in frames if getattr(f, "type", None) == "user_msg"]
    assert [f.msg_id for f in body] == ["m1", "m2", "m3"]


def test_ringbuffer_rebuild_on_empty_buffer_still_brackets():
    rb = RingBuffer(1000, 10_000_000)
    frames = rb.replay_from(0, cc_session_id="s", state="idle", rebuild=True)
    assert isinstance(frames[0], ReplayStart) and frames[0].rebuild is True
    assert isinstance(frames[-1], ReplayEnd)
    assert len(frames) == 2


def test_ringbuffer_marks_an_oversized_sequence_gap_and_advances_tail():
    rb = RingBuffer(10, 1000)
    first = UserMsg(msg_id="one", prompt="small")
    first.seq = 1
    dropped = UserMsg(msg_id="two", prompt="x" * 5000)
    dropped.seq = 2
    third = UserMsg(msg_id="three", prompt="small")
    third.seq = 3
    for event in (first, dropped, third):
        rb.append(event)

    frames = rb.replay_from(0, cc_session_id="s", state="running")
    assert frames[0].truncated is True
    assert frames[-1].to_seq == 3
    assert [event.msg_id for event in frames if event.type == "user_msg"] == [
        "one", "three"]


def test_current_turn_replay_reports_evicted_user_message():
    rb = RingBuffer(1, 10_000)
    user = UserMsg(msg_id="turn", prompt="hello")
    user.seq = 1
    state = StateEvent(state="running")
    state.seq = 2
    rb.append(user)
    rb.append(state)

    frames = rb.current_turn_replay(generation="g")
    assert len(frames) == 2
    assert frames[0].truncated is True
    assert frames[-1].to_seq == 2


def test_current_turn_replay_never_reuses_previous_turn_during_preflight():
    rb = RingBuffer(10, 10_000)
    old = UserMsg(msg_id="old", prompt="previous")
    old.seq = 1
    running = StateEvent(state="running")
    running.seq = 2
    rb.append(old)
    rb.append(running)

    assert rb.current_turn_replay(
        generation="g", message_id="new-not-emitted") == []


def test_current_turn_replay_reports_exact_evicted_active_boundary():
    rb = RingBuffer(2, 10_000)
    current = UserMsg(msg_id="current", prompt="current")
    current.seq = 2
    first = StateEvent(state="running")
    first.seq = 3
    second = StateEvent(state="running")
    second.seq = 4
    for event in (current, first, second):
        rb.append(event)

    frames = rb.current_turn_replay(
        generation="g",
        message_id="current",
        boundary_seq=2,
    )

    assert [frame.type for frame in frames] == [
        "replay_start", "state", "state", "replay_end",
    ]
    assert frames[0].truncated is True
    assert frames[0].from_seq == rb.head_seq
    assert frames[-1].to_seq == rb.tail_seq


def test_current_turn_replay_reports_marker_evicted_before_retained_binding():
    rb = RingBuffer(3, 10_000)
    current = UserMsg(msg_id="current", prompt="current")
    current.seq = 1
    binding = TurnBinding(msg_id="current", turn_id="native")
    binding.seq = 2
    first = StateEvent(state="running")
    first.seq = 3
    second = StateEvent(state="running")
    second.seq = 4
    for event in (current, binding, first, second):
        rb.append(event)

    assert rb.head_seq == binding.seq
    frames = rb.current_turn_replay(
        generation="g",
        message_id="current",
        boundary_seq=binding.seq,
    )

    assert [frame.type for frame in frames] == [
        "replay_start", "turn_binding", "state", "state", "replay_end",
    ]
    assert frames[0].truncated is True
    assert frames[0].from_seq == binding.seq


def test_current_turn_replay_keeps_and_compacts_retained_live_deltas():
    rb = RingBuffer(10_001, 10_000_000)
    current = UserMsg(msg_id="current", prompt="current")
    current.seq = 1
    binding = TurnBinding(msg_id="current", turn_id="native")
    binding.seq = 2
    rb.append(current)
    rb.append(binding)
    expected = []
    for index in range(10_000):
        text = f"{index % 10}" * 10
        expected.append(text)
        delta = Delta(
            message_id="assistant",
            turn_id="native",
            text=text,
            channel="commentary",
        )
        delta.seq = index + 3
        rb.append(delta)

    assert rb.head_seq == binding.seq
    frames = rb.current_turn_replay(
        generation="g",
        message_id="current",
        boundary_seq=binding.seq,
    )

    assert frames[0].type == "replay_start"
    assert frames[0].truncated is True
    assert frames[1].type == "turn_binding"
    compacted = [frame for frame in frames if frame.type == "delta"]
    assert 1 < len(compacted) < 10
    assert all(len(frame.text) <= 64 * 1024 for frame in compacted)
    assert "".join(frame.text for frame in compacted) == "".join(expected)
    assert [frame.seq for frame in compacted] == sorted(
        frame.seq for frame in compacted
    )
    assert compacted[-1].seq == rb.tail_seq
    assert frames[-1].type == "replay_end"


# ---- emit routing (sid = the context's browser routing key) ----

def test_emit_routes_by_temp_key_before_capture_then_real_sid():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-xyz", session_id=None)
        m.sessions[ctx.key] = ctx
        await m._emit_locked(ctx, UserMsg(msg_id="m1", prompt="hi"))
        assert tr.sent[-1].sid == "tmp-xyz"     # routed by temp key, NOT None
        await m._capture_session_id(ctx, "real-1")
        await m._emit_locked(ctx, UserMsg(msg_id="m2", prompt="hi"))
        assert ctx.key == "real-1"
        assert tr.sent[-1].sid == "real-1"      # routing key re-keyed atomically
    asyncio.run(run())


def test_live_turn_end_has_notification_context_but_ring_replay_does_not():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx(key="session-notify", session_id="session-notify")
        ctx.engine = "codex"
        ctx.space = "work"
        machine._remember_notification_title(
            ctx.key, "  Fix\nnotifications\u0000 now  ")

        await machine._emit_locked(ctx, TurnEnd(result=TurnResult(
            subtype="success", duration_ms=12, is_error=False,
        )))

        live = transport.sent[-1]
        assert live.notification_context == TurnNotificationContext(
            engine="codex",
            space="work",
            display_name="Fix notifications now",
        )
        replay = next(
            frame for frame in ctx.buffer.replay_from(
                0,
                cc_session_id=ctx.session_id,
                state="idle",
            )
            if isinstance(frame, TurnEnd)
        )
        assert replay.notification_context is None

    asyncio.run(run())


def test_btw_turn_end_routes_owner_only_and_points_notification_to_parent():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx(key="btw-private", session_id=None)
        ctx.engine = "claude"
        ctx.btw = True
        ctx.parent_sid = "parent-session"
        ctx.owner_client_id = "owner-client"
        machine._remember_notification_title(
            ctx.parent_sid, "Parent conversation")

        await machine._emit_locked(ctx, TurnEnd(result=TurnResult(
            subtype="error_during_execution",
            duration_ms=4,
            is_error=True,
        )))

        live = transport.sent[-1]
        assert live.to is None
        assert live.owner_id == "owner-client"
        assert live.sid == "btw-private"
        assert live.notification_context == TurnNotificationContext(
            engine="claude",
            space="code",
            display_name="Parent conversation",
            parent_session_id="parent-session",
        )
        replay = next(
            frame for frame in ctx.buffer.replay_from(
                0,
                cc_session_id=None,
                state="idle",
            )
            if isinstance(frame, TurnEnd)
        )
        assert replay.notification_context is None

    asyncio.run(run())


def test_turn_notification_context_protocol_roundtrip():
    message = TurnEnd(
        result=TurnResult(subtype="success", duration_ms=1, is_error=False),
        notification_context=TurnNotificationContext(
            engine="claude",
            space="code",
            display_name="A named session",
            parent_session_id="parent-1",
        ),
    )
    restored = deserialize(serialize(message))
    assert restored.notification_context == message.notification_context


# ---- focus-steal fix ----

@pytest.mark.parametrize("warm", [False, True])
def test_hello_restores_completed_parent_continuation(warm):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session", "session")
        machine.sessions[ctx.key] = ctx
        ctx.state = "running"
        ctx.claude_background_followup_pending = True
        ctx.claude_background_translator = SimpleNamespace(turn_id="parent")
        # The human input and continuation State have both left the ring.
        ctx.buffer = RingBuffer(2, 100_000)
        for event in [
            UserMsg(msg_id="parent", prompt="inspect"),
            StateEvent(state="running", msg_id="parent", continuation=True),
            Delta(message_id="followup", turn_id="parent", background=True,
                  channel="thinking", text="still "),
            Delta(message_id="followup", turn_id="parent", background=True,
                  channel="thinking", text="working"),
        ]:
            event.seq = ctx.next_seq()
            ctx.buffer.append(event)
        tail = ctx.seq
        await machine._handle_client_hello(Hello(
            role="client", client_id="reader", route_id="route",
            cursors={"session": tail} if warm else {},
            generations={"session": machine.instance_id} if warm else {},
        ))
        own = [event for event in transport.sent if event.sid == ctx.key]
        seed = next(event for event in own
                    if isinstance(event, StateEvent) and event.continuation)
        assert seed.msg_id == "parent" and seed.seq is None
        assert own.index(seed) > next(i for i, e in enumerate(own)
                                      if isinstance(e, ReplayEnd))
        assert seed.to == "reader" and seed.route_id == "route"
        assert ctx.seq == tail and ctx.buffer.tail_seq == tail
        assert not any(isinstance(event, UserMsg) for event in own)
        # Focus repeats current ownership without touching the model lifecycle.
        state = await machine._emit_current_state(ctx)
        assert state.continuation and state.msg_id == "parent"
        for state, managed in [("idle", False), ("running", True)]:
            ctx.state, ctx.claude_write_active = state, managed
            assert machine._claude_continuation_state(ctx) is None
        ctx.state, ctx.claude_write_active = "running", False
        ctx.claude_background_followup_pending = False
        assert machine._claude_continuation_state(ctx) is None

    asyncio.run(run())


def test_capture_follows_focus_when_captured_session_is_focused():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-1", session_id=None)
        m.sessions["tmp-1"] = ctx
        m.focused_sid = "tmp-1"                  # user is viewing this new session
        m._remember_notification_title("tmp-1", "First prompt")
        await m._capture_session_id(ctx, "real-1")
        assert "tmp-1" not in m.sessions and m.sessions["real-1"] is ctx
        assert ctx.key == "real-1" and ctx.session_id == "real-1"
        assert m.focused_sid == "real-1"         # focus followed the re-key
        assert "tmp-1" not in m._notification_titles
        assert m._notification_titles["real-1"] == "First prompt"
        rekeys = [s for s in tr.sent if getattr(s, "type", None) == "session_rekey"]
        assert rekeys and rekeys[-1].old_key == "tmp-1" and rekeys[-1].session_id == "real-1"
        # never a focus frame for a re-key
        assert not [s for s in tr.sent if getattr(s, "type", None) == "session_focus"]
    asyncio.run(run())


def test_capture_does_not_steal_focus_from_background_session():
    async def run():
        m, tr = _mk_machine()
        bg = _mk_ctx(key="tmp-bg", session_id=None)
        other = _mk_ctx(key="real-other", session_id="real-other")
        m.sessions["tmp-bg"] = bg
        m.sessions["real-other"] = other
        m.focused_sid = "real-other"             # user is viewing a DIFFERENT session
        await m._capture_session_id(bg, "real-bg")
        assert m.sessions["real-bg"] is bg and "tmp-bg" not in m.sessions
        assert bg.key == "real-bg"
        assert m.focused_sid == "real-other"     # focus NOT stolen by the background capture
        rekeys = [s for s in tr.sent if getattr(s, "type", None) == "session_rekey"]
        assert rekeys and rekeys[-1].old_key == "tmp-bg"
    asyncio.run(run())


def test_capture_reparents_btw_catalog_before_session_rekey_is_published():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx(key="tmp-parent", session_id=None)
        child = _mk_ctx(key="btw-child", session_id=None)
        child.btw = True
        child.parent_sid = parent.key
        child.owner_client_id = "owner-1"
        child.btw_created_at = 1.0
        child.btw_announced = True
        machine.sessions = {parent.key: parent, child.key: child}

        original_send = transport.send

        async def observe_send(message):
            if isinstance(message, SessionRekey):
                # SessionRekey is the browser's migration edge. By the time it
                # can leave the wrapper, a concurrent Hello must no longer be
                # able to snapshot the child under the old parent key.
                assert child.parent_sid == "real-parent"
                assert machine._btw_revision == 1
            await original_send(message)

        transport.send = observe_send
        await machine._capture_session_id(parent, "real-parent")

        assert child.parent_sid == "real-parent"
        assert machine._btw_revision == 1
        assert [message.type for message in transport.sent
                if isinstance(message, SessionRekey)] == ["session_rekey"]

    asyncio.run(run())


def test_lost_rekey_is_replayed_before_cursor_catchup():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx(key="tmp-lost", session_id=None)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key
        # The client saw seq 1 under tmp-lost, then missed the rekey. A later
        # state event under the real id remains in the same sequence namespace.
        first = UserMsg(msg_id="m1", prompt="hello")
        first.seq = ctx.next_seq()
        ctx.buffer.append(first)
        await machine._capture_session_id(ctx, "real-1")
        later = StateEvent(state="idle")
        later.seq = ctx.next_seq()
        ctx.buffer.append(later)
        transport.sent.clear()

        await machine._handle_client_hello(Hello(
            role="client", client_id="client-1", route_id="route-1",
            cursors={"tmp-lost": 1},
            generations={"tmp-lost": machine.instance_id}))

        assert [message.type for message in transport.sent] == [
            "btw_sync", "session_rekey", "replay_start", "state", "replay_end",
            "ask_user_sync", "background_process_sync", "session_control", "query_queue",
            "completion_state", "perm", "auto_compact"]
        assert transport.sent[1].old_key == "tmp-lost"
        assert transport.sent[1].session_id == "real-1"
        assert all(message.to == "client-1" for message in transport.sent)
        assert all(message.route_id == "route-1" for message in transport.sent)

    asyncio.run(run())


def test_live_ask_has_notification_context_without_replay_or_question_content():
    from cc_remote.protocol import AskUser

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx(key="ask-notify", session_id="ask-notify")
        ctx.engine = "claude"
        machine._remember_notification_title(ctx.key, "Question session")
        ask = AskUser(ask_id="a", question="Private question", allow_text=True)
        await machine._emit_locked(ctx, ask)
        live = transport.sent[-1]
        assert live.notification_context.engine == "claude"
        assert live.notification_context.display_name == "Question session"
        assert "Private question" not in live.notification_context.model_dump_json()
        restored = deserialize(serialize(live))
        assert restored.notification_context == live.notification_context
        assert ask.notification_context is None
        replay = next(frame for frame in ctx.buffer.replay_from(
            0, cc_session_id=ctx.session_id, state="running") if isinstance(frame, AskUser))
        assert replay.notification_context is None

    asyncio.run(run())
