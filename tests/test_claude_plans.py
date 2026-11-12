"""Root plan approval, native task progress and cross-client receipts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ToolUseBlock, ToolResultBlock, UserMessage

from cc_remote.config import RelayConfig, WrapperConfig
from cc_remote.protocol import (
    ClaudePlanState, DismissClaudePlan, GetClaudePlan, History, Perm, RollbackResult, ToolResult, ToolUse,
    TurnEnd, TurnResult, UserMsg, deserialize, serialize,
)
from cc_remote.wrapper.claude_plans import ClaudePlanStore
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.relay.pairing import RelayHub
from cc_remote.wrapper.stream import StreamTranslator, translate_history
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_rich_stream import _assistant


def use(name, key, **values):
    return ToolUse(message_id="message", tool_use_id=key, turn_id="turn", tool=name, input=values)


def result(key, error=False, task_id=None):
    event = ToolResult(tool_use_id=key, content="ok", is_error=error)
    event._claude_task_id = task_id
    return event


def approve(store, sid="session"):
    for event in [use("EnterPlanMode", "enter"), result("enter"),
                  use("ExitPlanMode", "exit", plan="# Plan\n\n1. Implement"), result("exit")]:
        store.observe(sid, event)
    return store.get(sid)


@pytest.fixture
def native_plan_source(tmp_path, monkeypatch):
    import cc_remote.wrapper.machine as machine_module

    def install(machine, events):
        path = tmp_path / "transcript.jsonl"
        messages = [SimpleNamespace(uuid="user", type="user", message={"role": "user", "content": "plan"})]
        for index, event in enumerate(events):
            if isinstance(event, ToolUse):
                kind = "assistant"
                content = {"type": "tool_use", "id": event.tool_use_id, "name": event.tool, "input": event.input}
            else:
                kind = "user"
                content = {"type": "tool_result", "tool_use_id": event.tool_use_id,
                           "content": event.content, "is_error": event.is_error}
            messages.append(SimpleNamespace(uuid=f"message-{index}", type=kind,
                                            message={"role": kind, "content": [content]}))
        path.write_text("".join(json.dumps(vars(message)) + "\n" for message in messages))
        monkeypatch.setattr(machine, "_claude_transcript_path", lambda *_: str(path))
        monkeypatch.setattr(machine_module, "transcript_compact_snapshot", lambda *a, **kw: (messages, {}, {}))
        monkeypatch.setattr(machine_module, "recover_claude_native_metadata", lambda sid, rows, **kw: rows)
        return path

    return install


def test_approval_is_not_completion_and_next_turn_does_not_retire(tmp_path):
    store = ClaudePlanStore(tmp_path)
    store.observe("session", use("EnterPlanMode", "enter"))
    store.observe("session", use("ExitPlanMode", "exit", plan="# Plan"))
    assert store.get("session").phase == "pending"
    store.observe("session", Perm(mode="bypassPermissions"))
    assert store.get("session").phase == "pending"
    store.observe("session", result("exit"))
    approved = store.get("session")
    assert approved.phase == "approved" and not approved.steps
    store.observe("session", TurnEnd(result=TurnResult(subtype="success", is_error=False, duration_ms=0)))
    store.observe("session", UserMsg(msg_id="next-turn", prompt="continue"))
    assert store.get("session") == approved
    assert ClaudePlanStore(tmp_path).get("session") == approved


def test_manual_mode_change_and_rejection_are_not_approval(tmp_path):
    store = ClaudePlanStore(tmp_path)
    store.observe("s", use("EnterPlanMode", "one"))
    store.observe("s", Perm(mode="bypassPermissions"))
    assert store.get("s").phase == "ready"
    store.observe("s", use("ExitPlanMode", "exit", plan="# Plan"))
    store.observe("s", result("exit", error=True))
    assert store.get("s").phase == "changes_requested"
    store.observe("s", Perm(mode="plan"))
    assert store.get("s").phase == "changes_requested"


@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize("dismissed", [False, True])
def test_reenter_plan_waits_for_success_and_preserves_failed_entry(tmp_path, error, dismissed):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Retained plan"), result("exit"),
               use("TodoWrite", "tasks", todos=[{
                   "content": "Verify", "status": "completed" if dismissed else "pending",
               }]), result("tasks")]
    old = store.recover("s", history)
    if dismissed:
        old = store.dismiss("s", old.id, old.revision)
    entry = use("EnterPlanMode", "retry").model_copy(update={"turn_id": "next-turn"})
    changed, pending = store.observe("s", entry)
    assert not changed and pending == old
    history.append(entry)
    store = ClaudePlanStore(tmp_path)
    assert store.recover("s", history, refresh=True) == old
    settled = result("retry", error=error)
    changed, plan = store.observe("s", settled)
    history.append(settled)
    assert changed is not error
    if error:
        assert plan == old
    else:
        assert plan.id == "plan:retry" and plan.turn_id == "next-turn"
        assert plan.phase == "draft" and not plan.document and not plan.steps
        assert not plan.dismissed and plan.revision > old.revision
    assert store.recover("s", history, refresh=True) == plan
    assert ClaudePlanStore(tmp_path).recover("s", history, refresh=True) == plan
    cold = ClaudePlanStore(tmp_path / "cold").recover("s", history)
    assert cold.model_dump(exclude={"revision", "dismissed"}) == plan.model_dump(exclude={"revision", "dismissed"})
    # Replayed calls/results never reset a settled projection.
    assert store.observe("s", entry) == (False, plan)
    assert store.observe("s", settled) == (False, plan)
    if error and not dismissed:
        store.observe("s", use("TaskUpdate", "finish", taskId="0", status="completed"))
        _, completed = store.observe("s", result("finish"))
        assert completed.steps == [{"step": "Verify", "status": "completed"}]


@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("dismissed", [False, True])
def test_failed_reentry_preserves_manual_exit_during_recovery(tmp_path, history_first, dismissed):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Retained plan"), result("exit", error=True)]
    store.recover("s", history)
    _, old = store.observe("s", Perm(mode="bypassPermissions"))
    if dismissed:
        old = store.dismiss("s", old.id, old.revision)
    for event in [use("EnterPlanMode", "retry"), result("retry", error=True)]:
        history.append(event)
        if history_first:
            assert store.recover("s", history, refresh=True) == old
        assert store.observe("s", event) == (False, old)
        assert store.recover("s", history, refresh=True) == old
        store = ClaudePlanStore(tmp_path)
        assert store.recover("s", history, refresh=True) == old


@pytest.mark.parametrize("error", [False, True])
def test_late_entry_result_cannot_replace_subsequent_submission(tmp_path, error):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Retained plan"), result("exit"),
               use("EnterPlanMode", "retry"),
               use("ExitPlanMode", "submit", plan="# Updated plan"), result("submit")]
    expected = store.recover("s", history)
    settled = result("retry", error=error)
    assert store.observe("s", settled) == (False, expected)
    assert store.recover("s", history + [settled], refresh=True) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("error", [False, True])
async def test_reenter_plan_publishes_only_confirmed_replacement(
        tmp_path, native_plan_source, history_first, error):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Retained plan"), result("exit"),
               use("TodoWrite", "tasks", todos=[{"content": "Verify", "status": "pending"}]),
               result("tasks")]
    native_plan_source(machine, history)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="pwa"))
    old = store.get(ctx.key)
    transport.sent.clear()
    for event in [use("EnterPlanMode", "retry"), result("retry", error=error)]:
        history.append(event)
        if history_first:
            native_plan_source(machine, history)
        await machine._emit(ctx, event)
        if isinstance(event, ToolUse) or error:
            assert store.get(ctx.key) == old
            assert not any(isinstance(e, ClaudePlanState) for e in transport.sent)
    updates = [e for e in transport.sent if isinstance(e, ClaudePlanState)]
    if not error:
        assert len(updates) == 1 and updates[0].to is None
        assert updates[0].plan.id == "plan:retry" and updates[0].plan.phase == "draft"
        assert not updates[0].plan.document and not updates[0].plan.steps
    expected = store.get(ctx.key)
    machine._claude_plans = ClaudePlanStore(tmp_path)
    native_plan_source(machine, history)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    assert transport.sent[-1].plan == expected and transport.sent[-1].to == "desktop"
    assert ctx.sdk.client is None


def test_checklist_only_changes_after_success_and_empty_is_not_completion(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    store.observe("session", use("TaskCreate", "create", subject="Run tests"))
    store.observe("session", result("create", task_id="1"))
    store.observe("session", use("TaskUpdate", "update", taskId="1", status="completed"))
    assert store.get("session").steps[0]["status"] == "pending"
    store.observe("session", result("update", error=True))
    assert store.get("session").steps[0]["status"] == "pending"
    plan = store.get("session")
    assert not store.dismiss("session", plan.id, plan.revision).dismissed
    store.observe("session", use("TaskUpdate", "done", taskId="1", status="completed"))
    store.observe("session", result("done"))
    plan = store.get("session")
    assert store.dismiss("session", plan.id, plan.revision).dismissed
    # Explicit native deletion removes only the checklist, preserving the document.
    store.observe("session", use("TaskUpdate", "delete", taskId="1", status="deleted"))
    store.observe("session", result("delete"))
    plan = store.get("session")
    assert not plan.steps and plan.document.startswith("# Plan") and not plan.dismissed
    store.observe("session", use("TodoWrite", "todos", todos=[{"content": "One", "status": "in_progress"}]))
    store.observe("session", result("todos"))
    assert store.get("session").steps[0]["status"] == "inProgress"
    store.observe("session", use("TodoWrite", "clear", todos=[]))
    store.observe("session", result("clear"))
    assert store.get("session").steps == []


@pytest.mark.parametrize("tool", ["TodoWrite", "TaskCreate"])
def test_manual_plan_submission_retains_earlier_checklist_after_restart(tmp_path, tool):
    store = ClaudePlanStore(tmp_path)
    # Manual selection has no EnterPlanMode call, and Perm is not present in
    # native transcript replay. Task results must survive either path.
    store.observe("s", Perm(mode="plan"))
    task = (use(tool, "task", todos=[{"content": "Verify", "status": "pending"}])
            if tool == "TodoWrite" else use(tool, "task", subject="Verify"))
    identity = "0" if tool == "TodoWrite" else "1"
    history = [task, result("task", task_id="1"),
               use("TaskUpdate", "working", taskId=identity, status="in_progress"), result("working")]
    for event in history:
        assert store.observe("s", event) == (False, None)
    store = ClaudePlanStore(tmp_path)
    assert store.recover("s", history, refresh=True) is None
    submission = [use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    for event in submission:
        store.observe("s", event)
    history += submission
    approved = store.get("s")
    assert approved.phase == "approved"
    assert approved.steps == [{"step": "Verify", "status": "inProgress"}]
    assert store.dismiss("s", approved.id, approved.revision) == approved
    assert store.recover("s", history, refresh=True) == approved
    cold = ClaudePlanStore(tmp_path / "cold").recover("s", history)
    assert cold.model_dump(exclude={"revision"}) == approved.model_dump(exclude={"revision"})

    store.observe("s", use("TaskUpdate", "done", taskId=identity, status="completed"))
    store.observe("s", result("done"))
    complete = store.get("s")
    assert complete.steps == [{"step": "Verify", "status": "completed"}]
    assert store.dismiss("s", complete.id, complete.revision).dismissed


@pytest.mark.parametrize("error", [False, True])
def test_checklist_result_after_first_submission_keeps_its_owner(tmp_path, error):
    store = ClaudePlanStore(tmp_path)
    history = [use("TaskCreate", "task", subject="Verify"),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    plan = store.recover("s", history)
    # The outstanding task still belongs to this first implicit plan.
    assert store.dismiss("s", plan.id, plan.revision) == plan
    settled = result("task", error=error, task_id="1")
    _, plan = store.observe("s", settled)
    assert plan.steps == ([] if error else [{"step": "Verify", "status": "pending"}])
    assert store.recover("s", history + [settled], refresh=True) == plan


def test_first_explicit_entry_does_not_inherit_unbound_or_delayed_tasks(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("TaskCreate", "old", subject="Old work"), result("old", task_id="1"),
               use("TaskCreate", "late", subject="Late old work"),
               use("EnterPlanMode", "enter"), result("enter"), result("late", task_id="2"),
               use("ExitPlanMode", "exit", plan="# New plan"), result("exit")]
    plan = store.recover("s", history)
    assert plan.document == "# New plan" and plan.steps == []


def test_unbound_checklist_ignores_child_and_failed_results(tmp_path):
    store = ClaudePlanStore(tmp_path)
    child = use("TodoWrite", "child", todos=[{"content": "Child", "status": "pending"}])
    child.parent_id = "agent"
    history = [child, result("child"), use("TaskCreate", "failed", subject="Failed"),
               result("failed", error=True, task_id="1"),
               use("TaskCreate", "root", subject="Root"), result("root", task_id="2"),
               use("TaskUpdate", "failed-update", taskId="2", status="completed"),
               result("failed-update", error=True),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    plan = store.recover("s", history)
    assert plan.steps == [{"step": "Root", "status": "pending"}]


@pytest.mark.parametrize("clear", ["TodoWrite", "TaskUpdate"])
def test_cleared_unbound_tasks_do_not_reappear_on_submission_or_rollback(tmp_path, clear):
    store = ClaudePlanStore(tmp_path)
    cleared = (use(clear, "clear", todos=[]) if clear == "TodoWrite"
               else use(clear, "clear", taskId="1", status="deleted"))
    history = [use("TaskCreate", "task", subject="Removed"), result("task", task_id="1"),
               cleared, result("clear")]
    submission = [use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    plan = store.recover("s", history + submission)
    assert plan.steps == []
    assert store.recover("s", history, rollback=True) is None
    for event in submission:
        store.observe("s", event)
    restored = store.get("s")
    assert restored.steps == [] and restored.revision > plan.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("resident", [False, True])
async def test_manual_plan_checklist_survives_targeted_reads_and_native_replay(
        tmp_path, native_plan_source, history_first, resident):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    history = []
    native_plan_source(machine, history)
    await machine._emit(ctx, Perm(mode="plan"))
    events = [use("TodoWrite", "task", todos=[{"content": "Verify", "status": "pending"}]),
              result("task"), use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    for event in events:
        history.append(event)
        if history_first:
            native_plan_source(machine, history)
            await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="pwa"))
        await machine._emit(ctx, event)
        if len(history) <= 2:
            assert store.get(ctx.key) is None
        else:
            assert store.get(ctx.key).steps == [{"step": "Verify", "status": "pending"}]
    approved = store.get(ctx.key)
    broadcasts = [e for e in transport.sent if isinstance(e, ClaudePlanState) and e.to is None]
    assert broadcasts[-1].plan == approved
    if not resident:
        machine.sessions.pop(ctx.key)
    machine._claude_plans = ClaudePlanStore(tmp_path)
    native_plan_source(machine, history)
    for client in ("desktop", "pwa"):
        await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id=client))
        assert transport.sent[-1].plan == approved
    await machine._handle_dismiss_claude_plan(DismissClaudePlan(
        sid=ctx.key, plan_id=approved.id, revision=approved.revision, client_id="pwa"))
    assert transport.sent[-1].plan == approved and not transport.sent[-1].plan.dismissed
    assert ctx.sdk.client is None


@pytest.mark.asyncio
async def test_legacy_plan_cache_recovers_missing_tasks_from_unchanged_source(tmp_path, native_plan_source):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    history = [use("TodoWrite", "task", todos=[{"content": "Verify", "status": "pending"}]),
               result("task"), use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    native_plan_source(machine, history)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="pwa"))
    with store._db() as db:
        data = store._load(db, ctx.key)
        source = data["source"]
        data.pop("projection_version")
        data["tasks"] = {}
        data["plan"]["steps"] = []
        data["plan"]["dismissed"] = True
        store._save(db, ctx.key, data)
    legacy = store.get(ctx.key)
    machine._claude_plans = ClaudePlanStore(tmp_path)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    restored = transport.sent[-1].plan
    assert restored.id == legacy.id and restored.revision > legacy.revision
    assert not restored.dismissed and restored.steps == [{"step": "Verify", "status": "pending"}]
    assert machine._claude_plans.source(ctx.key) == source
    assert ctx.sdk.client is None


def test_dismissal_is_durable_exact_revision_and_replay_safe(tmp_path):
    store = ClaudePlanStore(tmp_path)
    old = approve(store)
    store.observe("session", use("TodoWrite", "update", todos=[{"content": "New work", "status": "pending"}]))
    store.observe("session", result("update"))
    assert not store.dismiss("session", old.id, old.revision).dismissed
    store.observe("session", use("TodoWrite", "clear", todos=[]))
    store.observe("session", result("clear"))
    latest = store.get("session")
    dismissed = store.dismiss("session", latest.id, latest.revision)
    restarted = ClaudePlanStore(tmp_path)
    assert restarted.recover("session", [use("EnterPlanMode", "old")]) == dismissed
    restarted.observe("session", use("ExitPlanMode", "exit", plan="old replay"))
    restarted.observe("session", result("exit"))
    assert restarted.get("session") == dismissed
    restarted.observe("session", use("EnterPlanMode", "new"))
    assert restarted.get("session") == dismissed
    restarted.observe("session", result("new"))
    assert not restarted.get("session").dismissed
    assert restarted.get("session").id != old.id


def test_child_plan_cannot_replace_root_or_complete_its_steps(tmp_path):
    store = ClaudePlanStore(tmp_path)
    root = approve(store)
    child = use("EnterPlanMode", "child")
    child.parent_id = "agent"
    store.observe("session", child)
    store.observe("session", result("child"))
    assert store.get("session") == root


def test_root_background_followup_can_update_tasks_without_child_contamination(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    update = use("TodoWrite", "followup", todos=[{"content": "Verify", "status": "completed"}])
    update.background = True
    store.observe("session", update)
    store.observe("session", result("followup"))
    assert store.get("session").steps == [{"step": "Verify", "status": "completed"}]


def test_long_checklist_does_not_report_completion_for_only_visible_tasks(tmp_path):
    store = ClaudePlanStore(tmp_path)
    approve(store)
    tasks = [{"content": str(i), "status": "completed"} for i in range(128)]
    tasks.append({"content": "Still working", "status": "in_progress"})
    store.observe("session", use("TodoWrite", "many", todos=tasks))
    store.observe("session", result("many"))
    plan = store.get("session")
    assert len(plan.steps) == 128 and plan.steps[-1]["status"] == "pending"
    assert not store.dismiss("session", plan.id, plan.revision).dismissed


def test_refresh_reconciles_native_append_without_reverting_live_or_dismissed_state(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan")]
    pending = store.recover("s", history, source=1)
    assert pending.phase == "pending"
    history.append(result("exit"))
    approved = store.recover("s", history, source=2, refresh=True)
    assert approved.phase == "approved" and approved.revision > pending.revision
    assert store.recover("s", history[:-1], source=3, refresh=True) == approved
    dismissed = store.dismiss("s", approved.id, approved.revision)
    assert dismissed.dismissed
    assert store.recover("s", history, source=4, refresh=True) == dismissed
    history.extend([use("TaskCreate", "task", subject="More work"), result("task", task_id="1")])
    reopened = store.recover("s", history, source=5, refresh=True)
    assert not reopened.dismissed and reopened.revision > dismissed.revision
    assert reopened.steps[0]["status"] == "pending"
    assert store.recover("s", [], source=6, refresh=True) == reopened
    history.extend([use("EnterPlanMode", "new"), result("new")])
    new = store.recover("s", history, source=7, refresh=True)
    assert new.id != reopened.id and new.phase == "draft"


def test_native_plan_denial_is_not_reopened_by_lagging_source(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("ExitPlanMode", "exit", plan="# Plan")]
    store.recover("s", history)
    changed, rejected = store.reject("s", "exit")
    assert changed and rejected.phase == "changes_requested"
    assert store.recover("s", history, source=2, refresh=True) == rejected
    history.append(result("exit", error=True))
    assert store.recover("s", history, source=3, refresh=True) == rejected


@pytest.mark.asyncio
@pytest.mark.parametrize("dismissed", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_manual_exit_after_rejection_survives_reads_and_restart(tmp_path, native_plan_source, dismissed, legacy):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    events = [use("EnterPlanMode", "enter"), result("enter"),
              use("ExitPlanMode", "exit", plan="# Plan"), result("exit", error=True)]
    native_plan_source(machine, events)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    assert store.get(ctx.key).phase == "changes_requested"
    await machine._emit(ctx, Perm(mode="bypassPermissions"))
    ready = store.get(ctx.key)
    assert ready.phase == "ready"
    if legacy:
        # Existing projections predate explicit mode-call tracking.
        with store._db() as db:
            data = store._load(db, ctx.key)
            data.pop("mode_call", None)
            store._save(db, ctx.key, data)

    # Unrelated native appends force real recovery, including before dismissal.
    events.extend([use("Bash", "read", command="pwd"), result("read")])
    native_plan_source(machine, events)
    if dismissed:
        await machine._handle_dismiss_claude_plan(DismissClaudePlan(
            sid=ctx.key, plan_id=ready.id, revision=ready.revision, client_id="pwa"))
        assert store.get(ctx.key).dismissed
    expected = store.get(ctx.key)
    machine._claude_plans = store = ClaudePlanStore(tmp_path)
    events.extend([use("Bash", "read-again", command="pwd"), result("read-again")])
    native_plan_source(machine, events)
    transport.sent.clear()
    for client in ("desktop", "pwa"):
        await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id=client))
        assert transport.sent[-1].plan == expected
    assert all(e.to is not None for e in transport.sent)
    assert ctx.sdk.client is None


@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("next_activity", ["enter", "pending", "rejected", "tasks"])
def test_manual_exit_does_not_override_subsequent_native_plan_activity(tmp_path, history_first, next_activity):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit", error=True)]
    store.recover("s", history)
    store.observe("s", Perm(mode="acceptEdits"))
    ready = store.get("s")
    if next_activity == "enter":
        additions = [use("EnterPlanMode", "new"), result("new")]
        expected_phase = "draft"
    elif next_activity == "tasks":
        store.dismiss("s", ready.id, ready.revision)
        additions = [use("TodoWrite", "work", todos=[{"content": "New work", "status": "pending"}]), result("work")]
        expected_phase = "ready"
    else:
        # The same document can be submitted and rejected again on the same plan.
        additions = [use("ExitPlanMode", "retry", plan="# Plan")]
        if next_activity == "rejected":
            additions.append(result("retry", error=True))
        expected_phase = "changes_requested" if next_activity == "rejected" else "pending"
    if not history_first:
        for event in additions:
            store.observe("s", event)
        live = store.get("s")
    restored = store.recover("s", history + additions, refresh=True)
    assert restored.phase == expected_phase and not restored.dismissed
    assert restored.revision > ready.revision
    if next_activity == "tasks":
        assert restored.steps == [{"step": "New work", "status": "pending"}]
    if not history_first:
        assert restored == live
    assert ClaudePlanStore(tmp_path).recover("s", history + additions, refresh=True) == restored
    rewound = store.recover("s", history, rollback=True)
    assert rewound.phase == "changes_requested" and not rewound.dismissed
    assert rewound.revision > restored.revision


@pytest.mark.parametrize("from_history", [False, True])
def test_rollback_before_plan_keeps_revision_boundary_across_restart(tmp_path, from_history):
    store = ClaudePlanStore(tmp_path)
    plan = approve(store)
    dismissed = store.dismiss("session", plan.id, plan.revision)
    assert store.recover("session", [], rollback=True) is None
    restarted = ClaudePlanStore(tmp_path)
    assert restarted.get("session") is None
    events = [use("EnterPlanMode", "enter"), result("enter"),
              use("ExitPlanMode", "exit", plan="# Plan\n\n1. Implement"), result("exit")]
    if from_history:
        reopened = restarted.recover("session", events, refresh=True)
    else:
        for event in events:
            restarted.observe("session", event)
        reopened = restarted.get("session")
    assert reopened.id == dismissed.id and reopened.revision > dismissed.revision
    assert not reopened.dismissed
    assert restarted.dismiss("session", plan.id, plan.revision) == reopened


def test_failed_rollback_recovery_preserves_last_durable_plan(tmp_path):
    store = ClaudePlanStore(tmp_path)
    before = approve(store)

    def unreadable():
        yield use("EnterPlanMode", "older-plan")
        raise OSError("history read failed")

    with pytest.raises(OSError, match="history read failed"):
        store.recover("session", unreadable(), rollback=True)
    assert ClaudePlanStore(tmp_path).get("session") == before


def test_profile_migration_is_idempotent_and_preserves_dismissal(tmp_path):
    store = ClaudePlanStore(tmp_path)
    plan = approve(store)
    receipt = store.dismiss("session", plan.id, plan.revision)
    store.migrate_profile_sessions(lambda sid: f"account:{sid}", profile_revision=1)
    store.migrate_profile_sessions(lambda sid: f"account:{sid}", profile_revision=1)
    assert not store.known("session")
    assert store.get("account:session") == receipt


@pytest.mark.parametrize("existed", [False, True])
def test_activation_rollback_restores_plan_and_dismissal_receipt(tmp_path, existed):
    from deploy.work_registry_snapshot import create_snapshot, restore_snapshot

    state_dir = tmp_path / "state"
    roots = {"claude": tmp_path / "claude-work", "codex": tmp_path / "codex-work"}
    receipt = None
    if existed:
        store = ClaudePlanStore(state_dir)
        plan = approve(store)
        receipt = store.dismiss("session", plan.id, plan.revision)
    snapshot = tmp_path / "snapshot"
    create_snapshot(snapshot, roots, state_dir=state_dir)
    store = ClaudePlanStore(state_dir)
    store.migrate_profile_sessions(lambda sid: f"new:{sid}", profile_revision=1)
    approve(store, "new-session")
    # A stopped failed activation can leave a rollback journal.
    (state_dir / "claude-plans.sqlite3-journal").write_bytes(b"stale journal")
    restore_snapshot(snapshot)
    assert not (state_dir / "claude-plans.sqlite3-journal").exists()
    if existed:
        restored = ClaudePlanStore(state_dir)
        assert restored.get("session") == receipt
        assert not restored.known("new-session") and not restored.known("new:session")
    else:
        assert not (state_dir / "claude-plans.sqlite3").exists()


def test_legacy_snapshot_does_not_claim_to_restore_new_plan_database(tmp_path):
    from deploy.work_registry_snapshot import create_snapshot, restore_snapshot

    state_dir = tmp_path / "state"
    roots = {"claude": tmp_path / "claude-work", "codex": tmp_path / "codex-work"}
    snapshot = tmp_path / "snapshot"
    path = create_snapshot(snapshot, roots, state_dir=state_dir)
    manifest = json.loads(path.read_text())
    manifest["version"] = 3
    del manifest["wrapper_state"]["files"]["claude-plans.sqlite3"]
    del manifest["wrapper_state"]["files"]["collaboration.sqlite3"]
    path.write_text(json.dumps(manifest))
    store = ClaudePlanStore(state_dir)
    plan = approve(store)
    restore_snapshot(snapshot)
    assert store.get("session") == plan


def test_live_and_history_wait_for_native_exit_result(tmp_path):
    translator = StreamTranslator(10000, turn_id="turn")
    live = translator.feed(_assistant([ToolUseBlock(id="exit", name="ExitPlanMode", input={"plan": "# Plan"})]))
    assert not any(e.type == "process" and e.phase == "end" for e in live)
    ended = translator.feed(UserMessage(content=[ToolResultBlock(tool_use_id="exit", content="approved")]))
    assert any(e.type == "process" and e.summary == "计划已批准" for e in ended)
    messages = [
        SimpleNamespace(uuid="u", type="user", message={"role": "user", "content": "plan"}),
        SimpleNamespace(uuid="a", type="assistant", message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "exit", "name": "ExitPlanMode", "input": {"plan": "# Plan"}}]}),
        SimpleNamespace(uuid="r", type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "exit", "content": "approved"}]}),
    ]
    events = translate_history(messages, 10000)
    assert any(e.type == "process" and e.summary == "计划已批准" for e in events)
    restored = ClaudePlanStore(tmp_path).recover("session", events)
    assert restored.phase == "approved" and restored.document == "# Plan"


@pytest.mark.asyncio
async def test_read_and_dismiss_do_not_resume_and_broadcast_receipt(tmp_path):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._claude_plans = ClaudePlanStore(tmp_path)
    machine._claude_plan_lock = asyncio.Lock()
    machine._recover_claude_plan = AsyncMock()
    plan = approve(machine._claude_plans, ctx.key)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    recovered = transport.sent[-1]
    assert isinstance(recovered, ClaudePlanState) and recovered.to == "desktop"
    assert recovered.plan == plan
    await machine._handle_dismiss_claude_plan(DismissClaudePlan(
        sid=ctx.key, plan_id=plan.id, revision=plan.revision, client_id="pwa"))
    receipt = transport.sent[-1]
    assert receipt.plan.dismissed and receipt.to is None
    assert ctx.sdk.client is None
    assert deserialize(serialize(receipt)).plan.dismissed


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", [False, True])
async def test_pwa_dismissal_reaches_desktop_and_survives_fresh_read(
        tmp_path, native_plan_source, resident):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._claude_plans = ClaudePlanStore(tmp_path)
    if not resident:
        machine.sessions.pop(ctx.key)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    native_plan_source(machine, history)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="pwa"))
    plan = transport.sent[-1].plan

    hub = RelayHub(RelayConfig())
    pwa, desktop = SimpleNamespace(send=AsyncMock()), SimpleNamespace(send=AsyncMock())
    hub._machine_clients["device"] = {"pwa": pwa, "desktop": desktop}
    other_device = SimpleNamespace(send=AsyncMock())
    hub._machine_clients["other"] = {"desktop": other_device}
    await machine._handle_dismiss_claude_plan(deserialize(serialize(DismissClaudePlan(
        sid=ctx.key, plan_id=plan.id, revision=plan.revision, client_id="pwa"))))
    receipt = deserialize(serialize(transport.sent[-1]))
    assert receipt.plan.dismissed and receipt.to is None
    await hub._on_wrapper_msg(receipt, "device")
    pwa.send.assert_awaited_once_with(receipt)
    desktop.send.assert_awaited_once_with(receipt)
    other_device.send.assert_not_awaited()

    # Refresh gives Desktop a new connection and a fresh state read. Reopen
    # SQLite as well so this cannot pass using only the prior in-memory object.
    machine._claude_plans = ClaudePlanStore(tmp_path)
    refreshed_desktop = SimpleNamespace(send=AsyncMock())
    hub._machine_clients["device"] = {"pwa": pwa, "desktop-refreshed": refreshed_desktop}
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop-refreshed"))
    refreshed = deserialize(serialize(transport.sent[-1]))
    assert refreshed.plan == receipt.plan
    await hub._on_wrapper_msg(refreshed, "device")
    refreshed_desktop.send.assert_awaited_once_with(refreshed)
    assert pwa.send.await_count == desktop.send.await_count == 1
    assert ctx.sdk.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", [False, True])
async def test_targeted_read_publishes_native_plan_changes_before_live_replay(tmp_path, native_plan_source, resident):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    if not resident:
        machine.sessions.pop(ctx.key)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit"),
               use("TodoWrite", "tasks", todos=[{"content": "Verify", "status": "pending"}]), result("tasks")]
    native_plan_source(machine, history)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="first"))
    before = store.get(ctx.key)
    cursor = ctx.seq
    transport.sent.clear()
    additions = [use("TodoWrite", "done", todos=[{"content": "Verify", "status": "completed"}]), result("done")]
    native_plan_source(machine, history + additions)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="second"))
    updates = [e for e in transport.sent if isinstance(e, ClaudePlanState)]
    assert [e.to for e in updates] == [None, "second"]
    assert all(e.sid == ctx.key and e.plan == store.get(ctx.key) for e in updates)
    assert updates[0].plan.steps == [{"step": "Verify", "status": "completed"}]
    assert updates[0].plan.revision > before.revision
    assert updates[1].seq is None
    if resident:
        assert updates[0].seq > cursor
        for event in additions:
            await machine._emit(ctx, event)
        replay = ctx.buffer.replay_from(cursor, cc_session_id=ctx.key, state=ctx.state)
        assert [e.plan for e in replay if isinstance(e, ClaudePlanState)] == [updates[0].plan]
    else:
        assert updates[0].seq is None
    # Duplicate live events and unchanged reads must not rebroadcast the update.
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="third"))
    broadcasts = [e for e in transport.sent if isinstance(e, ClaudePlanState) and e.to is None]
    assert broadcasts == [updates[0]]
    assert ctx.sdk.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("old_phase", ["approved", "ready", "rejected"])
async def test_dismissed_plan_boundary_survives_refresh_restart_and_dismiss(
        tmp_path, native_plan_source, history_first, old_phase):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    events = [use("EnterPlanMode", "enter"), result("enter")]
    if old_phase == "approved":
        events.extend([use("ExitPlanMode", "exit", plan="# Old plan"), result("exit")])
    elif old_phase == "rejected":
        events.extend([use("ExitPlanMode", "exit", plan="# Old plan"), result("exit", error=True)])
    events.extend([use("TodoWrite", "done", todos=[{"content": "Old checklist", "status": "completed"}]),
                   result("done")])
    native_plan_source(machine, events)
    await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
    if old_phase in {"ready", "rejected"}:
        await machine._emit(ctx, Perm(mode="bypassPermissions"))
    old = store.get(ctx.key)
    assert old.phase == ("ready" if old_phase == "rejected" else old_phase)
    await machine._handle_dismiss_claude_plan(DismissClaudePlan(
        sid=ctx.key, plan_id=old.id, revision=old.revision, client_id="pwa"))
    assert store.get(ctx.key).dismissed

    # Selecting plan mode manually has no native EnterPlanMode tool call.
    # Each subsequent plan must retain its own boundary across full replay.
    for index in range(2):
        await machine._emit(ctx, Perm(mode="plan"))
        additions = [use("ExitPlanMode", f"new-{index}", plan=f"# New plan {index}"), result(f"new-{index}")]
        events.extend(additions)
        native_plan_source(machine, events)
        if not history_first:
            for event in additions:
                await machine._emit(ctx, event)
            live = store.get(ctx.key)
            assert live.id == f"plan:new-{index}" and not live.steps
        await machine._handle_get_claude_plan(GetClaudePlan(sid=ctx.key, client_id="desktop"))
        plan = transport.sent[-1].plan
        assert plan.id == f"plan:new-{index}" and plan.document == f"# New plan {index}"
        assert plan.phase == "approved" and not plan.steps and not plan.dismissed
        if not history_first:
            assert plan == live

        machine._claude_plans = store = ClaudePlanStore(tmp_path)
        # Advance the native fingerprint so both reads really rebuild history.
        events.extend([use("Bash", f"unrelated-{index}", command="pwd"), result(f"unrelated-{index}")])
        native_plan_source(machine, events)
        await machine._handle_dismiss_claude_plan(DismissClaudePlan(
            sid=ctx.key, plan_id=plan.id, revision=plan.revision, client_id="pwa"))
        receipt = transport.sent[-1]
        assert receipt.plan.id == plan.id and receipt.plan.dismissed and receipt.to is None
        assert not receipt.plan.steps
    assert ctx.sdk.client is None


def test_plan_boundary_rollback_keeps_only_retained_native_calls(tmp_path):
    store = ClaudePlanStore(tmp_path)
    old_events = [use("EnterPlanMode", "enter"), result("enter"),
                  use("ExitPlanMode", "exit", plan="# Old"), result("exit"),
                  use("TodoWrite", "done", todos=[{"content": "Old task", "status": "completed"}]),
                  result("done")]
    old = store.recover("s", old_events)
    store.dismiss("s", old.id, old.revision)
    new_events = [use("ExitPlanMode", "new", plan="# New"), result("new")]
    for event in new_events:
        store.observe("s", event)
    new = store.get("s")
    # Compatibility with a stored live plan from before boundary persistence.
    with store._db() as db:
        legacy = store._load(db, "s")
        legacy.pop("boundaries", None)
        store._save(db, "s", legacy)
    store = ClaudePlanStore(tmp_path)
    restored = store.recover("s", old_events + new_events, rollback=True)
    assert restored.id == new.id and restored.revision > new.revision and not restored.steps
    restored = store.recover("s", old_events, rollback=True)
    assert restored.id == old.id and restored.steps == old.steps and not restored.dismissed
    # The rewound dismissal and new boundary no longer exist. A reused tool
    # identity now revises the restored, visible plan instead of resetting it.
    for event in new_events:
        store.observe("s", event)
    revised = store.get("s")
    assert revised.id == old.id and revised.steps == old.steps
    assert store.recover("s", old_events + new_events, refresh=True) == revised


def test_native_task_update_reopens_dismissed_plan_before_next_exit(tmp_path):
    store = ClaudePlanStore(tmp_path)
    history = [use("EnterPlanMode", "enter"), result("enter"),
               use("ExitPlanMode", "exit", plan="# Plan"), result("exit")]
    old = store.recover("s", history)
    store.dismiss("s", old.id, old.revision)
    history.extend([use("TodoWrite", "tasks", todos=[{"content": "New work", "status": "pending"}]),
                    result("tasks"), result("exit"),
                    use("ExitPlanMode", "revision", plan="# Revised"), result("revision")])
    revised = store.recover("s", history, refresh=True)
    assert revised.id == old.id and not revised.dismissed
    assert revised.steps == [{"step": "New work", "status": "pending"}]
    assert store.recover("s", history, refresh=True) == revised


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "first_result"), [("draft", False), ("draft", True), ("approved", False)])
async def test_initial_recovery_publishes_plan_for_deduplicated_live_event(
        tmp_path, native_plan_source, phase, first_result):
    from cc_remote.tui_presentation import SessionPresentation

    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    events = [use("EnterPlanMode", "enter"), result("enter")]
    if phase == "approved":
        events.extend([use("ExitPlanMode", "exit", plan="# Plan"), result("exit")])
    native_plan_source(machine, events)
    assert not store.known(ctx.key)

    # Native writes can lead the consumer, including an entire approval. The
    # first live call or result is already recorded and will be deduplicated.
    await machine._emit(ctx, events[1 if first_result else 0])
    plan = store.get(ctx.key)
    assert plan.phase == phase
    updates = [event for event in transport.sent if isinstance(event, ClaudePlanState)]
    assert len(updates) == 1
    assert updates[0].plan == plan and updates[0].sid == ctx.key and updates[0].to is None
    tui = SessionPresentation(engine="claude")
    tui.event(deserialize(serialize(updates[0])).model_dump(), "")
    assert tui.claude_plan == plan.model_dump()

    for event in events:
        await machine._emit(ctx, event)
    assert store.get(ctx.key) == plan
    assert len([event for event in transport.sent if isinstance(event, ClaudePlanState)]) == 1
    assert ctx.sdk.client is None


@pytest.mark.asyncio
async def test_initial_recovery_publishes_pending_plan_before_permission_prompt(tmp_path, native_plan_source):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    events = [use("EnterPlanMode", "enter"), result("enter"),
              use("ExitPlanMode", "exit", plan="# Review")]
    native_plan_source(machine, events)
    assert not store.known(ctx.key)

    async def ask(*args, **kwargs):
        updates = [event for event in transport.sent if isinstance(event, ClaudePlanState)]
        assert len(updates) == 1
        assert updates[0].plan.phase == "pending" and updates[0].plan.document == "# Review"
        assert updates[0].sid == ctx.key and updates[0].to is None
        return "继续修改"

    machine._on_ask = ask
    context = SimpleNamespace(agent_id=None, suggestions=[], tool_use_id="exit")
    answer = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {"plan": "# Review"}, context)
    assert answer.behavior == "deny"
    assert all(event.type != "tool_use" for event in transport.sent)
    updates = [event for event in transport.sent if isinstance(event, ClaudePlanState)]
    assert len(updates) == 2 and updates[-1].plan.phase == "changes_requested"
    assert updates[-1].plan.revision > updates[0].plan.revision
    for event in [*events, result("exit", error=True)]:
        await machine._emit(ctx, event)
    assert store.get(ctx.key) == updates[-1].plan
    assert len([event for event in transport.sent if isinstance(event, ClaudePlanState)]) == 2
    assert ctx.sdk.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("resident", [False, True])
async def test_native_watch_publishes_plan_progress_and_retains_dismissal(tmp_path, native_plan_source, resident):
    from cc_remote.wrapper.codex_external import ProcessIdentity
    from tests.test_claude_external import _watch

    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    sid = ctx.key
    if not resident:
        machine.sessions.pop(sid)
    path = native_plan_source(machine, [])
    watch = machine._watch[sid] = _watch(path)
    machine._build_requested_history = AsyncMock(return_value=History(
        session_id=sid, revision="native", detail="summary", external=True))
    # Opening the session can seed an empty plan before the CLI starts work.
    await machine._handle_get_claude_plan(GetClaudePlan(sid=sid, client_id="desktop"))
    assert store.known(sid) and store.get(sid) is None
    events = []

    async def append_and_mirror(additions, *, fetch_first=False):
        events.extend(additions)
        native_plan_source(machine, events)
        if fetch_first:
            # Another client's targeted read may refresh the shared projection
            # before the watcher. Every already-connected client still needs it.
            await machine._handle_get_claude_plan(GetClaudePlan(sid=sid, client_id="new-client"))
        transport.sent.clear()
        await machine._poll_claude_watch(sid, watch, {ProcessIdentity(101, 1001)}, 1000.0,
                                        ownership_scan_complete=True)
        histories = [event for event in transport.sent if isinstance(event, History)]
        updates = [event for event in transport.sent if isinstance(event, ClaudePlanState)]
        assert len(histories) == 1 and histories[0].external
        assert len(updates) == 1
        assert updates[0].sid == sid and updates[0].to is None
        assert updates[0].plan == store.get(sid)
        assert machine._is_external(sid)
        assert ctx.sdk.client is None and ctx.turn_task is None
        return updates[0].plan

    draft = await append_and_mirror([use("EnterPlanMode", "enter"), result("enter")])
    assert draft.phase == "draft"
    active = await append_and_mirror([
        use("ExitPlanMode", "exit", plan="# Native plan"), result("exit"),
        use("TodoWrite", "tasks", todos=[{"content": "Verify", "status": "in_progress"}]), result("tasks"),
    ])
    assert active.phase == "approved" and active.document == "# Native plan"
    assert active.steps == [{"step": "Verify", "status": "inProgress"}]
    assert active.revision > draft.revision
    completed = await append_and_mirror([
        use("TodoWrite", "done", todos=[{"content": "Verify", "status": "completed"}]), result("done"),
    ], fetch_first=True)
    assert completed.steps == [{"step": "Verify", "status": "completed"}]
    assert completed.revision > active.revision
    await machine._handle_dismiss_claude_plan(DismissClaudePlan(
        sid=sid, plan_id=completed.id, revision=completed.revision, client_id="desktop"))
    dismissed = store.get(sid)
    assert dismissed.dismissed
    unchanged = await append_and_mirror([use("Bash", "other", command="pwd"), result("other")])
    assert unchanged == dismissed
    cleared = await append_and_mirror([use("TodoWrite", "clear", todos=[]), result("clear")])
    assert cleared.steps == [] and cleared.document == "# Native plan"
    assert cleared.revision > dismissed.revision
    machine._build_requested_history.assert_awaited_with(
        sid, before=None, limit=machine.MIRROR_LIMIT, cwd=None, detail="summary")


@pytest.mark.asyncio
async def test_mirrored_history_survives_unavailable_plan_projection(tmp_path):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    plan = approve(store, ctx.key)
    history = History(session_id=ctx.key, revision="native", detail="summary")
    machine._build_requested_history = AsyncMock(return_value=history)
    machine._recover_claude_plan = AsyncMock(side_effect=OSError("transcript unavailable"))
    assert await machine._push_mirrored_history(ctx.key) is history
    machine._recover_claude_plan.assert_awaited_once_with(ctx.key, refresh=True)
    assert transport.sent == [history]
    assert store.get(ctx.key) == plan and ctx.sdk.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("engine", "btw"), [("codex", False), ("claude", True)])
async def test_mirrored_history_does_not_publish_other_engine_or_private_plan(engine, btw):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    ctx.engine = engine
    ctx.btw = btw
    machine._watch[ctx.key] = {"engine": engine}
    history = History(session_id=ctx.key, revision="native", detail="summary")
    machine._build_requested_history = AsyncMock(return_value=history)
    machine._recover_claude_plan = AsyncMock()
    assert await machine._push_mirrored_history(ctx.key) is history
    machine._recover_claude_plan.assert_not_awaited()
    assert transport.sent == [history]


@pytest.mark.asyncio
@pytest.mark.parametrize("dismissed", [False, True])
async def test_conversation_rollback_restores_plan_for_connected_clients(tmp_path, monkeypatch, dismissed):
    import cc_remote.wrapper.machine as machine_module
    from cc_remote.tui_presentation import SessionPresentation

    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    store = machine._claude_plans = ClaudePlanStore(tmp_path)
    path = tmp_path / "transcript.jsonl"
    path.write_text("{}\n")
    source_stat = path.stat()
    source = [str(path), source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns]
    retained = [use("EnterPlanMode", "enter"), result("enter"),
                use("ExitPlanMode", "exit", plan="# Plan"), result("exit"),
                use("TodoWrite", "tasks", todos=[{"content": "Verify", "status": "pending"}]),
                result("tasks")]
    store.recover(ctx.key, retained, source=source)
    store.observe(ctx.key, use("TodoWrite", "completed", todos=[{"content": "Verify", "status": "completed"}]))
    store.observe(ctx.key, result("completed"))
    before = store.get(ctx.key)
    if dismissed:
        before = store.dismiss(ctx.key, before.id, before.revision)
    tui = SessionPresentation(engine="claude")
    tui.event(ClaudePlanState(plan=before).model_dump(), "")

    # Supply the native active chain after rewind through the real recovery
    # and translation path. Its fingerprint can match a previously read page.
    messages = [SimpleNamespace(uuid="user", type="user", message={"role": "user", "content": "plan"})]
    for index, event in enumerate(retained):
        if isinstance(event, ToolUse):
            kind = "assistant"
            content = {"type": "tool_use", "id": event.tool_use_id, "name": event.tool, "input": event.input}
        else:
            kind = "user"
            content = {"type": "tool_result", "tool_use_id": event.tool_use_id, "content": event.content}
        messages.append(SimpleNamespace(uuid=f"message-{index}", type=kind,
                                        message={"role": kind, "content": [content]}))
    monkeypatch.setattr(machine, "_claude_transcript_path", lambda *_: str(path))
    monkeypatch.setattr(machine_module, "transcript_compact_snapshot", lambda *a, **kw: (messages, {}, {}))
    monkeypatch.setattr(machine_module, "recover_claude_native_metadata", lambda sid, rows, **kw: rows)
    monkeypatch.setattr(machine, "_resync_watch", lambda _: None)
    machine._build_history = AsyncMock(return_value=History(
        session_id=ctx.key, revision="after-rollback", events=[], has_more=False))
    await machine._publish_rollback_outcome(ctx, RollbackResult(
        session_id=ctx.key, engine="claude", restore="conversation",
        conversation="succeeded", files="skipped", restored_turns=1),
        invalidate_conversation=True, invalidate_files=False)
    restored_event = next(event for event in transport.sent if isinstance(event, ClaudePlanState))
    restored = restored_event.plan
    assert restored.id == before.id and restored.revision > before.revision
    assert restored.steps == [{"step": "Verify", "status": "pending"}]
    assert not restored.dismissed and restored_event.to is None
    assert ClaudePlanStore(tmp_path).get(ctx.key) == restored
    tui.event(restored_event.model_dump(), "")
    tui.event(ClaudePlanState(plan=before).model_dump(), "")
    assert tui.claude_plan == restored.model_dump()

    # The removed tool identity can be used again on the restored branch;
    # its next result must reach clients immediately, not catch up from 1.
    await machine._emit(ctx, use("TodoWrite", "completed", todos=[{"content": "Verify", "status": "in_progress"}]))
    await machine._emit(ctx, result("completed"))
    advanced = [event for event in transport.sent if isinstance(event, ClaudePlanState)][-1]
    assert advanced.plan.revision > restored.revision
    tui.event(advanced.model_dump(), "")
    assert tui.claude_plan["steps"] == [{"step": "Verify", "status": "inProgress"}]
    assert ctx.sdk.client is None


@pytest.mark.asyncio
async def test_plan_permission_selection_only_grants_selected_mode():
    machine, _, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._on_ask = AsyncMock(return_value="自动接受编辑")
    answer = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {"plan": "# Plan"}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert len(answer.updated_permissions) == 1
    assert answer.updated_permissions[0].mode == "acceptEdits"
    assert machine._on_ask.call_args.kwargs["plan_document"] == "# Plan"
    # Returning the permission result must not optimistically mutate actual mode.
    assert ctx.sdk.permission_mode == "bypassPermissions"
    machine._on_ask.return_value = "继续修改"
    rejected = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert rejected.behavior == "deny"
    ctx.sdk.work_mode = True
    await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {}, SimpleNamespace(agent_id=None, suggestions=[]))
    assert not any(o["label"] == "Bypass Permissions" for o in machine._on_ask.call_args.args[2])


@pytest.mark.asyncio
async def test_permission_callback_before_tool_block_and_denial_settle_ui(tmp_path):
    machine, transport, ctx = _machine_with_sdk(SdkHandle(WrapperConfig()))
    machine._claude_plans = ClaudePlanStore(tmp_path)
    machine._claude_plan_lock = asyncio.Lock()
    machine._recover_claude_plan = AsyncMock()
    seen = []

    async def ask(*args, **kwargs):
        seen.append(machine._claude_plans.get(ctx.key))
        return "继续修改"

    machine._on_ask = ask
    context = SimpleNamespace(agent_id=None, suggestions=[], tool_use_id="native-exit")
    answer = await machine._on_claude_tool_permission(ctx, "ExitPlanMode", {"plan": "# Review"}, context)
    assert seen[0].phase == "pending" and seen[0].document == "# Review"
    assert answer.behavior == "deny"
    rejected = machine._claude_plans.get(ctx.key)
    assert rejected.phase == "changes_requested"
    assert all(event.type != "tool_use" for event in transport.sent)
    await machine._emit(ctx, use("ExitPlanMode", "native-exit", plan="# Review"))
    assert machine._claude_plans.get(ctx.key) == rejected
    await machine._emit(ctx, result("native-exit", error=True))
    assert machine._claude_plans.get(ctx.key) == rejected
