"""Async MCP activity and continuation rendering never drive model lifecycle."""
import asyncio
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cc_remote.protocol import ERR_AUTH, GetAgentDetail, SyncBtw, TurnEnd, TurnResult
from cc_remote.task_store import TaskStore, public_task
from cc_remote.wrapper.codex_tasks import read_task_activity, read_task_callbacks, read_task_parameters
from cc_remote.wrapper.history_store import materialize_history_turns
from tests.test_multisession import _mk_ctx, _mk_machine


def task(home, sid, title="Background verification"):
    home.mkdir(exist_ok=True)
    store = TaskStore(home)
    key = store.create(sid, argv=["echo", "PRIVATE_ARGUMENT"], cwd=str(home),
                       title=title, request_id=str(uuid4()))
    return store, key


async def codex_task_context(machine, sid, *, side_chat=False):
    key = f"btw-{uuid4().hex}" if side_chat else sid
    ctx = _mk_ctx(key, None if side_chat else sid)
    ctx.engine = "codex"
    if side_chat:
        ctx.btw = ctx.btw_announced = True
        ctx.owner_client_id = "requesting-owner"
        await machine._capture_session_id(ctx, sid)
    machine.sessions[key] = ctx
    return ctx


def test_absent_task_state_is_empty_without_writes(tmp_path):
    assert read_task_activity(tmp_path) == {}
    assert read_task_parameters(tmp_path, str(uuid4()), f"async-task:{uuid4()}") is None
    assert list(tmp_path.iterdir()) == []


def test_task_parameters_are_separate_and_command_credentials_are_redacted(tmp_path):
    sid = str(uuid4())
    store = TaskStore(tmp_path)
    key = store.create(sid, argv=[
        "python3", "verify job.py", "--target", "EP !11",
        "--api-key", "PRIVATE_FLAG", "--token=PRIVATE_OPTION",
        "Authorization: Basic PRIVATE_HEADER", "TOKEN='PRIVATE_ENV'",
        'run --password "PRIVATE_INLINE"',
    ], cwd=str(tmp_path), title="Verify build and card", request_id=str(uuid4()),
        timeout_seconds=1140)
    store.update(key, state="running", output=b"PRIVATE_OUTPUT")
    snapshot, = read_task_activity(tmp_path)[sid]
    assert snapshot.command is None and snapshot.cwd is None
    assert "timeout_seconds" not in snapshot.model_dump_json()
    detail = read_task_parameters(tmp_path, sid, snapshot.item_id)
    assert detail and detail.command.startswith("python3 'verify job.py' --target 'EP !11'")
    assert detail.cwd == str(tmp_path)
    assert detail.input == {"timeout_seconds": 1140}
    assert "PRIVATE" not in detail.model_dump_json() and "***" in detail.command
    assert store.get(key, sid)["spec"]["argv"][5] == "PRIVATE_FLAG"
    assert read_task_parameters(tmp_path, str(uuid4()), snapshot.item_id) is None
    other_home = tmp_path / "other-account"
    other_home.mkdir()
    assert read_task_parameters(other_home, sid, snapshot.item_id) is None
    assert list(other_home.iterdir()) == []


@pytest.mark.parametrize(("script", "redacted"), [
    (
        "curl -H 'Cookie: session=PRIVATE_COOKIE; second=PRIVATE_SECOND' https://example.invalid",
        "curl -H 'Cookie: ***' https://example.invalid",
    ),
    (
        'curl --header "Authorization: Basic PRIVATE_BASIC" https://example.invalid',
        'curl --header "Authorization: ***" https://example.invalid',
    ),
    (
        """curl --header="X-API-Key: PRIVATE_FIRST"'PRIVATE_CONTINUATION' https://example.invalid""",
        'curl --header="X-API-Key: ***',
    ),
    (
        r"curl -HProxy-Authorization:\ Basic\ PRIVATE_PROXY https://example.invalid",
        "curl -HProxy-Authorization: ***",
    ),
], ids=["cookie", "basic-auth", "concatenated-header", "attached-header"])
def test_task_parameters_redact_headers_inside_shell_scripts(tmp_path, script, redacted):
    sid = str(uuid4())
    store = TaskStore(tmp_path)
    argv = ["/bin/sh", "-c", script]
    key = store.create(sid, argv=argv, cwd=str(tmp_path), title="HTTP request",
                       request_id=str(uuid4()))
    detail = read_task_parameters(tmp_path, sid, f"async-task:{key}")
    assert detail is not None and detail.command is not None
    assert "PRIVATE" not in detail.model_dump_json()
    assert shlex.split(detail.command) == ["/bin/sh", "-c", redacted]
    assert store.get(key, sid)["spec"]["argv"] == argv


@pytest.mark.asyncio
@pytest.mark.parametrize("side_chat", [False, True], ids=["main", "side-chat"])
async def test_wrapper_sends_task_parameters_only_to_requester_without_waking_session(
    tmp_path, monkeypatch, side_chat,
):
    sid, other_sid = str(uuid4()), str(uuid4())
    _, key = task(tmp_path, sid)
    machine, transport = _mk_machine()
    ctx = await codex_task_context(machine, sid, side_chat=side_chat)
    other = await codex_task_context(machine, other_sid, side_chat=side_chat)
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))

    async def unexpected_watch(_sid):
        pytest.fail("Reading task parameters must not watch or resume a session")

    monkeypatch.setattr(machine, "_watch_session", unexpected_watch)
    cmd = GetAgentDetail(session_id=ctx.key, run_id=f"async-task:{key}", owner_id="requesting-owner",
                         client_id="requesting-client", request_id="parameters-request")
    detail = await machine._handle(cmd)
    assert transport.sent == [detail] and detail.to == cmd.client_id
    assert detail.owner_id == cmd.owner_id
    assert detail.sid == ctx.key and detail.request_id == cmd.request_id
    assert detail.events[0]["command"] == "echo PRIVATE_ARGUMENT"
    assert detail.events[0]["input"] == {"timeout_seconds": 3600}
    denied = await machine._handle(cmd.model_copy(update={"session_id": other.key}))
    assert not denied.authoritative and denied.error and denied.events == []
    if side_chat:
        for fields in (
            {"owner_id": "other-owner"}, {"session_id": sid}, {"client_id": None},
            {"run_id": "other-agent"},
        ):
            denied = await machine._handle(cmd.model_copy(update=fields))
            assert denied.type == "error" and denied.code == ERR_AUTH
    else:
        denied = await machine._handle(cmd.model_copy(update={"client_id": None}))
        assert not denied.authoritative and denied.error and denied.events == []


def test_callback_recovery_reads_only_exact_account_thread_acceptance(tmp_path):
    sid, other_sid, turn = str(uuid4()), str(uuid4()), str(uuid4())
    store, key = task(tmp_path / "first", sid)
    assert read_task_callbacks(store.home, sid) == ()
    store.update(key, state="completed", delivery="accepted", delivery_turn_id=turn)
    assert read_task_callbacks(store.home, sid) == (turn,)
    assert read_task_callbacks(store.home, other_sid) == ()
    other = tmp_path / "other"
    other.mkdir()
    assert read_task_callbacks(other, sid) == ()
    assert list(other.iterdir()) == []
    store.update(key, delivery="unknown")
    assert read_task_callbacks(store.home, sid) == ()


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


@pytest.mark.parametrize("delivery,expected_status,summary", [
    ("accepted", "pending", "等待处理完成"),
    ("failed", "failed", "未能处理通知"),
    ("rejected", "failed", "未能处理通知"),
    ("unknown", "failed", "处理结果未确认"),
])
def test_callback_outcomes_remain_visible_without_exposing_output(tmp_path, delivery, expected_status, summary):
    sid = str(uuid4())
    store, key = task(tmp_path, sid)
    store.update(key, state="completed", delivery=delivery,
                 output=b"PRIVATE_RESULT", delivery_error="PRIVATE_PROVIDER_ERROR")
    item = read_task_activity(tmp_path)[sid][0]
    assert item.status == expected_status and summary in item.summary
    assert "PRIVATE" not in item.model_dump_json()
    assert store.get(key)["state"] == "completed", "callback failure is not command failure"


@pytest.mark.asyncio
async def test_retained_warnings_do_not_hide_active_tasks_at_snapshot_limit(tmp_path, monkeypatch):
    from cc_remote.protocol import MAX_BACKGROUND_PROCESS_ITEMS
    sid = str(uuid4())
    for _ in range(MAX_BACKGROUND_PROCESS_ITEMS):
        store, key = task(tmp_path, sid)
        store.update(key, state="completed", delivery="failed")
    _, active = task(tmp_path, sid, "New active task")
    machine, transport = _mk_machine()
    ctx = _mk_ctx(sid, sid)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    frames = [frame for frame in transport.sent if frame.type == "background_process_sync"]
    assert len(frames) == 1 and len(frames[0].items) == MAX_BACKGROUND_PROCESS_ITEMS
    assert any(item.item_id == f"async-task:{active}" for item in frames[0].items)


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
async def test_side_chat_seeds_background_tasks_and_recovers_its_accepted_callback(tmp_path, monkeypatch):
    sid, parent_sid, turn_id = (str(uuid4()) for _ in range(3))
    store, key = task(tmp_path, sid)
    store.update(key, state="running")
    machine, transport = _mk_machine()
    await codex_task_context(machine, parent_sid)
    side = await codex_task_context(machine, sid, side_chat=True)
    side.parent_sid = parent_sid
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    item_id = f"async-task:{key}"

    await machine._refresh_codex_background_processes()
    snapshot, = (frame for frame in transport.sent if frame.type == "background_process_sync")
    assert snapshot.sid == side.key and snapshot.owner_id == side.owner_client_id
    assert [(item.item_id, item.status) for item in snapshot.items] == [(item_id, "running")]

    transport.sent.clear()
    await machine._handle(SyncBtw(sid=side.key, client_id="reconnected-client", owner_id=side.owner_client_id))
    seeded, = (frame for frame in transport.sent if frame.type == "background_process_sync")
    assert seeded.items == snapshot.items and seeded.seq is None
    assert seeded.sid == side.key and seeded.to == "reconnected-client"
    assert seeded.owner_id == side.owner_client_id and side.state == "idle"

    store.update(key, state="failed", delivery="accepted", delivery_turn_id=turn_id)
    side.sdk = SimpleNamespace(
        using_daemon_proxy=True,
        probe_owned_turn=AsyncMock(return_value=SimpleNamespace(turn_id=turn_id)),
        recover_owned_turn=AsyncMock(return_value=True),
    )
    await machine._refresh_codex_background_processes([side])
    side.sdk.probe_owned_turn.assert_awaited_once_with(turn_id)
    side.sdk.recover_owned_turn.assert_awaited_once_with(turn_id)
    assert side.session_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("side_chat", [False, True], ids=["main", "side-chat"])
async def test_native_completion_retires_only_its_callbacks_before_the_worker_poll(
    tmp_path, monkeypatch, side_chat,
):
    sid, other_sid, turn_id = (str(uuid4()) for _ in range(3))
    local_home, other_home = tmp_path / "local", tmp_path / "other"
    store, first = task(local_home, sid, "First callback")
    _, second = task(local_home, sid, "Second callback")
    _, running = task(local_home, sid, "Still running")
    _, other_thread = task(local_home, other_sid, "Other thread")
    other_store, other_account = task(other_home, sid, "Other account")
    for key in (first, second, other_thread):
        store.update(key, state="completed", delivery="accepted", delivery_turn_id=turn_id)
    other_store.update(other_account, state="completed", delivery="accepted", delivery_turn_id=turn_id)
    store.update(running, state="running")

    machine, transport = _mk_machine()
    current = await codex_task_context(machine, sid, side_chat=side_chat)
    different_thread = _mk_ctx(other_sid, other_sid)
    different_account = _mk_ctx(f"other:{sid}", sid)
    for ctx in (different_thread, different_account):
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda ctx: SimpleNamespace(
        home=other_home if ctx is different_account else local_home))
    await machine._refresh_codex_background_processes()
    assert set(current.codex_background_processes) == {f"async-task:{key}" for key in (first, second, running)}
    cached = next(
        frame for frame in transport.sent if frame.type == "background_process_sync" and frame.sid == current.key
    )

    for target, trusted, is_error in (
        (turn_id, False, False), (turn_id, True, True), (str(uuid4()), True, False),
    ):
        event = TurnEnd(turn_id=target, result=TurnResult(
            subtype="error_during_execution" if is_error else "success",
            duration_ms=1, is_error=is_error))
        event._codex_authoritative_terminal = trusted
        transport.sent.clear()
        await machine._emit(current, event)
        assert not any(frame.type == "background_process_sync" for frame in transport.sent)

    from cc_remote.wrapper.codex_stream import CodexStreamTranslator
    translator = CodexStreamTranslator(tool_result_max=8000)
    for key in (first, second):
        for event in translator.feed({"method": "item/completed", "params": {
            "turnId": turn_id, "threadId": sid, "item": {
                "id": f"receipt-{key}", "type": "functionCallOutput", "namespace": "cc_remote_tasks",
                "name": "task_result", "output": json.dumps(public_task(store.get(key), output=True)),
            },
        }}):
            await machine._emit(current, event)
    transport.sent.clear()
    for event in translator.feed({"method": "turn/completed", "params": {
        "threadId": sid, "turn": {"id": turn_id, "status": "completed", "durationMs": 1},
    }}):
        await machine._emit(current, event)
    frames = [frame for frame in transport.sent if frame.type == "background_process_sync"]
    assert len(frames) == 1 and frames[0].sid == current.key
    assert frames[0].owner_id == current.owner_client_id
    assert [(item.item_id, item.status) for item in frames[0].items] == [(f"async-task:{running}", "running")]
    assert store.get(first)["delivery"] == store.get(second)["delivery"] == "accepted"
    assert list(different_thread.codex_background_processes) == [f"async-task:{other_thread}"]
    assert list(different_account.codex_background_processes) == [f"async-task:{other_account}"]
    assert machine._refresh_cached_response(cached).items == frames[0].items

    transport.sent.clear()
    await machine._refresh_codex_background_processes()
    assert transport.sent == []


@pytest.mark.asyncio
async def test_completion_survives_a_stale_poll_and_late_callback_acceptance(tmp_path, monkeypatch):
    sid, turn_id = str(uuid4()), str(uuid4())
    store, accepted = task(tmp_path, sid)
    _, late = task(tmp_path, sid)
    store.update(accepted, state="completed", delivery="accepted", delivery_turn_id=turn_id)
    store.update(late, state="completed")

    machine, transport = _mk_machine()
    ctx = _mk_ctx(sid, sid)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    monkeypatch.setattr(machine, "_codex_profile_for_ctx", lambda _: SimpleNamespace(home=tmp_path))
    await machine._refresh_codex_background_processes()
    read, release = asyncio.Event(), asyncio.Event()
    to_thread = asyncio.to_thread

    async def pause_task_read(fn, *args, **kwargs):
        value = await to_thread(fn, *args, **kwargs)
        if fn is read_task_activity:
            read.set()
            await release.wait()
        return value

    with monkeypatch.context() as scoped:
        scoped.setattr(asyncio, "to_thread", pause_task_read)
        poll = asyncio.create_task(machine._refresh_codex_background_processes())
        try:
            await asyncio.wait_for(read.wait(), 2)
            event = TurnEnd(turn_id=turn_id, result=TurnResult(
                subtype="success", duration_ms=1, is_error=False))
            event._codex_authoritative_terminal = True
            await machine._emit(ctx, event)
            assert list(ctx.codex_background_processes) == [f"async-task:{late}"]
            transport.sent.clear()
        finally:
            release.set()
            await poll
    assert transport.sent == []
    assert list(ctx.codex_background_processes) == [f"async-task:{late}"]

    store.update(late, delivery="accepted", delivery_turn_id=turn_id)
    await machine._refresh_codex_background_processes()
    assert ctx.codex_background_processes == {}
    assert transport.sent[-1].type == "background_process_sync" and transport.sent[-1].items == []


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
