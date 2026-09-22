"""Zero-token tests for reliable client commands and wrapper deduplication."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from cc_remote.protocol import (
    AnswerQuestion,
    AskUser,
    BtwClosed,
    BtwOpened,
    BtwSessionInfo,
    BtwSync,
    CloseBtw,
    CommandAck,
    DeleteSession,
    Effort,
    Error,
    GetContext,
    GetDiff,
    GetHistory,
    History,
    Hello,
    Interrupt,
    ListSessions,
    Model,
    OpenBtw,
    Perm,
    Ping,
    Query,
    Snapshot,
    SetEffort,
    SetModel,
    SetPerm,
    SessionControl,
    SessionList,
    StateEvent,
    SwitchSession,
    ForkSession,
    Takeover,
    TakeoverState,
    UserMsg,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.claude_broker.client import BrokerClientError
from cc_remote.relay.pairing import RelayHub
from tests.test_multisession import _mk_ctx, _mk_machine


def test_command_envelope_and_routed_ack_roundtrip():
    command = Query(
        prompt="hello",
        msg_id="msg-1",
        cmd_id="cmd-1",
        client_id="client-1",
    )
    assert deserialize(serialize(command)) == command
    takeover = Takeover(
        sid="session-1", cmd_id="takeover-1", client_id="client-1")
    assert deserialize(serialize(takeover)) == takeover
    takeover_state = TakeoverState(
        sid="session-1", pending=True, message="waiting")
    assert deserialize(serialize(takeover_state)) == takeover_state
    with pytest.raises(ValidationError):
        Takeover(sid="session-1")
    ack = CommandAck(
        cmd_id="cmd-1",
        client_id="client-1",
        to="client-1",
    )
    assert deserialize(serialize(ack)) == ack
    with pytest.raises(ValidationError):
        CommandAck(cmd_id="cmd-1", client_id="client-1")
    with pytest.raises(ValidationError):
        Hello(role="client", client_id="client-1", cmd_id="not-reliable")
    with pytest.raises(ValidationError):
        Ping(n=1, cmd_id="not-reliable")


def test_v15_session_control_roundtrip_and_snapshot_carriers():
    control = SessionControl(
        sid="session-1",
        control_mode="external_cli",
        write_state="read_only",
        terminal_attached=True,
        reason="外部 CLI 正在控制",
        generation="wrapper-generation-1",
        revision=7,
        can_takeover=True,
    )
    assert deserialize(serialize(control)) == control
    assert is_downstream(control) is True

    snapshot = Snapshot(
        sid="session-1", cc_session_id="session-1", state="idle",
        control=control,
    )
    assert deserialize(serialize(snapshot)).control == control
    history = History(
        session_id="session-1", revision="history-revision",
        events=[], control=control,
    )
    assert deserialize(serialize(history)).control == control

    with pytest.raises(ValidationError):
        SessionControl(
            control_mode="terminal", write_state="read_only",
            terminal_attached=True, revision=8,
        )
    with pytest.raises(ValidationError):
        SessionControl(
            control_mode="remote", write_state="writable",
            terminal_attached=False, revision=-1,
        )
    with pytest.raises(ValidationError):
        SessionControl(
            control_mode="remote", write_state="writable",
            terminal_attached=False, generation="bad generation", revision=0,
        )


def test_open_btw_request_id_roundtrip_is_required_on_both_frames():
    command = OpenBtw(
        sid="parent-1",
        request_id="btw-request-1",
        cmd_id="btw-command-1",
        client_id="client-1",
    )
    assert deserialize(serialize(command)) == command
    opened = BtwOpened(
        request_id="btw-request-1",
        btw_sid="btw-1",
        parent_sid="parent-1",
        engine="claude",
        created_at=1.0,
        revision=1,
        to="client-1",
    )
    assert deserialize(serialize(opened)) == opened
    sync = BtwSync(
        generation="wrapper-1",
        revision=2,
        sessions=[BtwSessionInfo(
            btw_sid="btw-1",
            parent_sid="parent-1",
            engine="claude",
            created_at=1.0,
        )],
        to="client-1",
    )
    assert deserialize(serialize(sync)) == sync
    closed = BtwClosed(
        btw_sid="btw-1",
        parent_sid="parent-1",
        revision=3,
        to="client-1",
    )
    assert deserialize(serialize(closed)) == closed
    assert is_downstream(sync) is False
    assert is_downstream(closed) is False
    with pytest.raises(ValidationError):
        OpenBtw(sid="parent-1")
    with pytest.raises(ValidationError):
        BtwOpened(btw_sid="btw-1", parent_sid="parent-1", engine="claude")


def test_wrapper_deduplicates_processed_command_and_resends_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = Query(
            prompt="hello", msg_id="msg-1",
            cmd_id="cmd-1", client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["cmd-1"]
        acks = [msg for msg in transport.sent if isinstance(msg, CommandAck)]
        assert len(acks) == 2
        assert all(
            ack.cmd_id == "cmd-1"
            and ack.client_id == "client-1"
            and ack.to == "client-1"
            for ack in acks
        )

    asyncio.run(run())


def test_takeover_duplicate_is_at_most_once_and_only_resends_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = Takeover(
            sid="session-1", cmd_id="takeover-1", client_id="client-1")
        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["takeover-1"]
        assert [msg.type for msg in transport.sent] == [
            "command_ack", "command_ack"]

    asyncio.run(run())


def test_duplicate_safe_read_reexecutes_handler_before_ack():
    async def run():
        machine, transport = _mk_machine()
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        command = GetHistory(
            session_id="session-1",
            cmd_id="read-1",
            client_id="client-1",
        )
        await machine._process_command(command)
        await machine._process_command(command)

        assert handled == ["read-1", "read-1"]
        assert len([
            msg for msg in transport.sent if isinstance(msg, CommandAck)
        ]) == 2

    asyncio.run(run())


def test_duplicate_resident_switch_reseeds_current_state_with_new_sequence():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-1", "claude-1")
        ctx.sdk = SimpleNamespace(
            permission_mode="default",
            model="claude-sonnet-5",
            effort="high",
        )
        machine.sessions[ctx.key] = ctx
        command = SwitchSession(
            session_id=ctx.key,
            engine="claude",
            space="code",
            cmd_id="switch-1",
            client_id="client-1",
        )

        await machine._process_command(command)
        initial = [
            event for event in transport.sent if isinstance(event, StateEvent)
        ]
        assert [event.state for event in initial] == ["idle"]
        assert initial[0].seq is not None

        await machine._set_state(ctx, "running")
        await machine._process_command(command)

        states = [
            event for event in transport.sent if isinstance(event, StateEvent)
        ]
        assert [event.state for event in states] == [
            "idle", "running", "running"]
        assert states[-1].seq > states[-2].seq > states[0].seq
        assert states[-1].sid == ctx.key

    asyncio.run(run())


def test_wrapper_does_not_ack_or_remember_a_crashed_handler():
    async def run():
        machine, transport = _mk_machine()
        calls = 0

        async def boom(_cmd):
            nonlocal calls
            calls += 1
            raise RuntimeError("handler crashed")

        machine._handle = boom
        command = Query(
            prompt="hello", msg_id="msg-1",
            cmd_id="cmd-1", client_id="client-1",
        )
        for _ in range(2):
            with pytest.raises(RuntimeError, match="handler crashed"):
                await machine._process_command(command)

        assert calls == 2
        assert not [msg for msg in transport.sent if isinstance(msg, CommandAck)]

    asyncio.run(run())


@pytest.mark.parametrize(
    "command",
    [
        SetModel(
            sid="missing-session", model="claude-sonnet-4-5",
            cmd_id="missing-model", client_id="client-1",
        ),
        GetContext(
            sid="missing-session",
            cmd_id="missing-context", client_id="client-1",
        ),
    ],
)
def test_reliable_controls_reject_missing_session_before_ack(command):
    async def run():
        machine, transport = _mk_machine()

        await machine._process_command(command)

        assert [event.type for event in transport.sent] == [
            "error", "command_ack"]
        error = transport.sent[0]
        assert error.sid == "missing-session"
        assert error.request_id == command.cmd_id
        assert error.to == "client-1"

    asyncio.run(run())


def test_interrupt_missing_target_never_leaks_error_to_focused_session():
    async def run():
        machine, transport = _mk_machine()
        visible = _mk_ctx("visible-session", "visible-session")
        machine.sessions[visible.key] = visible
        machine.focused_sid = visible.key
        command = Interrupt(
            sid="missing-session",
            cmd_id="missing-interrupt", client_id="client-1",
        )

        await machine._process_command(command)

        assert [event.type for event in transport.sent] == [
            "error", "command_ack"]
        error = transport.sent[0]
        assert error.sid == "missing-session"
        assert error.request_id == command.cmd_id
        assert error.to == "client-1"
        assert visible.buffer.tail_seq == 0

    asyncio.run(run())


def test_non_running_interrupt_returns_correlated_failure_before_ack():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("idle-session", "idle-session")
        ctx.state = "idle"
        machine.sessions[ctx.key] = ctx
        command = Interrupt(
            sid=ctx.key, cmd_id="idle-interrupt", client_id="client-1",
        )

        await machine._process_command(command)

        errors = [event for event in transport.sent if isinstance(event, Error)]
        assert len(errors) == 1
        assert errors[0].request_id == command.cmd_id
        assert errors[0].to == command.client_id
        assert isinstance(transport.sent[-1], CommandAck)

    asyncio.run(run())


def test_get_diff_missing_target_returns_targeted_correlated_failure():
    async def run():
        machine, transport = _mk_machine()
        command = GetDiff(
            sid="missing-diff", file="README.md",
            cmd_id="diff-command", client_id="client-1",
        )

        await machine._process_command(command)

        assert [event.type for event in transport.sent] == [
            "error", "command_ack"]
        error = transport.sent[0]
        assert error.sid == command.sid
        assert error.request_id == command.cmd_id
        assert error.to == command.client_id

    asyncio.run(run())


def test_unknown_interaction_answer_returns_correlated_failure():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        machine.sessions[ctx.key] = ctx
        command = AnswerQuestion(
            sid=ctx.key,
            ask_id="expired-question",
            answer="允许一次",
            cmd_id="answer-command",
            client_id="client-1",
        )

        await machine._process_command(command)

        error = next(event for event in transport.sent
                     if isinstance(event, Error))
        assert error.request_id == command.cmd_id
        assert error.to == command.client_id
        assert isinstance(transport.sent[-1], CommandAck)

    asyncio.run(run())


def test_wrapper_dedupe_cache_is_bounded_per_client():
    async def run():
        machine, _ = _mk_machine()
        machine.COMMAND_IDS_PER_CLIENT = 2
        handled = []

        async def fake_handle(cmd):
            handled.append(cmd.cmd_id)

        machine._handle = fake_handle
        for cmd_id in ("one", "two", "three", "one"):
            await machine._process_command(Query(
                prompt=cmd_id,
                msg_id=f"msg-{cmd_id}",
                cmd_id=cmd_id,
                client_id="client-1",
            ))

        # "one" was evicted after "three", so it is processed again; the cache
        # itself remains at the configured hard bound.
        assert handled == ["one", "two", "three", "one"]
        assert len(machine._processed_commands["client-1"]) == 2

    asyncio.run(run())


def test_duplicate_claude_broker_model_and_permission_controls_replay_without_reexecution():
    class BrokerControls:
        is_claude_broker = True
        model = "old-model"
        effort = "high"
        permission_mode = "bypassPermissions"

        def __init__(self):
            self.model_calls = 0
            self.effort_calls = 0
            self.permission_calls = 0

        async def set_model(self, model):
            self.model_calls += 1
            self.model = model

        async def set_permission_mode(self, mode):
            self.permission_calls += 1
            self.permission_mode = mode

        async def set_effort(self, effort):
            self.effort_calls += 1
            self.effort = effort

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "claude"
        ctx.space = "code"
        ctx.sdk = BrokerControls()
        ctx.announced_model = "old-model"
        ctx.announced_perm = "bypassPermissions"
        machine.sessions["session-1"] = ctx

        model = SetModel(
            sid="session-1", model="claude-opus-4-1",
            cmd_id="model-1", client_id="client-1",
        )
        permission = SetPerm(
            sid="session-1", mode="default",
            cmd_id="perm-1", client_id="client-1",
        )
        effort = SetEffort(
            sid="session-1", effort="max",
            cmd_id="effort-1", client_id="client-1",
        )
        model_task = asyncio.create_task(machine._process_command(model))
        async with asyncio.timeout(1.0):
            while True:
                question = next(
                    (event for event in reversed(transport.sent)
                     if isinstance(event, AskUser)), None)
                if question is not None:
                    break
                await asyncio.sleep(0.01)
        await machine._process_command(AnswerQuestion(
            sid="session-1",
            ask_id=question.ask_id,
            answer=question.options[0]["label"],
            client_id="client-1",
        ))
        await model_task
        await machine._process_command(model)
        await machine._process_command(effort)
        await machine._process_command(effort)
        await machine._process_command(permission)
        await machine._process_command(permission)

        assert ctx.sdk.model_calls == 1
        assert ctx.sdk.effort_calls == 1
        assert ctx.sdk.permission_calls == 1
        models = [event for event in transport.sent if isinstance(event, Model)]
        efforts = [event for event in transport.sent if isinstance(event, Effort)]
        perms = [event for event in transport.sent if isinstance(event, Perm)]
        assert [event.model for event in models] == [
            "claude-opus-4-1", "claude-opus-4-1"]
        assert [event.mode for event in perms] == ["default", "default"]
        assert [event.effort for event in efforts] == ["max", "max"]
        assert models[-1].to == "client-1"
        assert efforts[-1].to == "client-1"
        assert perms[-1].to == "client-1"
        assert len([event for event in transport.sent
                    if isinstance(event, CommandAck)]) == 6

    asyncio.run(run())


def test_duplicate_failed_claude_broker_controls_replay_error_without_retrying_tui():
    class RejectingBroker:
        is_claude_broker = True
        model = "claude-sonnet-4-5"
        effort = "high"
        permission_mode = "bypassPermissions"

        def __init__(self):
            self.calls = 0
            self.effort_calls = 0

        async def set_permission_mode(self, _mode):
            self.calls += 1
            raise BrokerClientError(
                "control_unconfirmed", "no durable permission record")

        async def set_effort(self, _effort):
            self.effort_calls += 1
            raise BrokerClientError(
                "control_unconfirmed", "no durable effort record")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "claude"
        ctx.space = "code"
        ctx.sdk = RejectingBroker()
        ctx.announced_perm = "bypassPermissions"
        machine.sessions["session-1"] = ctx
        command = SetPerm(
            sid="session-1", mode="default",
            cmd_id="perm-failed", client_id="client-1",
        )
        effort = SetEffort(
            sid="session-1", effort="max",
            cmd_id="effort-failed", client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)
        await machine._process_command(effort)
        await machine._process_command(effort)

        assert ctx.sdk.calls == 1
        assert ctx.sdk.effort_calls == 1
        errors = [event for event in transport.sent if isinstance(event, Error)]
        assert len(errors) == 4
        assert all("持久确认" in event.message for event in errors)
        assert errors[-1].to == "client-1"
        assert ctx.announced_perm == "bypassPermissions"
        assert not [event for event in transport.sent if isinstance(event, Perm)]
        assert not [event for event in transport.sent if isinstance(event, Effort)]

    asyncio.run(run())


def test_business_rejection_is_acknowledged_after_error():
    async def run():
        machine, transport = _mk_machine()
        await machine._process_command(Query(
            sid="missing-session",
            prompt="hello",
            msg_id="msg-1",
            cmd_id="cmd-1",
            client_id="client-1",
        ))
        command = Query(
            sid="missing-session",
            prompt="hello",
            msg_id="msg-2",
            cmd_id="cmd-2",
            client_id="client-1",
        )
        await machine._process_command(command)
        await machine._process_command(command)
        assert [msg.type for msg in transport.sent] == [
            "error", "command_ack", "error", "command_ack",
            "error", "command_ack",
        ]
        replayed = transport.sent[-2]
        assert replayed.type == "error"
        assert replayed.msg_id == "msg-2"
        assert replayed.to == "client-1"

    asyncio.run(run())


def test_open_btw_missing_parent_error_is_correlated_targeted_and_replayed():
    async def run():
        machine, transport = _mk_machine()
        command = OpenBtw(
            sid="missing-parent",
            request_id="btw-request-1",
            cmd_id="btw-command-1",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert [message.type for message in transport.sent] == [
            "error", "command_ack", "error", "command_ack"]
        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 2
        assert all(
            message.request_id == "btw-request-1"
            and message.to == "client-1"
            and message.sid == "missing-parent"
            for message in errors
        )

    asyncio.run(run())


def test_ownerless_open_btw_fails_closed_without_broadcast():
    async def run():
        machine, transport = _mk_machine()
        await machine._handle(OpenBtw(
            sid="parent-1", request_id="ownerless-request"))
        assert transport.sent == []
        assert machine.sessions == {}

    asyncio.run(run())


def test_open_btw_spawn_rejection_is_correlated_and_cached():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("tmp-parent", session_id=None)
        machine.sessions[parent.key] = parent
        machine.focused_sid = parent.key
        command = OpenBtw(
            sid=parent.key,
            request_id="btw-request-spawn-fail",
            cmd_id="btw-command-spawn-fail",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 2
        assert all(
            message.request_id == "btw-request-spawn-fail"
            and message.to == "client-1"
            and message.sid == parent.key
            for message in errors
        )
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 2

    asyncio.run(run())


def test_open_btw_success_response_is_correlated_and_replayed_without_refork():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent-1", session_id="parent-1")
        fork = _mk_ctx("btw-fork-1", session_id=None)
        fork.key = "btw-fork-1"
        fork.btw = True
        fork.parent_sid = parent.session_id
        fork.sdk = SimpleNamespace(
            model="gpt-btw",
            effort="high",
            approval="never",
        )
        machine.sessions[parent.key] = parent
        machine.focused_sid = parent.key
        spawn_calls = 0

        async def fake_spawn(_parent, owner_client_id=None):
            nonlocal spawn_calls
            spawn_calls += 1
            assert owner_client_id == "client-1"
            # Mirror _spawn_btw(): every live fork must be owner-bound before
            # its first sequenced frame is emitted.
            fork.owner_client_id = owner_client_id
            machine.sessions[fork.key] = fork
            return fork

        machine._spawn_btw = fake_spawn
        command = OpenBtw(
            sid=parent.key,
            request_id="btw-request-success",
            cmd_id="btw-command-success",
            client_id="client-1",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        assert spawn_calls == 1
        opened = [message for message in transport.sent
                  if isinstance(message, BtwOpened)]
        assert len(opened) == 2
        assert all(
            message.request_id == "btw-request-success"
            and message.to == "client-1"
            and message.btw_sid == fork.key
            for message in opened
        )
        snapshots = [message for message in transport.sent
                     if isinstance(message, Snapshot)]
        assert len(snapshots) == 2
        assert all(
            message.sid == fork.key
            and message.to == "client-1"
            and message.generation == machine.instance_id
            for message in snapshots
        )
        permissions = [message for message in transport.sent
                       if isinstance(message, Perm)]
        assert len(permissions) == 2
        assert all(
            message.sid == fork.key
            and message.to == "client-1"
            and message.mode == "bypassPermissions"
            for message in permissions
        )
        models = [message for message in transport.sent
                  if isinstance(message, Model)]
        efforts = [message for message in transport.sent
                   if isinstance(message, Effort)]
        assert [(message.model, message.sid, message.to, message.owner_id)
                for message in models] == [
                    ("gpt-btw", fork.key, None, "client-1")]
        assert [(message.effort, message.sid, message.to, message.owner_id)
                for message in efforts] == [
                    ("high", fork.key, None, "client-1")]
        assert models[0].seq == 1 and efforts[0].seq == 2
        # Model/effort are mutable after the fork opens. They belong to the
        # sequenced owner-only ring, not the static OpenBtw response cache:
        # replaying the latter after an ACK loss must not roll current settings
        # back to their initial values.
        assert [entry[1].type for entry in fork.buffer._buf] == [
            "model", "effort", "auto_compact"]
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 2

    asyncio.run(run())


def test_open_btw_does_not_announce_fork_removed_during_spawn():
    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent-race", session_id="parent-race")
        fork = _mk_ctx("btw-race", session_id=None)
        fork.btw = True
        fork.parent_sid = parent.session_id
        machine.sessions[parent.key] = parent

        async def fake_spawn(_parent, owner_client_id=None):
            fork.owner_client_id = owner_client_id
            # Model the cleanup task winning after native spawn but before the
            # command can publish BtwOpened.
            return fork

        machine._spawn_btw = fake_spawn
        result = await machine._handle_open_btw(OpenBtw(
            sid=parent.key,
            request_id="btw-race-request",
            client_id="client-1",
        ))

        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert result.request_id == "btw-race-request"
        assert not any(isinstance(message, BtwOpened)
                       for message in transport.sent)
        assert machine._btw_revision == 0

    asyncio.run(run())


def test_btw_live_frames_are_routed_and_buffered_for_owner_only():
    async def run():
        machine, transport = _mk_machine()
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"

        await machine._emit(
            fork, UserMsg(msg_id="private-msg", prompt="private prompt"))

        sent = transport.sent[-1]
        assert sent.sid == "btw-private"
        assert sent.to is None
        assert sent.owner_id == "owner-client"
        buffered = list(fork.buffer._buf)
        assert len(buffered) == 1
        assert buffered[0][1].to is None
        assert buffered[0][1].owner_id == "owner-client"

    asyncio.run(run())


def test_nonowner_cannot_control_query_close_or_focus_btw_runtime():
    async def run():
        machine, transport = _mk_machine()
        normal = _mk_ctx("normal", session_id="normal")
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.parent_sid = "normal"
        fork.owner_client_id = "owner-client"
        machine.sessions = {normal.key: normal, fork.key: fork}
        machine.focused_sid = normal.key

        commands = [
            Query(
                sid=fork.key, prompt="steal", msg_id="private-query",
                cmd_id="query-command", client_id="other-client",
                owner_id="other-owner",
            ),
            Interrupt(
                sid=fork.key, cmd_id="interrupt-command",
                client_id="other-client", owner_id="other-owner",
            ),
            SetModel(
                sid=fork.key, model="gpt-stolen",
                cmd_id="model-command", client_id="other-client",
                owner_id="other-owner",
            ),
            SetEffort(
                sid=fork.key, effort="high",
                cmd_id="effort-command", client_id="other-client",
                owner_id="other-owner",
            ),
            CloseBtw(
                sid=fork.key, cmd_id="close-command",
                client_id="other-client", owner_id="other-owner",
            ),
            SwitchSession(
                session_id=fork.key, cmd_id="switch-command",
                client_id="other-client", owner_id="other-owner",
            ),
        ]
        for command in commands:
            await machine._process_command(command)

        errors = [message for message in transport.sent
                  if isinstance(message, Error)]
        assert len(errors) == 6
        assert all(
            message.code == "auth"
            and message.sid == fork.key
            and message.to == "other-client"
            and message.owner_id == "other-owner"
            for message in errors
        )
        assert len([message for message in transport.sent
                    if isinstance(message, CommandAck)]) == 6
        assert machine.sessions[fork.key] is fork
        assert fork.state == "idle" and fork.turn_task is None
        assert machine.focused_sid == normal.key

    asyncio.run(run())


def test_owner_controls_and_interrupts_only_its_btw_runtime():
    class OwnerSdk:
        model = "claude-before"
        effort = "low"
        applied_effort = "low"
        permission_mode = "bypassPermissions"

        def __init__(self):
            self.interrupts = 0

        async def set_model(self, model):
            self.model = model

        async def interrupt(self):
            self.interrupts += 1

    async def run():
        machine, transport = _mk_machine()
        parent = _mk_ctx("parent", session_id="parent")
        parent.sdk = SimpleNamespace(model="parent-model", effort="max")
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.parent_sid = parent.session_id
        fork.owner_client_id = "owner-client"
        fork.sdk = OwnerSdk()
        machine.sessions = {parent.key: parent, fork.key: fork}
        machine.focused_sid = parent.key

        await machine._process_command(SetModel(
            sid=fork.key, model="claude-after",
            cmd_id="model-command", client_id="owner-tab-2",
            owner_id="owner-client",
        ))
        await machine._process_command(SetEffort(
            sid=fork.key, effort="high",
            cmd_id="effort-command", client_id="owner-tab-2",
            owner_id="owner-client",
        ))
        fork.state = "running"
        await machine._process_command(Interrupt(
            sid=fork.key, cmd_id="interrupt-command",
            client_id="owner-tab-2", owner_id="owner-client",
        ))

        assert fork.sdk.model == "claude-after"
        assert fork.sdk.effort == "high"
        assert fork.sdk.interrupts == 1
        assert fork.state == "interrupting"
        assert parent.sdk.model == "parent-model"
        assert parent.sdk.effort == "max"
        assert parent.state == "idle"
        emitted = [
            message for message in transport.sent
            if isinstance(message, (Model, Effort))
        ]
        assert [(message.type, message.sid, message.to, message.owner_id)
                for message in emitted] == [
                    ("model", fork.key, None, "owner-client"),
                    ("effort", fork.key, None, "owner-client"),
                ]

    asyncio.run(run())


def test_claude_btw_real_id_is_hidden_and_cannot_be_cold_resumed(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        real_id = "11111111-2222-4333-8444-555555555555"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        fork.btw_real_id = real_id
        machine.sessions[fork.key] = fork
        machine._private_btw_sessions[real_id] = {
            "cwd": fork.cwd, "created_at": 1.0,
        }

        def info(session_id):
            return SimpleNamespace(
                session_id=session_id, summary=None, custom_title=None,
                last_modified=None, first_prompt=None, git_branch=None,
                cwd=fork.cwd, tag=None,
            )

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda limit=200: [info(real_id), info("normal-session")],
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="owner-client"))
        listing = next(message for message in transport.sent
                       if isinstance(message, SessionList))
        assert [row.session_id for row in listing.sessions] == ["normal-session"]

        spawned = []

        async def forbidden_spawn(*_args, **_kwargs):
            spawned.append(True)
            raise AssertionError("private fork must not be resumed")

        machine._spawn = forbidden_spawn
        # Both a non-owner and the original owner are denied when they address
        # the internal real transcript id rather than the stable btw-* key.
        for index, client in enumerate(("other-client", "owner-client"), 1):
            await machine._process_command(SwitchSession(
                session_id=real_id,
                cmd_id=f"switch-private-{index}",
                client_id=client,
            ))

        # The tombstone remains authoritative after the live ctx is gone too.
        machine.sessions.clear()
        await machine._process_command(SwitchSession(
            session_id=real_id,
            cmd_id="switch-private-cold",
            client_id="owner-client",
        ))
        assert not spawned
        denied = [message for message in transport.sent
                  if isinstance(message, Error)
                  and message.sid in {fork.key, real_id}]
        assert len(denied) == 3
        assert all(message.code == "auth" for message in denied)

    asyncio.run(run())


def test_metadata_only_claude_session_switch_fails_once_without_spawn(
        monkeypatch, tmp_path):
    async def run():
        machine, transport = _mk_machine()
        session_id = "11111111-2222-4333-8444-555555555555"
        claude_root = tmp_path / "claude-root"
        transcript = (
            claude_root / "projects" / "-metadata-only"
            / f"{session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"type":"ai-title","aiTitle":"orphan",'
            f'"sessionId":"{session_id}"' '}\n'
            '{"type":"mode","mode":"normal",'
            f'"sessionId":"{session_id}"' '}\n'
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))

        async def forbidden_spawn(*_args, **_kwargs):
            raise AssertionError("metadata-only session must not be resumed")

        machine._spawn = forbidden_spawn
        result = await machine._handle_switch_session(SwitchSession(
            session_id=session_id,
            engine="claude",
            space="code",
            cmd_id="switch-metadata-only",
            client_id="client-1",
        ))

        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert "无法确认" in result.message and "工作目录" in result.message
        assert "历史记录已保留" in result.message
        assert "删除" not in result.message
        assert result.sid == session_id
        assert result.to == "client-1"
        assert result.request_id == "switch-metadata-only"
        assert transport.sent == [result]

    asyncio.run(run())


def test_metadata_only_claude_session_can_be_deleted_by_exact_global_id(
        monkeypatch, tmp_path):
    async def run():
        machine, transport = _mk_machine()
        session_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        claude_root = tmp_path / "claude-root"
        transcript = (
            claude_root / "projects" / "-metadata-only"
            / f"{session_id}.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"type":"ai-title","aiTitle":"orphan",'
            f'"sessionId":"{session_id}"' '}\n'
            '{"type":"mode","mode":"normal",'
            f'"sessionId":"{session_id}"' '}\n'
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_root))

        async def not_codex(_sid):
            return False

        async def listed(_cmd):
            return None

        machine._is_codex_session = not_codex
        machine._handle_list_sessions = listed

        result = await machine._handle_delete_session(DeleteSession(
            session_id=session_id,
            engine="claude",
            space="code",
            cmd_id="delete-metadata-only",
            client_id="client-1",
        ))

        assert result is None
        assert not transcript.exists()
        assert not [message for message in transport.sent
                    if isinstance(message, Error)]

    asyncio.run(run())


def test_deleted_claude_fork_retry_only_acks_without_resurrecting(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        parent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        cutoff = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        child = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        cwd = "/repo/component"
        machine._claude_forks.begin(
            "fork-request", parent, cutoff, cwd)
        machine._claude_forks.claim_submission("fork-request")
        machine._claude_forks.complete("fork-request", child)
        forked = machine_module.SessionForked(
            parent_session_id=parent,
            session_id=child,
            cwd=cwd,
            target="same_cwd",
            last_turn_id=cutoff,
            request_id="fork-request",
            to="client-1",
        )
        machine._remember_command("client-1", "fork-cmd", (forked,))
        machine._claude_forks.begin_delete(child)
        machine._claude_forks.finish_delete(child)

        async def not_codex(_sid):
            return False

        machine._is_codex_session = not_codex
        await machine._process_command(ForkSession(
            session_id=parent,
            request_id="fork-request",
            last_turn_id=cutoff,
            cmd_id="fork-cmd",
            client_id="client-1",
        ))

        assert [message.type for message in transport.sent] == ["command_ack"]

    asyncio.run(run())


def test_failed_claude_fork_delete_restores_complete_journal(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        child = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        parent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        cutoff = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        machine._claude_forks.begin(
            "fork-request", parent, cutoff, "/repo/component")
        machine._claude_forks.claim_submission("fork-request")
        machine._claude_forks.complete("fork-request", child)

        async def not_codex(_sid):
            return False

        machine._is_codex_session = not_codex
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _sid: SimpleNamespace(cwd="/repo/component"),
        )
        monkeypatch.setattr(
            machine_module, "delete_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("delete failed")),
        )

        result = await machine._handle_delete_session(DeleteSession(
            session_id=child,
            engine="claude",
            space="code",
            cmd_id="delete-child",
            client_id="client-1",
        ))

        assert isinstance(result, Error)
        assert machine._claude_forks.child_entry(child)["status"] == "complete"
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_cwdless_claude_delete_still_rejects_unknown_transcript(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        session_id = "ffffffff-eeee-4ddd-8ccc-bbbbbbbbbbbb"
        deleted = []

        async def not_codex(_sid):
            return False

        machine._is_codex_session = not_codex
        monkeypatch.setattr(
            machine_module,
            "get_session_info",
            lambda _sid: SimpleNamespace(cwd=None),
        )
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: None)
        monkeypatch.setattr(
            machine_module,
            "delete_session",
            lambda sid, directory=None: deleted.append((sid, directory)),
        )

        result = await machine._handle_delete_session(DeleteSession(
            session_id=session_id,
            engine="claude",
            space="code",
            cmd_id="delete-unknown",
            client_id="client-1",
        ))

        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert deleted == []

    asyncio.run(run())


def test_claude_session_list_is_withheld_until_btw_real_id_is_tombstoned(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        fork = _mk_ctx("btw-pending", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        machine.sessions[fork.key] = fork

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("unsafe session scan must not run")),
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="requester-client"))

        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert isinstance(error, Error) and error.code == "busy"
        assert error.to == "requester-client"

    asyncio.run(run())


def test_claude_btw_tombstone_survives_restart_until_delete_succeeds(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        real_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"

        await machine._capture_session_id(fork, real_id)
        assert real_id in machine._private_btw_sessions
        assert machine._private_btw_file().exists()

        restarted = machine.__class__(machine.cfg, transport)
        assert real_id in restarted._private_btw_sessions

        def fail_delete(*_args, **_kwargs):
            raise PermissionError("still private")

        monkeypatch.setattr(machine_module, "delete_session", fail_delete)
        await restarted._cleanup_private_btw_sessions()
        assert real_id in restarted._private_btw_sessions

        monkeypatch.setattr(machine_module, "delete_session",
                            lambda *_args, **_kwargs: None)
        await restarted._cleanup_private_btw_sessions()
        assert real_id not in restarted._private_btw_sessions

    asyncio.run(run())


def test_corrupt_private_btw_state_refuses_fail_open_startup():
    machine, transport = _mk_machine()
    machine._private_btw_file().write_text("not-json")

    with pytest.raises(RuntimeError, match="refusing fail-open"):
        machine.__class__(machine.cfg, transport)


def test_btw_capture_persistence_failure_terminates_and_deletes_fork(
        tmp_path, monkeypatch):
    class Sdk:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True

    async def run():
        machine, transport = _mk_machine()
        real_id = "99999999-8888-4777-8666-555555555555"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.parent_sid = "parent-session"
        fork.owner_client_id = "owner-client"
        fork.btw_created_at = 1.0
        fork.btw_announced = True
        fork.sdk = Sdk()
        machine.sessions[fork.key] = fork
        artifact = tmp_path / "outside.md"
        artifact.write_text("# private", encoding="utf-8")
        machine._preview_capability_store.grant_path(
            "claude",
            "code",
            fork.key,
            str(artifact),
            mode="read",
            source="user_approved",
            persist=False,
        )

        monkeypatch.setattr(
            machine, "_persist_private_btw_sessions",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("disk denied")),
        )
        deleted = []
        monkeypatch.setattr(
            machine_module, "delete_session",
            lambda sid, directory=None: deleted.append((sid, directory)),
        )

        with pytest.raises(RuntimeError, match="fork terminated"):
            await machine._capture_session_id(fork, real_id)

        assert fork.sdk.disconnected is True
        assert fork.key not in machine.sessions
        assert fork.btw_real_id == real_id
        assert real_id not in machine._private_btw_sessions
        assert deleted == [(real_id, fork.cwd)]
        assert machine._preview_capability_store.snapshot(
            "claude", "code", fork.key,
        ) == {}
        closed = [message for message in transport.sent
                  if isinstance(message, BtwClosed)]
        assert len(closed) == 1
        assert closed[0].btw_sid == fork.key
        assert closed[0].parent_sid == "parent-session"
        assert closed[0].owner_id == "owner-client"

    asyncio.run(run())


def test_btw_capture_keeps_live_guard_when_persist_and_delete_both_fail(
        monkeypatch):
    class Sdk:
        async def disconnect(self):
            return None

    async def run():
        machine, _ = _mk_machine()
        real_id = "12345678-1234-4234-8234-123456789abc"
        fork = _mk_ctx("btw-private", session_id=None)
        fork.btw = True
        fork.owner_client_id = "owner-client"
        fork.sdk = Sdk()
        machine.sessions[fork.key] = fork

        monkeypatch.setattr(
            machine, "_persist_private_btw_sessions",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("disk denied")),
        )
        monkeypatch.setattr(
            machine_module, "delete_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                PermissionError("delete denied")),
        )

        with pytest.raises(RuntimeError, match="fork terminated"):
            await machine._capture_session_id(fork, real_id)

        assert fork.key not in machine.sessions
        assert real_id in machine._private_btw_sessions

        def info(session_id):
            return SimpleNamespace(
                session_id=session_id, summary=None, custom_title=None,
                last_modified=None, first_prompt=None, git_branch=None,
                cwd=fork.cwd, tag=None,
            )

        monkeypatch.setattr(
            machine_module, "list_sessions",
            lambda limit=200: [info(real_id), info("normal-session")],
        )
        await machine._handle_list_sessions(ListSessions(
            client_id="owner-client"))
        listing = next(message for message in machine.transport.sent
                       if isinstance(message, SessionList))
        assert [row.session_id for row in listing.sessions] == ["normal-session"]

    asyncio.run(run())


def test_relay_overwrites_spoofed_command_client_id_from_bound_hello():
    class ClientWs:
        def __init__(self, frames):
            self.frames = iter(frames)

        async def receive_text(self):
            try:
                return next(self.frames)
            except StopIteration as exc:
                raise WebSocketDisconnect() from exc

        async def send_text(self, _raw):
            return None

        async def close(self, code=1000, reason=""):
            return None

    class WrapperWs:
        def __init__(self):
            self.frames = []

        async def send_text(self, raw):
            self.frames.append(deserialize(raw))

    async def run():
        hub = RelayHub(SimpleNamespace(
            client_queue_cap=4, client_queue_bytes=4096))
        wrapper = WrapperWs()
        hub._wrapper_ws = wrapper
        client = ClientWs([
            serialize(Hello(role="client", client_id="bound-client")),
            serialize(Query(
                prompt="hello",
                msg_id="msg-1",
                cmd_id="cmd-1",
                client_id="spoofed-client",
            )),
        ])
        await hub.serve_client(client)

        forwarded = next(msg for msg in wrapper.frames if msg.type == "query")
        assert forwarded.client_id == "bound-client"
        assert forwarded.cmd_id == "cmd-1"

    asyncio.run(run())


def test_relay_routes_command_ack_only_to_originating_client():
    class Conn:
        def __init__(self):
            self.messages = []

        async def send(self, msg):
            self.messages.append(msg)

    async def run():
        hub = RelayHub(SimpleNamespace())
        origin, other = Conn(), Conn()
        hub._clients = {"origin": origin, "other": other}
        await hub._on_wrapper_msg(CommandAck(
            cmd_id="cmd-1", client_id="origin", to="origin"))
        assert [msg.cmd_id for msg in origin.messages] == ["cmd-1"]
        assert other.messages == []

    asyncio.run(run())
