"""DSH integration through the real shared WrapperMachine command boundary."""
import asyncio

import pytest

from cc_remote.protocol import (
    DshState, GetHistory, GetHistoryImage, GetTurnDetail, Hello, Models, NewSession, Query, SetDshControl,
    SwitchSession, TurnEnd, TurnResult, deserialize, is_downstream, serialize,
)
from cc_remote.wrapper.dsh_client import DshError
from cc_remote.wrapper.dsh_runtime import DshHandle, DshRuntime
from cc_remote.wrapper.dsh_stream import model_id
from tests.test_multisession import _mk_ctx, _mk_machine


class Client:
    def __init__(self):
        self.calls = []
        self.cursor = -1

    async def history_snapshot(self, sid, **options):
        self.calls.append(("snapshot", sid, options))
        return {"contract": 1, "header": {"version": 3, "id": sid[4:], "cwd": "/tmp"},
                "cursor": -1, "records": [], "hasMore": False,
                "projections": {"asOfSeq": -1, "values": {}}}

    async def rpc(self, endpoint, args=None):
        self.calls.append((endpoint, args))
        if endpoint == "session/modelCatalog":
            return {"default": {"provider": "custom", "model": "test"}, "groups": [{"id": "custom", "models": [{
                "id": "test", "reasoning": {"efforts": [{"id": "off", "name": "Off"}]}}]}]}
        if endpoint == "agentPresets/list":
            return {"presets": [{"id": "standard", "isDefault": True}]}
        if endpoint == "commands/execute":
            return {"commandId": "command", "result": {"kind": "success"}}
        return {"accepted": True}

    async def list_sessions(self):
        self.calls.append(("list",))
        return []

    async def close(self):
        pass


def setup_runtime():
    machine, transport = _mk_machine()
    client = Client()
    runtime = machine._dsh = DshRuntime(machine, client)
    async def connection():
        return client
    runtime.connection = connection
    return machine, transport, runtime, client


@pytest.mark.asyncio
async def test_cold_history_and_switch_do_not_follow_or_activate_agent():
    machine, transport, runtime, client = setup_runtime()
    await machine._handle(GetHistory(session_id="dsh@cold", detail="summary", client_id="viewer"))
    assert machine.sessions == {}
    assert transport.sent[-1].type == "history"
    await machine._handle(SwitchSession(session_id="dsh@cold", engine="dsh", client_id="viewer"))
    assert machine.focused_sid == "dsh@cold"
    assert machine.sessions["dsh@cold"].sdk.watch is None
    assert all(call[0] in {"snapshot", "list"} for call in client.calls)
    assert all(frame.to in {None, "viewer"} for frame in transport.sent)
    await runtime.close()


@pytest.mark.asyncio
async def test_engine_mismatch_never_falls_through_to_claude():
    machine, transport, _, client = setup_runtime()
    result = await machine._handle(SwitchSession(session_id="dsh@foreign", engine="codex"))
    assert result.type == "error" and result.code == "dsh_invalid_session"
    assert client.calls == [] and not machine.sessions
    assert transport.sent == [result]


@pytest.mark.asyncio
async def test_native_permission_is_not_a_claude_mode_and_requires_catalog_membership():
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "permission"}]
    ctx.sdk.state = DshState(permissions=[{"value": "read-only", "name": "Read only"}])
    async def activate(_ctx):
        pass
    runtime.activate = activate
    await machine._handle(SetDshControl(sid=ctx.key, kind="permission", value="read-only"))
    assert client.calls[-1] == ("commands/execute", {
        "agentId": "session", "line": "/permission read-only", "submittedAttachments": []})
    count = len(client.calls)
    denied = await machine._handle(SetDshControl(sid=ctx.key, kind="permission", value="bypassPermissions"))
    assert denied.type == "error" and len(client.calls) == count


@pytest.mark.asyncio
async def test_model_catalog_keeps_off_and_does_not_invent_presets():
    _, _, runtime, _ = setup_runtime()
    await runtime.read_catalog()
    result = Models(engine="dsh", models=runtime.catalog, dsh_presets=runtime.presets)
    assert result.models[0]["efforts"] == ["off"]
    runtime.validate_model(model_id("custom", "test"), "off")
    with pytest.raises(DshError):
        runtime.validate_model(model_id("custom", "test"), "max")
    assert result.dsh_presets[0].id == "standard"


@pytest.mark.asyncio
async def test_reconnect_restores_exact_native_controls_and_completion_receipt():
    machine, transport, _, _ = setup_runtime()
    ctx = _mk_ctx("dsh@session", "dsh@session")
    ctx.engine = "dsh"
    ctx.sdk = DshHandle("session", model="dsh:custom:test", effort="off", permission_mode="read-only")
    ctx.sdk.state = DshState(agent_preset="minimal", permission="read-only")
    machine.sessions[ctx.key] = ctx
    await machine._emit(ctx, TurnEnd(turn_id="native-segment", result=TurnResult(subtype="steered", duration_ms=0, is_error=False)))
    assert transport.sent[-1].notification_context is None
    assert not machine._session_presentation.get("dsh", ctx.key).completion_unread
    await machine._emit(ctx, TurnEnd(turn_id="native-cancelled", result=TurnResult(subtype="interrupted", duration_ms=1, is_error=False)))
    assert not machine._session_presentation.get("dsh", ctx.key).completion_unread
    assert transport.sent[-1].notification_context.engine == "dsh"
    await machine._emit(ctx, TurnEnd(turn_id="native-turn", result=TurnResult(subtype="success", duration_ms=100, is_error=False)))
    terminal = next(frame for frame in reversed(transport.sent) if frame.type == "turn_end")
    assert terminal.notification_context.engine == "dsh"
    assert machine._session_presentation.get("dsh", ctx.key).completion_unread
    transport.sent.clear()
    await machine._handle(Hello(role="client", client_id="viewer"))
    states = [frame for frame in transport.sent if frame.type == "dsh_state"]
    assert states and states[-1].agent_preset == "minimal"
    assert states[-1].to == "viewer" and states[-1].seq is None
    assert any(frame.type == "perm" and frame.mode == "read-only" for frame in transport.sent)


@pytest.mark.asyncio
async def test_deferred_queries_remain_wrapper_owned_until_native_idle():
    machine, _, runtime, _ = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    ctx.state = "running"
    calls = []
    async def query(_ctx, cmd, *, launch_receipt=None):
        calls.append(cmd.prompt)
        if launch_receipt:
            launch_receipt.set_result(True)
    runtime.query = query
    try:
        await machine._handle(Query(sid=ctx.key, prompt="queued work", msg_id="q1", delivery="queue", cmd_id="c1", client_id="viewer"))
        await asyncio.sleep(0)
        assert len(ctx.queued_queries) == 1 and calls == []
        ctx.state = "idle"
        ctx.queued_query_wakeup.set()
        await asyncio.wait_for(ctx.queued_query_drain_task, 1)
        assert calls == ["queued work"] and not ctx.queued_queries
    finally:
        if ctx.queued_query_drain_task:
            ctx.queued_query_drain_task.cancel()


def test_dsh_wire_is_versioned_and_preset_cannot_cross_engine_boundary():
    assert is_downstream(DshState())
    assert deserialize(serialize(DshState())).type == "dsh_state"
    with pytest.raises(ValueError):
        NewSession(engine="claude", dsh_agent_preset="standard")
    with pytest.raises(ValueError):
        NewSession(engine="dsh", space="work")
    new = NewSession(engine="dsh", dsh_effort="off", dsh_agent_preset="standard")
    assert deserialize(serialize(new)).dsh_effort == "off"


@pytest.mark.asyncio
async def test_old_projection_cannot_restore_a_completed_goal_or_old_permission():
    _, _, runtime, _ = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    goal = {"id": "goal", "revision": 2, "objective": "test", "phase": "complete", "maxGoalRounds": 8}
    ctx.sdk.goal_activation = {"id": "goal", "revision": 2, "activation": "disarmed"}
    await runtime.projections(ctx, {"goal": {"goal": goal, "roundsStarted": 3},
        "permissions": {"currentValue": "read-only", "options": []}}, seq=20)
    await runtime.projections(ctx, {"goal": {"goal": {**goal, "revision": 1, "phase": "active"}, "roundsStarted": 0},
        "permissions": {"currentValue": "danger-full-access", "options": []}}, seq=19)
    assert ctx.sdk.state.goal.phase == "complete"
    assert ctx.sdk.state.goal.activation == "disarmed"
    assert ctx.sdk.state.permission == "read-only"


@pytest.mark.asyncio
async def test_history_failure_settles_exact_detail_or_image_request():
    machine, _, runtime, _ = setup_runtime()
    runtime.history_cursors[("dsh@session", "turn")] = 4
    async def fail(*args, **kwargs):
        raise DshError("disconnected", "DSH 连接中断。")
    runtime.client.history_snapshot = fail
    detail = await machine._handle(GetTurnDetail(session_id="dsh@session", turn_id="turn", client_id="viewer"))
    assert detail.type == "turn_detail" and detail.error and not detail.authoritative
    assert detail.turn_id == "turn" and detail.to == "viewer"
    image = await machine._handle(GetHistoryImage(session_id="dsh@session", turn_id="turn",
        image_id="old", variant="thumbnail", request_id="image-1", client_id="viewer"))
    assert image.type == "history_image" and image.error
    assert image.request_id == "image-1" and image.to == "viewer"


@pytest.mark.asyncio
async def test_unknown_prompt_outcome_keeps_admission_receipt_and_is_never_retried():
    _, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    async def activate(_ctx):
        pass
    runtime.activate = activate
    writes = []
    async def fail(endpoint, args):
        writes.append((endpoint, args))
        raise DshError("disconnected", "结果未知", outcome_unknown=True)
    client.rpc = fail
    receipt = asyncio.get_running_loop().create_future()
    query = Query(sid=ctx.key, prompt="do once", msg_id="prompt")
    await runtime.query(ctx, query, launch_receipt=receipt)
    assert receipt.result() is True and ctx.state == "running"
    # After reconnect, native idle can arrive before its canonical human echo.
    ctx.state = "idle"
    await runtime.query(ctx, query)
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_command_rejects_unsupported_attachments_before_uploading():
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "compact"}]
    async def activate(_ctx):
        pass
    runtime.activate = activate
    count = len(client.calls)
    result = await machine._handle(SetDshControl(sid=ctx.key, kind="command", value="/compact",
        files=[{"filename": "note.txt", "data": "aGk="}], cmd_id="command", client_id="viewer"))
    assert result.type == "dsh_command_result" and result.status == "error"
    assert result.request_id == "command" and result.to == "viewer"
    assert len(client.calls) == count


@pytest.mark.asyncio
async def test_history_page_fence_excludes_live_events_arriving_during_read():
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    original = client.history_snapshot
    before = ctx.seq
    async def concurrent_read(*args, **kwargs):
        ctx.seq += 5
        return await original(*args, **kwargs)
    client.history_snapshot = concurrent_read
    result = await machine._handle(GetHistory(session_id=ctx.key, detail="summary"))
    assert result.live_seq == before and ctx.seq > before
