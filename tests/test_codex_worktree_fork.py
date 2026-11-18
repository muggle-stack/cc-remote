from __future__ import annotations

import asyncio
import json
import time
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
from cc_remote.codex_profiles import CodexProfile
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_worktrees import WorktreeSpec
from cc_remote.wrapper.codex_forks import CodexForkJournal
from cc_remote.wrapper.codex_forks import ForkJournalError
from cc_remote.wrapper.codex_rpc import CodexRpcOutcomeUnknown, CodexRpcRejected
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.fixture(autouse=True)
def _profile_model_catalog(monkeypatch):
    """Keep fork tests independent of the developer machine's live catalog."""
    async def catalog(*, codex_home=None, model_provider=None):
        return [
            {
                "id": "gpt-test",
                "efforts": ["high"],
                "default_effort": "high",
                "is_default": True,
            },
            {
                "id": "gpt-changed",
                "efforts": ["high"],
                "default_effort": "high",
                "is_default": False,
            },
        ]

    monkeypatch.setattr(machine_module, "codex_catalog", catalog)
    monkeypatch.setattr(machine_module, "codex_session_provider", lambda *_args, **_kwargs: "openai")
    monkeypatch.setattr(
        machine_module, "codex_current_provider", lambda **_kwargs: "")
    async def active_session(*_args, **_kwargs):
        return False
    monkeypatch.setattr(
        machine_module.WrapperMachine,
        "_native_session_archived_state",
        active_session,
    )


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


def test_same_cwd_fork_invalidates_catalog_before_publication(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine._codex_forks.begin(
            "request-1",
            "parent",
            "turn-2",
            "/repo/component",
        )
        machine._codex_session_list_cache = (
            0.0,
            [{"session_id": "parent"}],
            (),
        )
        initial_epoch = machine._codex_session_list_epoch
        sent = transport.send

        async def assert_fresh_catalog(message):
            if message.type == "session_forked":
                assert machine._codex_session_list_cache is None
                assert machine._codex_session_list_epoch == initial_epoch + 1
            await sent(message)

        async def list_sessions(_cmd):
            assert machine._codex_session_list_cache is None

        monkeypatch.setattr(transport, "send", assert_fresh_catalog)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)

        result = await machine._finish_same_cwd_fork(
            _command(last_turn_id="turn-2"),
            "parent",
            "/repo/component",
            "forked-thread",
        )

        assert result.session_id == "forked-thread"
        assert [message.type for message in transport.sent] == [
            "session_list_invalidated",
            "session_forked",
        ]

    asyncio.run(run())


def test_worktree_fork_invalidates_catalog_before_publication(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        spec = _spec()
        machine._codex_forks.begin(
            "request-1",
            "parent",
            "cc-remote-worktree-head",
            spec.cwd,
            target="worktree",
        )
        machine._codex_session_list_cache = (
            0.0,
            [{"session_id": "parent"}],
            (),
        )
        initial_epoch = machine._codex_session_list_epoch
        sent = transport.send

        async def assert_fresh_catalog(message):
            if message.type == "session_forked":
                assert machine._codex_session_list_cache is None
                assert machine._codex_session_list_epoch == initial_epoch + 1
            await sent(message)

        async def list_sessions(_cmd):
            assert machine._codex_session_list_cache is None

        monkeypatch.setattr(transport, "send", assert_fresh_catalog)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)

        result = await machine._finish_worktree_fork(
            _command(name=""),
            "parent",
            spec,
            "forked-thread",
            "cc-remote-fork:request-1",
        )

        assert result.session_id == "forked-thread"
        assert [message.type for message in transport.sent] == [
            "session_list_invalidated",
            "session_forked",
        ]

    asyncio.run(run())


def test_same_cwd_fork_refresh_includes_child_after_fresh_parent_cache(
    monkeypatch, tmp_path,
):
    async def run():
        machine, _ = _mk_machine()
        machine._codex_forks.begin(
            "request-1",
            "parent",
            "turn-2",
            "/repo/component",
        )
        machine._codex_session_list_cache = (
            time.monotonic(),
            [{"session_id": "parent"}],
            (),
        )
        published = []
        rollout = tmp_path / (
            "rollout-2026-08-13T09-06-10-forked-thread.jsonl")
        rollout.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "forked-thread",
                "cwd": "/repo/component",
                "thread_source": "cc-remote-fork:request-1",
                "forked_from_id": "parent",
            },
        }) + "\n")

        async def read_catalog():
            return ([
                {
                    "session_id": "parent",
                    "native_session_id": "parent",
                    "codex_profile_id": "primary",
                    "codex_profile_label": "Primary",
                    "summary": "Parent task",
                    "cwd": "/repo/component",
                    "last_modified": "1",
                },
            ], ())

        async def send_list(_cmd, raw, **_kwargs):
            published.append(raw)

        def find_rollouts(candidates):
            assert [item[2] for item in candidates] == ["forked-thread"]
            return {("primary", "forked-thread"): (str(rollout), False)}

        monkeypatch.setattr(
            machine,
            "_read_codex_profile_catalog",
            read_catalog,
        )
        monkeypatch.setattr(
            machine,
            "_find_completed_codex_fork_rollouts",
            find_rollouts,
        )
        monkeypatch.setattr(machine, "_send_codex_session_list", send_list)

        await machine._finish_same_cwd_fork(
            _command(last_turn_id="turn-2"),
            "parent",
            "/repo/component",
            "forked-thread",
        )

        assert len(published) == 1
        by_id = {row["session_id"]: row for row in published[0]}
        assert set(by_id) == {"parent", "forked-thread"}
        assert by_id["forked-thread"]["summary"] == "Parent task (fork)"
        assert by_id["forked-thread"]["forked_from_id"] == "parent"
        assert by_id["forked-thread"]["status"] == "notLoaded"

    asyncio.run(run())


def test_completed_fork_without_rollout_is_not_restored(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine._codex_forks.begin(
            "request-1", "parent", "turn-2", "/repo/component")
        machine._codex_forks.complete("request-1", "deleted-child")

        async def read_catalog():
            return ([{
                "session_id": "parent",
                "native_session_id": "parent",
                "codex_profile_id": "primary",
            }], ())

        monkeypatch.setattr(
            machine, "_read_codex_profile_catalog", read_catalog)
        monkeypatch.setattr(
            machine,
            "_find_completed_codex_fork_rollouts",
            lambda _candidates: {},
        )

        rows = await machine._refresh_codex_session_catalog()

        assert [row["session_id"] for row in rows] == ["parent"]

    asyncio.run(run())


def test_renamed_fork_keeps_title_while_native_catalog_omits_it(
    monkeypatch, tmp_path,
):
    async def run():
        machine, _ = _mk_machine()
        machine._codex_forks.begin(
            "request-1", "parent", "turn-2", "/repo/component")
        machine._codex_forks.complete("request-1", "forked-thread")
        rollout = tmp_path / (
            "rollout-2026-08-13T09-06-10-forked-thread.jsonl")
        rollout.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "id": "forked-thread",
                "cwd": "/repo/component",
                "thread_source": "cc-remote-fork:request-1",
                "forked_from_id": "parent",
            },
        }) + "\n")
        published = []
        rpc_calls = []

        async def is_codex(_sid):
            return True

        async def rpc(sid, method, params, **_kwargs):
            rpc_calls.append((sid, method, params))
            return {}

        async def read_catalog():
            return ([{
                "session_id": "parent",
                "native_session_id": "parent",
                "codex_profile_id": "primary",
                "codex_profile_label": "Primary",
                "summary": "Parent task",
            }], ())

        async def send_list(_cmd, raw, **_kwargs):
            published.append(raw)

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_codex_rpc_for_wire", rpc)
        monkeypatch.setattr(
            machine, "_read_codex_profile_catalog", read_catalog)
        monkeypatch.setattr(
            machine,
            "_find_completed_codex_fork_rollouts",
            lambda _candidates: {
                ("primary", "forked-thread"): (str(rollout), False),
            },
        )
        monkeypatch.setattr(machine, "_send_codex_session_list", send_list)

        await machine._handle_rename_session(SimpleNamespace(
            session_id="forked-thread",
            title="Renamed fork",
            engine="codex",
            space="code",
        ))

        assert rpc_calls == [(
            "forked-thread",
            "thread/name/set",
            {"threadId": "forked-thread", "name": "Renamed fork"},
        )]
        by_id = {row["session_id"]: row for row in published[-1]}
        assert by_id["forked-thread"]["summary"] == "Renamed fork"
        assert machine._codex_forks.completed_results(1)[0][
            "title"] == "Renamed fork"

    asyncio.run(run())


def test_completed_fork_rollout_scan_is_batched_and_archive_aware(tmp_path):
    machine, _ = _mk_machine()
    profile = CodexProfile(
        id="primary",
        label="Primary",
        home=tmp_path,
        is_default=True,
    )
    active = tmp_path / "sessions" / "2026" / "08" / "13"
    archived = tmp_path / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir()
    active_child = active / (
        "rollout-2026-08-13T09-06-10-child-active.jsonl")
    archived_child = archived / (
        "rollout-2026-08-12T09-06-10-child-archived.jsonl")
    active_child.write_text("{}\n")
    archived_child.write_text("{}\n")
    candidates = [
        ({}, profile, "child-active", "parent", "parent"),
        ({}, profile, "child-archived", "parent", "parent"),
    ]

    found = machine._find_completed_codex_fork_rollouts(candidates)

    assert found[("primary", "child-active")] == (
        str(active_child.resolve()), False)
    assert found[("primary", "child-archived")] == (
        str(archived_child.resolve()), True)


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
        assert result.cwd == "/repo/component"
        assert result.target == "same_cwd"
        assert result.git_branch is None
        assert result.last_turn_id == "turn-2"
        assert duplicate.session_id == "forked-thread"
        assert "request-1" not in machine._codex_fork_locks

        # A new wrapper process sees the completed result rather than relying on
        # the in-memory command ACK cache.
        reloaded = CodexForkJournal(machine.cfg.state_dir)
        entry = reloaded.begin(
            "request-1", "parent", "turn-2", "/repo/component")
        assert entry["status"] == "complete"
        assert entry["session_id"] == "forked-thread"

    asyncio.run(run())


def test_archived_codex_parent_rejects_new_fork_before_rpc(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}

        async def is_codex(_sid):
            return True

        async def archived(*_args, **_kwargs):
            return True

        async def rpc(*_args, **_kwargs):
            raise AssertionError("archived parent must not reach thread/fork")

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine, "_native_session_archived_state", archived)
        monkeypatch.setattr(machine, "_codex_rpc_for_wire", rpc)

        result = await machine._handle_fork_session(
            _command(last_turn_id="turn-2"))

        assert result.type == "error" and result.code == "auth"
        assert "已归档" in result.message
        assert machine._codex_forks.entries[
            "request-1"]["status"] == "rejected"

    asyncio.run(run())


def test_completed_codex_fork_replays_after_parent_is_archived(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}
        calls = 0
        archived_state = {"value": False}

        async def is_codex(_sid):
            return True

        async def archived(*_args, **_kwargs):
            return archived_state["value"]

        async def rpc(_wire_sid, method, _params, **_kwargs):
            nonlocal calls
            assert method == "thread/fork"
            calls += 1
            return {"thread": {"id": "forked-thread"}}

        async def list_sessions(_cmd):
            return None

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine, "_native_session_archived_state", archived)
        monkeypatch.setattr(machine, "_codex_rpc_for_wire", rpc)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(
            machine_module, "find_rollout_fork", lambda *_args: None)
        command = _command(last_turn_id="turn-2")

        first = await machine._handle_fork_session(command)
        archived_state["value"] = True
        replay = await machine._handle_fork_session(command)

        assert first.session_id == replay.session_id == "forked-thread"
        assert calls == 1

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


@pytest.mark.parametrize("provider", ["openai", "custom", None])
def test_codex_fork_resolves_parent_model_only_in_its_native_provider(monkeypatch, provider):
    async def run():
        machine, _ = _mk_machine()
        parent = _ctx()
        parent.sdk.model = "retired-model"
        machine.sessions = {"parent": parent}
        calls = []

        async def catalog(*, codex_home=None, model_provider=None):
            assert model_provider == provider
            return [{
                "id": "current-default",
                "efforts": ["low"],
                "default_effort": "low",
                "is_default": True,
            }]

        async def rpc(method, params, cwd=None):
            calls.append((method, params, cwd))
            return {"thread": {"id": "forked-thread"}}

        async def is_codex(_sid): return True
        async def list_sessions(_cmd): return None
        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(machine, "_list_codex_sessions", list_sessions)
        monkeypatch.setattr(machine_module, "codex_catalog", catalog)
        monkeypatch.setattr(
            machine_module, "codex_session_provider", lambda *_args, **_kwargs: provider)
        monkeypatch.setattr(machine_module, "codex_rpc", rpc)
        monkeypatch.setattr(
            machine_module, "find_rollout_fork", lambda *_args: None)

        result = await machine._handle_fork_session(
            _command(last_turn_id="turn-2"))

        assert result.session_id == "forked-thread"
        expected = "current-default" if provider == "openai" else "retired-model"
        assert calls[0][1]["model"] == expected
        assert machine._codex_forks.entries[
            "request-1"]["controls"]["model"] == expected

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
        assert any(
            message.type == "session_list_invalidated"
            for message in transport.sent
        )
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


def test_archived_codex_parent_rejects_worktree_fork_before_git(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {"parent": _ctx()}

        async def is_codex(_sid):
            return True

        async def archived(*_args, **_kwargs):
            return True

        def prepare(*_args, **_kwargs):
            raise AssertionError("archived parent must not create a worktree")

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine, "_native_session_archived_state", archived)
        monkeypatch.setattr(machine_module, "prepare_worktree", prepare)

        result = await machine._handle_fork_session_worktree(_command())

        assert result.type == "error" and result.code == "auth"
        assert "已归档" in result.message

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
