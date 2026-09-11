"""Cold official summary pages must retain every source-proven user steer."""
import copy
import json

import pytest

from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_history import CodexOfficialHistory
from cc_remote.wrapper.codex_rpc import CodexRpcResponseTooLarge
from cc_remote.wrapper.history_store import history_image_from_events
from tests.test_codex_history import _PNG_1X1, _agent, _turn, _user
from tests.test_multisession import _mk_ctx, _mk_machine


def setup_history(monkeypatch, tmp_path, *, oversized=False, incomplete=False):
    source = tmp_path / "steered.jsonl"
    rows, native = [], []
    for index in range(8):
        tid = f"native-{index}"
        items = []
        def record(payload, kind="event_msg"):
            rows.append({"timestamp": f"2026-09-11T01:{index:02d}:00Z",
                         "type": kind, "payload": payload})
        record({"type": "task_started", "turn_id": tid})
        for segment in range({0: 4, 1: 2}.get(index, 1)):
            prompt = f"prompt {index}/{segment}"
            # Rollout IDs and official item IDs are deliberately unrelated.
            record({"type": "message", "role": "user", "id": f"raw-{index}-{segment}",
                    "content": [{"type": "input_text", "text": prompt}]}, "response_item")
            record({"type": "user_message", "message": prompt})
            items.extend([_user(f"item-{index}-{segment}", prompt),
                          _agent(f"answer-{index}-{segment}", f"reply {index}/{segment}")])
            if index == 0 and segment == 2:
                items[-2]["content"].append({"type": "image", "url": f"data:image/png;base64,{_PNG_1X1}"})
        record({"type": "task_complete", "turn_id": tid})
        native.append(_turn(tid, items, items_view="full"))
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(source))
    native.reverse()
    calls = []

    async def rpc(method, params, cwd=None):
        calls.append((method, copy.deepcopy(params)))
        assert method in {"thread/turns/list", "thread/items/list"}
        if method == "thread/items/list":
            row = next(t for t in native if t["id"] == params["turnId"])
            items = row["items"][:1] if incomplete else row["items"]
            start = int(params["cursor"] or 0)
            # Force multiple pages so steers must survive item pagination too.
            data = items[start:start + 3]
            return {"data": [{"turnId": row["id"], "item": i} for i in data],
                    "nextCursor": str(start + 3) if start + 3 < len(items) else None}
        start = int(params["cursor"] or 0)
        data = copy.deepcopy(native[start:start + params["limit"]])
        view = params["itemsView"]
        for row in data:
            if view == "full" and row["id"] in {"native-0", "native-1"} and oversized:
                raise CodexRpcResponseTooLarge("bounded response")
            row["itemsView"] = view
            if view == "summary":
                row["items"] = [row["items"][0], row["items"][-1]]
            if view == "notLoaded":
                row["items"] = []
        end = start + len(data)
        return {"data": data, "nextCursor": str(end) if end < len(native) else None}

    machine, _ = _mk_machine()
    machine._codex_history = CodexOfficialHistory(4000, rpc=rpc)
    ctx = _mk_ctx("steered", "steered")
    ctx.engine = "codex"
    machine.sessions[ctx.key] = ctx
    return machine, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("older", [False, True])
async def test_cold_summary_recovers_all_steers_and_exact_detail(monkeypatch, tmp_path, oversized, older):
    machine, calls = setup_history(monkeypatch, tmp_path, oversized=oversized)
    before = None
    if older:
        head = await machine._build_requested_history(
            "steered", before=None, limit=6, cwd="/tmp", detail="summary")
        assert head.error is None and head.has_more
        before = head.oldest_id
    old = await machine._build_requested_history(
        "steered", before=before, limit=2 if older else 8, cwd="/tmp", detail="summary")
    assert old.error is None and old.authoritative and not old.has_more
    assert [t.prompt for t in old.turns[:6]] == [
        "prompt 0/0", "prompt 0/1", "prompt 0/2", "prompt 0/3", "prompt 1/0", "prompt 1/1"]
    assert [t.id for t in old.turns[:6]] == [
        "item-0-0", "item-0-1", "item-0-2", "item-0-3", "item-1-0", "item-1-1"]
    for turn in old.turns:
        assert [b["text"] for b in turn.blocks if b["kind"] == "text"] == [turn.prompt.replace("prompt", "reply")]
    assert len(old.turns[2].imageRefs) == 1
    assert all(not turn.imageRefs for i, turn in enumerate(old.turns) if i != 2)
    detail = await machine._codex_history.turn_events("steered", "item-0-2")
    assert [e["prompt"] for e in detail if e["type"] == "user_msg"] == ["prompt 0/2"]
    assert next(e for e in detail if e["type"] == "user_msg")["images"] == [
        {"media_type": "image/png", "data": _PNG_1X1}]
    assert history_image_from_events(
        detail, "item-0-2", old.turns[2].imageRefs[0]["image_id"],
    )["data"] == _PNG_1X1
    assert not machine._codex_rollout_history_active("steered")
    # Cached metadata must still prove the missing prompts after the full LRU
    # is evicted. Neither refreshing nor a cold browser depends on that cache.
    machine._codex_history._native_full_turns.clear()
    again = await machine._build_requested_history(
        "steered", before=before, limit=2 if older else 8, cwd="/tmp", detail="summary")
    assert [t.id for t in again.turns] == [t.id for t in old.turns]
    full_native = [p for method, p in calls if method == "thread/turns/list" and p["itemsView"] == "full"]
    assert all(p["limit"] <= 2 for p in full_native)


@pytest.mark.asyncio
async def test_source_proven_steers_are_not_deleted_when_official_items_are_incomplete(monkeypatch, tmp_path):
    machine, _ = setup_history(monkeypatch, tmp_path, oversized=True, incomplete=True)
    head = await machine._build_requested_history(
        "steered", before=None, limit=6, cwd="/tmp", detail="summary")
    old = await machine._build_requested_history(
        "steered", before=head.oldest_id, limit=2, cwd="/tmp", detail="summary")
    assert old.error and not old.authoritative
    assert not machine._codex_rollout_history_active("steered")


@pytest.mark.asyncio
async def test_recovered_steer_keeps_exact_active_turn_running():
    first = [_user("initial", "first"), _agent("answer-first", "first reply")]
    full = [*first, _user("steer", "continue"), _agent("answer-next", "working")]
    responses = [
        _turn("active", first, status="interrupted"),
        _turn("active", first, status="interrupted", items_view="full"),
        _turn("active", full, status="interrupted", items_view="full"),
    ]

    async def rpc(method, _params, cwd=None):
        assert method == "thread/turns/list"
        return {"data": [responses.pop(0)], "nextCursor": None}

    reader = CodexOfficialHistory(4000, rpc=rpc)
    page = await reader.summary_page(
        "live", before=None, limit=1, active_turn_ids={"active"},
        minimum_user_segments={"active": 2},
    )
    assert [turn["prompt"] for turn in page.turns] == ["first", "continue"]
    assert page.turns[-1]["done"] is False
    assert [event["result"]["subtype"] for event in page.events
            if event.get("type") == "turn_end"] == ["steered"]
