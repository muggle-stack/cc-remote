"""DSH 0.2.2 management boundaries and authoritative native outcomes."""
import asyncio
import json
from uuid import uuid4

import pytest

from cc_remote.protocol import ReadDsh, SetDshControl
from cc_remote.wrapper.dsh_client import DshError
from tests.test_dsh_runtime import setup_runtime
from tests.test_dsh_stream import record, user
from cc_remote.wrapper.dsh_stream import DshProjection


def command(action, **body):
    return SetDshControl(sid="dsh@root", kind="manage", cmd_id=uuid4().hex, client_id="browser",
                         value=json.dumps({"action": action, **body}))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    {"kind": "session", "sessionId": "private"},
    {"kind": "new_once", "sessionId": "private", "create": {}},
    {"kind": "new", "create": {}},
])
async def test_schedule_private_involvement_hides_catalog_history_and_mutations(target):
    machine, _, runtime, client = setup_runtime()
    runtime.sidechats.rows["dsh@private"] = {"owner": "another", "parent": "dsh@root", "created": 1, "cut": 1}
    calls = []
    async def rpc(endpoint, args=None):
        calls.append(endpoint)
        assert endpoint == "schedule/catalog"
        return [{"id": "hidden", "target": target, "involvedSessionIds": ["private"]}]
    client.rpc = rpc
    for kind in ("schedules", "schedule_history"):
        page = await machine._handle(ReadDsh(cmd_id=uuid4().hex, sid="dsh@root", kind=kind, query="hidden", client_id="browser"))
        assert not page.items
    for action in ("schedule_run", "schedule_delete", "schedule_update"):
        result = await machine._handle(command(action, id="hidden", expectedUpdatedAt=None))
        assert result.status == "error"
    assert set(calls) == {"schedule/catalog"} and not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [{"kind": "new"}, {"kind": "new_once", "create": {"cwd": "/project"}}])
async def test_schedule_creation_durable_unknown_receipt_prevents_duplicate_after_recovery(target):
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        raise DshError("lost", "结果未知", outcome_unknown=True)
    client.rpc = rpc
    cmd = command("schedule_create", request={"title": "task", "prompt": "sensitive prompt",
        "target": target, "timing": {"kind": "daily", "daily": {"time": "09:00:00", "time_zone": "Asia/Taipei"}}})
    first = await machine._handle(cmd)
    # A new journal reader models recovery while retaining only the native client stub.
    from cc_remote.wrapper.dsh_mutations import DshMutationJournal
    runtime.mutations = DshMutationJournal(machine.cfg.state_dir)
    second = await machine._handle(cmd)
    assert first.status == second.status == "unknown" and len(calls) == 1
    assert b"sensitive prompt" not in runtime.mutations.path.read_bytes()
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
async def test_schedule_cas_does_not_replace_revision_and_reserved_session_is_not_linked():
    machine, _, runtime, client = setup_runtime()
    row = {"id": "task", "title": "task", "prompt": "hi", "status": "paused", "updatedAt": "revision-2",
           "target": {"kind": "new_once", "sessionId": "reserved", "create": {"cwd": "/project"}}, "involvedSessionIds": []}
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "schedule/catalog":
            return [row]
        return {"id": "task", "code": "schedule_conflict"}
    client.rpc = rpc
    page = await machine._handle(ReadDsh(cmd_id=uuid4().hex, sid="dsh@root", kind="schedules"))
    assert not page.items[0].sid and "sessionId" not in page.items[0].data["target"]
    result = await machine._handle(command("schedule_resume", id="task", expectedUpdatedAt="revision-1"))
    assert result.status == "error" and not any(endpoint == "schedule/edit" for endpoint, _ in calls)
    result = await machine._handle(command("schedule_resume", id="task", expectedUpdatedAt="revision-2"))
    assert result.status == "error"
    assert calls[-1] == ("schedule/edit", {"request": {"id": "task", "action": "update", "expectedUpdatedAt": "revision-2", "patch": {"active": True}}})
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("application,restore,status,word", [
    ("applied", None, "success", "生效"), ("restart-required", None, "success", "重启"),
    ("failed", "applied", "error", "原版本已恢复"), ("cancelled", "failed", "error", "未能确认恢复"),
    (None, None, "unknown", "未确认"),
])
async def test_plugin_update_outcomes_and_replay(application, restore, status, word):
    machine, _, runtime, client = setup_runtime()
    calls = []
    async def rpc(endpoint, args=None):
        calls.append((endpoint, args))
        if endpoint == "pluginManager/listVersions":
            return {"status": "listed", "registry": "https://native.example/", "versions": [{"version": "2.0.0", "bundle": True}]}
        return {"step": "install", "from": "1.0.0", "to": "2.0.0", "result": {"application": application, "error": "private diagnostic"},
                **({"restore": {"application": restore}} if restore else {})}
    client.rpc = rpc
    cmd = command("bundle_update", name="example", version="2.0.0", registry="https://injected.invalid")
    first = await machine._handle(cmd)
    replay = await machine._handle(cmd)
    assert first.status == replay.status == status and word in first.text
    assert "private diagnostic" not in first.text
    assert [e for e, _ in calls] == ["pluginManager/listVersions", "pluginManager/updateBundle"]
    assert calls[-1][1]["options"]["registry"] == "https://native.example/"
    await runtime.close()


@pytest.mark.asyncio
async def test_incompatible_plugin_version_cannot_be_submitted_or_leak_registry_diagnostics():
    machine, _, runtime, client = setup_runtime()
    async def rpc(endpoint, args=None):
        assert endpoint == "pluginManager/listVersions"
        return {"status": "listed", "registry": "https://user:secret@registry.invalid/", "versions": [
            {"version": "2", "bundle": True, "incompatible": {"diagnostic": "private"}, "deprecated": "secret"}]}
    client.rpc = rpc
    page = await machine._handle(ReadDsh(cmd_id=uuid4().hex, sid="dsh@root", kind="bundles", query='{"versions":true,"name":"example"}'))
    assert page.items[0].state == "incompatible"
    assert "secret" not in page.model_dump_json() and "private" not in page.model_dump_json()
    result = await machine._handle(command("bundle_update", name="example", version="2"))
    assert result.status == "error"
    await runtime.close()


@pytest.mark.asyncio
async def test_external_subagent_background_is_visible_without_invalid_detail_link():
    machine, output, runtime, client = setup_runtime()
    child = {"id": "external-1", "mode": "external", "provider": "oar", "activity": "running", "label": "External work"}
    async def catalog(sid):
        return {"parentAvailable": True, "entries": [child]}
    client.list_subagents = catalog
    ctx = await runtime.ctx("dsh@root")
    await runtime.refresh_children(ctx)
    sync = next(e for e in reversed(output.sent) if e.type == "background_process_sync")
    assert sync.items[0].kind == "task" and sync.items[0].item_id.startswith("dsh-external-")
    page = await machine._handle(ReadDsh(cmd_id=uuid4().hex, sid=ctx.key, kind="subagents"))
    assert not page.items[0].sid and not page.items[0].mode and not page.items[0].controllable
    child["activity"] = "completed"
    await runtime.refresh_children(ctx)
    assert output.sent[-1].items == []
    await runtime.close()


@pytest.mark.parametrize("code,status", [("ABORTED", "cancelled"), ("ABORTED_BEFORE_DISPATCH", "cancelled"), ("CODE_RUN_FAILED", "failed")])
def test_structured_native_tool_cancellation_keeps_terminal_status(code, status):
    projection = DshProjection()
    record(projection, "turn/start", {"turn": 1})
    user(projection, "prompt")
    result = record(projection, "tool/result", {"turn": 1, "step": 1, "error": {"code": code},
        "message": {"role": "tool", "toolCallId": "run-code", "isError": True, "content": [{"type": "text", "text": "stopped"}]}})
    assert result[0].status == status and result[0].is_error


@pytest.mark.asyncio
async def test_control_commits_multiple_projection_fields_at_the_same_native_sequence():
    _, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@root")
    completed = asyncio.Event()
    async def stream(endpoint, args=None):
        yield {"type": "projection", "sessionId": "root", "seq": 20, "changes": [
            {"key": "permissions", "val": {"currentValue": "read-only", "options": []}},
            {"key": "goal", "val": {"roundsStarted": 1, "goal": {"id": "goal", "revision": 2, "objective": "check", "phase": "complete", "maxGoalRounds": 2}}},
        ]}
        completed.set()
        await asyncio.Future()
    client.stream = stream
    task = asyncio.create_task(runtime._control())
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert ctx.sdk.state.permission == "read-only" and ctx.sdk.state.goal.phase == "complete"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("mode", ["external", None, "continuable"])
def test_child_return_only_links_a_native_local_child(mode):
    projection = DshProjection()
    if mode:
        record(projection, "subagent/catalog", {"childId": "child", "mode": mode})
    record(projection, "turn/start", {"turn": 2})
    result = record(projection, "user/message", {"source": {"kind": "subagent-settled", "senderSessionId": "child"},
        "content": [{"type": "text", "text": "native return"}]})
    returned = next(frame for frame in result if frame.type == "process")
    assert returned.input["agent_run_id"] == ("dsh@child" if mode == "continuable" else None)
