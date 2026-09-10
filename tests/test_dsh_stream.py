"""V3 behavior regressions, including a capture from the pinned native runtime."""
import json
from pathlib import Path

import pytest

from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_stream import DshProjection, history_events, input_tokens, model_id, model_parts
from cc_remote.wrapper.history_store import materialize_history_turns

FIXTURE = Path(__file__).parent / "fixtures/dsh/native_v3_flow.json"


def native_flow():
    p = DshProjection()
    frames = []
    for native in json.loads(FIXTURE.read_text()):
        if native["type"] == "snapshot":
            for record in native["records"]:
                frames += p.record(record["event"])
            frames += p.baseline(native.get("assistantStream", {}))
        elif native["type"] == "event":
            frames += p.record(native["event"])
        else:
            frames += p.frame(native["frame"])
    return p, frames


def test_actual_native_stream_settles_once_under_its_human_owner():
    p, frames = native_flow()
    users = [f for f in frames if f.type == "user_msg"]
    assert len(users) == 1  # plugin/system/skill-catalog user-role records are hidden
    owner = next(f.turn_id for f in frames if f.type == "turn_binding")
    assert all(f.turn_id == owner for f in frames if f.type in {
        "assistant_msg_start", "delta", "assistant_msg_end", "turn_end"})
    rows = materialize_history_turns([f.model_dump(exclude_none=True) for f in frames])
    assert len(rows) == 1 and rows[0]["done"]
    assert rows[0]["blocks"][-1]["text"] == "Compatibility test"
    assert p.context().total_tokens == 130
    assert p.context().max_tokens == 64000


def test_cold_history_and_live_stream_have_same_final_identity_and_text():
    _, live = native_flow()
    p = DshProjection()
    cold = []
    for native in json.loads(FIXTURE.read_text()):
        records = native["records"] if native["type"] == "snapshot" else [native] if native["type"] == "event" else []
        for record in records:
            cold += p.record(record["event"])
    summaries = [materialize_history_turns([e.model_dump(exclude_none=True) for e in events]) for events in (live, cold)]
    assert summaries[0][0]["id"] == summaries[1][0]["id"]
    assert summaries[0][0]["blocks"] == summaries[1][0]["blocks"]


def record(p, kind, data, **extra):
    return p.record({"type": kind, "data": data, "seq": p.cursor+1,
                     "time": (p.cursor+2)*1000, **extra})


def user(p, client):
    return record(p, "user/message", {"id": client, "role": "user",
                  "source": {"kind": "user", "rpcId": client},
                  "content": [{"type": "text", "text": client}]})


def test_late_answer_keeps_pre_steer_owner_even_when_new_user_has_arrived():
    p = DshProjection()
    record(p, "turn/start", {"turn": 1})
    record(p, "step/start", {"turn": 1, "step": 1})
    first = user(p, "first")[-1].turn_id
    p.frame({"type": "start", "attemptId": "attempt", "turn": 1, "step": 1, "revision": 1})
    second_frames = user(p, "steer")
    second = second_frames[-1].turn_id
    assert second != first
    assert not any(frame.type == "turn_end" for frame in second_frames)
    old = record(p, "assistant/message", {"turn": 1, "step": 1,
                 "message": {"content": [{"type": "text", "text": "old answer"}]}})
    assert all(f.turn_id == first for f in old)
    old_end = record(p, "step/end", {"turn": 1, "step": 1})[0]
    assert old_end.turn_id == first and old_end.result.subtype == "steered"
    new = record(p, "assistant/message", {"turn": 1, "step": 2,
                 "message": {"content": [{"type": "text", "text": "new answer"}]}})
    assert all(f.turn_id == second for f in new)
    end = record(p, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})[0]
    assert end.turn_id == second


def test_failed_attempt_clears_provisional_text_without_committing_it():
    p = DshProjection()
    user(p, "prompt")
    p.frame({"type": "start", "attemptId": "attempt", "turn": 1, "step": 1, "revision": 1})
    p.frame({"type": "chunk", "attemptId": "attempt", "revision": 2, "index": 0,
             "chunk": {"type": "text-delta", "index": 0, "text": "discard this"}})
    frames = record(p, "assistant/attempt", {"turn": 1, "step": 1, "stream": []})
    assert any(f.type == "delta" and f.replace and not f.text for f in frames)
    assert not any(f.type == "assistant_msg_end" for f in frames)
    assert frames[-1].type == "process"
    assert p.open  # an abandoned attempt is not a terminal turn


def test_compaction_replacement_cannot_reintroduce_old_human_or_question():
    p = DshProjection()
    user(p, "original")
    original_owner = p.owner
    result = record(p, "user/message", {"source": {"kind": "user"}, "id": "replaced",
                    "content": [{"type": "text", "text": "old context"}]},
                    surfaceOp={"op": "replace", "startSeq": 0, "endSeq": 1})
    assert result == [] and p.owner == original_owner
    assert record(p, "approval/asked", {"id": "answered", "toolName": "shell"}) == []
    assert record(p, "approval/decided", {"id": "answered", "outcome": "allowed-once"}) == []


def test_gaps_are_explicit_and_duplicate_durable_events_are_ignored():
    p = DshProjection()
    event = {"seq": 0, "time": 0, "type": "turn/start", "data": {"turn": 1}}
    p.record(event)
    assert p.record(event) == []
    with pytest.raises(DshError, match="缺口"):
        p.record({**event, "seq": 2})
    p.frame({"type": "start", "attemptId": "a", "turn": 1, "step": 1, "revision": 1})
    with pytest.raises(DshError, match="不连续"):
        p.frame({"type": "chunk", "attemptId": "a", "revision": 3, "index": 1, "chunk": {}})


def test_disjoint_usage_and_opaque_model_routes():
    assert input_tokens({"inputTokens": 100, "cacheReadTokens": 20, "cacheWriteTokens": 10, "totalTokens": 150}) == 130
    assert model_parts(model_id("provider:custom", "org/model:v1")) == ("provider:custom", "org/model:v1")
    assert DshProjection().context().available is False


@pytest.mark.parametrize("kind,error", [("completed", False), ("error", True), ("max-tokens", True), ("blocked", True), ("aborted", False)])
def test_native_terminal_reason_is_preserved(kind, error):
    p = DshProjection()
    user(p, "prompt")
    frames = record(p, "turn/end", {"turn": 1, "reason": {"kind": kind}})
    assert frames[-1].result.is_error is error
    assert frames[-1].result.subtype != "success" or kind == "completed"


def test_autonomous_goal_round_opens_its_own_owner_in_live_and_cold_views():
    p = DshProjection()
    frames = record(p, "turn/start", {"turn": 1}) + user(p, "human")
    frames += record(p, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    frames += record(p, "turn/start", {"turn": 2})
    assert p.running
    opening = p.frame({"type": "start", "attemptId": "auto", "turn": 2, "step": 1, "revision": 1})
    assert len(opening) == 1 and opening[0].autonomous
    assert opening[0].turn_id == "dsh-turn-2"
    frames += opening
    frames += record(p, "assistant/message", {"turn": 2, "step": 1,
        "message": {"content": [{"type": "text", "text": "continued"}]}})
    frames += record(p, "turn/end", {"turn": 2, "reason": {"kind": "completed"}})
    rows = materialize_history_turns(history_events(frames))
    assert len(rows) == 2 and rows[1]["id"] == "dsh-turn-2"
    assert rows[1]["blocks"][0]["text"] == "continued"
    assert not p.running and not p.open


def test_late_pre_steer_answer_stays_in_its_canonical_history_row():
    p = DshProjection()
    frames = record(p, "turn/start", {"turn": 1}) + user(p, "first")
    p.frame({"type": "start", "attemptId": "first", "turn": 1, "step": 1, "revision": 1})
    frames += user(p, "steer")
    for step, text in [(1, "old answer"), (2, "new answer")]:
        frames += record(p, "assistant/message", {"turn": 1, "step": step,
            "message": {"content": [{"type": "text", "text": text}]}})
    frames += record(p, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    rows = materialize_history_turns(history_events(frames))
    assert [r["id"] for r in rows] == ["first", "steer"]
    assert [[b["text"] for b in r["blocks"] if b["kind"] == "text"] for r in rows] == [["old answer"], ["new answer"]]
