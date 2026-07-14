"""End-to-end reconnect + replay test.

Connects, starts a long streaming query, drops mid-stream (tracking its cursor),
reconnects with hello(cursors={sid: last_seq}), and verifies:
  - replay_start / replay_end markers arrive
  - all replayed/live seqs are strictly > last_seq
  - no duplicate seq, strictly increasing
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import cc_remote.config  # noqa: F401
from cc_remote.protocol import Hello, Query, deserialize, serialize
from tests.e2e_auth import client_connection

URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
PASSWORD = os.environ.get("LOGIN_PASSWORD", "")


async def main():
    cid = uuid.uuid4().hex
    # --- phase 1: connect, start a long query, collect ~2.5s, drop ---
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        # drain snapshot
        session_id = None
        generation = None
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.type == "snapshot":
                session_id = m.sid or m.cc_session_id
                generation = m.generation
                break
        assert session_id and generation, "snapshot did not identify session generation"
        await ws.send(serialize(Query(
            prompt="写一篇800字关于海洋的科普短文，要详细，分多段",
            msg_id=uuid.uuid4().hex)))
        last_seq = 0
        deltas1 = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 2.5
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            m = deserialize(raw)
            if m.type == "delta":
                deltas1 += 1
            s = getattr(m, "seq", None)
            if s and s > last_seq:
                last_seq = s
    print(f"phase1: deltas={deltas1} last_seq={last_seq} (dropped, reconnecting)")

    # --- phase 2: reconnect with last_seq, expect replay + live ---
    await asyncio.sleep(1.0)
    saw_start = saw_end = False
    live_deltas = 0
    seqs: list[int] = []
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(
            role="client", client_id=cid, cursors={session_id: last_seq},
            generations={session_id: generation})))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 45
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            m = deserialize(raw)
            if m.type == "replay_start":
                saw_start = True
                print(f"  replay_start {m.from_seq}->{m.to_seq} truncated={m.truncated}")
            elif m.type == "replay_end":
                saw_end = True
                print(f"  replay_end to={m.to_seq} truncated={m.truncated}")
            elif m.type == "delta" and saw_end:
                live_deltas += 1
            s = getattr(m, "seq", None)
            if s is not None:
                seqs.append(s)
            if m.type == "turn_end":
                break

    print(f"phase2: replay_start={saw_start} replay_end={saw_end} "
          f"live_deltas={live_deltas} total_seq'd={len(seqs)}")
    assert saw_start and saw_end, "missing replay_start/replay_end markers"
    assert seqs == sorted(seqs), f"seqs not strictly increasing: {seqs[:10]}"
    assert len(seqs) == len(set(seqs)), f"duplicate seqs: {seqs[:10]}"
    assert all(s > last_seq for s in seqs), (
        f"expected all seq > last_seq={last_seq}, got min={min(seqs) if seqs else None}")
    print("RECONNECT+REPLAY OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nRECONNECT FAILED: {e}", file=sys.stderr)
        sys.exit(1)
