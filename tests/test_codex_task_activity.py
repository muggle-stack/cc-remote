"""Async MCP activity and continuation rendering never drive model lifecycle."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cc_remote.task_store import TaskStore, public_task
from cc_remote.wrapper.codex_tasks import read_task_activity
from cc_remote.wrapper.history_store import materialize_history_turns
from tests.test_multisession import _mk_ctx, _mk_machine


def task(home, sid, title="Background verification"):
    home.mkdir(exist_ok=True)
    store = TaskStore(home)
    key = store.create(sid, argv=["echo", "PRIVATE_ARGUMENT"], cwd=str(home),
                       title=title, request_id=str(uuid4()))
    return store, key


def test_absent_task_state_is_empty_without_writes(tmp_path):
    assert read_task_activity(tmp_path) == {}
    assert list(tmp_path.iterdir()) == []


def test_activity_is_account_scoped_and_independent_of_parent_completion(tmp_path):
    sid, other_sid = str(uuid4()), str(uuid4())
    store, key = task(tmp_path / "first", sid)
    other, other_key = task(tmp_path / "second", sid, "Other account")
    task(store.home, other_sid, "Other session")
    initial = read_task_activity(store.home)
    assert initial[sid][0].status == "pending"
    store.update(key, state="running")
    assert read_task_activity(store.home)[sid][0].status == "running"
    store.update(key, state="completed", output=b"PRIVATE_OUTPUT")
    waiting = read_task_activity(store.home)[sid][0]
    assert waiting.status == "pending" and "等待通知" in waiting.summary
    assert "PRIVATE" not in waiting.model_dump_json()
    store.update(key, delivery="delivered")
    assert sid not in read_task_activity(store.home)
    assert read_task_activity(other.home)[sid][0].title == "Other account"
    other.cancel(other_key, sid)
    assert read_task_activity(other.home) == {}


def test_unreadable_task_state_is_not_an_empty_snapshot(tmp_path):
    store, _ = task(tmp_path, str(uuid4()))
    store.path.write_bytes(b"corrupt")
    with pytest.raises(Exception):
        read_task_activity(tmp_path)
    assert store.path.read_bytes() == b"corrupt"


def test_task_activity_rejects_symlink_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    (home / "cc-remote-async-tasks").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        read_task_activity(home)


@pytest.mark.asyncio
async def test_wrapper_sync_routes_activity_and_never_reopens_idle(tmp_path, monkeypatch):
    sid, other_sid = str(uuid4()), str(uuid4())
    store, key = task(tmp_path, sid)
    machine, transport = _mk_machine()
    current, other = _mk_ctx(sid, sid), _mk_ctx(other_sid, other_sid)
    for ctx in (current, other):
        ctx.engine = "codex"
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    frames = [frame for frame in transport.sent if frame.type == "background_process_sync"]
    assert len(frames) == 1
    assert next(frame for frame in frames if frame.sid == sid).items[0].title == "Background verification"
    assert other.codex_background_processes == {}, "a first empty read needs no live broadcast"
    assert all(frame.type == "background_process_sync" for frame in transport.sent)
    assert current.state == other.state == "idle"
    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == [], "unchanged polls do not repaint or grow replay"

    from cc_remote.wrapper import codex_tasks
    original = codex_tasks.read_task_activity
    monkeypatch.setattr(codex_tasks, "read_task_activity", lambda _: (_ for _ in ()).throw(OSError()))
    await machine._refresh_codex_background_processes()
    assert transport.sent == [] and current.codex_background_processes
    monkeypatch.setattr(codex_tasks, "read_task_activity", original)
    store.update(key, state="completed", delivery="delivered")
    await machine._refresh_codex_background_processes()
    assert len(transport.sent) == 1 and transport.sent[0].items == []
    assert current.state == "idle"


@pytest.mark.asyncio
async def test_activity_read_cannot_apply_to_replaced_session(tmp_path, monkeypatch):
    sid = str(uuid4())
    task(tmp_path, sid)
    machine, transport = _mk_machine()
    ctx = _mk_ctx(sid, sid)
    ctx.engine = "codex"
    machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))

    async def raced_read(fn, *args):
        value = fn(*args)
        ctx.session_id = str(uuid4())
        return value

    monkeypatch.setattr(asyncio, "to_thread", raced_read)
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes is None and transport.sent == []


@pytest.mark.asyncio
async def test_hello_reseeds_current_activity_even_after_replay_tail(tmp_path, monkeypatch):
    from cc_remote.protocol import Hello
    sid = str(uuid4())
    store, key = task(tmp_path, sid)
    machine, transport = _mk_machine()
    ctx = _mk_ctx(sid, sid)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    for delivered in (False, True):
        if delivered:
            store.update(key, state="completed", delivery="delivered")
        transport.sent.clear()
        await machine._handle_client_hello(Hello(
            role="client", client_id="fresh-browser", route_id="fresh-route",
            cursors={sid: ctx.buffer.tail_seq}, generations={sid: machine.instance_id}))
        seeds = [frame for frame in transport.sent if frame.type == "background_process_sync"
                 and frame.to == "fresh-browser"]
        assert len(seeds) == 1
        assert len(seeds[0].items) == (0 if delivered else 1)
        assert seeds[0].seq is None and seeds[0].route_id == "fresh-route"
        assert ctx.state == "idle"


@pytest.mark.parametrize("live_detail", [False, True])
def test_native_receipt_survives_summary_in_source_order(tmp_path, live_detail):
    from cc_remote.wrapper.codex_history import _translate_turn
    sid = str(uuid4())
    store, key = task(tmp_path, sid)
    store.update(key, state="completed", output=b"PRIVATE_OUTPUT", delivery="delivered")
    receipt = {"id": "callback", "type": "functionCallOutput", "namespace": "cc_remote_tasks",
               "name": "task_result", "output": json.dumps(public_task(store.get(key), output=True))}
    turn = {"id": "native-turn", "items": [
        {"type": "agentMessage", "id": "before", "phase": "final", "text": "Before callback"},
        receipt,
        {"type": "agentMessage", "id": "after", "phase": "final", "text": "After callback"},
    ], "status": "completed", "itemsView": "full", "startedAt": 100,
        "completedAt": 102, "durationMs": 2000, "error": None}
    events = [event for segment in _translate_turn(sid, turn, tool_result_max=8000) for event in segment]
    projected = materialize_history_turns(events, include_live_detail=live_detail)
    blocks = [block for turn in projected for block in turn["blocks"]]
    assert [block.get("message_id") or block.get("item_id") for block in blocks] == [
        "before", "callback", "after"]
    assert blocks[1]["server"] == "cc_remote_tasks" and blocks[1]["tool"] == "task_result"
    assert "PRIVATE_OUTPUT" not in json.dumps(projected)


def test_actual_rollout_tool_output_keeps_native_receipt_identity(tmp_path):
    from cc_remote.wrapper.codex_stream import codex_translate_history
    sid = str(uuid4())
    store, key = task(tmp_path, sid)
    store.update(key, state="completed", output=b"done", delivery="delivered")
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "native-turn"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "id": "callback",
            "namespace": "cc_remote_tasks", "name": "task_result",
            "output": json.dumps(public_task(store.get(key), output=True))}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Received", "phase": "final"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "native-turn"}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps({"timestamp": "2026-10-10T04:39:45Z", **row}) for row in rows))
    events, _ = codex_translate_history(str(path), 8000)
    events = [event.model_dump(mode="json") for event in events]
    receipts = [event for event in events if event.get("tool") == "task_result"]
    assert len(receipts) == 1 and receipts[0]["item_id"] == "callback"
    assert receipts[0]["turn_id"] == "native-turn"
    blocks = [block for turn in materialize_history_turns(events) for block in turn["blocks"]]
    assert any(block.get("tool") == "task_result" for block in blocks)


def test_official_summary_recovers_receipts_between_exact_native_answers(tmp_path):
    from cc_remote.wrapper.codex_history import CodexHistoryPage
    from cc_remote.wrapper.codex_stream import codex_history_native_witness
    from cc_remote.wrapper.machine import _apply_codex_process_witness
    sid = str(uuid4())
    store, key = task(tmp_path, sid)
    store.update(key, state="completed", output=b"PRIVATE_OUTPUT", delivery="delivered")
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "native-turn"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Run check"}},
        {"type": "response_item", "payload": {"type": "message", "id": "before", "role": "assistant",
            "phase": "final_answer", "content": [{"type": "output_text", "text": "Before"}]}},
        {"type": "response_item", "payload": {"type": "function_call_output", "id": "callback",
            "namespace": "cc_remote_tasks", "name": "task_result",
            "output": json.dumps(public_task(store.get(key), output=True))}},
        {"type": "response_item", "payload": {"type": "message", "id": "after", "role": "assistant",
            "phase": "final_answer", "content": [{"type": "output_text", "text": "After"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "native-turn"}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join(json.dumps({"timestamp": "2026-10-10T04:39:45Z", **row}) for row in rows))
    witness = codex_history_native_witness(str(path), max_turns=4)
    blocks = [{"kind": "text", "message_id": key, "text": key, "channel": "final"}
              for key in ("before", "after")]
    page = CodexHistoryPage(
        events=(), turns=({"id": "visible", "blocks": blocks, "done": True},), has_more=False,
        oldest_id="visible", newest_id="visible", native_turn_ids=("native-turn",),
        native_segment_by_visible_id={"visible": ("native-turn", 0)})
    for _ in range(2):
        _apply_codex_process_witness(page, witness)
    assert [block.get("message_id") or block.get("item_id") for block in blocks] == [
        "before", "callback", "after"]
    assert "PRIVATE_OUTPUT" not in repr(witness) + json.dumps(page.turns)
    assert page.turns[0]["done"] is True
    assert blocks[1]["server"] == "cc_remote_tasks"


def test_appended_receipt_rebuilds_its_exact_answer_anchor(tmp_path):
    from cc_remote.wrapper.codex_stream import (
        codex_history_process_append, codex_history_process_witnesses,
    )
    path = tmp_path / "append.jsonl"
    path.write_text(
        '{"type":"event_msg","payload":{"type":"task_started","turn_id":"native"}}\n'
        '{"type":"event_msg","payload":{"type":"user_message","message":"test"}}\n')
    previous = codex_history_process_witnesses(
        str(path), before="item", max_turns=1, native_turn_ids=("native",))
    receipt = {"type": "response_item", "payload": {
        "type": "function_call_output", "id": "receipt", "namespace": "cc_remote_tasks",
        "name": "task_result", "output": json.dumps({"task_id": str(uuid4()), "state": "completed"})}}
    answer = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "id": "answer", "phase": "final_answer", "content": [{"type": "output_text", "text": "Done"}]}}
    for row in (receipt, answer):
        start = path.stat().st_size
        with path.open("a") as output:
            output.write(json.dumps(row) + "\n")
        assert codex_history_process_append(str(path), previous=previous, start_offset=start,
                                            end_offset=path.stat().st_size) is None
        previous = codex_history_process_witnesses(
            str(path), before="item", max_turns=1, native_turn_ids=("native",))
    assert previous.process_by_native_segment[("native", 0)].task_receipts[0][1] == "answer"
