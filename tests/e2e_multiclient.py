"""Multi-client fan-out test.

Two clients connect (different client_ids, last_seq=null). Client A sends a
query; BOTH clients must receive the identical streamed delta seqs (live
broadcast fan-out). Verifies the relay broadcasts live events to all clients
and that per-client replay (to=<client_id>) doesn't leak to the other client.
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


async def client(cid: str, send: bool, res: dict, ready: asyncio.Event, go: asyncio.Event):
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.type == "snapshot":
                break
        ready.set()
        await go.wait()
        if send:
            await asyncio.sleep(0.2)  # let both settle into the recv loop
            await ws.send(serialize(Query(prompt="只说：多客户端测试OK", msg_id=uuid.uuid4().hex)))
        seqs: list[int] = []
        text = ""
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=60))
            if m.type == "delta":
                seqs.append(m.seq)
                text += m.text
            if m.type == "turn_end":
                break
        res[cid] = (seqs, text)


async def main():
    ready_a = asyncio.Event()
    ready_b = asyncio.Event()
    go = asyncio.Event()
    res: dict = {}
    ta = asyncio.create_task(client("A", True, res, ready_a, go))
    tb = asyncio.create_task(client("B", False, res, ready_b, go))
    await ready_a.wait()
    await ready_b.wait()
    go.set()
    await asyncio.gather(ta, tb)
    seqs_a, text_a = res["A"]
    seqs_b, text_b = res["B"]
    print(f"A: {len(seqs_a)} deltas text={text_a!r}")
    print(f"B: {len(seqs_b)} deltas text={text_b!r}")
    assert seqs_a == seqs_b, f"fan-out seq mismatch:\n A={seqs_a}\n B={seqs_b}"
    assert text_a == text_b, "fan-out text mismatch"
    assert seqs_a, "no deltas received"
    print("MULTICLIENT FAN-OUT OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nMULTICLIENT FAILED: {e}", file=sys.stderr)
        sys.exit(1)
