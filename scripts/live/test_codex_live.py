"""Live end-to-end test: CodexHandle (app-server driver) + CodexStreamTranslator.

Runs a real turn against the locally-configured codex (gpt-5.5 on the Mac) and
prints the resulting cc-remote wire-event stream. Proves the two new wrapper files
work in-tree, no machine.py changes, zero cc risk.

  cd cc-remote && python3 scripts/live/test_codex_live.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, REPO_ROOT)

from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_session_id, is_turn_terminal,
)

PROMPT = ("Use your shell tool to run ls -1 in the current directory, "
          "then tell me exactly how many entries it printed.")


async def main() -> int:
    cfg = SimpleNamespace(cc_cwd=os.getcwd(), tool_result_max=16000)
    h = CodexHandle(cfg, cwd=os.getcwd())
    await h.connect()
    print(f"[connected] thread_id={h.thread_id}")
    assert h.thread_id, "no thread id from thread/start"

    tr = CodexStreamTranslator(cfg.tool_result_max)
    await h.query(PROMPT)
    print(f"[turn started] turn_id={h.turn_id}\n")

    wire = []
    saw_terminal = False
    async for msg in h.receive_response():
        if codex_session_id(msg) and not h.thread_id:
            pass
        wire.extend(tr.feed(msg))
        if is_turn_terminal(msg):
            saw_terminal = True

    for ev in wire:
        d = ev.model_dump()
        t = ev.type
        if t == "delta":
            sys.stdout.write(d["text"])
        elif t == "assistant_msg_start":
            print(f"\n[assistant_msg_start {d['message_id'][:14]}]")
        elif t == "assistant_msg_end":
            print("\n[assistant_msg_end]")
        elif t == "tool_use":
            print(f"[tool_use {d['tool']} id={d['tool_use_id'][:10]} input={str(d['input'])[:70]}]")
        elif t == "tool_result":
            prev = d["content"].replace("\n", " ")[:60]
            print(f"[tool_result is_error={d['is_error']} “{prev}…”]")
        elif t == "turn_end":
            print(f"\n[turn_end {d['result']['subtype']} is_error={d['result']['is_error']} {d['result']['duration_ms']}ms]")

    await h.disconnect()

    types = [e.type for e in wire]
    print(f"\n\n=== wire events: {types}")
    ok = (saw_terminal
          and "tool_use" in types and "tool_result" in types
          and "delta" in types and types[-1] == "turn_end")
    print("=== RESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
