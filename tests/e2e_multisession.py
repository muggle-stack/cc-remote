"""End-to-end multi-session test: client -> relay -> wrapper -> cc.

Verifies the two multi-session correctness fixes at the WIRE level (the pure
logic is also covered token-free in tests/test_multisession.py):

  P1 focus-steal fix: new_session -> session_focus(tmp-key); the first tiny turn
     captures the real cc id and the wrapper emits SESSION_REKEY (old_key->sid),
     NOT a second session_focus (which would steal the view of a background
     session).

  P2 evict+rebuild fix: with MAX_CONCURRENT_SESSIONS=2, create enough sessions
     to evict session A, then switch back to A. The catch-up must arrive as a
     REBUILD replay (replay_start.rebuild=true) so the client discards stale
     turns instead of duplicating.

Makes a few SMALL real model calls (trivial "OK" prompts) through the user's
local proxy. Run against an ISOLATED stack (own relay + wrapper with a temp
CC_CWD) so it never touches a live phone wrapper:

  MAX_CONCURRENT_SESSIONS=2 CC_CWD=/tmp/ccrm-e2e RELAY_URL=ws://127.0.0.1:8765/ws \
  LOGIN_PASSWORD=strong-password WRAPPER_TOKEN=<random>  python -m tests.e2e_multisession
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import cc_remote.config  # noqa: F401  (triggers .env load)
from cc_remote.protocol import Hello, Query, NewSession, SwitchSession, deserialize, serialize
from tests.e2e_auth import client_connection

URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
PASSWORD = os.environ.get("LOGIN_PASSWORD", "")


async def recv_until(ws, want_types, timeout=120):
    """Collect frames until one of want_types arrives (or timeout)."""
    events = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return events, None
        msg = deserialize(raw)
        events.append(msg)
        if msg.type in want_types:
            return events, msg


async def new_session_and_capture(ws, label):
    """new_session -> session_focus(tmp) -> tiny turn -> session_rekey. Returns
    (tmp_key, real_sid). Asserts the focus-steal fix along the way."""
    await ws.send(serialize(NewSession()))
    _, focus = await recv_until(ws, {"session_focus"}, timeout=20)
    assert focus and focus.session_id.startswith("tmp-"), f"{label}: expected session_focus(tmp-*), got {focus}"
    tmp_key = focus.session_id
    print(f"{label}: new_session -> session_focus({tmp_key})")

    await ws.send(serialize(Query(prompt="Reply with exactly: OK", msg_id=uuid.uuid4().hex)))
    ev, end = await recv_until(ws, {"turn_end"}, timeout=180)
    rekeys = [e for e in ev if e.type == "session_rekey"]
    focuses = [e for e in ev if e.type == "session_focus"]
    assert end and end.result.subtype == "success", f"{label}: turn did not succeed: {end}"
    assert rekeys, f"{label}: expected a session_rekey on id capture, got none"
    rk = rekeys[0]
    assert rk.old_key == tmp_key and rk.session_id != tmp_key, f"{label}: bad rekey {rk}"
    assert not focuses, f"{label}: id-capture emitted session_focus (FOCUS-STEAL): {focuses}"
    print(f"{label}: turn ok -> session_rekey({tmp_key} -> {rk.session_id})  [no focus-steal]")
    return tmp_key, rk.session_id


async def main():
    cid = uuid.uuid4().hex
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        await recv_until(ws, {"snapshot"}, timeout=15)
        print("connected -> snapshot")

        # P1 + set up P2: session A (real id captured, focus-steal checked)
        _, real_a = await new_session_and_capture(ws, "A")
        print("P1 PASS (focus-steal fix: rekey, not focus)\n")

        # session B — with cap=2 this evicts the idle bootstrap; A stays resident idle
        await new_session_and_capture(ws, "B")

        # session C — evicts idle non-focused A (no query needed)
        await ws.send(serialize(NewSession()))
        _, focus_c = await recv_until(ws, {"session_focus"}, timeout=20)
        assert focus_c and focus_c.session_id.startswith("tmp-"), focus_c
        print(f"C: new_session -> session_focus({focus_c.session_id})  (A should now be evicted)")

        # switch back to A — A was evicted, so it re-spawns and catch-up must REBUILD
        await ws.send(serialize(SwitchSession(session_id=real_a)))
        ev, focus_a = await recv_until(ws, {"session_focus"}, timeout=30)
        rebuilds = [e for e in ev if e.type == "replay_start" and getattr(e, "rebuild", False)]
        assert focus_a and (focus_a.session_id == real_a), f"switch back to A: got {focus_a}"
        assert rebuilds, f"switch to evicted A: expected replay_start(rebuild=true), got {[e.type for e in ev]}"
        print(f"switch->A: replay_start(rebuild=true) x{len(rebuilds)} then session_focus({real_a})")
        print("P2 PASS (evict+rebuild fix)\n")

        print("ALL MULTI-SESSION E2E OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nE2E FAILED: {e}", file=sys.stderr)
        sys.exit(1)
