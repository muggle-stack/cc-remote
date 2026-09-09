"""Offline unit tests for the codex context-window + interrupt-mapping fixes.
No model calls — feeds synthetic notifications shaped exactly like the real ones
(captured from gpt-5.5: tokenUsage.{last,total,modelContextWindow})."""
import asyncio
import os
import tempfile

import cc_remote.wrapper.codex_sessions as codex_sessions
import cc_remote.wrapper.codex_handle as codex_handle_module

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER
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


def test_context_rejects_invalid_live_token_usage_values():
    h = CodexHandle(_Cfg())
    h.last_token_usage = {
        "last": {"totalTokens": True},
        "total": {"totalTokens": -5},
        "modelContextWindow": "not-a-number",
    }
    h.context_window = True

    usage = asyncio.run(h.get_context_usage())

    assert usage["used_tokens"] is None
    assert isinstance(usage["context_window"], int)
    assert not isinstance(usage["context_window"], bool)

    h.last_token_usage = {
        "last": {"totalTokens": MAX_SAFE_WIRE_INTEGER + 1},
        "total": {"totalTokens": MAX_SAFE_WIRE_INTEGER + 1},
        "modelContextWindow": MAX_SAFE_WIRE_INTEGER + 1,
    }
    h.context_window = MAX_SAFE_WIRE_INTEGER + 1
    usage = asyncio.run(h.get_context_usage())
    assert usage["used_tokens"] is None
    assert 0 < usage["context_window"] <= MAX_SAFE_WIRE_INTEGER


def test_work_cold_resume_recovers_context_until_live_notification(monkeypatch):
    recovered_calls = []

    def recover(session_id, *, codex_home=None):
        recovered_calls.append((session_id, codex_home))
        return {
            "last": {"totalTokens": 103658},
            "modelContextWindow": 258400,
        }

    monkeypatch.setattr(codex_handle_module, "recover_codex_context_usage", recover)
    h = CodexHandle(_Cfg(), work_mode=True, codex_home="/tmp/profile")
    h.thread_id = "native-session"
    cold = asyncio.run(h.get_context_usage())
    assert cold["used_tokens"] == 103658
    assert cold["context_window"] == 258400
    assert recovered_calls == [(
        "native-session", os.path.realpath("/tmp/profile"))]

    asyncio.run(h._dispatch({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {
            "last": {"totalTokens": 104321},
            "modelContextWindow": 300000,
        }},
    }))
    live = asyncio.run(h.get_context_usage())
    assert live["used_tokens"] == 104321
    assert live["context_window"] == 300000
    assert len(recovered_calls) == 1


def test_code_cold_resume_recovers_profile_scoped_context(monkeypatch):
    recovered_calls = []

    def recover(session_id, *, codex_home=None):
        recovered_calls.append((session_id, codex_home))
        return {
            "last": {"totalTokens": 88_765},
            "modelContextWindow": 258_400,
        }

    monkeypatch.setattr(
        codex_handle_module, "recover_codex_context_usage", recover)
    handle = CodexHandle(_Cfg(), codex_home="/tmp/code-profile")
    handle.thread_id = "code-native-session"

    usage = asyncio.run(handle.get_context_usage())

    assert usage["used_tokens"] == 88_765
    assert usage["context_window"] == 258_400
    assert recovered_calls == [(
        "code-native-session", os.path.realpath("/tmp/code-profile"))]


def test_work_context_recovery_discards_old_thread_race(monkeypatch):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def recover(_session_id, *, codex_home=None):
            del codex_home
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return {
                "last": {"totalTokens": 999},
                "modelContextWindow": 1000,
            }

        monkeypatch.setattr(
            codex_handle_module, "recover_codex_context_usage", recover)
        handle = CodexHandle(_Cfg(), work_mode=True)
        handle.thread_id = "old-thread"
        reading = asyncio.create_task(handle.get_context_usage())
        await started.wait()
        handle.thread_id = "new-thread"
        handle._generation += 1
        release.set()
        usage = await reading
        assert usage["used_tokens"] is None
        assert handle.last_token_usage is None

    asyncio.run(run())


def test_work_context_recovery_retries_after_transient_miss(monkeypatch):
    calls = 0

    def recover(_session_id, *, codex_home=None):
        nonlocal calls
        del codex_home
        calls += 1
        if calls == 1:
            return None
        return {
            "last": {"totalTokens": 456},
            "modelContextWindow": 1000,
        }

    monkeypatch.setattr(
        codex_handle_module, "recover_codex_context_usage", recover)
    handle = CodexHandle(_Cfg(), work_mode=True)
    handle.thread_id = "native-session"

    first = asyncio.run(handle.get_context_usage())
    second = asyncio.run(handle.get_context_usage())

    assert first["used_tokens"] is None
    assert second["used_tokens"] == 456
    assert calls == 2


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

    tr3 = CodexStreamTranslator(8000)
    fail = tr3.feed({"method": "turn/completed", "params": {"turn": {"status": "failed"}}})
    assert fail[-1].result.subtype == "error" and fail[-1].result.is_error is True


def test_config_fast_default_read_never_writes_file():
    """Reading a fresh-thread Fast default is strictly read-only."""
    src = ('model_provider = "cubence"\nmodel = "gpt-5.5"\n'
           'model_reasoning_effort = "xhigh"\nservice_tier = "fast"\n\n'
           '[model_providers.cubence]\nbase_url = "https://x/v1"\n')
    tf = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    tf.write(src)
    tf.close()
    orig = codex_sessions._CONFIG
    codex_sessions._CONFIG = tf.name
    try:
        before = open(tf.name).read()
        assert codex_sessions.codex_fast_enabled() is True
        assert open(tf.name).read() == before
    finally:
        codex_sessions._CONFIG = orig
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
    assert len(evs) == 1 and isinstance(evs[0], Error)
    assert evs[0].message == (
        "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。"
    )
    assert "401" not in evs[0].message and "API key" not in evs[0].message
    network = CodexStreamTranslator(8000).feed({
        "method": "error",
        "params": {
            "willRetry": False,
            "error": {
                "message": "stream disconnected before completion: "
                           "error sending request for url "
                           "https://chatgpt.com/backend-api/codex/responses",
                "codexErrorInfo": "other",
            },
        },
    })
    assert len(network) == 1 and isinstance(network[0], Error)
    assert network[0].message == "网络连接异常，请检查网络后重试。"
    assert "chatgpt.com" not in network[0].message
    # failed turn/completed -> a safe Error, then a TurnEnd(is_error)
    tr2 = CodexStreamTranslator(8000)
    out = tr2.feed({"method": "turn/completed", "params": {"turn": {"status": "failed", "error": {"message": "request timed out"}}}})
    assert any(isinstance(e, Error)
               and e.message == "请求超时，请重新尝试。"
               for e in out), out
    assert out[-1].result.is_error is True and out[-1].result.subtype == "error"
    print("  codex errors surface: retry visible, terminal details sanitized  OK")


def test_codex_terminal_provider_failures_keep_distinct_safe_copy():
    from cc_remote.protocol import Error

    cases = [
        (
            {
                "message": "stream disconnected before completion",
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 401},
                },
            },
            "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
        ),
        (
            {
                "message": "error sending request",
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 403},
                },
            },
            "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
        ),
        (
            {"message": "error sending request: HTTP 429 Too Many Requests"},
            "请求过于频繁或当前额度受限，请稍后重试。",
        ),
        (
            {"message": "request timed out with status 408"},
            "请求超时，请重新尝试。",
        ),
        (
            {
                "message": "stream disconnected before completion",
                "codexErrorInfo": {
                    "responseStreamDisconnected": {"httpStatusCode": 503},
                },
            },
            "Codex 上游服务暂时不可用，请稍后重试。",
        ),
        (
            {"message": "TLS error while opening provider socket"},
            "网络连接异常，请检查网络后重试。",
        ),
        (
            {"message": "model execution failed"},
            "Codex 本次回复未完成，请重试。",
        ),
        *[(error, "当前模型繁忙，请稍后重试或切换模型。") for error in (
            {"codexErrorInfo": "serverOverloaded"},
            {"codex_error_info": "server_overloaded"},
            {"codexErrorInfo": {"serverOverloaded": {}}},
            {"message": "Selected model is at capacity. Please try a different model. "
                        "https://private.invalid/?token=SECRET"},
        )],
        (
            {"codex_error_info": {"response_stream_disconnected": {"http_status_code": 401}}},
            "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。",
        ),
    ]

    for error, expected in cases:
        events = CodexStreamTranslator(8000).feed({
            "method": "error",
            "params": {"willRetry": False, "error": error},
        })
        assert len(events) == 1 and isinstance(events[0], Error)
        assert events[0].message == expected
        assert "HTTP" not in events[0].message

    custom_provider = CodexStreamTranslator(8000).feed({
        "method": "error",
        "params": {
            "willRetry": False,
            "error": {"message": "cubence API key rejected: HTTP 401"},
        },
    })
    assert custom_provider[0].message == (
        "模型服务认证已失效或当前账号无权限，"
        "请检查当前服务的凭据或账号权限后重试。"
    )
    assert "Codex 登录" not in custom_provider[0].message


def test_codex_capacity_retry_remains_progress_until_a_real_terminal():
    from cc_remote.protocol import Error, StateEvent, TurnEnd

    translator = CodexStreamTranslator(8000)
    events = translator.feed({"method": "error", "params": {
        "willRetry": True, "error": {"codexErrorInfo": "serverOverloaded"},
    }})
    assert len(events) == 1 and isinstance(events[0], StateEvent)
    assert events[0].state == "running" and events[0].phase == "retrying"
    assert events[0].detail == "当前模型繁忙，Codex 正在重试…"
    assert not any(isinstance(event, (Error, TurnEnd)) for event in events)


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
