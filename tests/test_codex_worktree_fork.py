from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    ForkSession,
    ForkSessionWorktree,
    SessionForked,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_worktrees import WorktreeSpec
from cc_remote.wrapper.codex_forks import CodexForkJournal
from cc_remote.wrapper.codex_forks import ForkJournalError
from cc_remote.wrapper.codex_rpc import CodexRpcOutcomeUnknown, CodexRpcRejected
from tests.test_multisession import _mk_ctx, _mk_machine


def _ctx(state: str = "idle"):
    ctx = _mk_ctx("parent", "parent")
    ctx.engine = "codex"
    ctx.state = state
    ctx.cwd = "/repo/component"
    ctx.sdk = SimpleNamespace(model="gpt-test")
    return ctx


def _spec(*, created: bool = True) -> WorktreeSpec:
    return WorktreeSpec(
        repository_root="/repo",
        worktree_root="/state/worktrees/repo/fork-1",
        cwd="/state/worktrees/repo/fork-1/component",
        branch="cc-remote/fork-1",
        created=created,
        branch_created=created,
    )


def _command(**overrides):
    values = {
        "session_id": "parent",
        "request_id": "request-1",
        "name": "Feature fork",
        "client_id": "client-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_worktree_fork_protocol_roundtrips_as_control_messages():
    command = deserialize(serialize(ForkSessionWorktree(
        session_id="parent", request_id="request-1", name="feature")))
    assert command.type == "fork_session_worktree"
    assert command.session_id == "parent" and command.name == "feature"

    event = deserialize(serialize(SessionForked(
        parent_session_id="parent",
        session_id="forked",
        cwd="/tmp/forked",
        target="worktree",
        git_branch="cc-remote/feature",
        request_id="request-1",
    )))
    assert event.type == "session_forked" and event.session_id == "forked"
    assert event.target == "worktree" and event.last_turn_id is None
    assert is_downstream(event) is False

    ordinary = deserialize(serialize(ForkSession(
        session_id="parent", request_id="request-2",
        last_turn_id="turn-2")))
    assert ordinary.type == "fork_session"
    assert ordinary.last_turn_id == "turn-2"


def test_codex_same_cwd_fork_uses_selected_turn_and_is_durable(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        parent = _ctx("running")
        machine.sessions = {"parent": parent}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            assert method == "thread/fork"
            return {"thread": {"id": "forked-thread"}}

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        cmd = _command(last_turn_id="turn-2")

        result = await machine._handle_fork_session(cmd)
        duplicate = await machine._handle_fork_session(cmd)

        assert calls == [("thread/fork", {
            "threadId": "parent",
            "lastTurnId": "turn-2",
            "ephemeral": False,
            "threadSource": "cc-remote-fork:request-1",
        }, None)]
        assert result.session_id == "forked-thread"
        assert result.cwd == "/repo/component"
        assert result.target == "same_cwd"
        assert result.git_branch is None
        assert result.last_turn_id == "turn-2"
        assert duplicate.session_id == "forked-thread"

        # A new wrapper process sees the completed result rather than relying on
        # the in-memory command ACK cache.
        reloaded = CodexForkJournal(machine.cfg.state_dir)
        entry = reloaded.begin(
            "request-1", "parent", "turn-2", "/repo/component")
        assert entry["status"] == "complete"
        assert entry["session_id"] == "forked-thread"

    asyncio.run(run())


def test_codex_same_cwd_fork_recovers_committed_rollout_intent(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine._codex_forks.begin(
            "request-1", "parent", "turn-2", "/repo/component")

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        async def rpc(*_args, **_kwargs):
            raise AssertionError("thread/fork must not be repeated")

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: {
            "session_id": "recovered-thread",
            "cwd": "/repo/component",
            "thread_source": "cc-remote-fork:request-1",
            "forked_from_id": "parent",
        })

        result = await machine._handle_fork_session(
            _command(last_turn_id="turn-2"))

        assert result.session_id == "recovered-thread"
        assert machine._codex_forks.entries["request-1"]["status"] == "complete"

    asyncio.run(run())


def test_codex_same_cwd_fork_does_not_ack_when_result_journal_fails(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            return {"thread": {"id": "forked-thread"}}

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        original_complete = machine._codex_forks.complete
        monkeypatch.setattr(
            machine._codex_forks, "complete",
            lambda *_args: (_ for _ in ()).throw(ForkJournalError("disk full")))
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)

        assert calls and len(calls) == 1
        assert not any(message.type in {"error", "command_ack"}
                       for message in transport.sent)

        # Same reliable command retries the known child correlation rather than
        # issuing another thread/fork, then ACKs only after persistence succeeds.
        monkeypatch.setattr(machine._codex_forks, "complete", original_complete)
        await machine._process_command(cmd)
        assert len(calls) == 1
        assert [message.type for message in transport.sent][-2:] == [
            "session_forked", "command_ack"]

    asyncio.run(run())


def test_codex_same_cwd_fork_unknown_rpc_outcome_only_reconciles_on_retry(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        rpc_calls = []
        visible = {"child": None}

        async def rpc(method, params, cwd=None):
            rpc_calls.append((method, params, cwd))
            raise CodexRpcOutcomeUnknown("response stream closed")

        def find(*_args):
            child = visible["child"]
            if not child:
                return None
            return {
                "session_id": child,
                "cwd": "/repo/component",
                "thread_source": "cc-remote-fork:request-1",
                "forked_from_id": "parent",
            }

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", find)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)
        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)
        assert len(rpc_calls) == 1
        assert not any(message.type in {"error", "command_ack"}
                       for message in transport.sent)

        visible["child"] = "recovered-thread"
        await machine._process_command(cmd)
        assert len(rpc_calls) == 1
        forked = [message for message in transport.sent
                  if message.type == "session_forked"]
        assert forked and {message.session_id for message in forked} == {
            "recovered-thread"}
        assert transport.sent[-1].type == "command_ack"

    asyncio.run(run())


def test_codex_same_cwd_fork_background_reconcile_acks_current_connection(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0.001
        machine.FORK_BACKGROUND_ATTEMPTS = 10
        rpc_calls = []
        scans = 0

        async def rpc(method, params, cwd=None):
            rpc_calls.append((method, params, cwd))
            raise CodexRpcOutcomeUnknown("response stream closed")

        def find(*_args):
            nonlocal scans
            scans += 1
            # 1 pre-submit scan + 4 immediate reconcile scans; the first
            # background pass sees the durable rollout marker.
            if scans <= 5:
                return None
            return {
                "session_id": "background-child",
                "cwd": "/repo/component",
                "thread_source": "cc-remote-fork:request-1",
                "forked_from_id": "parent",
            }

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", find)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)
        task = machine._codex_fork_tasks["request-1"]
        await asyncio.wait_for(task, timeout=1)

        assert len(rpc_calls) == 1
        assert [message.type for message in transport.sent][-2:] == [
            "session_forked", "command_ack"]
        seen, cached = machine._command_seen("client-1", "cmd-1")
        assert seen and cached[0].session_id == "background-child"

        # A reconnect replay races only with the completed command cache; it
        # replays the same child and never invokes thread/fork again.
        await machine._process_command(cmd)
        assert len(rpc_calls) == 1
        assert [message.type for message in transport.sent][-2:] == [
            "session_forked", "command_ack"]

    asyncio.run(run())


def test_codex_same_cwd_fork_restart_submitted_state_never_reforks(monkeypatch):
    async def run():
        first, first_transport = _mk_machine()
        first._codex_forks.begin(
            "request-1", "parent", "turn-2", "/repo/component")
        first._codex_forks.mark_submitted("request-1")

        transport = type(first_transport)()
        machine = machine_module.WrapperMachine(first.cfg, transport)
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        rpc_calls = []

        async def rpc(*args, **kwargs):
            rpc_calls.append((args, kwargs))
            raise AssertionError("submitted request must never be replayed")

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)
        await asyncio.wait_for(
            machine._codex_fork_tasks["request-1"], timeout=1)

        assert rpc_calls == []
        assert machine._codex_forks.entries["request-1"]["status"] == "submitted"
        reconcile = next(message for message in transport.sent
                         if message.type == "error")
        assert reconcile.code == "fork_reconciling"
        assert not any(message.type == "command_ack" for message in transport.sent)

    asyncio.run(run())


def test_codex_same_identity_new_request_aliases_submitted_child(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        machine._codex_forks.begin(
            "request-old", "parent", "turn-2", "/repo/component")
        machine._codex_forks.mark_submitted("request-old")
        rpc_calls = []
        visible = {"child": None}

        async def rpc(*args, **kwargs):
            rpc_calls.append((args, kwargs))
            raise AssertionError("alias request must not issue thread/fork")

        def find(marker, *_args):
            assert marker == "cc-remote-fork:request-old"
            if not visible["child"]:
                return None
            return {
                "session_id": visible["child"],
                "cwd": "/repo/component",
                "thread_source": marker,
                "forked_from_id": "parent",
            }

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", find)
        cmd = ForkSession(
            session_id="parent", request_id="request-new",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-new")

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session(cmd)
        await asyncio.wait_for(
            machine._codex_fork_tasks["request-new"], timeout=1)
        assert machine._codex_forks.entries["request-new"]["status"] == "alias"
        assert rpc_calls == []

        visible["child"] = "shared-child"
        result = await machine._handle_fork_session(cmd)

        assert result.session_id == "shared-child"
        assert result.request_id == "request-new"
        assert rpc_calls == []
        assert {machine._codex_forks.entries[key]["session_id"] for key in (
            "request-old", "request-new")} == {"shared-child"}

    asyncio.run(run())


def test_codex_same_cwd_fork_explicit_rpc_rejection_is_terminal(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}

        async def rpc(*_args, **_kwargs):
            raise CodexRpcRejected("invalid lastTurnId")

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        await machine._process_command(cmd)

        assert [message.type for message in transport.sent][-2:] == [
            "error", "command_ack"]

    asyncio.run(run())


def test_codex_same_cwd_fork_pre_submit_failure_is_terminal(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}

        async def rpc(*_args, **_kwargs):
            raise FileNotFoundError("codex not installed")

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        await machine._process_command(cmd)

        assert [message.type for message in transport.sent][-2:] == [
            "error", "command_ack"]
        assert machine._codex_forks.entries["request-1"]["status"] == "rejected"

    asyncio.run(run())


def test_codex_worktree_fork_uses_persistent_rpc_and_returns_correlated_result(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                return {"thread": {"id": "forked-thread"}}
            if method == "thread/name/set":
                return {}
            raise AssertionError(method)

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "codex_session_settings", lambda _sid: {})

        result = await machine._handle_fork_session_worktree(_command())

        fork = next(call for call in calls if call[0] == "thread/fork")
        assert fork[1] == {
            "threadId": "parent",
            "cwd": "/state/worktrees/repo/fork-1/component",
            "ephemeral": False,
            "threadSource": "cc-remote-fork:request-1",
            "model": "gpt-test",
        }
        assert ("thread/name/set", {
            "threadId": "forked-thread", "name": "Feature fork",
        }, "/state/worktrees/repo/fork-1/component") in calls
        assert result.type == "session_forked"
        assert result.target == "worktree"
        assert result.session_id == "forked-thread"
        assert result.request_id == "request-1"
        assert result.to == "client-1"
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_codex_worktree_fork_recovers_existing_thread_without_refork(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append(method)
            if method == "thread/list":
                return {"data": [{
                    "id": "existing-fork",
                    # app-server 0.144.1 omits this field from thread/list even
                    # though thread/read returns it.
                    "forkedFromId": None,
                    "cwd": "/state/worktrees/repo/fork-1/component",
                }] if params["archived"] is False else []}
            if method == "thread/read":
                return {"thread": {
                    "id": "existing-fork",
                    "forkedFromId": "parent",
                    "cwd": "/state/worktrees/repo/fork-1/component",
                }}
            if method == "thread/name/set":
                return {}
            raise AssertionError("thread/fork must not run during recovery")

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec(created=False))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.session_id == "existing-fork"
        assert "thread/fork" not in calls

    asyncio.run(run())


def test_codex_worktree_fork_rolls_back_fresh_worktree_on_confirmed_failure(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        rolled_back = []

        async def rpc(method, params, cwd=None):
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                raise RuntimeError("fork rejected")
            raise AssertionError(method)

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "codex_session_settings", lambda _sid: {})
        monkeypatch.setattr(
            machine_module, "rollback_worktree", lambda spec: rolled_back.append(spec))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error"
        assert result.request_id == "request-1"
        assert "派生失败" in result.message
        assert rolled_back == [_spec()]
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_codex_worktree_fork_rejects_running_parent_before_git(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx("running")}
        prepared = []

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: prepared.append(True))

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error" and result.code == "busy"
        assert prepared == []

    asyncio.run(run())


def test_codex_worktree_message_fork_allows_completed_turn_while_parent_runs(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx("running")}
        prepared = []

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: (
            prepared.append(True) or (_ for _ in ()).throw(RuntimeError("stop"))))

        try:
            await machine._handle_fork_session_worktree(
                _command(last_turn_id="completed-turn"))
        except RuntimeError as exc:
            assert str(exc) == "stop"
        assert prepared == [True]

    asyncio.run(run())
