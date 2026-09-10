"""Provider refusals stay failed across live, history, and ownership replay."""
import json
import sqlite3

import pytest

from cc_remote.protocol import Error, TurnEnd
from cc_remote.wrapper.codex_external import parse_turn_markers
from cc_remote.wrapper.machine import _codex_terminal_status
from cc_remote.wrapper.history_store import (
    HistoryIndexStore, HistorySourceFingerprint, MaterializedHistoryPage,
    _historical_turn_failure, materialize_history_turns,
)
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator,
    _provider_failure_message,
    codex_translate_history,
)


@pytest.mark.parametrize("info", ["cyber_policy", "cyberPolicy",
                                  {"cyberPolicy": {}}])
@pytest.mark.parametrize("key", ["codexErrorInfo", "codex_error_info"])
def test_policy_error_is_not_auth_network_or_retry_advice(info, key):
    message = _provider_failure_message({
        key: info, "message": "403 stream disconnected secret-token",
    })
    assert "cyber_policy" in message
    assert "secret-token" not in message
    assert "请重试" not in message
    assert "凭据" not in message


def _record(kind, **payload):
    return {"type": "event_msg", "timestamp": "2026-09-08T08:00:00Z",
            "payload": {"type": kind, "turn_id": "turn-1", **payload}}


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("error", [
    {"codex_error_info": "cyber_policy", "message": "private provider text"},
    {"message": "500 private provider text"},
])
def test_failed_task_complete_history_and_fence_agree(tmp_path, visible, error):
    records = [
        _record("task_started"),
        _record("user_message", message="Inspect OpenSBI IPI logic"),
    ]
    if visible:
        records.append(_record("agent_message", message="Checking the code"))
    records.append(_record("task_complete", error=error))
    raw = "".join(json.dumps(row) + "\n" for row in records)
    path = tmp_path / "rollout.jsonl"
    path.write_text(raw)
    events, _ = codex_translate_history(str(path), 8000)
    errors = [event for event in events if isinstance(event, Error)]
    ends = [event for event in events if isinstance(event, TurnEnd)]
    assert len(errors) == 1 and len(ends) == 1
    assert ends[0].result.is_error and ends[0].result.subtype == "error"
    assert "private provider text" not in errors[0].message
    if "codex_error_info" in error:
        assert "cyber_policy" in errors[0].message
    summaries = materialize_history_turns([
        event.model_dump(mode="json") for event in events])
    assert summaries[0]["error"] == errors[0].message
    markers = parse_turn_markers(raw.encode())
    assert markers.terminals[0].status == "failed"


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_live_policy_failure_finishes_and_next_turn_can_succeed(status):
    translator = CodexStreamTranslator(8000)
    events = translator.feed({"method": "turn/completed", "params": {
        "turn": {"id": "turn-1", "status": status, "error": {
            "codexErrorInfo": "cyberPolicy", "message": "private text",
        }},
    }})
    assert any(isinstance(e, Error) and "cyber_policy" in e.message
               for e in events)
    assert any(isinstance(e, TurnEnd) and e.result.is_error for e in events)
    # A new translator is the wrapper's per-query boundary. No policy error
    # may leak into the next user-initiated turn or fabricate a retry.
    next_turn = CodexStreamTranslator(8000)
    next_turn.feed({"method": "item/agentMessage/delta", "params": {
        "itemId": "answer-2", "delta": "The supplied log shows an IPI.",
    }})
    events = next_turn.feed({"method": "turn/completed", "params": {
        "turn": {"id": "turn-2", "status": "completed", "error": None},
    }})
    assert any(isinstance(e, TurnEnd) and not e.result.is_error for e in events)


def test_wrapper_cannot_synthesize_success_over_provider_error():
    assert _codex_terminal_status({"params": {"turn": {
        "status": "completed", "error": {"codexErrorInfo": "cyberPolicy"},
    }}}) == "failed"


def test_null_task_complete_error_remains_successful(tmp_path):
    raw = "".join(json.dumps(row) + "\n" for row in [
        _record("task_started"), _record("user_message", message="hello"),
        _record("task_complete", error=None, last_agent_message="done"),
    ])
    path = tmp_path / "rollout.jsonl"
    path.write_text(raw)
    events, _ = codex_translate_history(str(path), 8000)
    assert all(not e.result.is_error for e in events if isinstance(e, TurnEnd))
    assert parse_turn_markers(raw.encode()).terminals[0].status == "completed"


def test_policy_summary_accepts_only_the_reviewed_product_copy():
    message = _provider_failure_message({"codexErrorInfo": "cyberPolicy"})
    assert _historical_turn_failure(message) == message
    for raw in ("cyber_policy private provider text", message + " secret-token"):
        assert _historical_turn_failure(raw) == "该轮未正常结束"


def test_cached_policy_failure_is_rebuilt_once_without_changing_rollout(tmp_path):
    path = tmp_path / "rollout.jsonl"
    raw = "".join(json.dumps(row) + "\n" for row in [
        _record("task_started"), _record("user_message", message="inspect code"),
        _record("task_complete", error={"codex_error_info": "cyber_policy"}),
    ])
    path.write_text(raw)
    source = HistorySourceFingerprint.capture(path)
    events, _ = codex_translate_history(str(path), 8000)
    wire = tuple(event.model_dump(mode="json") for event in events)
    repaired = materialize_history_turns(wire)
    stale = dict(repaired[0], error="该轮未正常结束")
    page = MaterializedHistoryPage(
        events=wire, turns=(stale,), has_more=False,
        oldest_id=stale["id"], newest_id=stale["id"],
    )
    state = tmp_path / "state"
    store = HistoryIndexStore(state)
    assert store.put_page("session", "codex", source,
                          before=None, limit=4, page=page)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=31")
    migrated = HistoryIndexStore(state)
    assert migrated.get_page("session", "codex", source,
                             before=None, limit=4) is None
    rebuilt = MaterializedHistoryPage(
        events=wire, turns=repaired, has_more=False,
        oldest_id=stale["id"], newest_id=stale["id"],
    )
    assert migrated.put_page("session", "codex", source,
                             before=None, limit=4, page=rebuilt)
    assert HistoryIndexStore(state).get_page(
        "session", "codex", source, before=None, limit=4) == rebuilt
    assert "cyber_policy" in repaired[0]["error"]
    assert path.read_text() == raw
