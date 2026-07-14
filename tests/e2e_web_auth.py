"""Verifies the browser WebSocket cookie-auth path.

The client first posts LOGIN_PASSWORD to /api/login, then connects with the
HttpOnly session cookie and an exact Origin header.  No secret enters the URL.
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
    async with await client_connection(URL, PASSWORD) as ws:
        await ws.send(serialize(Hello(role="client", client_id=cid, last_seq=None)))
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.type == "snapshot":
                break
        await ws.send(serialize(Query(prompt="用一句话说你好", msg_id=uuid.uuid4().hex)))
        deltas = 0
        text = ""
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=60))
            if m.type == "delta":
                deltas += 1
                text += m.text
            if m.type == "turn_end":
                assert m.result.subtype == "success", f"expected success, got {m.result.subtype}"
                break
        assert deltas > 0, "no streaming deltas"
        print(f"WEB-AUTH (HttpOnly cookie) OK: {deltas} deltas, text={text!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nWEB-AUTH FAILED: {e}", file=sys.stderr)
        sys.exit(1)
