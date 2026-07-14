"""Live proof of the two user-facing fixes, exercising the EXACT CodexHandle +
translator methods machine.py calls:
  - h.get_context_usage()  (context icon path -> _handle_get_context)
  - h.interrupt() + tr.feed()  (stop button path -> _handle_interrupt + _run_turn)
Runs real gpt-5.5 turns against the local codex. Zero machine boot needed."""
import asyncio, os, sys
from types import SimpleNamespace

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, REPO_ROOT)
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import CodexStreamTranslator, is_turn_terminal


async def main() -> int:
    cfg = SimpleNamespace(cc_cwd=os.getcwd(), tool_result_max=16000)
    h = CodexHandle(cfg, cwd=os.getcwd())
    await h.connect()
    print(f"[connected] thread={h.thread_id}")

    # ---- (1) CONTEXT before any turn: never crashes, has a capacity ----
    u = await h.get_context_usage()
    print(f"[ctx pre-turn] used={u['used_tokens']} window={u['context_window']}")
    assert u["context_window"] and u["context_window"] > 0

    # ---- (2) a short turn, then CONTEXT reflects real usage ----
    tr = CodexStreamTranslator(cfg.tool_result_max)
    await h.query("Say only: ok")
    async for msg in h.receive_response():
        tr.feed(msg)
        if is_turn_terminal(msg):
            break
    u = await h.get_context_usage()
    pct = (u["used_tokens"] / u["context_window"] * 100) if u["context_window"] else 0
    print(f"[ctx post-turn] used={u['used_tokens']} / {u['context_window']} = {pct:.1f}%")
    assert u["used_tokens"] and u["used_tokens"] > 0, "context usage should be populated after a turn"
    assert u["context_window"] == h.context_window, "window should be the server-authoritative value"
    print(f"[ctx] server-authoritative window captured = {h.context_window}  OK")

    # ---- (3) INTERRUPT a long turn; expect a terminal 已打断 TurnEnd ----
    tr2 = CodexStreamTranslator(cfg.tool_result_max)
    await h.query("Print numbers 1..500, each on its own line with a sentence about it.")
    deltas = 0
    sent = False
    turn_end = None
    async for msg in h.receive_response():
        for ev in tr2.feed(msg):
            if getattr(ev, "type", None) == "turn_end":
                turn_end = ev
        if msg.get("method") == "item/agentMessage/delta":
            deltas += 1
            if deltas >= 3 and not sent:
                sent = True
                await h.interrupt()          # <-- exact call _handle_interrupt makes
                print(f"[interrupt] sent after {deltas} deltas")
        if is_turn_terminal(msg):
            break
    assert turn_end is not None, "no TurnEnd after interrupt -> would drain-timeout in machine"
    sub = turn_end.result.subtype
    print(f"[interrupt] TurnEnd subtype={sub} is_error={turn_end.result.is_error}")
    assert sub == "error_during_execution", f"expected 已打断 mapping, got {sub}"
    print("[interrupt] -> reducer shows '— 已打断 —'  OK")

    await h.disconnect()
    print("\nALL LIVE CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
