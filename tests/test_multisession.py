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

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    serialize, deserialize, is_downstream,
    Hello, SessionRekey, StateEvent, UserMsg, ReplayStart, ReplayEnd,
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
    cfg.state_dir = Path(tempfile.mkdtemp(prefix="cc-remote-test-"))  # don't touch real state
    tr = _StubTransport()
    return WrapperMachine(cfg, tr), tr


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


# ---- emit routing (sid = ctx.session_id or ctx.key) ----

def test_emit_routes_by_temp_key_before_capture_then_real_sid():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-xyz", session_id=None)
        await m._emit_locked(ctx, UserMsg(msg_id="m1", prompt="hi"))
        assert tr.sent[-1].sid == "tmp-xyz"     # routed by temp key, NOT None
        ctx.session_id = "real-1"
        await m._emit_locked(ctx, UserMsg(msg_id="m2", prompt="hi"))
        assert tr.sent[-1].sid == "real-1"      # routed by real sid once known
    asyncio.run(run())


# ---- focus-steal fix ----

def test_capture_follows_focus_when_captured_session_is_focused():
    async def run():
        m, tr = _mk_machine()
        ctx = _mk_ctx(key="tmp-1", session_id=None)
        m.sessions["tmp-1"] = ctx
        m.focused_sid = "tmp-1"                  # user is viewing this new session
        await m._capture_session_id(ctx, "real-1")
        assert "tmp-1" not in m.sessions and m.sessions["real-1"] is ctx
        assert ctx.key == "real-1" and ctx.session_id == "real-1"
        assert m.focused_sid == "real-1"         # focus followed the re-key
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
            "session_rekey", "replay_start", "state", "replay_end", "perm"]
        assert transport.sent[0].old_key == "tmp-lost"
        assert transport.sent[0].session_id == "real-1"
        assert all(message.to == "client-1" for message in transport.sent)
        assert all(message.route_id == "route-1" for message in transport.sent)

    asyncio.run(run())
