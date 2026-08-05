from __future__ import annotations

import asyncio
import os
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

# The source production path (machine.py's ``_handle_fork_session_locked``)
# normalizes the source cwd with ``os.path.realpath`` before using it as a
# journal identity key. On Windows that turns a bare "/repo/component" into
# "D:\\repo\\component" (drive-relative root), while POSIX leaves it
# unchanged, so tests must route every occurrence through the same call to
# stay aligned with what the code under test actually stores/compares.
_SOURCE_CWD = os.path.realpath("/repo/component")


def _ctx(state: str = "idle"):
    ctx = _mk_ctx("parent", "parent")
    ctx.engine = "codex"
    ctx.state = state
    ctx.cwd = _SOURCE_CWD
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
            "model": "gpt-test",
            "approvalPolicy": "never",
        }, None)]
        assert result.session_id == "forked-thread"
        assert result.cwd == _SOURCE_CWD
        assert result.target == "same_cwd"
        assert result.git_branch is None
        assert result.last_turn_id == "turn-2"
        assert duplicate.session_id == "forked-thread"
        assert "request-1" not in machine._codex_fork_locks

        # A new wrapper process sees the completed result rather than relying on
        # the in-memory command ACK cache.
        reloaded = CodexForkJournal(machine.cfg.state_dir)
        entry = reloaded.begin(
            "request-1", "parent", "turn-2", _SOURCE_CWD)
        assert entry["status"] == "complete"
        assert entry["session_id"] == "forked-thread"

    asyncio.run(run())


def test_codex_same_cwd_fork_inherits_model_and_permissions_once(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        parent = _ctx()
        parent.sdk.approval = "on-request"
        parent.sdk.permission_profile = ":workspace"
        parent.sdk.web_search = "live"
        machine.sessions = {"parent": parent}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            return {"thread": {"id": "forked-thread"}}

        async def profiles(cwd):
            assert cwd == "/repo/component"
            return [{
                "id": ":workspace", "description": None, "allowed": True,
            }]

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "codex_permission_profiles", profiles)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)

        command = _command(last_turn_id="turn-2")
        await machine._handle_fork_session(command)
        assert calls[0][1]["model"] == "gpt-test"
        assert calls[0][1]["approvalPolicy"] == "on-request"
        assert calls[0][1]["permissions"] == ":workspace"
        assert calls[0][1]["config"] == {"web_search": "live"}
        inherited = machine._codex_controls.get("forked-thread")
        assert inherited.approval_policy == "on-request"
        assert inherited.permission_profile == ":workspace"
        assert inherited.web_search == "live"
        assert machine._codex_forks.entries[
            "request-1"]["controls"] == {
                "model": "gpt-test",
                "approval_policy": "on-request",
                "permission_profile": ":workspace",
                "web_search": "live",
            }

        machine._codex_controls.update(
            "forked-thread",
            approval_policy="never",
            permission_profile=":danger-full-access",
            web_search=None,
        )
        parent.sdk.model = "gpt-changed"
        parent.sdk.approval = "never"
        await machine._handle_fork_session(command)
        child_choice = machine._codex_controls.get("forked-thread")
        assert child_choice.approval_policy == "never"
        assert child_choice.permission_profile == ":danger-full-access"
        assert len(calls) == 1

    asyncio.run(run())


def test_codex_same_cwd_fork_does_not_flatten_granular_approval(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        parent = _ctx()
        parent.sdk.approval = "on-request"
        parent.sdk.approval_policy = {"granular": {
            "mcp_elicitations": True,
            "rules": False,
            "sandbox_approval": True,
        }}
        parent.sdk.permission_profile = ":workspace"
        machine.sessions = {"parent": parent}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            return {"thread": {"id": "forked-thread"}}

        async def profiles(_cwd):
            return [{
                "id": ":workspace", "description": None, "allowed": True,
            }]

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "codex_permission_profiles", profiles)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)

        await machine._handle_fork_session(
            _command(last_turn_id="turn-2"))

        params = calls[0][1]
        assert "approvalPolicy" not in params
        assert params["permissions"] == ":workspace"
        assert machine._codex_forks.entries[
            "request-1"]["controls"] == {
                "model": "gpt-test",
                "permission_profile": ":workspace",
            }

    asyncio.run(run())


def test_codex_same_cwd_fork_recovers_committed_rollout_intent(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine._codex_forks.begin(
            "request-1", "parent", "turn-2", _SOURCE_CWD)

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        async def rpc(*_args, **_kwargs):
            raise AssertionError("thread/fork must not be repeated")

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: {
            "session_id": "recovered-thread",
            "cwd": _SOURCE_CWD,
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
        parent = _ctx()
        parent.sdk.web_search = "cached"
        machine.sessions = {"parent": parent}
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
                "cwd": _SOURCE_CWD,
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
                "cwd": _SOURCE_CWD,
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
            "request-1", "parent", "turn-2", _SOURCE_CWD)
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
            "request-old", "parent", "turn-2", _SOURCE_CWD)
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
                "cwd": _SOURCE_CWD,
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
        assert "request-1" not in machine._codex_fork_locks

    asyncio.run(run())


def test_codex_fork_submit_journal_failure_releases_intent_lock(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}

        async def is_codex(_sid):
            return True

        def fail_submission(_request_id):
            raise ForkJournalError("disk full")

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine_module, "find_rollout_fork", lambda *_args: None)
        monkeypatch.setattr(
            machine._codex_forks, "claim_submission", fail_submission)
        cmd = ForkSession(
            session_id="parent", request_id="request-1",
            last_turn_id="turn-2", client_id="client-1", cmd_id="cmd-1")

        await machine._process_command(cmd)

        assert [message.type for message in transport.sent][-2:] == [
            "error", "command_ack"]
        assert machine._codex_forks.entries["request-1"]["status"] == "intent"
        assert "request-1" not in machine._codex_fork_locks

    asyncio.run(run())


def test_codex_worktree_fork_uses_persistent_rpc_and_returns_correlated_result(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        parent = _ctx()
        parent.sdk.web_search = "cached"
        machine.sessions = {"parent": parent}
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                assert machine._codex_forks.entries[
                    "request-1"]["status"] == "submitted"
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
            "approvalPolicy": "never",
            "config": {"web_search": "cached"},
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
        duplicate = await machine._handle_fork_session_worktree(_command())
        assert duplicate.session_id == "forked-thread"
        assert sum(method == "thread/name/set" for method, *_ in calls) == 1
        assert "request-1" not in machine._codex_fork_locks
        assert machine._codex_controls.get(
            "forked-thread").approval_policy == "never"
        assert machine._codex_controls.get(
            "forked-thread").web_search == "cached"

    asyncio.run(run())


def test_codex_cold_fork_does_not_restore_stale_named_over_granular(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        machine._codex_controls.update(
            "parent",
            approval_policy="on-request",
            permission_profile=":workspace",
            web_search=None,
        )
        monkeypatch.setattr(
            machine_module,
            "codex_session_settings",
            lambda _sid: {
                "model": "gpt-test",
                "approval_policy_granular": True,
            },
        )

        controls = await machine._codex_fork_control_snapshot("parent", None)

        assert controls == {
            "model": "gpt-test",
            "permission_profile": ":workspace",
        }

    asyncio.run(run())


def test_worktree_retry_never_overwrites_name_after_journal_failure(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        visible_name = {"value": None}
        name_sets = []

        async def rpc(method, params, cwd=None):
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                return {"thread": {"id": "forked-thread"}}
            if method == "thread/name/set":
                visible_name["value"] = params["name"]
                name_sets.append(params["name"])
                return {}
            if method == "thread/read":
                return {"thread": {
                    "id": "forked-thread",
                    "forkedFromId": "parent",
                    "cwd": cwd,
                    "name": visible_name["value"],
                }}
            raise AssertionError(method)

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine, "_ensure_codex_fork_reconciler",
                            lambda *_args, **_kwargs: None)
        original_finalize = machine._codex_forks.mark_name_finalized
        attempts = 0

        def fail_once(request_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ForkJournalError("disk full")
            return original_finalize(request_id)

        monkeypatch.setattr(
            machine._codex_forks, "mark_name_finalized", fail_once)

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session_worktree(_command())
        visible_name["value"] = "User renamed this thread"

        result = await machine._handle_fork_session_worktree(_command())

        assert result.session_id == "forked-thread"
        assert visible_name["value"] == "User renamed this thread"
        assert name_sets == ["Feature fork"]
        assert machine._codex_forks.entries[
            "request-1"]["name_finalized"] is True

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
                raise CodexRpcRejected("fork rejected")
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
        assert result.message == "Codex 会话派生未完成，请稍后重试。"
        assert "fork rejected" not in result.message
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


def test_codex_worktree_fork_revalidates_parent_permission_profile(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        parent = _ctx()
        parent.sdk.permission_profile = ":workspace"
        parent.sdk.approval = "on-request"
        machine.sessions = {"parent": parent}
        rolled_back = []

        async def is_codex(_sid): return True

        async def profiles(cwd):
            assert cwd == "/state/worktrees/repo/fork-1/component"
            return [{
                "id": ":workspace", "description": None, "allowed": False,
            }]

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "codex_permission_profiles", profiles)
        monkeypatch.setattr(
            machine_module,
            "rollback_worktree",
            lambda spec: rolled_back.append(spec),
        )

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error" and result.code == "auth"
        assert rolled_back == [_spec()]
        assert transport.sent[-1] is result

    asyncio.run(run())


def test_codex_worktree_fork_retry_uses_its_frozen_permission_snapshot(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        parent = _ctx()
        parent.sdk.permission_profile = ":workspace"
        parent.sdk.approval = "on-request"
        machine.sessions = {"parent": parent}
        machine._codex_forks.begin(
            "request-1",
            "parent",
            "cc-remote-worktree-head",
            "/state/worktrees/repo/fork-1/component",
            {
                "model": "gpt-test",
                "approval_policy": "on-request",
                "permission_profile": ":workspace",
            },
            target="worktree",
        )

        async def is_codex(_sid): return True

        async def profiles(_cwd):
            raise AssertionError(
                "a durable retry must not re-probe the frozen profile")

        async def rpc(method, params, cwd=None):
            if method == "thread/list":
                return {"data": [{
                    "id": "existing-fork",
                    "forkedFromId": "parent",
                    "cwd": cwd,
                }] if params["archived"] is False else []}
            if method == "thread/name/set":
                return {}
            raise AssertionError(method)

        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec(created=False))
        monkeypatch.setattr(machine_module, "codex_permission_profiles", profiles)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)

        result = await machine._handle_fork_session_worktree(_command())

        assert result.session_id == "existing-fork"
        inherited = machine._codex_controls.get("existing-fork")
        assert inherited.approval_policy == "on-request"
        assert inherited.permission_profile == ":workspace"

    asyncio.run(run())


def test_codex_worktree_fork_restart_submitted_state_never_reforks(monkeypatch):
    async def run():
        first, first_transport = _mk_machine()
        first._codex_forks.begin(
            "request-1",
            "parent",
            "cc-remote-worktree-head",
            "/state/worktrees/repo/fork-1/component",
            {"model": "gpt-test"},
            target="worktree",
        )
        first._codex_forks.mark_submitted("request-1")

        transport = type(first_transport)()
        machine = machine_module.WrapperMachine(first.cfg, transport)
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_RECONCILE_ATTEMPTS = 1
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            if method in {"thread/list", "thread/read"}:
                return {"data": []} if method == "thread/list" else {}
            raise AssertionError("submitted worktree fork must never be replayed")

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec(created=False))
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)

        cmd = _command(cmd_id="cmd-1")
        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session_worktree(cmd)
        await asyncio.wait_for(
            machine._codex_fork_tasks["request-1"], timeout=1)

        assert not any(method == "thread/fork" for method, *_rest in calls)
        assert machine._codex_forks.entries[
            "request-1"]["status"] == "submitted"
        assert "request-1" in machine._codex_fork_locks
        assert any(
            message.type == "error" and message.code == "fork_reconciling"
            for message in transport.sent
        )

    asyncio.run(run())


def test_codex_worktree_unknown_outcome_keeps_worktree_and_never_acks(
    monkeypatch,
):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_RECONCILE_ATTEMPTS = 1
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        rolled_back = []

        async def rpc(method, params, cwd=None):
            if method == "thread/list":
                return {"data": []}
            if method == "thread/fork":
                raise CodexRpcOutcomeUnknown("response lost after submit")
            if method == "thread/read":
                return {}
            raise AssertionError(method)

        async def is_codex(_sid): return True
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "prepare_worktree", lambda *_args: _spec())
        monkeypatch.setattr(machine_module, "find_rollout_fork", lambda *_args: None)
        monkeypatch.setattr(
            machine_module,
            "rollback_worktree",
            lambda spec: rolled_back.append(spec),
        )

        cmd = _command(cmd_id="cmd-1")
        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session_worktree(cmd)
        await asyncio.wait_for(
            machine._codex_fork_tasks["request-1"], timeout=1)

        assert rolled_back == []
        assert machine._codex_forks.entries[
            "request-1"]["status"] == "uncertain"
        assert not any(message.type == "command_ack" for message in transport.sent)

    asyncio.run(run())


def test_codex_worktree_background_reconcile_publishes_worktree_result(
    monkeypatch,
):
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_RECONCILE_ATTEMPTS = 1
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        machine._codex_forks.begin(
            "request-1",
            "parent",
            "cc-remote-worktree-head",
            "/state/worktrees/repo/fork-1/component",
            {"model": "gpt-test"},
            target="worktree",
        )
        machine._codex_forks.mark_submitted("request-1")
        marker_reads = 0
        rpc_methods = []

        def find_marker(*_args):
            nonlocal marker_reads
            marker_reads += 1
            if marker_reads >= 2:
                return {"session_id": "background-child"}
            return None

        async def rpc(method, params, cwd=None):
            rpc_methods.append(method)
            if method == "thread/list":
                return {"data": []}
            if method == "thread/name/set":
                return {}
            raise AssertionError(method)

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(machine_module, "find_rollout_fork", find_marker)
        monkeypatch.setattr(
            machine_module, "prepare_worktree", lambda *_args: _spec(created=False))

        cmd = _command(cmd_id="cmd-1")
        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session_worktree(cmd)
        await asyncio.wait_for(
            machine._codex_fork_tasks["request-1"], timeout=1)

        assert "thread/fork" not in rpc_methods
        event = next(
            message for message in transport.sent
            if message.type == "session_forked")
        assert event.session_id == "background-child"
        assert event.target == "worktree"
        assert event.cwd == "/state/worktrees/repo/fork-1/component"
        assert any(message.type == "command_ack" for message in transport.sent)
        assert "request-1" not in machine._codex_fork_locks

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
