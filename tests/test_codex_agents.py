"""Agent details never start a model, cross accounts, or show forked context."""
import json
from types import SimpleNamespace

import pytest

from cc_remote.wrapper.codex_agents import load_detail
from cc_remote.wrapper.codex_stream import CodexStreamTranslator, codex_translate_history
from tests.test_multisession import _mk_ctx, _mk_machine


def rollout(home, sid, parent=None, rows=(), archived=False):
    root = home / ("archived_sessions" if archived else "sessions")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-{sid}.jsonl"
    meta = {"id": sid, "agent_path": f"/root/{sid}"}
    if parent:
        meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    records = [("session_meta", meta), *rows]
    path.write_text("".join(json.dumps({"timestamp": "2026-09-18T00:00:00Z", "type": t, "payload": p}) + "\n"
                            for t, p in records))
    return path


def task(turn):
    return "event_msg", {"type": "task_started", "turn_id": turn}


def answer(text):
    return "event_msg", {"type": "agent_message", "message": text, "phase": "final_answer"}


def activity(child, call="activity"):
    return "event_msg", {"type": "item_completed", "turn_id": "parent-turn", "item": {
        "type": "SubAgentActivity", "id": call, "agent_thread_id": child,
        "agent_path": "/root/worker", "kind": "interacted"}}


def test_child_detail_excludes_inherited_context_and_prompt_and_supports_old_cards(tmp_path):
    rollout(tmp_path, "parent", rows=[task("parent-turn"), activity("child")])
    child = rollout(tmp_path, "child", "parent", rows=[
        task("parent-turn"), answer("INHERITED_PARENT"), task("child-turn"),
        ("event_msg", {"type": "user_message", "message": "PRIVATE_DELEGATION"}),
        ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "tool",
                           "arguments": '{"cmd":"pwd"}'}),
        ("response_item", {"type": "function_call_output", "call_id": "tool", "output": "exit code 0"}),
        answer("child answer"), ("event_msg", {"type": "task_complete", "turn_id": "child-turn"}),
    ], archived=True)
    rows, status, title, revision = load_detail(tmp_path, "parent", "activity", 8000)
    direct = load_detail(tmp_path, "parent", "codex-agent:child", 8000)
    assert direct[1:] == (status, title, revision)
    assert [{k: v for k, v in r.items() if k != "ts"} for r in direct[0]] == [
        {k: v for k, v in r.items() if k != "ts"} for r in rows]
    text = json.dumps(rows)
    assert "child answer" in text and "tool_use" in text and "tool_result" in text
    assert "INHERITED_PARENT" not in text and "PRIVATE_DELEGATION" not in text
    assert status == "succeeded" and title == "/root/child"
    child.write_text(child.read_text() + json.dumps({"type": "event_msg", "payload": {
        "type": "task_started", "turn_id": "next"}}) + "\n")
    newer = load_detail(tmp_path, "parent", "codex-agent:child", 8000)
    assert newer[1] == "running" and newer[3] != revision


def test_only_proven_descendants_in_same_account_can_be_read(tmp_path):
    home = tmp_path / "account"
    rollout(home, "parent")
    rollout(home, "child", "parent")
    rollout(home, "nested", "child")
    rollout(home, "unrelated")
    rollout(tmp_path / "other", "foreign", "parent")
    assert load_detail(home, "parent", "codex-agent:nested", 8000)[0] == []
    for target in ("parent", "unrelated", "foreign", "../../other", "missing"):
        with pytest.raises(ValueError):
            load_detail(home, "parent", f"codex-agent:{target}", 8000)
    (home / "sessions/rollout-foreign.jsonl").symlink_to(tmp_path / "other/sessions/rollout-foreign.jsonl")
    with pytest.raises(ValueError):
        load_detail(home, "parent", "codex-agent:foreign", 8000)


def test_live_and_replayed_cards_reference_same_child(tmp_path):
    native = {"method": "item/completed", "params": {"threadId": "parent", "turnId": "turn",
        "item": {"type": "subAgentActivity", "id": "activity", "kind": "interacted",
                 "agentThreadId": "child", "agentPath": "/root/worker"}}}
    live = CodexStreamTranslator(8000).feed(native)[0]
    path = rollout(tmp_path, "parent", rows=[task("turn"), activity("child")])
    replayed = next(e for e in codex_translate_history(str(path), 8000)[0]
                    if e.type == "process" and e.kind == "agent")
    assert live.input == replayed.input == {"agent_run_id": "codex-agent:child"}


@pytest.mark.parametrize(("sender", "recipient", "title"), [
    ("child", "parent", "向主代理汇报"),
    ("nested", "parent", "向主代理汇报"),
    ("nested", "child", "向上级代理汇报"),
])
def test_ancestor_reports_expand_the_exact_message_without_child_navigation(
        tmp_path, sender, recipient, title):
    rollout(tmp_path, "parent", rows=[task("parent-turn")])
    rollout(tmp_path, "child", "parent", rows=[task("child-turn")])
    rollout(tmp_path, "sibling", "parent", rows=[task("sibling-turn"), answer("sibling answer"),
        ("event_msg", {"type": "task_complete", "turn_id": "sibling-turn"})])
    rows = [
        task("parent-turn"),
        ("response_item", {"type": "function_call", "name": "send_message", "call_id": "missing",
                           "arguments": json.dumps({"message": "INHERITED_REPORT"})}),
        ("event_msg", {"type": "thread_settings_applied", "thread_id": sender}),
        task("own-turn"),
        ("response_item", {"type": "function_call", "name": "send_message", "call_id": "report",
                           "arguments": json.dumps({"target": "/root", "message": "已核对\n汇报正文"})}),
        activity(recipient, "report"),
        ("response_item", {"type": "function_call", "name": "send_message", "call_id": "other-report",
                           "arguments": json.dumps({"message": "另一条消息"})}),
        activity(recipient, "other-report"), activity(recipient, "missing"),
        activity("grandchild", "spawned"), activity("sibling", "sibling-call"),
    ]
    rollout(tmp_path, sender, "parent" if sender == "child" else "child", rows=rows)
    rollout(tmp_path, "grandchild", sender, rows=[task("grandchild-turn"), answer("nested answer"),
        ("event_msg", {"type": "task_complete", "turn_id": "grandchild-turn"})])
    events = load_detail(tmp_path, "parent", f"codex-agent:{sender}", 8000)[0]
    cards = {e["item_id"]: e for e in events if e["type"] == "process" and e["kind"] == "agent"}
    assert cards["report"]["title"] == title
    assert cards["report"]["detail"] == "已核对\n汇报正文"
    assert cards["report"]["input"] == {"agent_run_id": None}
    assert cards["report"]["status"] == "succeeded"
    assert cards["other-report"]["detail"] == "另一条消息"
    assert cards["missing"]["input"] == {"agent_run_id": None}
    assert cards["missing"]["title"] == ("主代理动态" if recipient == "parent" else "上级代理动态")
    assert "detail" not in cards["missing"]
    assert "INHERITED_REPORT" not in json.dumps(events)
    for call, target, text in [("spawned", "grandchild", "nested answer"),
                               ("sibling-call", "sibling", "sibling answer")]:
        run_id = cards[call]["input"]["agent_run_id"]
        assert run_id == f"codex-agent:{target}"
        assert text in json.dumps(load_detail(tmp_path, "parent", run_id, 8000)[0])


def test_native_send_input_to_ancestor_displays_its_prompt(tmp_path):
    rollout(tmp_path, "parent")
    rollout(tmp_path, "child", "parent", rows=[task("own"),
        ("event_msg", {"type": "item_completed", "item": {
            "type": "CollabAgentToolCall", "id": "native-report", "tool": "sendInput",
            "receiver_thread_ids": ["parent"], "sender_thread_id": "child",
            "prompt": "原生汇报正文", "status": "completed"}})])
    events = load_detail(tmp_path, "parent", "codex-agent:child", 8000)[0]
    card = next(e for e in events if e["type"] == "process" and e["kind"] == "agent")
    assert card["title"] == "向主代理汇报"
    assert card["detail"] == "原生汇报正文"
    assert card["input"] == {"agent_run_id": None}


def test_native_child_boundary_survives_parent_rollback_and_multi_agent_cards(tmp_path):
    # The current parent need not retain the context originally forked into a
    # child. A native child thread-settings boundary excludes that old prefix.
    rollout(tmp_path, "parent", rows=[("event_msg", {"type": "item_completed", "item": {
        "type": "CollabAgentToolCall", "id": "wait-both", "tool": "wait",
        "receiver_thread_ids": ["child", "other"], "status": "completed"}})])
    rollout(tmp_path, "child", "parent", rows=[task("inherited"), answer("old parent"),
        ("event_msg", {"type": "thread_settings_applied", "thread_id": "child"}),
        task("own"), answer("own answer"), ("event_msg", {"type": "task_complete", "turn_id": "own"})])
    rollout(tmp_path, "other", "parent")
    events = load_detail(tmp_path, "parent", "codex-agent:child", 8000)[0]
    assert "own answer" in json.dumps(events) and "old parent" not in json.dumps(events)
    group = load_detail(tmp_path, "parent", "wait-both", 8000)[0]
    assert [e["item_id"] for e in group] == ["codex-agent:child", "codex-agent:other"]
    parent_path = tmp_path / "sessions/rollout-parent.jsonl"
    cards = [e for e in codex_translate_history(str(parent_path), 8000)[0]
             if e.type == "process" and e.kind == "agent"]
    assert cards[0].input["receivers"] == ["child", "other"]


@pytest.mark.asyncio
async def test_machine_pages_and_invalidates_details_without_engine_access(tmp_path, monkeypatch):
    machine, _ = _mk_machine()
    monkeypatch.setattr(machine, "_codex_target", lambda sid: (SimpleNamespace(home=tmp_path, id="primary"), sid))
    ctx = _mk_ctx("parent", "parent")
    ctx.engine = "codex"
    machine.sessions[ctx.key] = ctx
    rollout(tmp_path, "parent")
    child = rollout(tmp_path, "child", "parent", rows=[task("t"), answer("answer"),
                    ("response_item", {"type": "function_call", "name": "send_message", "call_id": "report",
                                       "arguments": '{"target":"/root","message":"paged report"}'}),
                    activity("parent", "report"),
                    ("event_msg", {"type": "token_count"})])
    cmd = SimpleNamespace(session_id="parent", run_id="codex-agent:child", request_id="r",
                          client_id="browser", revision=None, detail_revision=None, before=None, limit=1)
    detail = await machine._handle_get_agent_detail(cmd)
    assert detail.authoritative and detail.has_more
    assert detail.to == "browser"
    assert len(detail.events) == 1 and detail.events[0]["type"] == "process"
    assert detail.events[0]["detail"] == "paged report"
    assert detail.events[0]["input"] == {"agent_run_id": None}
    cmd.before, cmd.detail_revision = detail.oldest_cursor, detail.detail_revision
    page = await machine._handle_get_agent_detail(cmd)
    assert page.authoritative and page.events != detail.events
    assert len(page.events) == 1 and page.events[0]["type"] == "tool_use"
    child.write_text(child.read_text() + json.dumps({"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "t"}}) + "\n")
    stale = await machine._handle_get_agent_detail(cmd)
    assert not stale.authoritative and "已更新" in stale.error
