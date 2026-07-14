"""Session resume test.

After the wrapper is restarted (so it --resumes the persisted cc session id),
this asks cc to recall prior questions. If resume works, cc remembers the
conversation history from before the restart.
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
        await ws.send(serialize(Query(
            prompt="刚才我问了你哪些问题？请简短列出我之前问过的内容",
            msg_id=uuid.uuid4().hex)))
        text = ""
        while True:
            m = deserialize(await asyncio.wait_for(ws.recv(), timeout=60))
            if m.type == "delta":
                text += m.text
                sys.stdout.write(m.text)
                sys.stdout.flush()
            if m.type == "turn_end":
                break
        print()
        print(f"RESUME RESPONSE ({len(text)} chars): {text!r}")


if __name__ == "__main__":
    asyncio.run(main())
