"""Native quota failures retain their cause across live and persisted views."""
import asyncio
import json

import pytest

from cc_remote.protocol import Error, StateEvent, TurnEnd
from cc_remote.wrapper.codex_history import CodexOfficialHistory
from cc_remote.wrapper.codex_stream import CodexStreamTranslator, codex_translate_history
from cc_remote.wrapper.history_store import materialize_history_turns


EXPECTED = (
    "本轮使用的 Codex 账号额度已用完。"
    "可切换账号、补充额度，或等待恢复后重试。"
)
DATED = EXPECTED + "官方提示可于 2026-09-15 09:24（设备当地时间）重试。"
DIAGNOSTIC = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at Sep 15th, 2026 9:24 AM."
)


@pytest.mark.parametrize("error, expected", [
    ({"codexErrorInfo": "usageLimitExceeded"}, EXPECTED),
    ({"codex_error_info": "usage_limit_exceeded"}, EXPECTED),
    ({"codexErrorInfo": {"usageLimitExceeded": {}}}, EXPECTED),
    ({"codex_error_info": {"usage_limit_exceeded": {}}}, EXPECTED),
    ({"message": DIAGNOSTIC}, DATED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": DIAGNOSTIC,
      "additionalDetails": "HTTP 429 bearer SECRET"}, DATED),
    ({"codex_error_info": "usage_limit_exceeded", "message": "PRIVATE_SECRET"}, EXPECTED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Feb 30th, 2026 9:24 AM."}, EXPECTED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Sep 15th, 2026 13:24 AM."}, EXPECTED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Sep 15th, 2026 9:60 AM."}, EXPECTED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Sep 15th, 0000 9:24 AM."}, EXPECTED),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Sep 15th, 2026 12:00 AM."},
     EXPECTED + "官方提示可于 2026-09-15 00:00（设备当地时间）重试。"),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "try again at Sep 15th, 2026 12:00 PM."},
     EXPECTED + "官方提示可于 2026-09-15 12:00（设备当地时间）重试。"),
    ({"codexErrorInfo": "usageLimitExceeded", "message": "x" * 8192 + DIAGNOSTIC}, EXPECTED),
    ({"message": "HTTP 429 Too Many Requests"}, "请求过于频繁或当前额度受限，请稍后重试。"),
    ({"codexErrorInfo": "serverOverloaded"}, "当前模型繁忙，请稍后重试或切换模型。"),
    ({"message": "request timed out"}, "请求超时，请重新尝试。"),
])
def test_provider_failure_notification_and_failed_terminal_agree(error, expected):
    for method, params in (
        ("error", {"error": error, "willRetry": False}),
        ("turn/completed", {"turn": {"id": "native", "status": "failed", "error": error}}),
    ):
        events = CodexStreamTranslator(8000).feed({"method": method, "params": params})
        failures = [event for event in events if isinstance(event, Error)]
        assert len(failures) == 1
        assert failures[0].message == expected
        assert "SECRET" not in failures[0].message
        if method == "turn/completed":
            assert isinstance(events[-1], TurnEnd)
            assert events[-1].result.subtype == "error"


def test_usage_limit_retry_is_progress_until_the_official_terminal():
    translator = CodexStreamTranslator(8000)
    events = translator.feed({"method": "error", "params": {
        "willRetry": True, "error": {"codexErrorInfo": "usageLimitExceeded"},
    }})
    assert len(events) == 1 and isinstance(events[0], StateEvent)
    assert events[0].state == "running" and events[0].phase == "retrying"
    assert "账号额度已用完" in events[0].detail
    terminal = translator.feed({"method": "turn/completed", "params": {"turn": {
        "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"},
    }}})
    assert terminal[0].message == EXPECTED
    assert terminal[-1].result.is_error


def test_real_rollout_usage_limit_survives_materialization_without_tainting_followup(tmp_path):
    path = tmp_path / "rollout.jsonl"
    payloads = [
        {"type": "task_started", "turn_id": "limited-native"},
        {"type": "user_message", "message": "Continue"},
        {"type": "task_complete", "turn_id": "limited-native", "last_agent_message": None,
         "error": {"codex_error_info": "usage_limit_exceeded", "message": DIAGNOSTIC}},
        {"type": "task_started", "turn_id": "next-native"},
        {"type": "user_message", "message": "Continue with another account"},
        # A new account's quota cannot replace the failed request's retry date.
        {"type": "token_count", "rate_limits": {"primary": {"used_percent": 0, "resets_at": 1}}},
        {"type": "task_complete", "turn_id": "next-native", "last_agent_message": "Done"},
    ]
    path.write_text("".join(json.dumps({
        "type": "event_msg", "payload": payload, "timestamp": f"2026-09-10T04:34:{index:02d}Z",
    }) + "\n" for index, payload in enumerate(payloads)))
    events, _ = codex_translate_history(str(path), 8000)
    turns = materialize_history_turns([event.model_dump(mode="json") for event in events])
    assert len(turns) == 2
    assert turns[0]["forkPointId"] == "limited-native"
    assert turns[0]["done"] and turns[0]["error"] == DATED
    assert turns[1]["forkPointId"] == "next-native"
    assert turns[1]["done"] and not turns[1].get("error")


def test_official_usage_limit_summary_retains_reason_and_retry_date():
    async def rpc(_method, _params, cwd=None):
        return {"data": [{
            "id": "limited", "status": "failed", "itemsView": "summary",
            "items": [{"type": "userMessage", "id": "prompt", "content": [{"type": "text", "text": "Run"}]}],
            "error": {"codexErrorInfo": "usageLimitExceeded", "message": DIAGNOSTIC},
        }], "nextCursor": None}

    page = asyncio.run(CodexOfficialHistory(64 * 1024, rpc=rpc).summary_page("session", before=None, limit=1))
    assert page.turns[0]["done"] and page.turns[0]["error"] == DATED
    assert "https://" not in json.dumps(page.turns)


@pytest.mark.parametrize("suffix", [
    "PRIVATE_SECRET", "官方提示可于 2026-02-30 09:24（设备当地时间）重试。",
    "官方提示可于 2026-09-15 09:24（设备当地时间）重试。PRIVATE_SECRET",
])
def test_history_rejects_unreviewed_suffixes(suffix):
    turns = materialize_history_turns([
        {"type": "user_msg", "msg_id": "prompt", "prompt": "Run"},
        {"type": "error", "code": "cc_crash", "message": EXPECTED + suffix},
    ])
    assert turns[0]["error"] == "该轮未正常结束"
