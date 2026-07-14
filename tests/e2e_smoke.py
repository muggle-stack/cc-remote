"""Non-interactive end-to-end smoke: client -> relay -> wrapper -> cc (GLM).

Run after `python -m cc_remote.relay` and `python -m cc_remote.wrapper` are up.
Verifies:
  Q1: a short streaming query produces deltas + turn_end(success)
  Q2: interrupting a long generation yields turn_end(error_during_execution)
  Q3: the query right after Q3 starts cleanly (drain worked, no Q2 bleed)

Makes real model calls through the user's local proxy (127.0.0.1:19191 -> z.AI).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import cc_remote.config  # noqa: F401  (triggers .env load)
from cc_remote.protocol import Hello, Interrupt, Query, deserialize, serialize
from tests.e2e_auth import client_connection

URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
PASSWORD = os.environ.get("LOGIN_PASSWORD", "")


async def recv_until(ws, want_types, timeout=90):
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


async def interrupt_after(ws, delay):
    await asyncio.sleep(delay)
    await ws.send(serialize(Interrupt()))
    print(f"  [interrupt sent after {delay}s]")


async def main():
    cid = uuid.uuid4().hex
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        ev, _ = await recv_until(ws, {"snapshot"}, timeout=10)
        print(f"hello -> snapshot: {'yes' if ev else 'no'}")

        # Q1: short streaming
        await ws.send(serialize(Query(prompt="用一句话说你好", msg_id=uuid.uuid4().hex)))
        ev, end = await recv_until(ws, {"turn_end"})
        deltas = [e for e in ev if e.type == "delta"]
        text = "".join(d.text for d in deltas)
        print(f"Q1: deltas={len(deltas)} text={text!r}")
        print(f"    turn_end subtype={end.result.subtype if end else None}")
        assert end and end.result.subtype == "success", f"Q1 expected success, got {end}"
        assert deltas, "Q1 expected streaming deltas"
        print("Q1 PASS (streaming)")

        # Q2: long generation, interrupt mid-stream
        await ws.send(serialize(Query(prompt="写一篇800字关于海洋的科普短文，要详细", msg_id=uuid.uuid4().hex)))
        itask = asyncio.create_task(interrupt_after(ws, 1.5))
        ev, end = await recv_until(ws, {"turn_end"}, timeout=90)
        await itask
        q2_text = "".join(e.text for e in ev if e.type == "delta")
        print(f"Q2: partial text len={len(q2_text)} turn_end subtype={end.result.subtype if end else None}")
        assert end and end.result.subtype == "error_during_execution", (
            f"Q2 expected error_during_execution, got {end}")
        print("Q2 PASS (interrupt -> error_during_execution)")

        # Q3: immediately after, verify NO bleed from Q2
        await ws.send(serialize(Query(prompt="只回复两个字：你好", msg_id=uuid.uuid4().hex)))
        ev, end = await recv_until(ws, {"turn_end"}, timeout=90)
        q3_text = "".join(e.text for e in ev if e.type == "delta")
        print(f"Q3: text={q3_text!r} turn_end subtype={end.result.subtype if end else None}")
        assert end and end.result.subtype == "success", f"Q3 expected success, got {end}"
        # A 2-word reply must be short and not contain the ocean essay
        assert len(q3_text) < 80, f"Q3 looks polluted by Q2 (len={len(q3_text)}): {q3_text!r}"
        assert "海洋" not in q3_text and "海" not in q3_text[:5], f"Q3 may contain Q2 bleed: {q3_text!r}"
        print("Q3 PASS (no bleed — drain worked)")

        print("\nALL E2E OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nE2E FAILED: {e}", file=sys.stderr)
        sys.exit(1)
