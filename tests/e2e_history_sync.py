"""History sync test: a new client must fetch the FULL conversation that
happened before it connected — including user prompts and assistant replies.

Flow: client A sends a query; client B connects fresh, receives a lightweight
Snapshot, then uses GetHistory for A's session and gets one bulk History frame.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import cc_remote.config  # noqa: F401
from cc_remote.protocol import GetHistory, Hello, Query, deserialize, serialize
from tests.e2e_auth import client_connection

URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
PASSWORD = os.environ.get("LOGIN_PASSWORD", "")


async def recv_until(ws, want, timeout=90):
    evs = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return evs, None
        m = deserialize(raw)
        evs.append(m)
        if m.type in want:
            return evs, m


async def client_collect(cid, prompt, want, results):
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        if prompt:
            # drain the initial (empty-buffer) snapshot/replay before sending
            await recv_until(ws, {"replay_end", "snapshot"}, 10)
            await ws.send(serialize(Query(prompt=prompt, msg_id=uuid.uuid4().hex)))
        evs, _ = await recv_until(ws, want, 90)
        results[cid] = evs


async def main():
    ra: dict = {}
    await client_collect("A", "只说：同步测试OK", {"turn_end"}, ra)
    a = ra["A"]
    a_user = [e for e in a if e.type == "user_msg"]
    a_text = "".join(e.text for e in a if e.type == "delta")
    print(f"A: user_msgs={len(a_user)} text={a_text!r}")
    assert a_user, "A should see its own broadcast user_msg"
    assert "同步测试OK" in a_text, f"A missing assistant text: {a_text!r}"

    target_sid = a_user[-1].sid
    assert target_sid, "A's user_msg must carry a session id"
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id="B")))
        await recv_until(ws, {"snapshot"}, 10)
        await ws.send(serialize(GetHistory(
            session_id=target_sid, client_id="B", limit=60)))
        b, history = await recv_until(ws, {"history"}, 30)

    assert history is not None, "B should receive a History frame"
    payload = history.events
    b_user = [row for row in payload if row.get("type") == "user_msg"]
    b_text = "".join(row.get("text", "") for row in payload if row.get("type") == "delta")
    b_turn_end = [row for row in payload if row.get("type") == "turn_end"]
    print(f"B: history_frames={len([e for e in b if e.type == 'history'])} "
          f"user_msgs={len(b_user)} turn_end={len(b_turn_end)} text={b_text!r}")
    assert b_user, "B should see A's user_msg in History"
    assert b_user[-1]["prompt"] == "只说：同步测试OK", f"wrong prompt: {b_user[-1]['prompt']!r}"
    assert b_turn_end, "B should see A's turn_end in History"
    assert "同步测试OK" in b_text, f"B missing A's assistant text: {b_text!r}"
    print("HISTORY SYNC OK — fresh client fetches full conversation incl. prompts")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nHISTORY SYNC FAILED: {e}", file=sys.stderr)
        sys.exit(1)
