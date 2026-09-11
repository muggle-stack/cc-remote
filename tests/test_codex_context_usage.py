"""Zero-model regressions for compaction-aligned, source-bound context usage."""
import asyncio
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from cc_remote.wrapper import codex_context_usage as native
from cc_remote.wrapper import codex_handle as handle_module
from cc_remote.wrapper.codex_handle import CodexHandle
from tests.test_codex_context_interrupt import _Cfg


SID = "test-session"
TURN = "native-turn"
STAMP = "2026-09-11T02:14:39.052Z"
SECONDS = int(datetime.fromisoformat(STAMP.replace("Z", "+00:00")).timestamp())
LAST = {"totalTokens": 245325, "inputTokens": 241984, "outputTokens": 3341}
BODY = ("trace: post sampling token usage turn_id=native-turn "
        "total_usage_tokens=284793 auto_compact_scope_tokens=284793 "
        "auto_compact_scope_limit=Some(284211) auto_compact_limit_scope=Total "
        "full_context_window_limit=Some(300000) token_limit_reached=true")


@pytest.fixture
def source(tmp_path, monkeypatch):
    rollout = tmp_path / "rollout.jsonl"
    records = [
        {"type": "turn_context", "payload": {"turn_id": TURN}},
        {"timestamp": STAMP, "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"total_tokens": 245325, "input_tokens": 241984,
                                     "output_tokens": 3341},
                "model_context_window": 300000}}},
    ]
    rollout.write_text("".join(json.dumps(r) + "\n" for r in records))
    monkeypatch.setattr(native, "codex_rollout_path", lambda *a, **kw: str(rollout))
    db = sqlite3.connect(tmp_path / "logs_2.sqlite")
    db.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, thread_id TEXT, ts INTEGER, "
               "ts_nanos INTEGER, target TEXT, feedback_log_body TEXT)")
    db.execute("CREATE INDEX by_thread_time ON logs(thread_id,ts)")
    db.execute("INSERT INTO logs VALUES(1,?,?,?,?,?)",
               (SID, SECONDS, 54195000, "codex_core::session::turn", BODY))
    db.commit()
    yield tmp_path, rollout, db
    db.close()


def read(source, **overrides):
    return native.read_codex_context_estimate(
        **{"session_id": SID, "codex_home": str(source[0]),
           "last": LAST, "window": 300000, **overrides})


def test_screenshot_82_percent_uses_native_95_percent_estimate(source):
    estimate = read(source)
    assert estimate == native.CodexContextEstimate(284793, 284211)
    assert round(estimate.used_tokens / 300000 * 100) == 95
    assert estimate.used_tokens >= estimate.threshold_tokens


def test_account_thread_window_and_sample_must_all_match(source, tmp_path):
    assert read(source, session_id="another-session") is None
    assert read(source, codex_home=str(tmp_path / "another-account")) is None
    assert not (tmp_path / "another-account").exists()
    assert read(source, window=400000) is None
    assert read(source, last={**LAST, "totalTokens": 245326}) is None
    assert read(source, turn_id="another-turn") is None


@pytest.mark.parametrize("before_sample", [False, True])
def test_pre_compaction_estimate_never_survives_compaction(source, before_sample):
    _, rollout, _ = source
    with rollout.open("a") as f:
        f.write(json.dumps({"type": "compacted", "payload": {}}) + "\n")
        if not before_sample:
            f.write(json.dumps({"type": "event_msg", "timestamp": "2026-09-11T02:16:58.973Z",
                "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"total_tokens": 28643},
                    "model_context_window": 300000}}}) + "\n")
    assert read(source, last={"totalTokens": 28643} if not before_sample else LAST) is None


def test_old_same_turn_sample_is_rejected_and_new_sample_recovers(source):
    _, rollout, db = source
    stamp = datetime.fromtimestamp(SECONDS + 180, timezone.utc).isoformat()
    with rollout.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "token_count", "info": {"last_token_usage": {"total_tokens": 33700},
                                           "model_context_window": 300000}}}) + "\n")
    assert read(source, last={"totalTokens": 33700}) is None
    body = BODY.replace("284793", "33824").replace("token_limit_reached=true", "token_limit_reached=false")
    db.execute("INSERT INTO logs VALUES(2,?,?,?,?,?)",
               (SID, SECONDS + 180, 1000000, "codex_core::session::turn", body))
    db.commit()
    assert read(source, last={"totalTokens": 33700}).used_tokens == 33824


@pytest.mark.parametrize("old,new", [
    ("turn_id=native-turn", "turn_id=other-turn"),
    ("full_context_window_limit=Some(300000)", "full_context_window_limit=Some(400000)"),
    ("auto_compact_limit_scope=Total", "auto_compact_limit_scope=BodyAfterPrefix"),
    ("auto_compact_scope_tokens=284793", "auto_compact_scope_tokens=100000"),
    ("total_usage_tokens=284793", "total_usage_tokens=-1"),
    ("total_usage_tokens=284793", "total_usage_tokens=9007199254740992"),
    ("auto_compact_scope_limit=Some(284211)", "auto_compact_scope_limit=Some(400000)"),
])
def test_unsupported_or_unrelated_native_rows_stay_unknown(source, old, new):
    source[2].execute("UPDATE logs SET feedback_log_body=?", (BODY.replace(old, new),))
    source[2].commit()
    assert read(source) is None


def test_ambiguous_or_wrong_time_logs_stay_unknown(source):
    db = source[2]
    db.execute("UPDATE logs SET ts=ts-1")
    db.commit()
    assert read(source) is None
    db.execute("UPDATE logs SET ts=ts+1")
    db.execute("INSERT INTO logs SELECT 2,thread_id,ts,ts_nanos+1,target,feedback_log_body FROM logs")
    db.commit()
    assert read(source) is None


def test_missing_schema_and_large_foreign_history_do_not_leak_or_scan(source):
    db = source[2]
    db.executemany("INSERT INTO logs VALUES(?, 'other-session', ?, 0, ?, ?)",
                   ((i, SECONDS, "codex_core::session::turn", BODY) for i in range(2, 10002)))
    db.commit()
    assert read(source).used_tokens == 284793
    db.execute("DROP TABLE logs")
    db.commit()
    assert read(source) is None


def test_rollout_rewrite_during_lookup_invalidates_estimate(source, monkeypatch):
    original = native._sample

    def rewritten(*args):
        sample = original(*args)
        source[1].write_text("{}\n")
        return sample

    monkeypatch.setattr(native, "_sample", rewritten)
    assert read(source) is None


def test_oversized_new_compaction_cannot_revive_old_estimate(source):
    with source[1].open("a") as f:
        f.write(json.dumps({"type": "compacted", "payload": {"message": "x" * (1024 * 1024)}}) + "\n")
    assert read(source) is None


def test_live_estimator_replaces_request_usage_and_compaction_invalidates_it(source):
    async def run():
        handle = CodexHandle(_Cfg(), codex_home=str(source[0]))
        handle.thread_id = SID
        await handle._dispatch({"method": "thread/tokenUsage/updated", "params": {
            "threadId": SID, "turnId": TURN, "tokenUsage": {
                "last": LAST, "total": {"totalTokens": 9000000}, "modelContextWindow": 300000}}})
        usage = await handle.get_context_usage()
        assert usage["used_tokens"] == 284793
        assert usage["source"] == "native_estimate"
        assert usage["auto_compact_threshold_tokens"] == 284211
        assert usage["raw"]["last"] == LAST
        handle.turn_id = TURN
        await handle._dispatch({"method": "thread/compacted", "params": {"threadId": SID, "turnId": TURN}})
        assert (await handle.get_context_usage())["used_tokens"] is None
    asyncio.run(run())


def test_context_read_discards_generation_or_usage_race(source, monkeypatch):
    async def run():
        handle = CodexHandle(_Cfg(), codex_home=str(source[0]))
        handle.thread_id = SID
        handle.last_token_usage = {"last": LAST}
        handle.context_window = 300000

        def changed(*args, **kwargs):
            handle._generation += 1
            return native.CodexContextEstimate(284793, 284211)

        monkeypatch.setattr(handle_module, "read_codex_context_estimate", changed)
        result = await handle.get_context_usage()
        assert result["used_tokens"] is None
        assert result["source"] == "recent_turn"
    asyncio.run(run())


def test_model_usage_fallback_never_uses_session_cumulative_tokens():
    handle = CodexHandle(_Cfg())
    handle.last_token_usage = {"total": {"totalTokens": 9000000}}
    assert asyncio.run(handle.get_context_usage())["used_tokens"] is None
