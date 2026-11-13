"""Room ownership, durable delivery and native query/steer regression tests."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from cc_remote.collaboration import CollaborationMemberSpec, shared_prompt
from cc_remote.claude_profiles import ClaudeProfileRegistry
from cc_remote.codex_profiles import CodexProfileRegistry
from cc_remote.collaboration_store import CollaborationStore
from cc_remote.protocol import (
    AssistantMsgEnd,
    AssistantMsgStart,
    CollaborationCommand,
    Delta,
    Error,
    ListSessions,
    RenameSession,
    SessionFocus,
    SessionInfo,
    SessionList,
    TurnEnd,
    TurnResult,
    TurnSteered,
    UserMsg,
    deserialize,
    is_client_message,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def native_named_member(engine):
    machine, transport = _mk_machine()
    machine.cfg.dsh_connection_file = machine.cfg.state_dir / "dsh-connection.json"
    controller = machine._collaboration
    controller.start = AsyncMock()
    native = "11111111-1111-4111-8111-111111111111"
    if engine == "claude":
        sid = machine._claude_wire_sid(machine._claude_profile(None), native)
    elif engine == "codex":
        sid = machine._codex_wire_sid(machine._codex_profile(None), native)
    else:
        native = "session-" + native
        sid = "dsh@" + native
    title = {"value": "原生会话名称"}

    def listing():
        return SessionList(engine=engine, sessions=[SessionInfo(
            session_id=sid, native_session_id=native, summary=title["value"],
            engine=engine, cwd="/tmp", space="code")])

    async def rename(command):
        title["value"] = command.title
        return listing()

    machine._handle_list_sessions = AsyncMock(side_effect=lambda _cmd: listing())
    machine._handle_rename_session = AsyncMock(side_effect=rename)

    async def dispatch(command):
        if isinstance(command, ListSessions):
            return listing()
        if isinstance(command, RenameSession):
            title["value"] = command.title
        return None

    machine._dsh.dispatch = AsyncMock(side_effect=dispatch)
    return machine, transport, controller, sid, native, title


async def store_named_member(controller, engine, sid, native):
    store = await controller.database()
    spec, account = controller.prepare_member(CollaborationMemberSpec(engine=engine, name="旧聊天室别名", cwd="/tmp"))
    room_id = store.mutate("owner", "create", "create", {"name": "Room"}, prepared_members=[
        (spec, account, {"sid": sid, "native_sid": native, "cwd": "/tmp"}),
    ])
    return store, room_id, store.room("owner", room_id)["members"][0]


@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.asyncio
async def test_native_title_is_authoritative_and_old_room_aliases_never_rename_it(engine):
    machine, _transport, controller, sid, native, title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    revision = store.room("owner", room_id)["revision"]
    snapshot = await controller.snapshot("owner", room_id)
    assert snapshot["room"]["members"][0]["name"] == "原生会话名称"
    assert snapshot["room"]["revision"] == revision
    assert "pending_title" not in snapshot["room"]["members"][0]
    assert machine._handle_rename_session.await_count == 0
    assert all(isinstance(call.args[0], ListSessions) for call in machine._dsh.dispatch.await_args_list)
    title["value"] = "在原生 Harness 改名"
    controller.title_catalogs.clear()
    snapshot = await controller.snapshot("owner", room_id)
    assert snapshot["room"]["members"][0]["name"] == title["value"]
    assert store.member_by_id(member["id"])["spec"]["name"] == title["value"]


@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.asyncio
async def test_room_rename_uses_the_existing_native_handler_and_replays_its_receipt(engine):
    machine, _transport, controller, sid, native, title = native_named_member(engine)
    machine.focused_sid = "keep-focus"
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    command = CollaborationCommand(operation="rename_member", owner_id="owner", client_id="client", cmd_id="rename",
                                   room_id=room_id, member_id=member["id"], name="聊天室中改名", revision=1)
    result = await controller.handle(command)
    assert not result.error
    assert title["value"] == "聊天室中改名"
    assert result.data["room"]["members"][0]["name"] == title["value"]
    assert machine.focused_sid == "keep-focus"
    replay = await controller.handle(command)
    assert not replay.error
    if engine == "dsh":
        writes = [call.args[0] for call in machine._dsh.dispatch.await_args_list if isinstance(call.args[0], RenameSession)]
    else:
        writes = [call.args[0] for call in machine._handle_rename_session.await_args_list]
    assert len(writes) == 1
    assert writes[0].session_id == sid
    assert writes[0].engine == engine
    assert store.room("owner", room_id)["revision"] == 2
    conflicting = command.model_copy(update={"name": "不能复用请求编号"})
    assert (await controller.handle(conflicting)).error
    assert title["value"] == "聊天室中改名"


@pytest.mark.asyncio
async def test_native_rename_failure_keeps_actual_title_and_validates_owner_and_revision_first():
    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    command = CollaborationCommand(operation="rename_member", owner_id="owner", client_id="client", cmd_id="rename",
                                   room_id=room_id, member_id=member["id"], name="请求的新名称", revision=1)
    for changes in [{"owner_id": "foreign"}, {"revision": 9}]:
        assert (await controller.handle(command.model_copy(update=changes))).error
    machine._handle_rename_session.assert_not_awaited()
    machine._handle_rename_session.return_value = Error(code="rename_failed", message="原生重命名失败")
    machine._handle_rename_session.side_effect = None
    result = await controller.handle(command)
    assert result.error == "原生重命名失败"
    snapshot = await controller.snapshot("owner", room_id)
    assert snapshot["room"]["members"][0]["name"] == "原生会话名称"
    assert store.room("owner", room_id)["revision"] == 1


@pytest.mark.asyncio
async def test_new_session_title_waits_for_native_identity_and_survives_storage_reopen():
    machine, _transport, controller, sid, native, title = native_named_member("claude")
    prepared = await controller.prepare_binding(
        CollaborationMemberSpec(engine="claude", name="创建时设置的名称", cwd="/tmp"), "create")
    store = await controller.database()
    room_id = store.mutate("owner", "create", "create", {"name": "Room"}, prepared_members=[prepared])
    member = store.room("owner", room_id)["members"][0]
    await controller.snapshot("owner", room_id)
    machine._handle_rename_session.assert_not_awaited()
    store.bind(member["id"], sid, native, "/tmp")
    controller.store = CollaborationStore(machine.cfg.state_dir)
    snapshot = await controller.snapshot("owner", room_id)
    assert title["value"] == "创建时设置的名称"
    assert snapshot["room"]["members"][0]["name"] == title["value"]
    assert controller.store.member_by_id(member["id"])["pending_title"] is None


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_reapply_a_title_after_an_explicit_native_rename():
    machine, _transport, controller, sid, native, title = native_named_member("claude")
    prepared = await controller.prepare_binding(
        CollaborationMemberSpec(engine="claude", name="创建时的名称", cwd="/tmp"),
        "create",
    )
    store = await controller.database()
    room_id = store.mutate(
        "owner", "create", "create", {"name": "Room"}, prepared_members=[prepared]
    )
    member = store.room("owner", room_id)["members"][0]
    store.bind(member["id"], sid, native, "/tmp")
    stale = store.room("owner", room_id)
    result = await controller.handle(CollaborationCommand(
        operation="rename_member", owner_id="owner", client_id="client", cmd_id="rename",
        room_id=room_id, member_id=member["id"], name="最新的原生名称", revision=1,
    ))
    assert not result.error
    await controller.sync_native_titles(stale)
    assert title["value"] == "最新的原生名称"
    assert store.member_by_id(member["id"])["name"] == title["value"]
    machine._handle_rename_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_id_capture_during_rename_preserves_the_requested_title(monkeypatch):
    _machine, _transport, controller, sid, native, title = native_named_member("claude")
    prepared = await controller.prepare_binding(
        CollaborationMemberSpec(engine="claude", cwd="/tmp"), "create"
    )
    store = await controller.database()
    room_id = store.mutate(
        "owner", "create", "create", {"name": "Room"}, prepared_members=[prepared]
    )
    member = store.room("owner", room_id)["members"][0]
    original = store.rename_target

    def capture_after_preflight(*args):
        target = original(*args)
        store.bind(member["id"], sid, native, "/tmp")
        return target

    monkeypatch.setattr(store, "rename_target", capture_after_preflight)
    result = await controller.handle(CollaborationCommand(
        operation="rename_member", owner_id="owner", client_id="client", cmd_id="rename",
        room_id=room_id, member_id=member["id"], name="接收 ID 时的名称", revision=1,
    ))
    assert not result.error
    assert title["value"] == "接收 ID 时的名称"
    assert store.member_by_id(member["id"])["pending_title"] is None


def test_existing_collaboration_database_adds_pending_titles_without_reusing_legacy_aliases(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    with store.db() as db:
        db.execute("ALTER TABLE members DROP COLUMN pending_title")
    reopened = CollaborationStore(tmp_path)
    assert reopened.member_by_id(member["id"])["pending_title"] is None
    assert reopened.room("owner", room_id)["members"][0]["name"] == member["name"]


def test_restore_retains_binding_and_cancelled_deliveries(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    store.bind(member["id"], "wire-session", "native-session", "/tmp/original")
    delivery = send(store, room_id)
    store.mutate("owner", "remove", "remove_member", {
        "room_id": room_id, "member_id": member["id"],
    })
    store = CollaborationStore(tmp_path)
    body = {"room_id": room_id, "member_id": member["id"]}
    store.mutate("owner", "restore", "restore_member", body)
    store.mutate("owner", "restore", "restore_member", body)
    restored = store.room("owner", room_id)
    assert restored["default_member"] == member["id"]
    assert len(restored["members"]) == 1
    member = restored["members"][0]
    assert not member["removed"]
    assert (member["sid"], member["native_sid"], member["cwd"]) == (
        "wire-session", "native-session", "/tmp/original",
    )
    assert store.delivery(delivery["id"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        store.mutate("another-owner", "restore", "restore_member", body)


def test_restore_observes_active_member_limit(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, first = create_room(store)
    store.mutate("owner", "remove", "remove_member", {
        "room_id": room_id, "member_id": first["id"],
    })
    for index in range(16):
        store.mutate("owner", f"add-{index}", "add_member", {"room_id": room_id},
                     prepared_members=[(first["spec"], "account")])
    with pytest.raises(ValueError, match="16"):
        store.mutate("owner", "restore", "restore_member", {
            "room_id": room_id, "member_id": first["id"],
        })
    assert store.room("owner", room_id)["members"][0]["removed"]


def test_native_names_keep_member_targets_even_when_titles_match(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, first = create_room(store)
    store.mutate("owner", "add", "add_member", {"room_id": room_id},
                 prepared_members=[(first["spec"], "account")])
    second = store.room("owner", room_id)["members"][1]
    store.mutate("owner", "rename", "rename_member", {
        "room_id": room_id, "member_id": first["id"], "name": "代码实现",
    })
    store.mutate("owner", "send", "send", {
        "room_id": room_id, "message_id": "input", "prompt": "@代码实现 核对",
        "targets": [first["id"], second["id"]],
    })
    store.mutate("owner", "rename-again", "rename_member", {
        "room_id": room_id, "member_id": first["id"], "name": "实现会话",
    })
    assert store.messages("owner", room_id)[0]["targets"] == [first["id"], second["id"]]
    store.mutate("owner", "duplicate", "rename_member", {
        "room_id": room_id, "member_id": second["id"], "name": "实现会话",
    })
    assert store.room("owner", room_id)["members"][0]["name"] == "实现会话"
    assert store.room("owner", room_id)["members"][1]["name"] == "实现会话"
    assert first["id"] != second["id"]


def test_native_session_bindings_are_unique_and_same_account_sessions_remain_distinct(tmp_path):
    store = CollaborationStore(tmp_path)
    spec = CollaborationMemberSpec(engine="codex", name="实现", session_id="one").model_dump()
    binding = {"sid": "one", "native_sid": "one", "cwd": "/tmp"}
    room_id = store.mutate("owner", "create", "create", {"name": "Room"},
                           prepared_members=[(spec, "account", binding)])
    with pytest.raises(ValueError, match="已关联"):
        store.mutate("owner", "other", "create", {"name": "Other"},
                     prepared_members=[(spec, "account", binding)])
    assert len(store.rooms("owner")) == 1
    store.mutate("owner", "two", "add_member", {"room_id": room_id},
                 prepared_members=[({**spec, "name": "审查", "session_id": "two"}, "account",
                                    {**binding, "sid": "two", "native_sid": "two"})])
    members = store.room("owner", room_id)["members"]
    assert [member["sid"] for member in members] == ["one", "two"]
    assert members[0]["id"] != members[1]["id"]
    assert store.bound_sessions("account") == {"one", "two"}


def test_existing_session_selection_is_read_only_and_validates_the_catalog():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        native = "11111111-1111-4111-8111-111111111111"
        sid = machine._claude_wire_sid(machine._claude_profile(None), native)
        machine._handle_list_sessions = AsyncMock(return_value=SessionList(
            engine="claude", sessions=[
                SessionInfo(session_id=sid, summary="已有会话", cwd="/tmp", engine="claude"),
                SessionInfo(session_id="work", cwd="/tmp", engine="claude", space="work"),
                SessionInfo(session_id="archived", cwd="/tmp", engine="claude", tag="archived"),
            ],
        ))
        machine._spawn = AsyncMock()
        machine._handle_query = AsyncMock()
        machine.focused_sid = "keep-focus"
        spec = CollaborationMemberSpec(engine="claude", session_id=sid, cwd="/invented/path")
        prepared, _account, binding = await controller.prepare_binding(spec, "request")
        assert prepared["cwd"] == "/tmp"
        assert prepared["name"] == "已有会话"
        assert binding["native_sid"] == native
        assert machine.focused_sid == "keep-focus"
        machine._spawn.assert_not_called()
        machine._handle_query.assert_not_called()
        with pytest.raises(ValueError, match="不属于"):
            await controller.prepare_binding(CollaborationMemberSpec(
                engine="claude", session_id="unknown",
            ), "unknown-request")
        with pytest.raises(ValidationError):
            CollaborationMemberSpec(engine="claude", session_id=sid, model="override")

    asyncio.run(run())


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_existing_session_picker_keeps_native_ids_in_the_selected_account(tmp_path, engine):
    async def run():
        machine, _transport = _mk_machine()
        root_field = "config_dir" if engine == "claude" else "home"
        registry_type = ClaudeProfileRegistry if engine == "claude" else CodexProfileRegistry
        registry = registry_type.from_json(json.dumps({
            "one": {root_field: str(tmp_path / "one"), "label": "One", "default": True},
            "two": {root_field: str(tmp_path / "two"), "label": "Two"},
        }))
        setattr(machine, f"_{engine}_profiles", registry)
        native = "11111111-1111-4111-8111-111111111111"
        one = registry.wire_session_id("one", native)
        two = registry.wire_session_id("two", native)
        machine._handle_list_sessions = AsyncMock(return_value=SessionList(
            engine=engine, sessions=[
                SessionInfo(session_id=one, engine=engine, cwd="/tmp", summary="账号一"),
                SessionInfo(session_id=two, engine=engine, cwd="/tmp", summary="账号二"),
            ],
        ))
        controller = machine._collaboration
        candidates = await controller.session_candidates(CollaborationMemberSpec(
            engine=engine, profile_id="one",
        ), "list-one")
        assert [row["sid"] for row in candidates] == [one]
        with pytest.raises(ValueError, match="不属于"):
            await controller.prepare_binding(CollaborationMemberSpec(
                engine=engine, profile_id="one", session_id=two,
            ), "cross-account")

    asyncio.run(run())


def create_room(store, *, owner="owner", request="create", engine="claude"):
    spec = CollaborationMemberSpec(
        engine=engine, name=f"{engine} · default", cwd="/tmp"
    ).model_dump()
    room_id = store.mutate(
        owner, request, "create", {"name": "Room"}, prepared_members=[(spec, "account")]
    )
    return room_id, store.room(owner, room_id)["members"][0]


def send(
    store,
    room_id,
    *,
    message_id="message",
    request="send",
    owner="owner",
    prompt="hello",
):
    store.mutate(
        owner,
        request,
        "send",
        {"room_id": room_id, "message_id": message_id, "prompt": prompt},
    )
    return store.deliveries(room_id)[-1]


def test_protocol_validates_operations_and_engine_configuration():
    command = CollaborationCommand(
        operation="create",
        cmd_id="c",
        client_id="browser",
        owner_id="owner",
        name="Discussion",
        members=[{"engine": "claude"}],
    )
    assert deserialize(serialize(command)) == command
    assert is_client_message(command)
    with pytest.raises(ValidationError):
        CollaborationCommand(operation="send", cmd_id="c", client_id="browser")
    with pytest.raises(ValidationError):
        CollaborationMemberSpec(engine="claude", service_tier="fast")
    with pytest.raises(ValidationError):
        CollaborationMemberSpec(engine="dsh", profile_id="invented")


def test_owner_bound_requests_survive_restart_without_duplicate_inputs(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    store = CollaborationStore(tmp_path)
    assert send(store, room_id)["id"] == delivery["id"]
    assert len(store.messages("owner", room_id)) == 1
    assert len(store.deliveries(room_id)) == 1
    assert member["id"] == store.room("owner", room_id)["default_member"]
    with pytest.raises(ValueError):
        store.room("other-owner", room_id)
    with pytest.raises(ValueError):
        send(store, room_id, request="send", prompt="different input")
    with pytest.raises(ValueError):
        send(store, room_id, owner="other-owner", request="other")


def test_context_has_attributed_public_history_and_a_frozen_boundary(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    store.publish(member["id"], "native-one", "public answer", ["earlier"])
    delivery = send(store, room_id, prompt="current instruction")
    store.publish(member["id"], "native-later", "later answer", [])
    messages, current = store.context(delivery)
    prompt = shared_prompt(store.room("owner", room_id), member, messages, current)
    assert "public answer" in prompt and "current instruction" in prompt
    assert "later answer" not in prompt
    assert "claude · default" in prompt
    assert prompt.endswith("current instruction")


def test_stop_claim_is_durable_and_does_not_target_a_later_turn(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    first = send(store, room_id)
    store.consume(first["id"], "native-one")
    assert store.stop_pending("owner", "stop-one", room_id, member["id"])[1]
    store.finish(member["id"], "cancelled")
    second = send(store, room_id, message_id="next", request="next")
    store.consume(second["id"], "native-two")
    store = CollaborationStore(tmp_path)
    assert not store.stop_pending("owner", "stop-one", room_id, member["id"])[1]
    assert store.deliveries(room_id)[-1]["status"] == "running"


def test_public_state_excludes_attachment_payload_and_account_fingerprint(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, _member = create_room(store)
    send(store, room_id)
    assert "account_key" not in store.public_room("owner", room_id)["members"][0]
    assert "payload" not in store.public_deliveries(room_id)[0]


def test_removed_members_keep_source_identity_and_cancel_pending_inputs(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    send(store, room_id)
    store.publish(member["id"], "source", "answer", [])
    store.mutate(
        "owner",
        "remove",
        "remove_member",
        {"room_id": room_id, "member_id": member["id"]},
    )
    assert store.deliveries(room_id)[0]["status"] == "cancelled"
    assert store.room("owner", room_id)["members"][0]["removed"]
    assert store.messages("owner", room_id)[-1]["member_id"] == member["id"]


def test_idle_input_and_public_reply_use_the_original_session_without_focus_change():
    async def run():
        machine, transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="claude", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = "original-focus"
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        delivery = send(store, room_id)

        async def query(cmd):
            await machine._emit(ctx, UserMsg(msg_id=cmd.msg_id, prompt=cmd.prompt))
            await machine._emit(
                ctx, AssistantMsgStart(message_id="answer", channel="final")
            )
            await machine._emit(
                ctx, Delta(message_id="answer", channel="final", text="public result")
            )
            await machine._emit(
                ctx, AssistantMsgEnd(message_id="answer", channel="final")
            )
            await machine._emit(
                ctx,
                TurnEnd(
                    turn_id="native-turn",
                    result=TurnResult(subtype="success", is_error=False, duration_ms=1),
                ),
            )

        machine._handle_query = AsyncMock(side_effect=query)
        machine._handle_interrupt = AsyncMock()
        await controller.deliver(delivery)
        assert machine._handle_query.await_count == 1
        assert store.deliveries(room_id)[0]["status"] == "succeeded"
        assert store.messages("owner", room_id)[-1]["text"] == "public result"
        assert machine.focused_sid == "original-focus"
        machine._handle_interrupt.assert_not_awaited()
        assert not any(isinstance(frame, SessionFocus) for frame in transport.sent)
        assert "成员" in machine._handle_query.await_args.args[0].prompt

    asyncio.run(run())


@pytest.mark.parametrize(
    ("subtype", "is_error", "stopping", "expected"),
    [
        ("success", False, False, "succeeded"),
        ("success", False, True, "succeeded"),
        ("error", True, True, "failed"),
        ("error_during_execution", True, False, "failed"),
        ("error_during_execution", True, True, "cancelled"),
        ("interrupted", False, True, "cancelled"),
    ],
)
def test_room_terminal_uses_native_stop_evidence(subtype, is_error, stopping, expected):
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="claude", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner", "create", "create", {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        machine.sessions[ctx.key] = ctx
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        delivery = send(store, room_id)
        store.consume(delivery["id"], "native-turn")
        controller.watch(ctx, member["id"])
        if stopping:
            store.stop_pending("owner", "stop", room_id, member["id"])
        await machine._emit(
            ctx,
            TurnEnd(turn_id="native-turn", result=TurnResult(
                subtype=subtype, is_error=is_error, duration_ms=1,
            )),
        )
        row = store.deliveries(room_id)[0]
        assert row["status"] == expected
        assert row["error"] == (subtype if expected == "failed" else None)

    asyncio.run(run())


def test_running_inputs_use_steer_and_share_one_native_reply():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="codex", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        ctx.engine, ctx.state = "codex", "running"
        machine.sessions[ctx.key] = ctx
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        machine._handle_query = AsyncMock()
        machine._handle_interrupt = AsyncMock()

        async def steer(cmd):
            await machine._emit(
                ctx,
                TurnSteered(msg_id=cmd.msg_id, turn_id="same-turn", prompt=cmd.prompt),
            )

        machine._handle_steer = AsyncMock(side_effect=steer)
        first = send(store, room_id)
        await controller.deliver(first)
        second = send(store, room_id, message_id="second", request="second")
        await controller.deliver(second)
        await machine._emit(
            ctx, AssistantMsgStart(message_id="answer", channel="final")
        )
        await machine._emit(
            ctx, Delta(message_id="answer", text="combined answer", channel="final")
        )
        await machine._emit(ctx, AssistantMsgEnd(message_id="answer", channel="final"))
        await machine._emit(
            ctx,
            TurnEnd(
                turn_id="same-turn",
                result=TurnResult(subtype="success", is_error=False, duration_ms=1),
            ),
        )
        assert machine._handle_steer.await_count == 2
        machine._handle_query.assert_not_awaited()
        machine._handle_interrupt.assert_not_awaited()
        replies = [
            message
            for message in store.messages("owner", room_id)
            if message["role"] == "assistant"
        ]
        assert len(replies) == 1
        assert set(replies[0]["reply_to"]) == {"message", "second"}
        assert all(row["status"] == "succeeded" for row in store.deliveries(room_id))

    asyncio.run(run())


def test_accepted_but_unconsumed_steer_survives_an_earlier_terminal():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="claude", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        delivery = send(store, room_id)
        machine._handle_steer = AsyncMock()
        await controller.deliver(delivery)
        await machine._emit(
            ctx,
            TurnEnd(
                turn_id="earlier",
                result=TurnResult(subtype="success", is_error=False, duration_ms=1),
            ),
        )
        assert store.deliveries(room_id)[0]["status"] == "accepted"
        assert store.member_by_id(member["id"])["synced_seq"] == 0

    asyncio.run(run())


def test_definite_busy_rejection_can_reselect_delivery_mode():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="claude", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        machine.sessions[ctx.key] = ctx
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        delivery = send(store, room_id)
        machine._handle_query = AsyncMock(
            return_value=Error(code="busy", message="busy")
        )
        await controller.deliver(delivery)
        assert store.deliveries(room_id)[0]["status"] == "queued"
        ctx.state = "running"
        machine._handle_steer = AsyncMock()
        await controller.deliver(store.deliveries(room_id)[0])
        assert machine._handle_steer.await_count == 1
        assert store.deliveries(room_id)[0]["status"] == "accepted"

    asyncio.run(run())


def test_pending_attachments_are_shared_and_released_after_all_targets_finish(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, first = create_room(store)
    spec = CollaborationMemberSpec(engine="codex", name="Codex · iris").model_dump()
    store.mutate(
        "owner",
        "add",
        "add_member",
        {"room_id": room_id},
        prepared_members=[(spec, "account")],
    )
    second = store.room("owner", room_id)["members"][1]
    store.mutate(
        "owner",
        "attach",
        "send",
        {
            "room_id": room_id,
            "message_id": "attachments",
            "targets": [first["id"], second["id"]],
            "files": [{"filename": "note.txt", "data": "YQ=="}],
        },
    )
    rows = store.deliveries(room_id)
    assert len(rows) == 2
    assert all(row["payload"] == "{}" for row in rows)
    store.consume(rows[0]["id"])
    store.finish(first["id"], "succeeded")
    assert store.payload("attachments")["files"][0]["data"] == "YQ=="
    store.consume(rows[1]["id"])
    store.finish(second["id"], "succeeded")
    assert store.payload("attachments") == {}
    public = store.messages("owner", room_id)[0]
    assert public["attachments"] == [{"kind": "file", "name": "note.txt"}]
    assert "payload" not in public


def test_prepare_is_cancellable_and_never_resurrects_a_removed_input(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    assert store.claim(delivery["id"])
    store.stop_pending("owner", "stop", room_id, member["id"])
    assert store.submit(delivery["id"], "query") is None
    store.set_delivery(delivery["id"], "queued")
    assert store.deliveries(room_id)[0]["status"] == "cancelled"


def test_native_replay_preserves_a_persisted_stop_request(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-turn")
    store.stop_pending("owner", "stop", room_id, member["id"])
    reopened = CollaborationStore(tmp_path)
    reopened.consume(delivery["id"], "native-turn")
    assert reopened.delivery(delivery["id"])["status"] == "stopping"
    reopened.finish(member["id"], "cancelled")
    assert reopened.delivery(delivery["id"])["status"] == "cancelled"


def test_input_stopped_between_prepare_and_native_dispatch_is_not_submitted(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    store.claim(delivery["id"])
    assert store.submit(delivery["id"], "query")
    store.stop_pending("owner", "stop", room_id, member["id"])
    assert not store.can_submit(delivery["id"])
    assert store.deliveries(room_id)[0]["status"] == "cancelled"


def test_later_queued_input_uses_the_consumed_watermark_at_dispatch(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    first = send(store, room_id)
    second = send(store, room_id, message_id="second", request="second")
    store.consume(first["id"], "native-one")
    store.claim(second["id"])
    prepared = store.submit(second["id"], "steer")
    assert prepared["context_before"] == first["context_through"]
    store.finish(member["id"], "cancelled")
    store.set_delivery(second["id"], "queued")
    store.claim(second["id"])
    assert store.submit(second["id"], "query")["context_before"] == 0


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_background_creation_preserves_focus_and_native_account_defaults(engine):
    async def run():
        machine, transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine=engine, cwd="/tmp", effort="high")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        delivery = send(store, room_id)
        machine.focused_sid = "original"
        ctx = _mk_ctx("tmp-background")
        ctx.engine = engine
        if engine == "codex":
            from types import SimpleNamespace

            ctx.sdk = SimpleNamespace(
                service_tier=None, activate_runtime_events=AsyncMock()
            )

        async def spawn(**_options):
            machine.sessions[ctx.key] = ctx
            return ctx

        machine._spawn = AsyncMock(side_effect=spawn)
        machine._save_claude_user_defaults = AsyncMock()
        await controller.context(member, delivery)
        assert machine.focused_sid == "original"
        machine._save_claude_user_defaults.assert_not_awaited()
        assert not any(isinstance(frame, SessionFocus) for frame in transport.sent)
        assert machine._spawn.await_args.kwargs["effort"] == "high"
        assert store.member_by_id(member["id"])["sid"] == ctx.key

    asyncio.run(run())


def test_native_history_recovers_an_uncertain_input_without_a_second_query():
    async def run():
        from cc_remote.protocol import History, TurnBinding

        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="claude", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        delivery = send(store, room_id)
        store.bind(member["id"], "native", "native", "/tmp")
        store.set_delivery(delivery["id"], "unknown")
        events = [
            UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello"),
            TurnBinding(msg_id="native-user", turn_id="native-turn"),
            AssistantMsgStart(message_id="thinking", channel="thinking"),
            Delta(message_id="thinking", channel="thinking", text="private reasoning"),
            AssistantMsgEnd(message_id="thinking", channel="thinking"),
            AssistantMsgStart(message_id="answer", channel="final"),
            Delta(message_id="answer", channel="final", text="public answer"),
            AssistantMsgEnd(message_id="answer", channel="final"),
            TurnEnd(
                turn_id="native-turn",
                result=TurnResult(subtype="success", is_error=False, duration_ms=1),
            ),
        ]
        machine._history_page_singleflight = AsyncMock(
            return_value=History(
                session_id="native",
                revision="r",
                events=[event.model_dump() for event in events],
            )
        )
        machine._handle_query = AsyncMock()
        machine._spawn = AsyncMock()
        await controller.reconcile(store.member_by_id(member["id"]))
        assert store.deliveries(room_id)[0]["status"] == "succeeded"
        assert store.messages("owner", room_id)[-1]["text"] == "public answer"
        assert "private reasoning" not in str(store.messages("owner", room_id))
        await controller.reconcile(store.member_by_id(member["id"]))
        assert len(store.messages("owner", room_id)) == 2
        machine._handle_query.assert_not_awaited()
        machine._spawn.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("additional_reply", [False, True])
async def test_failed_reply_projection_stays_recoverable_after_terminal(
    monkeypatch, restart, additional_reply
):
    import sqlite3

    from cc_remote.protocol import History, TurnBinding
    from cc_remote.wrapper.collaboration import Collaboration

    machine, transport, controller, sid, native, _title = native_named_member("codex")
    store, room_id, member = await store_named_member(controller, "codex", sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    delivery = send(store, room_id)
    events = [
        UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello"),
        TurnBinding(msg_id="native-user", turn_id="native-turn"),
        AssistantMsgStart(message_id="answer", turn_id="native-turn", channel="final"),
        Delta(message_id="answer", turn_id="native-turn", channel="final", text="recovered reply"),
        AssistantMsgEnd(message_id="answer", turn_id="native-turn", channel="final"),
    ]
    if additional_reply:
        events.extend([
            AssistantMsgStart(message_id="second-answer", turn_id="native-turn", channel="final"),
            Delta(message_id="second-answer", turn_id="native-turn", channel="final", text="another reply"),
            AssistantMsgEnd(message_id="second-answer", turn_id="native-turn", channel="final"),
        ])
    terminal = TurnEnd(
        turn_id="native-turn",
        result=TurnResult(subtype="success", is_error=False, duration_ms=1),
    )
    events.append(terminal)
    publish = store.publish
    failures = 1

    def fail_once(*args):
        nonlocal failures
        if failures:
            failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return publish(*args)

    monkeypatch.setattr(store, "publish", fail_once)
    for event in events:
        await machine._emit(ctx, event)
    assert any(isinstance(frame, TurnEnd) for frame in transport.sent)
    assert store.delivery(delivery["id"])["status"] == "running"
    assert [row["id"] for row in store.deliveries(room_id, active=True)] == [delivery["id"]]

    if restart:
        controller = Collaboration(machine)
        machine._collaboration = controller
        store = await controller.database()
        monkeypatch.setattr(controller, "schedule_reconcile", lambda _member: None)
        await controller.restore_observers()
        # A terminal replay may arrive before native-history repair on restart.
        await machine._emit(ctx, terminal)
        assert store.delivery(delivery["id"])["status"] == "running"

    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id=sid, revision="recovered", live_seq=ctx.seq,
        events=[event.model_dump() for event in events],
    ))
    machine._handle_query = AsyncMock()
    machine._handle_steer = AsyncMock()
    await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert {row["text"] for row in replies} == (
        {"recovered reply", "another reply"} if additional_reply else {"recovered reply"}
    )
    assert all(row["reply_to"] == ["message"] for row in replies)
    assert store.delivery(delivery["id"])["status"] == "succeeded"
    assert not store.deliveries(room_id, active=True)
    await controller.reconcile(store.member_by_id(member["id"]))
    assert len(store.messages("owner", room_id)) == 1 + len(replies)
    machine._handle_query.assert_not_awaited()
    machine._handle_steer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_live_seq", [False, True])
@pytest.mark.parametrize("admission_frame", ["user_msg", "assistant_msg_end"])
async def test_history_replay_cannot_publish_or_complete_new_live_input(
    monkeypatch, has_live_seq, admission_frame
):
    from cc_remote.protocol import History, TurnBinding

    machine, _transport, controller, sid, native, _title = native_named_member("codex")
    store, room_id, member = await store_named_member(controller, "codex", sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    first = send(store, room_id)
    store.consume(first["id"], "old-turn")
    second = send(store, room_id, message_id="second", request="second")
    events = [
        UserMsg(msg_id="old-user", client_msg_id=first["id"], prompt="old input"),
        TurnBinding(msg_id="old-user", turn_id="old-turn"),
        AssistantMsgStart(message_id="old-answer", turn_id="old-turn", channel="final"),
        Delta(message_id="old-answer", turn_id="old-turn", channel="final", text="old reply"),
        AssistantMsgEnd(message_id="old-answer", turn_id="old-turn", channel="final"),
        TurnEnd(turn_id="old-turn", result=TurnResult(subtype="success", is_error=False, duration_ms=1)),
    ]
    live_input = [
        UserMsg(msg_id="new-user", client_msg_id=second["id"], prompt="new input"),
        TurnBinding(msg_id="new-user", turn_id="new-turn"),
    ]
    history = History(
        session_id=sid, revision="old", live_seq=ctx.seq if has_live_seq else None,
        events=[event.model_dump() for event in events],
    )
    machine._history_page_singleflight = AsyncMock(return_value=history)
    observe = controller.observe
    admitted = False

    async def admit_during_replay(context, frame, **kwargs):
        nonlocal admitted
        if context is not ctx and frame.type == admission_frame and not admitted:
            admitted = True
            for event in live_input:
                await machine._emit(ctx, event)
        return await observe(context, frame, **kwargs)

    monkeypatch.setattr(controller, "observe", admit_during_replay)
    await controller.reconcile(store.member_by_id(member["id"]))
    assert admitted
    assert store.delivery(second["id"])["status"] == "running"
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert all(row["reply_to"] == ["message"] for row in replies)

    # A later read contains both turns. Only the old turn has a terminal.
    history.live_seq = ctx.seq if has_live_seq else None
    history.events.extend(event.model_dump() for event in live_input)
    history.in_progress = True
    await controller.reconcile(store.member_by_id(member["id"]))
    assert store.delivery(first["id"])["status"] == "succeeded"
    assert store.delivery(second["id"])["status"] == "running"
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == "old reply"
    assert replies[0]["reply_to"] == ["message"]


def test_scoped_terminal_keeps_other_native_deliveries_running(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    first = send(store, room_id)
    second = send(store, room_id, message_id="second", request="second")
    store.consume(first["id"], "old-turn")
    store.consume(second["id"], "new-turn")
    store.finish(member["id"], "succeeded", delivery_ids={first["id"]})
    assert store.delivery(first["id"])["status"] == "succeeded"
    assert store.delivery(second["id"])["status"] == "running"
    store.finish(member["id"], "succeeded", delivery_ids=set())
    assert store.delivery(second["id"])["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,claude_checkpoint", [
    ("claude", True), ("claude", False), ("codex", False), ("dsh", False),
])
async def test_native_terminal_matches_its_owner_and_preserves_a_newer_turn(engine, claude_checkpoint):
    machine, _transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    first = send(store, room_id)
    second = send(store, room_id, message_id="second", request="second")
    store.consume(first["id"], "old-turn")
    store.consume(second["id"], "new-turn")
    terminal = TurnEnd(
        turn_id="assistant-fork" if engine == "claude" else "old-turn",
        checkpoint_id="old-turn" if claude_checkpoint else None,
        result=TurnResult(subtype="success", is_error=False, duration_ms=1),
    )
    if engine == "claude" and not claude_checkpoint:
        terminal._changes_turn_id = "old-turn"
    await machine._emit(ctx, terminal)
    assert store.delivery(first["id"])["status"] == "succeeded"
    assert store.delivery(second["id"])["status"] == "running"


@pytest.mark.asyncio
async def test_abandoned_history_projection_keeps_the_live_text_buffer(monkeypatch):
    from cc_remote.protocol import History, TurnBinding

    machine, _transport, controller, sid, native, _title = native_named_member("codex")
    store, room_id, member = await store_named_member(controller, "codex", sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-turn")
    start = AssistantMsgStart(message_id="answer", turn_id="native-turn", channel="final")
    prefix = Delta(message_id="answer", turn_id="native-turn", channel="final", text="prefix")
    await machine._emit(ctx, start)
    await machine._emit(ctx, prefix)
    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id=sid, revision="in-progress", live_seq=ctx.seq, in_progress=True,
        events=[event.model_dump() for event in [
            UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello"),
            TurnBinding(msg_id="native-user", turn_id="native-turn"), start, prefix,
        ]],
    ))
    observe = controller.observe
    advanced = False

    async def advance_during_replay(context, frame, **kwargs):
        nonlocal advanced
        result = await observe(context, frame, **kwargs)
        if context is not ctx and frame.type == "assistant_msg_start" and not advanced:
            advanced = True
            await machine._emit(ctx, Delta(
                message_id="answer", turn_id="native-turn", channel="final", text="suffix",
            ))
        return result

    monkeypatch.setattr(controller, "observe", advance_during_replay)
    await controller.reconcile(store.member_by_id(member["id"]))
    assert advanced
    await machine._emit(ctx, AssistantMsgEnd(
        message_id="answer", turn_id="native-turn", channel="final",
    ))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == "prefixsuffix"
    assert replies[0]["reply_to"] == ["message"]


@pytest.mark.asyncio
async def test_native_followup_recovery_uses_its_owner_instead_of_the_original_input():
    from cc_remote.protocol import History, TurnBinding

    machine, _transport, controller, sid, native, _title = native_named_member("dsh")
    store, room_id, member = await store_named_member(controller, "dsh", sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = "dsh"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    delivery = send(store, room_id)
    store.consume(delivery["id"], "old-turn")
    store.finish(member["id"], "succeeded")
    binding = TurnBinding(
        msg_id="followup-turn", turn_id="followup-turn", autonomous=True, continuation="subagent",
    )
    await machine._emit(ctx, binding)
    assert store.delivery(delivery["id"])["status"] == "running"
    assert store.delivery(delivery["id"])["native_turn"] == "followup-turn"
    events = [
        UserMsg(msg_id="old-user", client_msg_id=delivery["id"], prompt="hello"),
        TurnBinding(msg_id="old-user", turn_id="old-turn"),
        AssistantMsgStart(message_id="old-answer", turn_id="old-turn", channel="final"),
        Delta(message_id="old-answer", turn_id="old-turn", channel="final", text="old reply"),
        AssistantMsgEnd(message_id="old-answer", turn_id="old-turn", channel="final"),
        TurnEnd(turn_id="old-turn", result=TurnResult(subtype="success", is_error=False, duration_ms=1)),
        binding,
        AssistantMsgStart(message_id="new-answer", turn_id="followup-turn", channel="final"),
        Delta(message_id="new-answer", turn_id="followup-turn", channel="final", text="followup reply"),
        AssistantMsgEnd(message_id="new-answer", turn_id="followup-turn", channel="final"),
        TurnEnd(turn_id="followup-turn", result=TurnResult(subtype="success", is_error=False, duration_ms=1)),
    ]
    machine._dsh.connection = AsyncMock()
    machine._dsh.handle_get_history = AsyncMock(return_value=History(
        session_id=sid, revision="followup", live_seq=ctx.seq,
        events=[event.model_dump() for event in events],
    ))
    await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == "followup reply"
    assert replies[0]["reply_to"] == ["message"]
    assert store.delivery(delivery["id"])["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("oversized", ["reply", "large_reply", "tool"])
@pytest.mark.parametrize("detail_state", ["missing", "lossy", "changed", "complete"])
async def test_lossy_native_history_recovers_complete_reply_or_keeps_delivery_active(
    monkeypatch, tmp_path, engine, oversized, detail_state
):
    import cc_remote.wrapper.machine as machine_module
    from cc_remote.protocol import ToolResult, ToolUse, TurnBinding
    from cc_remote.wrapper.codex_stream import CodexHistoryWindow
    from cc_remote.wrapper.history_store import HistoryIndexStore

    machine, transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    delivery = send(store, room_id)
    owner = "native-turn" if engine == "codex" else "native-user"
    store.consume(delivery["id"], owner)
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    machine.sessions[sid] = ctx
    repeats = {"reply": 15000, "large_reply": 150000, "tool": 1}
    answer = "complete reply " * repeats[oversized]
    events = [UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello")]
    if engine == "codex":
        events.append(TurnBinding(msg_id="native-user", turn_id=owner))
    if oversized == "tool":
        events.extend([
            ToolUse(message_id="tool-message", tool_use_id="tool", tool="read_file", input={"text": "x" * 200000}),
            ToolResult(tool_use_id="tool", content="x" * 200000, is_error=False),
        ])
    events.extend([
        AssistantMsgStart(message_id="answer", turn_id=owner, channel="final"),
        Delta(message_id="answer", turn_id=owner, channel="final", text=answer),
        AssistantMsgEnd(message_id="answer", turn_id=owner, channel="final"),
        TurnEnd(turn_id=owner if engine == "codex" else "assistant-fork", checkpoint_id="native-user" if engine == "claude" else None,
                result=TurnResult(subtype="success", is_error=False, duration_ms=1)),
    ])
    source = tmp_path / "transcript.jsonl"
    source.write_text("{}\n")
    monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: str(source))
    monkeypatch.setattr(machine_module, "get_session_messages", lambda *_args, **_kwargs: ["source"])
    monkeypatch.setattr(machine_module, "last_assistant_model", lambda _messages: None)
    monkeypatch.setattr(machine_module, "translate_history", lambda *_args, **_kwargs: [event.model_copy(deep=True) for event in events])
    if engine == "codex":
        machine._codex_rollout_for_wire = lambda _sid: str(source)
        monkeypatch.setattr(machine_module, "codex_history_window_info", lambda *_args, **_kwargs: CodexHistoryWindow(0, source.stat().st_size, False))
        monkeypatch.setattr(machine_module, "codex_translate_history", lambda *_args, **_kwargs: ([event.model_copy(deep=True) for event in events], None))
    machine.cfg.ws_max_size_bytes = 64 * 1024
    machine._history_index = HistoryIndexStore(tmp_path / "history")
    history = await machine._build_history_source(sid, limit=16)
    assert history.authoritative and history.error is None
    assert any(event["type"] == "error" for event in history.events)
    assert not any(event["type"] == "delta" for event in history.events)
    machine._history_page_singleflight = AsyncMock(return_value=history)
    read_detail = machine._history_index.get_turn_detail
    if detail_state == "missing":
        monkeypatch.setattr(machine._history_index, "get_turn_detail", lambda *_args: None)
    elif detail_state == "lossy":
        monkeypatch.setattr(machine._history_index, "get_turn_detail", lambda *_args: history.events)
    elif detail_state == "changed":
        def append_during_read(*args):
            detail = read_detail(*args)
            with source.open("a") as stream:
                stream.write("{}\n")
            return detail
        monkeypatch.setattr(machine._history_index, "get_turn_detail", append_during_read)
    machine._handle_query = AsyncMock()
    machine._handle_steer = AsyncMock()
    machine._spawn = AsyncMock()
    await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    if detail_state == "complete":
        assert len(replies) == 1
        assert replies[0]["reply_to"] == ["message"]
        assert store.delivery(delivery["id"])["status"] == "succeeded"
    else:
        assert not replies
        assert store.delivery(delivery["id"])["status"] == "running"
        assert store.deliveries(room_id, active=True)
        monkeypatch.setattr(machine._history_index, "get_turn_detail", read_detail)
        await controller.reconcile(store.member_by_id(member["id"]))
        assert store.delivery(delivery["id"])["status"] == "succeeded"
    # The room page is a bounded preview. Validate the complete persisted body.
    with store.db() as db:
        assert db.execute("SELECT text FROM messages WHERE role='assistant'").fetchone()[0] == answer
    assert not transport.sent
    machine._handle_query.assert_not_awaited()
    machine._handle_steer.assert_not_awaited()
    machine._spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded_frame", [False, True])
async def test_native_recovery_pages_back_to_a_delivery_older_than_sixteen_turns(
    monkeypatch, tmp_path, bounded_frame
):
    import cc_remote.wrapper.machine as machine_module
    from cc_remote.protocol import GetHistory
    from cc_remote.wrapper.history_store import HistoryIndexStore

    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-user-0")
    ctx = _mk_ctx(sid, native)
    machine.sessions[sid] = ctx
    events = []
    for index in range(40):
        events.extend([
            UserMsg(msg_id=f"native-user-{index}", client_msg_id=delivery["id"] if index == 0 else None, prompt=f"question {index}"),
            AssistantMsgStart(message_id=f"answer-{index}", turn_id=f"native-user-{index}", channel="final"),
            Delta(message_id=f"answer-{index}", turn_id=f"native-user-{index}", channel="final", text=f"reply {index} " * 1000),
            AssistantMsgEnd(message_id=f"answer-{index}", turn_id=f"native-user-{index}", channel="final"),
            TurnEnd(turn_id=f"fork-{index}", checkpoint_id=f"native-user-{index}",
                    result=TurnResult(subtype="success", is_error=False, duration_ms=1)),
        ])
    source = tmp_path / "transcript.jsonl"
    source.write_text("{}\n")
    monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: str(source))
    monkeypatch.setattr(machine_module, "get_session_messages", lambda *_args, **_kwargs: ["source"])
    monkeypatch.setattr(machine_module, "last_assistant_model", lambda _messages: None)
    monkeypatch.setattr(machine_module, "translate_history", lambda *_args, **_kwargs: [event.model_copy(deep=True) for event in events])
    machine._history_index = HistoryIndexStore(tmp_path / "history")
    if bounded_frame:
        machine.cfg.ws_max_size_bytes = 64 * 1024
    newest = await machine._history_page_singleflight(GetHistory(session_id=sid, limit=16), sid)
    assert newest.has_more
    assert not any(event.get("msg_id") == "native-user-0" for event in newest.events)
    read_page = machine._history_page_singleflight
    machine._history_page_singleflight = AsyncMock(side_effect=read_page)
    machine._handle_query = AsyncMock()
    machine._spawn = AsyncMock()
    await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == "reply 0 " * 1000
    assert replies[0]["reply_to"] == ["message"]
    assert store.delivery(delivery["id"])["status"] == "succeeded"
    assert any(call.args[0].before == newest.oldest_id for call in machine._history_page_singleflight.await_args_list)
    machine._handle_query.assert_not_awaited()
    machine._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_older_input_alias_binds_a_terminal_on_a_newer_history_page():
    from cc_remote.protocol import History, TurnBinding

    machine, _transport, controller, sid, native, _title = native_named_member("codex")
    store, room_id, member = await store_named_member(controller, "codex", sid, native)
    delivery = send(store, room_id)
    store.set_delivery(delivery["id"], "unknown")
    newest = History(session_id=sid, revision="stable", has_more=True, oldest_id="older", events=[
        TurnBinding(msg_id="native-turn", turn_id="native-turn").model_dump(),
        AssistantMsgStart(message_id="answer", turn_id="native-turn", channel="final").model_dump(),
        Delta(message_id="answer", turn_id="native-turn", channel="final", text="complete reply").model_dump(),
        AssistantMsgEnd(message_id="answer", turn_id="native-turn", channel="final").model_dump(),
        TurnEnd(turn_id="native-turn", result=TurnResult(subtype="success", is_error=False, duration_ms=1)).model_dump(),
    ])
    oldest = History(session_id=sid, revision="stable", before="older", events=[
        UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello").model_dump(),
        TurnBinding(msg_id="native-user", turn_id="native-turn").model_dump(),
        TurnEnd(turn_id="native-turn", result=TurnResult(subtype="steered", is_error=False, duration_ms=1)).model_dump(),
    ])

    async def read_page(command, _sid):
        return oldest if command.before else newest

    machine._history_page_singleflight = AsyncMock(side_effect=read_page)
    await controller.reconcile(store.member_by_id(member["id"]))
    assert store.delivery(delivery["id"])["status"] == "succeeded"
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["text"] == "complete reply"
    assert replies[0]["reply_to"] == ["message"]
    assert [call.args[0].before for call in machine._history_page_singleflight.await_args_list] == [None, "older", None]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["missing_cursor", "repeated_cursor", "revision", "error", "non_authoritative", "session"])
async def test_invalid_history_page_boundaries_keep_recovery_eligible(boundary):
    from cc_remote.protocol import History

    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "not-on-these-pages")
    newest = History(session_id=sid, revision="stable", has_more=True,
                     oldest_id=None if boundary == "missing_cursor" else "older")
    oldest = History(session_id="another-session" if boundary == "session" else sid, revision="different" if boundary == "revision" else "stable",
                     before="older", has_more=True, oldest_id="older",
                     error="read failed" if boundary == "error" else None,
                     authoritative=boundary != "non_authoritative")

    async def read_page(command, _sid):
        return oldest if command.before else newest

    machine._history_page_singleflight = AsyncMock(side_effect=read_page)
    await controller.reconcile(store.member_by_id(member["id"]))
    assert store.delivery(delivery["id"])["status"] == "running"
    assert store.deliveries(room_id, active=True)
    assert len(store.messages("owner", room_id)) == 1
    reads = [call.args[0].before for call in machine._history_page_singleflight.await_args_list]
    assert reads[0] is None
    assert len(reads) <= 3
    if boundary != "missing_cursor":
        assert reads[1] == "older"


@pytest.mark.asyncio
async def test_shared_history_read_keeps_its_earlier_live_watermark():
    from cc_remote.protocol import History

    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-user")
    ctx = _mk_ctx(sid, native)
    ctx.seq = 5
    machine.sessions[sid] = ctx
    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id=sid, revision="stale", live_seq=4, events=[
            UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello").model_dump(),
            TurnEnd(turn_id="fork", checkpoint_id="native-user",
                    result=TurnResult(subtype="success", is_error=False, duration_ms=1)).model_dump(),
        ],
    ))
    await controller.reconcile(store.member_by_id(member["id"]))
    assert store.delivery(delivery["id"])["status"] == "running"


async def emit_native_claude_reply(machine, ctx, stream_id, canonical_id, *, ambiguous=False):
    from claude_agent_sdk.types import AssistantMessage, ResultMessage, StreamEvent, TextBlock
    from cc_remote.wrapper.stream import StreamTranslator

    translator = StreamTranslator(10000, turn_id="native-user")
    frames = translator.feed(StreamEvent(
        uuid=stream_id, session_id=ctx.session_id,
        event={"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "same answer"}},
    ))
    frames.extend(translator.feed(AssistantMessage(
        content=[TextBlock(text="same answer")], model="claude-test", uuid=canonical_id,
        stop_reason=None if ambiguous else "end_turn",
    )))
    if ambiguous:
        completed = translator.feed(ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1,
            is_error=False, num_turns=1, session_id=ctx.session_id,
        ))
        frames.extend(frame for frame in completed if isinstance(frame, AssistantMsgEnd))
    for frame in frames:
        await machine._emit(ctx, frame)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_claude_live_reply_keeps_its_canonical_identity_after_recovery(monkeypatch, restart, ambiguous):
    from types import SimpleNamespace
    from cc_remote.protocol import History
    from cc_remote.wrapper.collaboration import Collaboration
    from cc_remote.wrapper.stream import translate_history

    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-user")
    ctx = _mk_ctx(sid, native)
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    canonical = "12121212-1212-4212-8212-121212121212"
    await emit_native_claude_reply(
        machine, ctx, "34343434-3434-4434-8434-343434343434", canonical,
        ambiguous=ambiguous,
    )
    initial = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(initial) == 1
    version = store.room("owner", room_id)["context_version"]
    if restart:
        controller = Collaboration(machine)
        machine._collaboration = controller
        store = await controller.database()
        monkeypatch.setattr(controller, "schedule_reconcile", lambda _member: None)
        await controller.restore_observers()
    frames = translate_history([
        SimpleNamespace(type="user", uuid="native-user", message={"role": "user", "content": "hello"}),
        SimpleNamespace(type="assistant", uuid=canonical, parent_tool_use_id=None,
                        message={"role": "assistant", "content": [{"type": "text", "text": "same answer"}]}),
    ], 10000, client_message_ids={"native-user": delivery["id"]})
    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id=sid, revision="native", live_seq=ctx.seq,
        events=[frame.model_dump() for frame in frames],
    ))
    await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert replies[0]["id"] == initial[0]["id"]
    assert replies[0]["source"] == f"claude:{member['id']}:{canonical}"
    assert store.room("owner", room_id)["context_version"] == version
    assert store.delivery(delivery["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_equal_claude_reply_text_keeps_distinct_native_message_identities():
    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-user")
    ctx = _mk_ctx(sid, native)
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    for stream, canonical in [
        ("34343434-3434-4434-8434-343434343434", "12121212-1212-4212-8212-121212121212"),
        ("78787878-7878-4878-8878-787878787878", "56565656-5656-4656-8656-565656565656"),
    ]:
        await emit_native_claude_reply(machine, ctx, stream, canonical)
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert len(replies) == 2
    assert replies[0]["text"] == replies[1]["text"] == "same answer"
    assert len({row["source"] for row in replies}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["live", "history"])
@pytest.mark.parametrize("phase", [None, "commentary", "final_answer"])
@pytest.mark.parametrize("has_questions", [False, True])
async def test_native_async_progress_stays_out_of_public_replies(tmp_path, source, phase, has_questions):
    from cc_remote.protocol import History, TurnBinding
    from cc_remote.wrapper.codex_stream import CodexStreamTranslator, codex_translate_history

    machine, _transport, controller, sid, native, _title = native_named_member("codex")
    store, room_id, member = await store_named_member(controller, "codex", sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "task")
    ctx = _mk_ctx(sid, native)
    ctx.engine = "codex"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    text = "Which directory?" if has_questions else "Still working"
    questions = [{"title": text, "options": ["Here", "Other"]}] if has_questions else None
    terminal = TurnEnd(turn_id="task", result=TurnResult(subtype="success", is_error=False, duration_ms=1))
    if source == "live":
        translator = CodexStreamTranslator(8000)
        frames = translator.feed({"method": "item/completed", "params": {
            "threadId": "thread", "turnId": "task", "item": {
                "id": "notice", "type": "agentMessage", "phase": phase,
                "text": text, "delivery": "async", "questions": questions,
            },
        }})
        for frame in frames:
            await machine._emit(ctx, frame)
        # A later channel promotion may omit the earlier delivery metadata.
        await machine._emit(ctx, AssistantMsgEnd(message_id="notice", turn_id="task", channel="final"))
        assert store.delivery(delivery["id"])["status"] == "running"
        expected = ["Which directory?\n- Here\n- Other"] if has_questions else []
        assert [row["text"] for row in store.messages("owner", room_id) if row["role"] == "assistant"] == expected
        for frame in translator.feed({"method": "item/completed", "params": {
            "threadId": "thread", "turnId": "task", "item": {
                "id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "Done",
            },
        }}):
            await machine._emit(ctx, frame)
        await machine._emit(ctx, terminal)
    else:
        rows = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "task"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": text, "phase": phase,
                                                "delivery": "async", "questions": questions}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Done", "phase": "final_answer"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "task"}},
        ]
        path = tmp_path / "rollout.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        native_frames, _ = codex_translate_history(str(path), 8000)
        frames = [
            UserMsg(msg_id="native-user", client_msg_id=delivery["id"], prompt="hello"),
            TurnBinding(msg_id="native-user", turn_id="task"),
            *[frame for frame in native_frames if isinstance(frame, (AssistantMsgStart, Delta, AssistantMsgEnd))],
            terminal,
        ]
        machine._history_page_singleflight = AsyncMock(return_value=History(
            session_id=sid, revision="native", live_seq=ctx.seq,
            events=[frame.model_dump() for frame in frames],
        ))
        await controller.reconcile(store.member_by_id(member["id"]))
    replies = [row for row in store.messages("owner", room_id) if row["role"] == "assistant"]
    assert [row["text"] for row in replies] == (["Which directory?\n- Here\n- Other", "Done"] if has_questions else ["Done"])
    assert store.delivery(delivery["id"])["status"] == "succeeded"


def test_native_preflight_rejection_updates_the_pending_room_input():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        room_id, member = create_room(store)
        delivery = send(store, room_id)
        store.set_delivery(delivery["id"], "accepted")
        ctx = _mk_ctx("native", "native")
        controller.watch(ctx, member["id"])
        await machine._emit(
            ctx, Error(code="busy", msg_id=delivery["id"], message="本次未发送")
        )
        assert store.deliveries(room_id)[0]["status"] == "queued"
        assert store.member_by_id(member["id"])["synced_seq"] == 0

    asyncio.run(run())


def test_submission_outcome_unknown_keeps_its_original_native_identity():
    async def run():
        machine, _transport = _mk_machine()
        controller = machine._collaboration
        store = await controller.database()
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine="codex", cwd="/tmp")
        )
        room_id = store.mutate(
            "owner",
            "create",
            "create",
            {"name": "Room"},
            prepared_members=[(spec, account)],
        )
        member = store.room("owner", room_id)["members"][0]
        ctx = _mk_ctx("native", "native")
        ctx.engine, ctx.state = "codex", "running"
        machine.sessions[ctx.key] = ctx
        store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
        delivery = send(store, room_id)
        machine._handle_steer = AsyncMock(
            return_value=Error(code="steer_outcome_unknown", message="unknown")
        )
        await controller.deliver(delivery)
        assert store.deliveries(room_id)[0]["status"] == "unknown"
        await controller.deliver(store.deliveries(room_id)[0])
        assert machine._handle_steer.await_count == 1

    asyncio.run(run())


def test_background_continuation_reopens_only_its_recorded_room_source(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-user")
    store.finish(member["id"], "succeeded")
    store.resume_source(member["id"], ["foreign-turn"])
    assert store.deliveries(room_id)[0]["status"] == "succeeded"
    store.resume_source(member["id"], ["native-user"])
    assert store.deliveries(room_id)[0]["status"] == "running"
    store.finish(member["id"], "succeeded")
    store.clear_source(member["id"])
    store.resume_source(member["id"], autonomous=True)
    assert store.deliveries(room_id)[0]["status"] == "succeeded"


def test_collaboration_snapshot_captures_wal_and_supports_legacy_manifests(tmp_path):
    import json
    import sqlite3
    from deploy.work_registry_snapshot import create_snapshot, restore_snapshot

    store = CollaborationStore(tmp_path / "state")
    room_id, _member = create_room(store)
    writer = sqlite3.connect(store.path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE rooms SET name='WAL room' WHERE id=?", (room_id,))
    writer.commit()
    roots = {engine: tmp_path / f"{engine}-work" for engine in ("claude", "codex")}
    snapshot = tmp_path / "snapshot"
    manifest_path = create_snapshot(snapshot, roots, state_dir=store.path.parent)
    writer.close()
    store.mutate("owner", "rename", "update", {"room_id": room_id, "name": "changed"})
    restore_snapshot(snapshot)
    assert store.room("owner", room_id)["name"] == "WAL room"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = 4
    del manifest["wrapper_state"]["files"]["collaboration.sqlite3"]
    manifest_path.write_text(json.dumps(manifest))
    store.mutate(
        "owner", "rename-next", "update", {"room_id": room_id, "name": "new room"}
    )
    restore_snapshot(snapshot)
    assert store.room("owner", room_id)["name"] == "new room"


@pytest.mark.asyncio
@pytest.mark.parametrize("cold", [False, True])
async def test_dsh_room_uses_native_create_query_and_steer_without_focus_change(cold):
    from pathlib import Path
    from tests.test_dsh_runtime import setup_runtime

    machine, transport, runtime, client = setup_runtime()
    machine.cfg.dsh_connection_file = Path("/tmp/dsh-connection")
    runtime.activate = AsyncMock()
    if cold:
        runtime.client = None

        async def connection():
            runtime.client = client
            return client

        runtime.connection = AsyncMock(side_effect=connection)
    controller = machine._collaboration
    store = await controller.database()
    spec, account = controller.prepare_member(
        CollaborationMemberSpec(engine="dsh", cwd="/tmp", dsh_agent_preset="standard")
    )
    room_id = store.mutate(
        "owner",
        "create",
        "create",
        {"name": "DSH room"},
        prepared_members=[(spec, account)],
    )
    machine.focused_sid = "original"
    delivery = send(store, room_id)
    await controller.deliver(delivery)
    member = store.room("owner", room_id)["members"][0]
    assert member["sid"] == "dsh@created"
    assert machine.focused_sid == "original"
    assert not any(isinstance(frame, SessionFocus) for frame in transport.sent)
    second = send(store, room_id, message_id="second", request="second")
    await controller.deliver(second)
    calls = [call[1]["request"] for call in client.calls if call[0] == "session/prompt"]
    assert [call["mode"] for call in calls] == ["queue", "steer"]
    assert [call["requestId"] for call in calls] == [delivery["id"], second["id"]]
    assert "成员包括" in calls[0]["content"][0]["text"]
    if cold:
        runtime.connection.assert_awaited()
    await runtime.close()


@pytest.mark.asyncio
async def test_dsh_room_catalog_connects_a_cold_native_runtime():
    from pathlib import Path
    from tests.test_dsh_runtime import setup_runtime

    machine, _transport, runtime, client = setup_runtime()
    machine.cfg.dsh_connection_file = Path("/tmp/dsh-connection")
    runtime.client = None

    async def connection():
        runtime.client = client
        return client

    runtime.connection = AsyncMock(side_effect=connection)
    result = await machine._collaboration.catalog(
        CollaborationCommand(
            operation="catalog",
            member={"engine": "dsh", "cwd": "/tmp"},
            client_id="browser",
            cmd_id="cold-catalog",
            owner_id="owner",
        )
    )
    assert result.data["catalog"]["engine"] == "dsh"
    assert result.data["catalog"]["models"]
    runtime.connection.assert_awaited_once()
    assert not any(call[0] == "session/prompt" for call in client.calls)
    assert not machine.sessions
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("cold_connection", [False, True])
async def test_room_stop_restores_a_cold_dsh_context(running, cold_connection):
    from pathlib import Path
    from unittest.mock import Mock
    from tests.test_dsh_runtime import setup_runtime, long_native_snapshot

    machine, transport, runtime, client = setup_runtime()
    machine.cfg.dsh_connection_file = Path("/tmp/dsh-connection")
    snapshot = await long_native_snapshot(client, completed=not running, steps=1)
    client.history_snapshot = AsyncMock(return_value=snapshot)
    runtime.start_follow = Mock()
    if cold_connection:
        runtime.client = None

        async def connection():
            runtime.client = client
            return client

        runtime.connection = AsyncMock(side_effect=connection)
    controller = machine._collaboration
    store = await controller.database()
    await controller.start()
    spec, account = controller.prepare_member(
        CollaborationMemberSpec(engine="dsh", cwd="/tmp")
    )
    room_id = store.mutate(
        "owner", "create", "create", {"name": "DSH room"},
        prepared_members=[(spec, account)],
    )
    member = store.room("owner", room_id)["members"][0]
    store.bind(member["id"], "dsh@session", "session", "/tmp")
    delivery = send(store, room_id)
    store.consume(delivery["id"], "dsh-seq-1")
    machine.focused_sid = "original"
    command = CollaborationCommand(
        operation="stop", room_id=room_id, member_id=member["id"],
        cmd_id="stop", client_id="browser", owner_id="owner",
    )
    try:
        result = await controller.handle(command)
        assert result.error is None
        cancelled = [call for call in client.calls if call[0] == "session/cancel"]
        assert len(cancelled) == int(running)
        if running:
            assert cancelled[0][1]["request"] == {"sessionId": "session"}
            await machine._emit(machine.sessions["dsh@session"], TurnEnd(
                turn_id="dsh-seq-1",
                result=TurnResult(subtype="interrupted", is_error=False, duration_ms=1),
            ))
            assert store.delivery(delivery["id"])["status"] == "cancelled"
        assert not any(call[0] in {"session/create", "session/prompt"} for call in client.calls)
        assert machine.focused_sid == "original"
        assert not any(isinstance(frame, SessionFocus) for frame in transport.sent)
    finally:
        await controller.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("running", [False, True])
async def test_room_stop_restores_a_cold_native_context(engine, running):
    from types import SimpleNamespace

    machine, transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-turn")
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    ctx.space = "code"
    ctx.cwd = "/tmp"
    ctx.state = "running" if running else "idle"
    ctx.sdk = SimpleNamespace(interrupt=AsyncMock(), query=AsyncMock())

    async def resume(**_options):
        machine.sessions[ctx.key] = ctx
        return ctx

    machine._spawn = AsyncMock(side_effect=resume)
    machine._handle_new_session = AsyncMock()
    machine._handle_query = AsyncMock()
    machine.focused_sid = "original"
    command = CollaborationCommand(
        operation="stop", room_id=room_id, member_id=member["id"],
        cmd_id="stop", client_id="browser", owner_id="owner",
    )
    await machine._dispatch_incoming_command(command)
    await machine._command_scheduler.drain()
    result = next(frame for frame in reversed(transport.sent)
                  if frame.type == "collaboration_state" and frame.request_id == "stop")
    assert result.error is None
    machine._spawn.assert_awaited_once_with(
        resume_id=native, raise_on_failure=True, engine=engine,
        cwd="/tmp", **{f"{engine}_profile_id": member["spec"]["profile_id"]},
    )
    assert ctx.sdk.interrupt.await_count == int(running)
    assert store.delivery(delivery["id"])["status"] == "stopping"
    assert controller.members[id(ctx)][1] == member["id"]
    assert machine.focused_sid == "original"
    assert not any(isinstance(frame, SessionFocus) for frame in transport.sent)
    ctx.sdk.query.assert_not_awaited()
    machine._handle_query.assert_not_awaited()
    machine._handle_new_session.assert_not_awaited()
    replay = await controller.handle(command)
    assert replay.error is None
    assert ctx.sdk.interrupt.await_count == int(running)
    store.finish(member["id"], "cancelled")
    later = send(store, room_id, message_id="later", request="later")
    store.consume(later["id"], "later-native-turn")
    ctx.state = "running"
    assert (await controller.handle(command)).error is None
    assert store.delivery(later["id"])["status"] == "running"
    assert ctx.sdk.interrupt.await_count == int(running)
    assert machine._spawn.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["error", "missing"])
async def test_room_stop_reports_failed_native_recovery_and_can_retry(engine, failure):
    from types import SimpleNamespace

    machine, _transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-turn")
    machine._spawn = AsyncMock(
        side_effect=RuntimeError("native service unavailable") if failure == "error" else None,
        return_value=None,
    )
    machine._handle_query = AsyncMock()
    machine._handle_new_session = AsyncMock()
    command = CollaborationCommand(
        operation="stop", room_id=room_id, member_id=member["id"],
        cmd_id="stop", client_id="browser", owner_id="owner",
    )
    result = await controller.handle(command)
    assert result.error
    assert store.delivery(delivery["id"])["status"] == "stopping"
    assert (await controller.handle(command)).error
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    ctx.cwd = "/tmp"
    ctx.state = "running"
    ctx.sdk = SimpleNamespace(interrupt=AsyncMock(), query=AsyncMock())
    machine.sessions[ctx.key] = ctx
    assert (await controller.handle(command)).error
    retry = await controller.handle(command.model_copy(update={"cmd_id": "retry-stop"}))
    assert retry.error is None
    ctx.sdk.interrupt.assert_awaited_once()
    ctx.sdk.query.assert_not_awaited()
    machine._handle_query.assert_not_awaited()
    machine._handle_new_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_room_stop_only_cancels_queued_work_without_resuming_a_cold_session(engine):
    machine, _transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    delivery = send(store, room_id)
    machine._spawn = AsyncMock()
    machine._handle_interrupt = AsyncMock()
    result = await controller.handle(CollaborationCommand(
        operation="stop", room_id=room_id, member_id=member["id"],
        cmd_id="stop", client_id="browser", owner_id="owner",
    ))
    assert result.error is None
    assert store.delivery(delivery["id"])["status"] == "cancelled"
    machine._spawn.assert_not_awaited()
    machine._handle_interrupt.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_room_stop_surfaces_a_native_interrupt_error(engine):
    machine, _transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    delivery = send(store, room_id)
    store.consume(delivery["id"], "native-turn")
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    ctx.state = "reconnecting"
    machine.sessions[sid] = ctx
    machine._handle_interrupt = AsyncMock(return_value=Error(
        code="not_running", message="原生会话尚未恢复，请稍后重试。",
    ))
    result = await controller.handle(CollaborationCommand(
        operation="stop", room_id=room_id, member_id=member["id"],
        cmd_id="stop", client_id="browser", owner_id="owner",
    ))
    assert result.error == "原生会话尚未恢复，请稍后重试。"
    assert store.delivery(delivery["id"])["status"] == "stopping"
    machine._handle_interrupt.assert_awaited_once()


def test_context_keeps_the_reply_version_that_existed_when_the_user_sent(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    store.publish(member["id"], "source", "original answer", [])
    delivery = send(store, room_id)
    store.publish(member["id"], "source", "later revision", [])
    messages, _current = store.context(delivery)
    assert messages[0]["text"] == "original answer"
    assert store.messages("owner", room_id)[0]["text"] == "later revision"


@pytest.mark.asyncio
async def test_room_storage_failure_is_reported_without_stopping_native_controls():
    machine, _transport = _mk_machine()
    path = machine.cfg.state_dir / "collaboration.sqlite3"
    path.write_bytes(b"invalid database")
    path.chmod(0o600)
    await machine._collaboration.start()
    result = await machine._collaboration.handle(
        CollaborationCommand(
            operation="list", cmd_id="list", client_id="browser", owner_id="owner"
        )
    )
    assert result.error
    assert not machine._collaboration.workers
    await machine._collaboration.close()


def test_unknown_wait_can_be_closed_without_resubmitting_the_original_input(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, _member = create_room(store)
    delivery = send(store, room_id)
    store.set_delivery(delivery["id"], "unknown")
    body = {"room_id": room_id, "delivery_id": delivery["id"]}
    for _ in range(2):
        store.mutate("owner", "dismiss", "dismiss", body)
    assert store.deliveries(room_id)[0]["status"] == "cancelled"
    assert len(store.deliveries(room_id)) == 1
    assert len(store.messages("owner", room_id)) == 1


@pytest.mark.asyncio
async def test_room_creations_share_native_pool_admission_with_browser_creates():
    from cc_remote.protocol import NewSession

    machine, _transport = _mk_machine()
    controller = machine._collaboration
    store = await controller.database()
    spec, account = controller.prepare_member(
        CollaborationMemberSpec(engine="claude", cwd="/tmp")
    )
    room_id = store.mutate(
        "owner",
        "create",
        "create",
        {"name": "Room"},
        prepared_members=[(spec, account), (spec, account)],
    )
    members = store.room("owner", room_id)["members"]
    active, maximum = 0, 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def create(command, **_options):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        await release.wait()
        await asyncio.sleep(0.01)
        ctx = _mk_ctx("tmp-" + command.cmd_id)
        machine.sessions[ctx.key] = ctx
        active -= 1
        return SessionFocus(session_id=ctx.key)

    machine._handle_new_session = AsyncMock(side_effect=create)
    first = asyncio.create_task(controller.context(members[0], {"id": "first"}))
    await entered.wait()
    await machine._dispatch_incoming_command(
        NewSession(cmd_id="browser", client_id="browser", cwd="/tmp")
    )
    second = asyncio.create_task(controller.context(members[1], {"id": "second"}))
    release.set()
    await asyncio.gather(first, second)
    await machine._command_scheduler.drain()
    assert maximum == 1
    assert machine._handle_new_session.await_count == 3


def test_message_identity_deduplicates_a_business_retry_with_a_new_request_id(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, _member = create_room(store)
    first = send(store, room_id)
    retried = send(store, room_id, request="after-reload")
    assert first["id"] == retried["id"]
    assert len(store.messages("owner", room_id)) == 1
    with pytest.raises(ValueError):
        send(store, room_id, request="changed-body", prompt="different")


@pytest.mark.asyncio
async def test_native_echo_is_authoritative_when_a_steer_receipt_is_lost():
    machine, _transport = _mk_machine()
    controller = machine._collaboration
    store = await controller.database()
    spec, account = controller.prepare_member(
        CollaborationMemberSpec(engine="codex", cwd="/tmp")
    )
    room_id = store.mutate(
        "owner",
        "create",
        "create",
        {"name": "Room"},
        prepared_members=[(spec, account)],
    )
    member = store.room("owner", room_id)["members"][0]
    ctx = _mk_ctx("native", "native")
    ctx.engine, ctx.state = "codex", "running"
    machine.sessions[ctx.key] = ctx
    store.bind(member["id"], ctx.key, ctx.session_id, ctx.cwd)
    delivery = send(store, room_id)

    async def steer(cmd):
        await machine._emit(
            ctx,
            TurnSteered(msg_id=cmd.msg_id, turn_id="native-turn", prompt=cmd.prompt),
        )
        return Error(code="steer_outcome_unknown", message="receipt lost")

    machine._handle_steer = AsyncMock(side_effect=steer)
    await controller.deliver(delivery)
    assert store.delivery(delivery["id"])["status"] == "running"
    assert store.member_by_id(member["id"])["synced_seq"] == delivery["context_through"]


def test_incremental_context_reuses_the_members_own_native_output(tmp_path):
    store = CollaborationStore(tmp_path)
    room_id, member = create_room(store)
    first = send(store, room_id)
    store.consume(first["id"], "native-turn")
    store.publish(member["id"], "own-answer", "x" * (1024 * 1024 + 1), ["message"])
    store.finish(member["id"], "succeeded")
    second = send(
        store, room_id, message_id="second", request="second", prompt="整理一下总结"
    )
    messages, current = store.context(second)
    assert messages == [current]
    assert shared_prompt(
        store.room("owner", room_id), member, messages, current
    ).endswith("整理一下总结")


@pytest.mark.asyncio
async def test_foreign_user_echo_cannot_consume_queued_room_inputs():
    machine, _transport = _mk_machine()
    controller = machine._collaboration
    store = await controller.database()
    room_id, member = create_room(store)
    delivery = send(store, room_id)
    ctx = _mk_ctx("native", "native")
    controller.watch(ctx, member["id"])
    await machine._emit(
        ctx, UserMsg(msg_id="foreign-native-user", prompt="native input")
    )
    assert store.delivery(delivery["id"])["status"] == "queued"
    assert store.member_by_id(member["id"])["synced_seq"] == 0
