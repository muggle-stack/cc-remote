"""Offline unit tests for the codex context-window + interrupt-mapping fixes.
No model calls — feeds synthetic notifications shaped exactly like the real ones
(captured from gpt-5.5: tokenUsage.{last,total,modelContextWindow})."""
import asyncio
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_stream import CodexStreamTranslator


class _Cfg:
    tool_result_max = 8000
    cc_cwd = "/tmp"


def test_context_window_capture_and_usage():
    h = CodexHandle(_Cfg())
    # before any turn: no server value, falls back to a config-declared window,
    # used is None (renders as 0) — never crashes.
    u0 = asyncio.run(h.get_context_usage())
    assert u0["context_window"] and u0["context_window"] > 0, u0
    assert u0["used_tokens"] is None, u0

    # real notification shape (verified live).
    notif = {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
        "last":  {"totalTokens": 21246, "inputTokens": 21241, "cachedInputTokens": 4992,
                  "outputTokens": 5, "reasoningOutputTokens": 0},
        "total": {"totalTokens": 21246, "inputTokens": 21241, "cachedInputTokens": 4992,
                  "outputTokens": 5, "reasoningOutputTokens": 0},
        "modelContextWindow": 258400}}}
    asyncio.run(h._dispatch(notif))
    assert h.context_window == 258400, h.context_window
    u = asyncio.run(h.get_context_usage())
    assert u["used_tokens"] == 21246, u
    assert u["context_window"] == 258400, u
    pct = u["used_tokens"] / u["context_window"] * 100
    assert 8.0 < pct < 8.5, pct
    print(f"  context: used={u['used_tokens']} / {u['context_window']} = {pct:.1f}%  OK")


def test_context_uses_last_not_cumulative_total():
    """On a later turn, `total` is the cumulative session sum (over-counts context);
    the gauge must use `last` (current depth)."""
    h = CodexHandle(_Cfg())
    asyncio.run(h._dispatch({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
        "last":  {"totalTokens": 40000},
        "total": {"totalTokens": 120000},   # 3 turns' cumulative
        "modelContextWindow": 258400}}}))
    u = asyncio.run(h.get_context_usage())
    assert u["used_tokens"] == 40000, u   # last, NOT 120000
    print(f"  context uses last(40000) not total(120000)  OK")


def test_interrupt_status_maps_to_cc_vocab():
    tr = CodexStreamTranslator(8000)
    evs = tr.feed({"method": "turn/completed", "params": {"turn": {
        "id": "turn-interrupted", "status": "interrupted", "durationMs": 3000}}})
    assert len(evs) == 1
    te = evs[0]
    assert te.result.subtype == "error_during_execution", te.result.subtype
    assert te.result.is_error is True
    assert te.turn_id == "turn-interrupted"
    print(f"  interrupted -> subtype={te.result.subtype} is_error={te.result.is_error}  OK")

    tr2 = CodexStreamTranslator(8000)
    tr2.feed({"method": "item/agentMessage/delta", "params": {
        "itemId": "answer-1", "delta": "done"}})
    ok = tr2.feed({"method": "turn/completed", "params": {"turn": {
        "id": "turn-completed", "status": "completed", "durationMs": 500}}})
    assert ok[-1].result.subtype == "success" and ok[-1].result.is_error is False
    assert ok[-1].turn_id == "turn-completed"
    print(f"  completed -> subtype=success is_error=False  OK")

    tr3 = CodexStreamTranslator(8000)
    fail = tr3.feed({"method": "turn/completed", "params": {"turn": {"status": "failed"}}})
    assert fail[-1].result.subtype == "error" and fail[-1].result.is_error is True
    print(f"  failed -> subtype=error is_error=True  OK")


def test_config_fast_default_read_never_writes_file():
    """Reading a fresh-thread Fast default is strictly read-only."""
    import os, tempfile, cc_remote.wrapper.codex_sessions as cs
    src = ('model_provider = "cubence"\nmodel = "gpt-5.5"\n'
           'model_reasoning_effort = "xhigh"\nservice_tier = "fast"\n\n'
           '[model_providers.cubence]\nbase_url = "https://x/v1"\n')
    tf = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    tf.write(src); tf.close()
    orig = cs._CONFIG
    cs._CONFIG = tf.name
    try:
        before = open(tf.name).read()
        assert cs.codex_fast_enabled() is True
        assert open(tf.name).read() == before
    finally:
        cs._CONFIG = orig
        os.unlink(tf.name)


def test_codex_errors_surface():
    """A failed codex turn (provider timeout / 401 / stream drop) must reach the
    client as an Error, not silence. Transient retries remain non-terminal but
    visible so a provider outage does not look like a frozen UI."""
    from cc_remote.protocol import Error
    tr = CodexStreamTranslator(8000)
    # transient retry -> progress, never a terminal Error/TurnEnd
    retry = tr.feed({"method": "error", "params": {"willRetry": True,
        "error": {"message": "Reconnecting... 2/5", "codexErrorInfo": {
            "responseStreamDisconnected": {"httpStatusCode": 503}}}}})
    assert [e.type for e in retry] == ["state"]
    assert retry[0].state == "running"
    assert "503" in retry[0].detail and "2/5" in retry[0].detail
    assert retry[0].phase == "retrying"
    # terminal error -> Error
    evs = tr.feed({"method": "error", "params": {"willRetry": False,
        "error": {"message": "unexpected status 401 Unauthorized", "additionalDetails": "Incorrect API key"}}})
    assert len(evs) == 1 and isinstance(evs[0], Error) and "401" in evs[0].message and "codex" in evs[0].message
    # failed turn/completed -> surfaces turn.error, then a TurnEnd(is_error)
    tr2 = CodexStreamTranslator(8000)
    out = tr2.feed({"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {"message": "request timed out"}}}})
    assert any(isinstance(e, Error) and "request timed out" in e.message for e in out), out
    assert out[-1].result.is_error is True and out[-1].result.subtype == "error"
    print("  codex errors surface: retry visible, 401 + failed-turn -> Error  OK")


def test_codex_empty_completed_is_an_error_but_tool_activity_is_not():
    """The production 503 incident ended as completed/error=null with no agent
    item. That shape must never become a silent success."""
    from cc_remote.protocol import Error, TurnEnd

    empty = CodexStreamTranslator(8000).feed({
        "method": "turn/completed",
        "params": {"turn": {"status": "completed", "durationMs": 237252}},
    })
    assert isinstance(empty[0], Error)
    assert "没有返回任何内容" in empty[0].message
    assert isinstance(empty[-1], TurnEnd)
    assert empty[-1].result.subtype == "error"
    assert empty[-1].result.is_error is True

    tool_only = CodexStreamTranslator(8000)
    tool_only.feed({"method": "item/started", "params": {"item": {
        "type": "commandExecution", "id": "tool-1", "command": "true"}}})
    done = tool_only.feed({"method": "turn/completed", "params": {
        "turn": {"status": "completed", "durationMs": 10}}})
    assert not any(isinstance(event, Error) for event in done)
    assert done[-1].result.subtype == "success"
    assert done[-1].result.is_error is False

    completed_only = CodexStreamTranslator(8000)
    answer = completed_only.feed({"method": "item/completed", "params": {
        "item": {"type": "agentMessage", "id": "answer-only",
                 "text": "provider sent no deltas"}}})
    assert [event.type for event in answer] == [
        "assistant_msg_start", "delta", "assistant_msg_end"]
    final = completed_only.feed({"method": "turn/completed", "params": {
        "turn": {"status": "completed", "durationMs": 20}}})
    assert final[-1].result.subtype == "success"


if __name__ == "__main__":
    import os
    test_context_window_capture_and_usage()
    test_context_uses_last_not_cumulative_total()
    test_interrupt_status_maps_to_cc_vocab()
    test_config_fast_toggle_preserves_file()
    test_codex_errors_surface()
    print("ALL PASS")
