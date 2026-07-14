"""Zero-token transaction coverage for Claude message-level session forks."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import ForkSession, SessionForked
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.claude_forks import (
    ClaudeForkJournalError,
    claude_fork_marker,
)
from tests.test_multisession import _mk_ctx, _mk_machine


PARENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CUTOFF = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CHILD = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CWD = "/repo/component"


def _info(
    session_id: str = PARENT,
    *,
    cwd: str = CWD,
    title: str = "Source session",
):
    return SimpleNamespace(
        session_id=session_id,
        custom_title=title,
        summary=title,
        first_prompt="first prompt",
        last_modified=None,
        git_branch=None,
        cwd=cwd,
        tag=None,
    )


def _command(
    *,
    request_id: str = "request-1",
    cmd_id: str = "cmd-1",
    session_id: str = PARENT,
    cutoff: str = CUTOFF,
) -> ForkSession:
    return ForkSession(
        session_id=session_id,
        request_id=request_id,
        last_turn_id=cutoff,
        client_id="client-1",
        cmd_id=cmd_id,
    )


async def _is_claude(_session_id: str) -> bool:
    return False


async def _no_list(_cmd) -> None:
    return None


def _resident_machine(monkeypatch):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(PARENT, PARENT)
    ctx.engine = "claude"
    ctx.cwd = CWD
    machine.sessions = {PARENT: ctx}
    monkeypatch.setattr(machine, "_is_codex_session", _is_claude)
    return machine, transport


def test_claude_handler_passes_exact_sdk_boundary_then_renames_and_lists(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        operations: list[tuple] = []
        visible_title = {"value": claude_fork_marker("request-1")}

        def get_info(session_id, directory=None):
            operations.append(("info", session_id, directory))
            if session_id == PARENT:
                return _info(title="Source session")
            assert session_id == CHILD
            return _info(CHILD, title=visible_title["value"])

        def fork(parent, *, directory, up_to_message_id, title):
            operations.append((
                "fork", parent, directory, up_to_message_id, title))
            return SimpleNamespace(session_id=CHILD)

        def rename(session_id, title, directory=None):
            operations.append(("rename", session_id, title, directory))
            visible_title["value"] = title

        def list_all(*, limit):
            operations.append(("list", limit, visible_title["value"]))
            assert visible_title["value"] == "Source session (fork)"
            return [_info(CHILD, title=visible_title["value"])]

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(machine_module, "rename_session", rename)
        monkeypatch.setattr(machine_module, "list_sessions", list_all)
        monkeypatch.setattr(machine, "_bg_blocked_session_ids", lambda: set())

        result = await machine._handle_fork_session(_command())

        assert isinstance(result, SessionForked)
        assert (
            result.parent_session_id,
            result.session_id,
            result.cwd,
            result.target,
            result.last_turn_id,
            result.request_id,
            result.to,
        ) == (
            PARENT, CHILD, CWD, "same_cwd", CUTOFF, "request-1", "client-1",
        )
        assert ("fork", PARENT, CWD, CUTOFF,
                claude_fork_marker("request-1")) in operations
        assert ("rename", CHILD, "Source session (fork)", CWD) in operations
        assert next(i for i, row in enumerate(operations) if row[0] == "rename") \
            < next(i for i, row in enumerate(operations) if row[0] == "list")
        assert [message.type for message in transport.sent] == [
            "session_forked", "session_list"]
        assert transport.sent[-1].sessions[0].session_id == CHILD
        assert machine._claude_forks.entries["request-1"]["status"] == "complete"
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_same_reliable_request_replays_result_without_second_sdk_fork(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        fork_calls = 0

        def get_info(session_id, directory=None):
            return _info(
                session_id,
                title=("Source session" if session_id == PARENT
                       else "Source session (fork)"),
            )

        def fork(*_args, **_kwargs):
            nonlocal fork_calls
            fork_calls += 1
            return SimpleNamespace(session_id=CHILD)

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)
        cmd = _command()

        await machine._process_command(cmd)
        await machine._process_command(cmd)

        assert fork_calls == 1
        forked = [message for message in transport.sent
                  if message.type == "session_forked"]
        assert len(forked) == 2
        assert {message.session_id for message in forked} == {CHILD}
        assert len([message for message in transport.sent
                    if message.type == "command_ack"]) == 2
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_new_request_for_same_submitted_identity_aliases_without_refork(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_BACKGROUND_ATTEMPTS = 1
        machine._claude_forks.begin(
            "request-old", PARENT, CUTOFF, CWD)
        machine._claude_forks.mark_submitted("request-old")
        fork_calls = 0
        markers: list[str] = []

        def get_info(session_id, directory=None):
            return _info(
                session_id,
                title=("Source session" if session_id == PARENT
                       else "Source session (fork)"),
            )

        def fork(*_args, **_kwargs):
            nonlocal fork_calls
            fork_calls += 1
            raise AssertionError("an alias must never issue a second SDK fork")

        def find(marker, parent, cutoff, cwd):
            markers.append(marker)
            assert (parent, cutoff, cwd) == (PARENT, CUTOFF, CWD)
            return {"session_id": CHILD, "cwd": CWD, "marker": marker}

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(machine_module, "find_claude_fork", find)
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)
        cmd = _command(request_id="request-new", cmd_id="cmd-new")

        # get_canonical exposes the submitted root immediately, so the alias can
        # recover synchronously without first entering background reconciliation.
        await machine._process_command(cmd)

        assert fork_calls == 0
        assert markers == [claude_fork_marker("request-old")]
        entries = machine._claude_forks.entries
        assert entries["request-new"]["canonical_request_id"] == "request-old"
        assert {entries[key]["session_id"] for key in (
            "request-old", "request-new")} == {CHILD}
        assert [message.type for message in transport.sent][-2:] == [
            "session_forked", "command_ack"]
        assert "request-new" not in machine._claude_fork_locks

    asyncio.run(run())


def test_submitted_marker_is_recovered_without_repeating_sdk_fork(monkeypatch):
    async def run():
        machine, _ = _resident_machine(monkeypatch)
        machine._claude_forks.begin("request-1", PARENT, CUTOFF, CWD)
        machine._claude_forks.mark_submitted("request-1")
        scans: list[tuple] = []

        def get_info(session_id, directory=None):
            return _info(
                session_id,
                title=("Source session" if session_id == PARENT
                       else "Source session (fork)"),
            )

        def fork(*_args, **_kwargs):
            raise AssertionError("submitted marker recovery must not refork")

        def find(marker, parent, cutoff, cwd):
            scans.append((marker, parent, cutoff, cwd))
            return {"session_id": CHILD, "cwd": CWD, "marker": marker}

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(machine_module, "find_claude_fork", find)
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)

        result = await machine._handle_fork_session(_command())

        assert result.session_id == CHILD
        assert scans == [(
            claude_fork_marker("request-1"), PARENT, CUTOFF, CWD)]
        assert machine._claude_forks.entries["request-1"]["status"] == "complete"
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_invalid_cutoff_is_terminal_rejection_and_is_acked(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        fork_calls = 0

        def fork(*_args, **_kwargs):
            nonlocal fork_calls
            fork_calls += 1
            raise ValueError("message cutoff was not found")

        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda session_id, directory=None: _info(session_id),
        )
        monkeypatch.setattr(machine_module, "fork_session", fork)
        cmd = _command()

        await machine._process_command(cmd)
        await machine._process_command(cmd)

        assert fork_calls == 1
        assert machine._claude_forks.entries["request-1"]["status"] == "rejected"
        assert [message.type for message in transport.sent] == [
            "error", "command_ack", "error", "command_ack"]
        assert all(message.to == "client-1" for message in transport.sent)
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_pre_journal_terminal_error_also_releases_request_lock(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda _session_id, directory=None: None,
        )

        result = await machine._handle_fork_session(_command())

        assert result.type == "error"
        assert [message.type for message in transport.sent] == ["error"]
        assert machine._claude_forks.get("request-1") is None
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_ambiguous_sdk_exception_is_not_acked_and_retry_only_reconciles(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_RECONCILE_ATTEMPTS = 1
        fork_calls = 0
        scheduled: list[tuple] = []

        def fork(*_args, **_kwargs):
            nonlocal fork_calls
            fork_calls += 1
            raise OSError("write outcome unknown")

        monkeypatch.setattr(
            machine_module, "get_session_info",
            lambda session_id, directory=None: _info(session_id),
        )
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(
            machine_module, "find_claude_fork", lambda *_args: None)
        monkeypatch.setattr(
            machine, "_ensure_claude_fork_reconciler",
            lambda *args: scheduled.append(args),
        )
        cmd = _command()

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)
        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._process_command(cmd)

        assert fork_calls == 1
        assert len(scheduled) == 2
        assert machine._claude_forks.entries["request-1"]["status"] == "uncertain"
        assert not any(message.type in {"error", "command_ack"}
                       for message in transport.sent)
        assert machine._command_seen("client-1", "cmd-1") == (False, ())
        # Unresolved requests intentionally retain their per-request lock.
        assert "request-1" in machine._claude_fork_locks

    asyncio.run(run())


@pytest.mark.parametrize(("status", "child_lookup"), [
    ("submitted", "missing"),
    ("complete", "raises"),
])
def test_recovery_does_not_require_parent_session_info(
    monkeypatch, status, child_lookup,
):
    """The durable journal remains authoritative after the parent disappears."""
    async def run():
        machine, _ = _resident_machine(monkeypatch)
        machine._claude_forks.begin("request-1", PARENT, CUTOFF, CWD)
        machine._claude_forks.mark_submitted("request-1")
        if status == "complete":
            machine._claude_forks.complete("request-1", CHILD)
        lookups: list[tuple] = []
        scans: list[tuple] = []

        def get_info(session_id, directory=None):
            lookups.append((session_id, directory))
            # A source lookup would return/raise the same failure. Recovery must
            # skip it and consult only the already-known child for cosmetic title
            # finalization, whose failure is deliberately best-effort.
            assert session_id == CHILD
            if child_lookup == "raises":
                raise OSError("session store unavailable")
            return None

        def find(marker, parent, cutoff, cwd):
            scans.append((marker, parent, cutoff, cwd))
            return {"session_id": CHILD, "cwd": CWD, "marker": marker}

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(
            machine_module, "fork_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovery must not refork")),
        )
        monkeypatch.setattr(machine_module, "find_claude_fork", find)
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)

        result = await machine._handle_fork_session(_command())

        assert result.session_id == CHILD
        assert lookups == [(CHILD, CWD)]
        if status == "submitted":
            assert scans == [(
                claude_fork_marker("request-1"), PARENT, CUTOFF, CWD)]
        else:
            assert scans == []
        assert machine._claude_forks.get_canonical(
            "request-1")["status"] == "complete"
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_background_recovery_retries_after_one_journal_scan_error(monkeypatch):
    async def run():
        machine, transport = _resident_machine(monkeypatch)
        machine.FORK_RECONCILE_DELAY = 0
        machine.FORK_RECONCILE_ATTEMPTS = 1
        machine.FORK_BACKGROUND_ATTEMPTS = 3
        machine._claude_forks.begin("request-1", PARENT, CUTOFF, CWD)
        machine._claude_forks.mark_submitted("request-1")
        scans = 0

        def find(marker, parent, cutoff, cwd):
            nonlocal scans
            scans += 1
            assert (marker, parent, cutoff, cwd) == (
                claude_fork_marker("request-1"), PARENT, CUTOFF, CWD)
            if scans == 1:
                return None  # foreground scan schedules background recovery
            if scans == 2:
                raise ClaudeForkJournalError("session list temporarily unavailable")
            return {"session_id": CHILD, "cwd": CWD, "marker": marker}

        def get_info(session_id, directory=None):
            assert (session_id, directory) == (CHILD, CWD)
            return _info(CHILD, title="Already visible")

        monkeypatch.setattr(machine_module, "find_claude_fork", find)
        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(
            machine_module, "fork_session",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("submitted recovery must not refork")),
        )
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)
        cmd = _command()

        with pytest.raises(machine_module._ForkOutcomeUncertain):
            await machine._handle_fork_session(cmd)
        task = machine._claude_fork_tasks["request-1"]
        await asyncio.wait_for(task, timeout=1)

        assert scans == 3
        assert [message.type for message in transport.sent][-2:] == [
            "session_forked", "command_ack"]
        assert machine._claude_forks.get_canonical(
            "request-1")["status"] == "complete"
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_ack_loss_retry_never_overwrites_user_renamed_child(monkeypatch):
    async def run():
        machine, _ = _resident_machine(monkeypatch)
        machine._claude_forks.begin("request-1", PARENT, CUTOFF, CWD)
        machine._claude_forks.mark_submitted("request-1")
        machine._claude_forks.complete("request-1", CHILD)
        lookups: list[tuple] = []
        rename_calls: list[tuple] = []

        def get_info(session_id, directory=None):
            lookups.append((session_id, directory))
            assert session_id == CHILD
            return _info(CHILD, title="用户自己的新标题")

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(
            machine_module, "rename_session",
            lambda *args, **kwargs: rename_calls.append((args, kwargs)),
        )
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)

        result = await machine._handle_fork_session(_command())

        assert result.session_id == CHILD
        assert lookups == [(CHILD, CWD)]
        assert rename_calls == []
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())


def test_cold_claude_source_uses_session_info_cwd_without_spawning(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {}
        monkeypatch.setattr(machine, "_is_codex_session", _is_claude)
        source_lookups: list[tuple] = []
        fork_calls: list[tuple] = []

        def get_info(session_id, directory=None):
            source_lookups.append((session_id, directory))
            return _info(
                session_id,
                cwd="/cold/repository",
                title=("Cold source" if session_id == PARENT
                       else "Cold source (fork)"),
            )

        def fork(parent, *, directory, up_to_message_id, title):
            fork_calls.append((
                parent, directory, up_to_message_id, title))
            return SimpleNamespace(session_id=CHILD)

        monkeypatch.setattr(machine_module, "get_session_info", get_info)
        monkeypatch.setattr(machine_module, "fork_session", fork)
        monkeypatch.setattr(machine, "_handle_list_sessions", _no_list)

        result = await machine._handle_fork_session(_command())

        assert source_lookups[0] == (PARENT, None)
        assert fork_calls == [(
            PARENT, "/cold/repository", CUTOFF,
            claude_fork_marker("request-1"),
        )]
        assert result.cwd == "/cold/repository"
        assert result.session_id == CHILD
        assert machine.sessions == {}
        assert "request-1" not in machine._claude_fork_locks

    asyncio.run(run())
