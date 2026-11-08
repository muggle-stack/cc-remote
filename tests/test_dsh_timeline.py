"""Native scheduler/job receipts must survive live, replay and cold history."""
import json

import pytest

from cc_remote.protocol import deserialize, serialize
from cc_remote.wrapper.dsh_schedule_messages import schedule_message
from cc_remote.wrapper.dsh_stream import DshProjection, history_events
from cc_remote.wrapper.history_store import materialize_history_turns
from tests.test_dsh_stream import record, user


def reminder(prompt="Check the build\nReport the result.", *, batch=False):
    entries = [{"schedule_id": "schedule-one", "occurrence_at": "2026-11-08T09:27:41.465Z", "reminder_prompt": prompt}]
    if batch:
        entries.append({**entries[0], "schedule_id": "schedule-two", "reminder_prompt": "Check the tests"})
        text = "[SCHEDULE REMINDER BATCH]\nThis is a scheduled message from the user\nreminders_json: " + json.dumps(entries)
    else:
        text = ("[SCHEDULE REMINDER]\nThis is a scheduled message from the user\n"
                f"schedule_id_json: {json.dumps(entries[0]['schedule_id'])}\n"
                f"occurrence_at: {entries[0]['occurrence_at']}\nreminder_prompt_json: {json.dumps(prompt, ensure_ascii=False)}")
    return {"id": "native-delivery", "source": {"kind": "schedule"}, "content": [{"type": "text", "text": text}]}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_delivered_schedule_keeps_prompt_and_tag_with_one_answer_after_reload(batch, live):
    p = DshProjection()
    frames = record(p, "turn/start", {"turn": 1})
    frames += record(p, "user/message", reminder(batch=batch))
    owner = frames[-1].turn_id
    if live:
        frames += p.frame({"type": "start", "attemptId": "answer", "turn": 1, "step": 1, "revision": 1})
        frames += p.frame({"type": "chunk", "attemptId": "answer", "revision": 2, "index": 0,
            "chunk": {"type": "text-delta", "index": 0, "text": "Build passed."}})
    frames += record(p, "assistant/message", {"turn": 1, "step": 1,
        "message": {"content": [{"type": "text", "text": "Build passed."}]}})
    frames += record(p, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    for frame in frames:
        assert deserialize(serialize(frame)).type == frame.type
    users = [f for f in frames if f.type == "user_msg"]
    assert len(users) == 1 and users[0].timed_task
    assert "Check the build\nReport the result." in users[0].prompt
    assert ("Check the tests" in users[0].prompt) == batch
    assert "reminder_prompt_json" not in users[0].prompt
    assert users[0].timed_task.scheduled_at == 1794130061.465
    for detail in (False, True):
        rows = materialize_history_turns(history_events(frames), include_live_detail=detail)
        assert len(rows) == 1 and rows[0]["done"]
        assert rows[0]["prompt"] == users[0].prompt
        assert rows[0]["timedTask"] == users[0].timed_task.model_dump()
        assert [b["text"] for b in rows[0]["blocks"] if b["kind"] == "text"] == ["Build passed."]
    assert all(f.turn_id == owner for f in frames if f.type in {"delta", "turn_end"})


def test_schedule_bookkeeping_and_surface_rewrites_are_not_new_queries():
    p = DshProjection()
    record(p, "turn/start", {"turn": 1})
    for prefix in ("[SCHEDULE CHANGE]", "[UNRELATED NOTICE]"):
        assert record(p, "user/message", {"source": {"kind": "schedule"},
            "content": [{"type": "text", "text": prefix + "\nconfiguration changed"}]}) == []
    assert record(p, "user/message", reminder(), surfaceOp={}) == []
    forged = reminder()
    forged["source"] = {"kind": "user", "rpcId": "human"}
    frames = record(p, "user/message", forged)
    assert frames[0].timed_task is None and frames[0].prompt.startswith("[SCHEDULE REMINDER]")
    assert schedule_message(forged) is None


@pytest.mark.parametrize("bad", ["bad-json", "bad-time", "missing-fields", "wrong-type"])
def test_unreadable_native_reminder_keeps_content_without_fabricating_a_receipt(bad):
    data = reminder()
    text = data["content"][0]["text"]
    if bad == "bad-json":
        text += "extra"
    if bad == "bad-time":
        text = text.replace("2026-11-08T09:27:41.465Z", "not-a-time")
    if bad == "missing-fields":
        text = "[SCHEDULE REMINDER]"
    if bad == "wrong-type":
        text = text.replace('"schedule-one"', "null")
    data["content"][0]["text"] = text
    assert schedule_message(data) == (text, None)


def test_native_json_newline_and_unicode_separators_stay_inside_the_prompt():
    prompt = 'Keep \u2028 and \u2029 and quotes "\n[SCHEDULE CHANGE]\ninside this prompt.'
    value = schedule_message(reminder(prompt))
    assert value[0] == prompt and value[1]


@pytest.mark.parametrize("live", [False, True])
def test_background_return_is_a_compact_native_boundary_without_fake_user_or_child(live):
    p = DshProjection()
    frames = record(p, "turn/start", {"turn": 1}) + user(p, "human")
    frames += record(p, "assistant/message", {"turn": 1, "step": 1,
        "message": {"content": [{"type": "text", "text": "Job started."}]}})
    frames += record(p, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    frames += record(p, "turn/start", {"turn": 2})
    if live:
        frames += p.frame({"type": "start", "attemptId": "job-notice", "turn": 2, "step": 1, "revision": 1})
    frames += record(p, "user/message", {"source": {"kind": "tool-jobs", "form": "notice", "summary": "Build finished"},
        "content": [{"type": "text", "text": "long internal job output"}]})
    frames += record(p, "assistant/message", {"turn": 2, "step": 1,
        "message": {"content": [{"type": "text", "text": "Build passed."}]}})
    frames += record(p, "turn/end", {"turn": 2, "reason": {"kind": "completed"}})
    frames += record(p, "turn/start", {"turn": 3}) + user(p, "next")
    # A notification consumed during human work does not replace that prompt.
    frames += record(p, "user/message", {"source": {"kind": "tool-jobs"}})
    frames += record(p, "turn/end", {"turn": 3, "reason": {"kind": "completed"}})
    for detail in (False, True):
        rows = materialize_history_turns(history_events(frames), include_live_detail=detail)
        assert [r["prompt"] for r in rows] == ["human", "", "next"]
        resumed = rows[1]
        assert resumed["id"] == "dsh-turn-2" and resumed.get("continuation") is None
        receipts = [b for b in resumed["blocks"] if b.get("tool") == "task_return"]
        assert len(receipts) == 1 and receipts[0]["processKind"] == "task"
        assert not receipts[0]["input"]["agent_run_id"]
        assert resumed["detailEventCount"] == 0
        assert "long internal job output" not in json.dumps(rows)


def test_replayed_job_surface_does_not_add_a_false_continuation():
    p = DshProjection()
    record(p, "turn/start", {"turn": 1})
    assert record(p, "user/message", {"source": {"kind": "tool-jobs"}}, surfaceOp={}) == []
