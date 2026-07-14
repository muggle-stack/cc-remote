"""Live wrapper-level /btw proof: drive the REAL machine handlers end-to-end with
a live codex parent — open_btw -> query(fork) -> inherits context -> close_btw."""
import asyncio, os, sys
from types import SimpleNamespace

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, REPO_ROOT)
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import is_turn_terminal
from cc_remote.wrapper.ringbuffer import RingBuffer

CFG = SimpleNamespace(tool_result_max=8000, ring_max_events=2000, ring_max_bytes=2_000_000,
                      max_concurrent_sessions=8, cc_cwd=os.getcwd(), state_dir="/tmp/btwtest",
                      drain_timeout=30.0)

async def main():
    os.makedirs("/tmp/btwtest", exist_ok=True)
    sent = []
    class T:
        async def send(self, m): sent.append(m)
    m = WrapperMachine.__new__(WrapperMachine)
    m.cfg = CFG; m.sessions = {}; m.focused_sid = None; m.transport = T()

    # --- real parent codex session with context ---
    ph = CodexHandle(CFG, cwd=os.getcwd()); await ph.connect()
    ph_tr = __import__("cc_remote.wrapper.codex_stream", fromlist=["CodexStreamTranslator"]).CodexStreamTranslator(8000)
    await ph.query("Remember codeword QUOKKA. Acknowledge in one word.")
    async for msg in ph.receive_response():
        if is_turn_terminal(msg): break
    parent = SessionContext(session_id=ph.thread_id, sdk=ph, buffer=RingBuffer(2000, 2_000_000),
                            cwd=os.getcwd(), engine="codex", key=ph.thread_id)
    m.sessions[parent.key] = parent; m.focused_sid = parent.key
    print("parent:", parent.key)

    # --- OPEN BTW ---
    await m._handle_open_btw(SimpleNamespace(
        type="open_btw", sid=parent.key, client_id="c1",
        request_id="live-btw-request"))
    opened = [s for s in sent if getattr(s, "type", None) == "btw_opened"]
    assert opened, "no btw_opened emitted"
    btw_sid = opened[0].btw_sid
    print("btw_opened:", btw_sid, "engine=", opened[0].engine, "to=", opened[0].to)
    assert opened[0].to == "c1", "must route to requester"
    assert btw_sid in m.sessions and m.sessions[btw_sid].btw
    assert m.focused_sid == parent.key, "opening btw must NOT steal focus"

    # --- QUERY ON THE FORK ---
    sent.clear()
    await m._handle_query(SimpleNamespace(type="query", sid=btw_sid, prompt="What codeword did I ask you to remember? one word.",
                                          msg_id="mm1", images=None, files=None))
    await m.sessions[btw_sid].turn_task   # run the fork turn to completion
    text = "".join(getattr(s, "text", "") for s in sent
                   if getattr(s, "type", None) == "delta" and getattr(s, "sid", None) == btw_sid)
    print("fork answer:", text.strip()[:80])
    assert "QUOKKA" in text.upper(), f"fork must inherit parent context, got: {text!r}"
    # every fork frame routed under btw_sid (not parent)
    assert all(getattr(s, "sid", None) in (btw_sid, None) for s in sent if getattr(s, "type", None) == "delta")
    # parent untouched
    assert parent.session_id == ph.thread_id and parent.key in m.sessions
    print("parent untouched OK; focus still", m.focused_sid == parent.key)

    # --- CLOSE BTW ---
    await m._handle_close_btw(SimpleNamespace(type="close_btw", sid=btw_sid))
    assert btw_sid not in m.sessions, "close must remove the fork"
    print("btw closed + removed OK")

    await ph.disconnect()
    print("\nWRAPPER /btw E2E PASS")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
