"""DSH integration through the real shared WrapperMachine command boundary."""
import asyncio

import pytest

from cc_remote.protocol import (
    DshState, GetContext, GetEngineCapabilities, GetHistory, GetHistoryImage, GetTurnDetail, Hello, ListSessions, Models, NewSession, Query, SetDshControl,
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
        self.catalog = {"default": {"provider": "deepseek-official", "model": "deepseek-v4-pro",
                                    "reasoningEffort": "max"}, "groups": [
            {"id": "deepseek-official", "models": [
                {"id": "deepseek-flash", "name": "DeepSeek-V41-Flash",
                 "reasoning": {"efforts": [{"id": "off", "name": "Off"}], "defaultEffort": "off"}},
                {"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"},
                {"id": "deepseek-v4-flash-vision-exp"}]},
            {"id": "custom", "models": [{"id": "deepseek-flash"}]}]}

    async def history_snapshot(self, sid, **options):
        self.calls.append(("snapshot", sid, options))
        return {"contract": 1, "header": {"version": 3, "id": sid[4:], "cwd": "/tmp"},
                "cursor": -1, "records": [], "hasMore": False,
                "projections": {"asOfSeq": -1, "values": {}},
                **({"commands": [{"name": "goal", "description": "Native goal"},
                                 {"name": "plan", "description": "Native plan"}]}
                   if options.get("commands") else {})}

    async def rpc(self, endpoint, args=None):
        self.calls.append((endpoint, args))
        if endpoint == "session/modelCatalog":
            return self.catalog
        if endpoint == "session/create":
            return {"sessionId": "created"}
        if endpoint == "session/selectModel":
            return {"selected": {k: v for k, v in args["request"].items() if k != "sessionId"}}
        if endpoint == "agentPresets/list":
            return {"presets": [{"id": "standard", "isDefault": True}]}
        if endpoint == "commands/execute":
            return {"commandId": "command", "result": {"kind": "success"}}
        if endpoint == "skills/list":
            return {"skills": [{"name": args["request"]["sessionId"] + "-skill"}]}
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
async def test_sidebar_excludes_delegated_agents_but_retains_user_forks(monkeypatch):
    machine, transport, runtime, client = setup_runtime()
    items = [
        {"sessionId": "root", "running": True},
        {"sessionId": "user-fork", "parentSessionId": "root", "running": False},
        {"sessionId": "child", "parentSessionId": "root", "origin": "subagent", "running": True},
        {"sessionId": "grandchild", "parentSessionId": "child", "origin": "subagent", "running": False},
    ]
    items = [{"updatedAt": 1000, "cwd": "/tmp", **item} for item in items]
    async def list_sessions():
        return items
    monkeypatch.setattr(client, "list_sessions", list_sessions)
    runtime.archived.add("grandchild")
    result = await machine._handle(ListSessions(engine="dsh", client_id="viewer", cmd_id="catalog"))
    assert [row.session_id for row in result.sessions] == ["dsh@root", "dsh@user-fork"]
    assert result.sessions[1].forked_from_id == "dsh@root"
    assert result.to == "viewer" and result.request_id == "catalog"
    assert transport.sent[-1] == result
    # Child history and descendant control authorization still use the full
    # native catalog, not the sidebar projection.
    assert len(await client.list_sessions()) == 4
    await runtime.close()


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
    assert [c.name for c in machine.sessions["dsh@cold"].sdk.state.commands] == ["goal", "plan"]
    assert any(frame.type == "dsh_state" and {c.name for c in frame.commands} == {"goal", "plan"}
               for frame in transport.sent)
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
async def test_skills_read_targets_cold_dsh_session_without_changing_other_clients_focus():
    machine, transport, runtime, client = setup_runtime()
    other = _mk_ctx("codex-other", "codex-other")
    other.engine = "codex"
    machine.sessions[other.key] = other
    machine.focused_sid = other.key
    result = await machine._handle(GetEngineCapabilities(
        engine="dsh", space="code", sid="dsh@cold", cwd="/tmp", skills_only=True,
        cmd_id="skills-read", client_id="viewer",
    ))
    assert result.type == "engine_capabilities"
    assert result.sid == "dsh@cold" and result.to == "viewer"
    assert result.request_id == "skills-read"
    assert [item.name for item in result.items] == ["cold-skill"]
    assert machine.focused_sid == other.key
    assert machine.sessions["dsh@cold"].sdk.watch is None
    assert client.calls == [("snapshot", "dsh@cold", {"max_messages": 8}),
                            ("skills/list", {"request": {"sessionId": "cold"}})]
    assert transport.sent[-1] == result
    assert all(frame.sid == "dsh@cold" for frame in transport.sent)
    await runtime.close()


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
    assert len(result.models) == 1 and result.models[0]["efforts"] == ["off"]
    assert runtime.default_model == model_id("deepseek-official", "deepseek-flash")
    assert runtime.default_effort == "off"
    runtime.validate_model(runtime.default_model, "off")
    with pytest.raises(DshError):
        runtime.validate_model(runtime.default_model, "max")
    for provider, model in [("deepseek-official", "deepseek-v4-pro"), ("custom", "deepseek-flash")]:
        with pytest.raises(DshError):
            runtime.validate_model(model_id(provider, model), None)
    assert result.dsh_presets[0].id == "standard"


@pytest.mark.asyncio
async def test_new_session_selects_flash_even_when_the_native_default_is_another_model():
    machine, _, runtime, client = setup_runtime()
    async def activate(_ctx):
        pass
    runtime.activate = activate
    await machine._handle(NewSession(engine="dsh", cwd="/tmp", dsh_agent_preset="standard"))
    selected = next(call[1] for call in client.calls if call[0] == "session/selectModel")
    assert selected == {"request": {"sessionId": "created", "provider": "deepseek-official",
                                    "model": "deepseek-flash", "reasoningEffort": "off"}}
    assert machine._focused_ctx().sdk.model == runtime.default_model


@pytest.mark.asyncio
async def test_unavailable_flash_and_unoffered_models_never_create_a_session():
    machine, _, runtime, client = setup_runtime()
    rejected = await machine._handle(NewSession(engine="dsh", cwd="/tmp",
        model=model_id("deepseek-official", "deepseek-v4-pro")))
    assert rejected.code == "dsh_invalid_model"
    client.catalog["groups"] = []
    rejected = await machine._handle(NewSession(engine="dsh", cwd="/tmp"))
    assert rejected.code == "dsh_invalid_model"
    assert runtime.catalog == [] and runtime.default_model is None
    assert all(call[0] in {"session/modelCatalog", "agentPresets/list"} for call in client.calls)
    assert not machine.sessions


@pytest.mark.asyncio
async def test_cold_context_uses_native_projection_and_compaction_without_starting_an_agent():
    machine, _, runtime, client = setup_runtime()
    snapshot = await client.history_snapshot("dsh@session")
    snapshot.update(cursor=40, projections={"asOfSeq": 40, "values": {
        "contextPressure": {"pressureTokens": 550, "projectedTokens": 593, "contextWindow": 1000000},
        "modelSelection": {"next": {"provider": "deepseek-official", "model": "deepseek-flash"}},
    }})
    async def read(*args, **kwargs):
        client.calls.append(("snapshot",))
        return snapshot
    client.history_snapshot = read
    report = await machine._handle(GetContext(sid="dsh@session", client_id="viewer", cmd_id="context-1"))
    assert report.available and report.total_tokens == 593 and report.max_tokens == 1000000
    assert report.source == "native_estimate" and report.percentage == .06
    assert report.model == model_id("deepseek-official", "deepseek-flash")
    assert report.to == "viewer" and report.request_id == "context-1"
    ctx = machine.sessions["dsh@session"]
    await runtime.projections(ctx, {"contextPressure": {
        "pressureTokens": 550, "projectedTokens": 0, "contextWindow": 1000000}}, seq=41)
    stale = await machine._handle(GetContext(sid=ctx.key))
    assert stale.available and stale.total_tokens == 0  # old snapshot cannot undo compaction
    await runtime.projections(ctx, {"contextPressure": None}, seq=42)
    cleared = await machine._handle(GetContext(sid=ctx.key))
    assert not cleared.available and cleared.max_tokens == 0
    assert ctx.sdk.watch is None and all(call[0] == "snapshot" for call in client.calls)


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


async def long_native_snapshot(client, *, completed=False, steps=64):
    snapshot = await Client.history_snapshot(client, "dsh@session")
    records = snapshot["records"]
    def add(kind, data):
        records.append({"event": {"seq": len(records), "time": 1000 * (len(records) + 1),
                                  "type": kind, "data": data}})
    add("turn/start", {"turn": 1})
    add("user/message", {"id": "prompt", "source": {"kind": "user", "rpcId": "prompt"},
                         "content": [{"type": "text", "text": "inspect the project"}]})
    for step in range(steps):
        add("step/start", {"turn": 1, "step": step})
        add("assistant/message", {"turn": 1, "step": step, "message": {"content": [
            {"type": "reasoning", "text": f"reasoning {step}"},
            {"type": "text", "text": f"checking item {step}"},
            {"type": "tool-call", "toolCallId": f"read-{step}", "toolName": "read", "input": "{}"},
        ]}})
        add("tool/call", {"turn": 1, "step": step, "callId": f"read-{step}", "name": "read", "arguments": "{}"})
        add("tool/result", {"turn": 1, "step": step, "message": {"content": [
            {"type": "tool-result", "toolCallId": f"read-{step}", "content": [{"type": "text", "text": "contents"}]},
        ]}})
        add("step/end", {"turn": 1, "step": step})
    if completed:
        add("assistant/message", {"turn": 1, "step": steps, "message": {"content": [
            {"type": "text", "text": "inspection complete"},
        ]}})
        add("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    snapshot["cursor"] = len(records) - 1
    snapshot["projections"]["asOfSeq"] = snapshot["cursor"]
    return snapshot


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.asyncio
async def test_long_first_turn_has_bounded_summary_and_complete_process_detail(completed):
    machine, _, runtime, client = setup_runtime()
    snapshot = await long_native_snapshot(client, completed=completed)
    async def read(*args, **kwargs):
        return snapshot
    client.history_snapshot = read
    result = await machine._handle(GetHistory(session_id="dsh@session", detail="summary"))
    assert deserialize(serialize(result)) == result
    assert not result.has_more and not result.error
    assert result.in_progress is not completed
    assert not machine.sessions, "reading history must not activate a native Agent"
    assert len(result.turns) == 1
    row = result.turns[0].model_dump()
    assert row["done"] is completed and not row.get("error")
    assert len(row["blocks"]) <= 32
    finals = [block["text"] for block in row["blocks"] if block.get("channel") == "final"]
    assert finals == (["inspection complete"] if completed else [])
    assert any(block.get("channel") == "commentary" for block in row["blocks"])
    assert row["processDetailState"] == "present" and row["detailEventCount"] >= 64
    events, cursor = [], None
    for _ in range(8):
        detail = await machine._handle(GetTurnDetail(session_id="dsh@session", turn_id=row["id"], before=cursor, limit=256))
        events = detail.events + events
        if not detail.has_more:
            break
        cursor = detail.oldest_cursor
    comments = [e for e in events if e["type"] == "delta" and e.get("channel") == "commentary"]
    thoughts = [e for e in events if e["type"] == "delta" and e.get("channel") == "thinking"]
    assert len(comments) == len(thoughts) == 64
    assert comments[0]["text"] == "checking item 0" and comments[-1]["text"] == "checking item 63"
    assert not detail.has_more


@pytest.mark.parametrize("history_fails", [False, True])
@pytest.mark.asyncio
async def test_follow_recovers_running_native_turn_without_false_idle_or_read_only(history_fails):
    _, transport, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    assert ctx.state == "idle"
    snapshot = await long_native_snapshot(client)
    async def read(*args, **options):
        if history_fails and "through_seq" not in options:
            raise DshError("disconnected", "temporary history read failure")
        return snapshot
    async def stream(*args, **kwargs):
        yield {"type": "snapshot", "cursor": snapshot["cursor"]}
        await asyncio.Future()
    async def rpc(endpoint, *args):
        assert endpoint == "commands/list"
        return []
    client.history_snapshot, client.stream, client.rpc = read, stream, rpc
    ctx.sdk.watch = asyncio.create_task(runtime._follow(ctx))
    try:
        await asyncio.wait_for(ctx.sdk.ready.wait(), 2)
        assert ctx.sdk.connected and ctx.sdk.state.connected
        assert ctx.state == "running"
        assert not ctx.sdk.state.error
        assert not any(e.type == "state" and e.state == "idle" for e in transport.sent)
        assert not any(e.type == "session_control" and e.write_state == "read_only" for e in transport.sent)
        assert ctx.active_turn_binding.msg_id == "prompt"
        assert ctx.active_turn_binding.turn_id == "dsh-seq-1"
        histories = [e for e in transport.sent if e.type == "history"]
        assert histories
        if history_fails:
            assert histories[-1].error and not histories[-1].authoritative
        else:
            assert histories[-1].in_progress and not histories[-1].turns[-1].done
    finally:
        await runtime.close()


@pytest.mark.parametrize("delegated", [False, True])
@pytest.mark.asyncio
async def test_pending_question_restores_cold_owner_without_focus_or_activation(delegated):
    machine, _, runtime, client = setup_runtime()
    machine.focused_sid = "other-session"
    async def catalog():
        return [{"sessionId": "root", "running": True},
                {"sessionId": "child", "origin": "subagent", "parentSessionId": "root", "running": True}]
    client.list_sessions = catalog
    async def follow(ctx):
        await asyncio.Future()
    runtime._follow = follow
    captured = []
    async def ask(ctx, question, options, **fields):
        captured.append((ctx.key, question))
        return "Yes"
    machine._on_ask_locked = ask
    runtime.event_client = "listener"
    await runtime._question({"eventId": "pending", "event": "user-questions/request",
        "agentId": "child" if delegated else "root", "request": {"questions": [
            {"id": "choice", "question": "Continue?", "options": [{"label": "Yes"}, {"label": "No"}]},
        ]}}, runtime.event_client)
    assert captured == [("dsh@root", "Continue?")]
    assert machine.focused_sid == "other-session" and list(machine.sessions) == ["dsh@root"]
    assert machine.sessions["dsh@root"].sdk.watch is not None
    assert [call[0] for call in client.calls] == ["snapshot", "$events/result"]
    assert client.calls[-1][1]["outcome"] == {"kind": "result", "value": {"answers": [
        {"id": "choice", "selected": ["Yes"]},
    ]}}
    await runtime.close()


@pytest.mark.asyncio
async def test_question_reconnect_preserves_answers_and_unanswered_page_identity():
    machine, _, runtime, client = setup_runtime()
    await runtime.ctx("dsh@root")
    seen = []
    async def ask(ctx, question, options, **fields):
        seen.append((question, fields["ask_id"]))
        if len(seen) == 2:
            raise asyncio.CancelledError
        return "Yes" if question == "First?" else "Custom answer"
    machine._on_ask_locked = ask
    frame = {"eventId": "pending", "event": "user-questions/request", "agentId": "root", "request": {"questions": [
        {"id": "first", "question": "First?", "options": [{"label": "Yes"}, {"label": "No"}]},
        {"id": "second", "question": "Second?", "options": []},
    ]}}
    runtime.event_client = "old-listener"
    with pytest.raises(asyncio.CancelledError):
        await runtime._question(frame, runtime.event_client)
    assert not any(call[0] == "$events/result" for call in client.calls)
    runtime.event_client = "new-listener"
    await runtime._question(frame, runtime.event_client)
    assert [q for q, _ in seen] == ["First?", "Second?", "Second?"]
    assert seen[1][1] == seen[2][1]
    assert not runtime.question_progress
    assert client.calls[-1][1] == {"clientId": "new-listener", "eventId": "pending", "outcome": {
        "kind": "result", "value": {"answers": [
            {"id": "first", "selected": ["Yes"]},
            {"id": "second", "selected": [], "custom": "Custom answer"},
        ]},
    }}


@pytest.mark.asyncio
async def test_question_owner_recovery_and_browser_focus_share_one_context():
    machine, _, runtime, client = setup_runtime()
    async def read(*args, **kwargs):
        await asyncio.sleep(0)
        return await Client.history_snapshot(client, *args, **kwargs)
    client.history_snapshot = read
    first, second = await asyncio.gather(runtime.ctx("dsh@root"), runtime.ctx("dsh@root"))
    assert first is second is machine.sessions["dsh@root"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("action", ["create", "edit", "pause", "resume", "complete", "clear"])
@pytest.mark.asyncio
async def test_goal_actions_forward_native_round_caps_and_exact_client_ref(action):
    from cc_remote.protocol import ActDshGoal
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "goal"}]
    async def activate(_ctx):
        pass
    runtime.activate = activate
    fields = {"objective": "finish tests", "max_rounds": 64} if action in {"create", "edit"} else {}
    if action != "create":
        fields.update(goal_id="shown-goal", revision=3)
    command = ActDshGoal(sid=ctx.key, action=action, cmd_id="goal-action", client_id="viewer", **fields)
    result = await machine._handle(command)
    assert result.type == "dsh_command_result" and result.status == "success"
    assert result.request_id == command.cmd_id and result.to == "viewer"
    expected = {"agentId": "session"}
    if action != "create":
        expected["ref"] = {"id": "shown-goal", "revision": 3}
    if action in {"create", "edit"}:
        expected["request"] = {"objective": "finish tests", "maxGoalRounds": 64}
    assert ("goals/" + action, expected) in client.calls
    assert not any(call[0] == "commands/execute" for call in client.calls)


@pytest.mark.asyncio
async def test_goal_action_stale_ref_is_not_retried_or_replaced():
    from cc_remote.protocol import ActDshGoal
    machine, _, runtime, client = setup_runtime()
    ctx = await runtime.ctx("dsh@session")
    ctx.sdk.commands = [{"name": "goal"}]
    async def activate(_ctx):
        pass
    runtime.activate = activate
    writes = []
    async def fail(endpoint, args):
        writes.append((endpoint, args))
        raise DshError("GOAL_STALE_REF", "目标已更新，请重新查看后编辑。")
    client.rpc = fail
    result = await machine._handle(ActDshGoal(sid=ctx.key, action="edit", goal_id="old", revision=1,
        objective="keep this draft", max_rounds=128, cmd_id="edit", client_id="viewer"))
    assert result.type == "dsh_command_result" and result.status == "error"
    assert result.to == "viewer" and result.request_id == "edit"
    assert len(writes) == 1 and writes[0][1]["ref"] == {"id": "old", "revision": 1}


@pytest.mark.parametrize("fields", [
    {"action": "create", "objective": "x", "revision": 1},
    {"action": "create", "objective": " "},
    {"action": "edit", "objective": "x"},
    {"action": "edit", "goal_id": "g", "revision": 1},
    {"action": "pause", "goal_id": "g", "revision": 1, "max_rounds": 32},
    {"action": "create", "objective": "x", "max_rounds": True},
    {"action": "create", "objective": "x", "max_rounds": 0},
])
def test_goal_action_rejects_invalid_native_mutation_shapes(fields):
    from cc_remote.protocol import ActDshGoal
    with pytest.raises(ValueError):
        ActDshGoal(sid="dsh@s", cmd_id="a", **fields)
