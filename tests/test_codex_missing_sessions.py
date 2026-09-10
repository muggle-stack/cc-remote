"""Missing catalog rows must not resurrect idle wrapper-only sessions."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import (
    CommandAck, DeleteSession, Error, Query, SessionListInvalidated,
)
from cc_remote.wrapper import codex_sessions, machine as machine_module
from cc_remote.wrapper.codex_handle import CodexAppServerError
from tests.test_multisession import _mk_ctx, _mk_machine


SID = "11111111-1111-4111-8111-111111111111"


def _home(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    return home


def test_confirm_missing_requires_readable_db_and_both_rollout_roots(tmp_path):
    home = _home(tmp_path)
    missing = codex_sessions.codex_session_confirmed_missing
    assert missing(SID, codex_home=home)
    for root in ("sessions", "archived_sessions"):
        folder = home / root / "2026"
        folder.mkdir(parents=True)
        path = folder / f"rollout-{SID}.jsonl"
        path.write_text("{}\n")
        assert not missing(SID, codex_home=home)
        path.unlink()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?)", (SID,))
    assert not missing(SID, codex_home=home)


def test_missing_or_corrupt_db_cannot_prove_deletion(tmp_path):
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=tmp_path)
    (tmp_path / "state_5.sqlite").write_bytes(b"not sqlite")
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=tmp_path)


def test_environment_home_is_used_for_both_db_and_rollout(
    tmp_path, monkeypatch,
):
    home = _home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "sessions").mkdir()
    (home / "sessions" / f"rollout-{SID}.jsonl").write_text("{}\n")
    assert not codex_sessions.codex_session_confirmed_missing(SID)


def test_failed_or_symlinked_scan_cannot_prove_deletion(tmp_path, monkeypatch):
    home = _home(tmp_path)
    (home / "sessions").mkdir()
    (home / "sessions" / "linked").symlink_to(tmp_path)
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=home)
    (home / "sessions" / "linked").unlink()

    def denied(*args, **kwargs):
        raise PermissionError("cannot inspect root")

    monkeypatch.setattr(codex_sessions.os, "walk", denied)
    assert not codex_sessions.codex_session_confirmed_missing(
        SID, codex_home=home)


def _resident(monkeypatch):
    machine, transport = _mk_machine()
    ctx = _mk_ctx(SID, SID)
    ctx.engine = "codex"
    ctx.codex_checkpoint = False
    ctx.sdk = SimpleNamespace(
        proc=None, disconnect=AsyncMock(), read_thread_parent=AsyncMock(),
        turn_active=False, turn_start_pending=False,
    )
    machine.sessions[SID] = ctx
    machine.focused_sid = SID
    monkeypatch.setattr(machine_module, "codex_session_confirmed_missing",
                        lambda *args, **kwargs: True)
    return machine, transport, ctx


@pytest.mark.asyncio
async def test_deleted_resident_is_evicted_and_all_clients_invalidated(
    monkeypatch,
):
    machine, transport, ctx = _resident(monkeypatch)
    machine._watch[SID] = object()
    machine._notification_titles[SID] = "orphan"
    healthy = _mk_ctx("healthy", "healthy")
    machine.sessions["healthy"] = healthy
    assert await machine._prune_missing_codex_context(ctx)
    assert SID not in machine.sessions
    assert machine.sessions["healthy"] is healthy
    assert machine.focused_sid is None
    assert SID not in machine._watch
    assert SID not in machine._notification_titles
    ctx.sdk.disconnect.assert_awaited_once()
    ctx.sdk.read_thread_parent.assert_not_awaited()
    hints = [e for e in transport.sent if isinstance(e, SessionListInvalidated)]
    assert len(hints) == 1 and hints[0].to is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["running", "turn", "pending", "queued"])
async def test_busy_or_queued_orphan_is_never_pruned(monkeypatch, protected):
    machine, _, ctx = _resident(monkeypatch)
    if protected == "running":
        ctx.state = "running"
    elif protected == "turn":
        ctx.sdk.turn_active = True
    elif protected == "pending":
        ctx.sdk.turn_start_pending = True
    else:
        monkeypatch.setattr(machine, "_session_has_deferred_query_ownership",
                            lambda candidate: candidate is ctx)
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx
    ctx.sdk.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, RuntimeError("timeout"),
                                  CodexAppServerError({
                                      "code": -32600, "message": "unauthorized",
                                  })])
async def test_loaded_or_uncertain_native_thread_is_retained(
    monkeypatch, error,
):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.proc = SimpleNamespace(returncode=None)
    ctx.sdk.read_thread_parent.side_effect = error
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx


@pytest.mark.asyncio
async def test_live_handle_must_confirm_exact_missing_identity(monkeypatch):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.shared_daemon_affinity = True
    ctx.sdk.proc = SimpleNamespace(returncode=None)
    ctx.sdk.read_thread_parent.side_effect = CodexAppServerError({
        "code": -32600, "message": f"no rollout found for thread id {SID}",
    })
    assert await machine._prune_missing_codex_context(ctx)


@pytest.mark.asyncio
async def test_delete_orphan_never_attempts_resume_or_native_delete(
    monkeypatch,
):
    machine, _, ctx = _resident(monkeypatch)
    machine._cold_codex_delete_context = AsyncMock(
        side_effect=AssertionError("must not resume"))
    machine._handle_list_sessions = AsyncMock()
    await machine._handle_delete_session(DeleteSession(
        session_id=SID, engine="codex", space="code", client_id="browser-a",
    ))
    assert SID not in machine.sessions
    machine._cold_codex_delete_context.assert_not_awaited()
    machine._handle_list_sessions.assert_awaited_once()
    ctx.sdk.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_does_not_overlay_deleted_resident(monkeypatch):
    machine, _, ctx = _resident(monkeypatch)
    monkeypatch.setattr(machine_module, "list_codex_sessions",
                        AsyncMock(return_value=[]))
    rows, _ = await machine._read_codex_profile_catalog()
    assert all(row["native_session_id"] != SID for row in rows)
    assert SID not in machine.sessions
    ctx.sdk.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["scan", "journal"])
@pytest.mark.parametrize("delivery", ["queue", "replace"])
async def test_queue_submission_and_orphan_retirement_are_atomic(
    monkeypatch, boundary, delivery,
):
    machine, transport, ctx = _resident(monkeypatch)
    paused, resume, submitting = (
        asyncio.Event(), asyncio.Event(), asyncio.Event())
    to_thread = asyncio.to_thread
    missing = machine_module.codex_session_confirmed_missing
    scans = 0

    async def pause_retirement(function, *args, **kwargs):
        nonlocal scans
        if function is missing:
            scans += 1
        if ((boundary == "scan" and function is missing and scans == 2)
                or (boundary == "journal"
                    and function == machine._codex_forks.begin_delete)):
            paused.set()
            await resume.wait()
        return await to_thread(function, *args, **kwargs)

    enqueue = machine._enqueue_deferred_query

    async def submit(candidate, command):
        submitting.set()
        return await enqueue(candidate, command)

    # Exercise the real command ACK, queue locks and drain worker; only the
    # native engine boundary is unavailable. Accepted work must stay retained.
    machine._handle_immediate_query = AsyncMock(return_value=Error(
        code="not_running", message="test engine unavailable"))
    monkeypatch.setattr(machine_module.asyncio, "to_thread", pause_retirement)
    monkeypatch.setattr(machine, "_enqueue_deferred_query", submit)
    pruning = asyncio.create_task(machine._prune_missing_codex_context(ctx))
    submission = None
    try:
        await asyncio.wait_for(paused.wait(), 2)
        command = Query(
            sid=SID, prompt="keep this queued prompt", msg_id="queued-race",
            delivery=delivery, cmd_id="queue-command", client_id="browser-a",
        )
        submission = asyncio.create_task(machine._process_command(command))
        await asyncio.wait_for(submitting.wait(), 2)
        if boundary == "scan":
            # The expensive absence scan must not block queue acceptance.
            await asyncio.wait_for(asyncio.shield(submission), 2)
        resume.set()
        removed = await asyncio.wait_for(pruning, 2)
        await asyncio.wait_for(submission, 2)
        if boundary == "scan":
            assert not removed
            assert machine.sessions[SID] is ctx
            assert [q.msg_id for q in ctx.queued_queries] == [command.msg_id]
            assert machine._queued_query_count == 1
            assert any(isinstance(e, CommandAck)
                       and e.cmd_id == command.cmd_id for e in transport.sent)
            ctx.sdk.disconnect.assert_not_awaited()
        else:
            assert removed and SID not in machine.sessions
            assert ctx.queued_queries == []
            assert machine._queued_query_count == 0
            assert any(isinstance(e, Error) and e.code == "not_running"
                       and e.msg_id == command.msg_id for e in transport.sent)
            machine._handle_immediate_query.assert_not_awaited()
    finally:
        resume.set()
        tasks = [task for task in (
            pruning, submission, ctx.queued_query_drain_task,
        ) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_activity_during_retirement_preserves_fork_replay(monkeypatch):
    machine, _, ctx = _resident(monkeypatch)
    original_to_thread = asyncio.to_thread
    begin = machine._codex_forks.begin_delete
    abort = machine._codex_forks.abort_delete = AsyncMock()

    async def native_activity(function, *args, **kwargs):
        if function == begin:
            ctx.sdk.turn_active = True
            return "delete_pending"
        if function is abort:
            return await abort(*args, **kwargs)
        return await original_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(machine_module.asyncio, "to_thread", native_activity)
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx
    abort.assert_awaited_once_with(SID)
    ctx.sdk.disconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "exited"])
async def test_shared_session_survives_proxy_reconnect_gap(monkeypatch, proxy):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.shared_daemon_affinity = True
    ctx.sdk.proc = None if proxy is None else SimpleNamespace(returncode=1)
    machine._notification_titles[SID] = "keep shared session"
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx
    assert machine.focused_sid == SID
    assert machine._notification_titles[SID] == "keep shared session"
    ctx.sdk.disconnect.assert_not_awaited()
    ctx.sdk.read_thread_parent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["probe", "scan", "journal"])
@pytest.mark.parametrize("replacement", [False, True])
async def test_shared_proxy_change_invalidates_orphan_retirement(
    monkeypatch, boundary, replacement,
):
    machine, _, ctx = _resident(monkeypatch)
    ctx.sdk.shared_daemon_affinity = True
    ctx.sdk.proc = SimpleNamespace(returncode=None)
    original_to_thread = asyncio.to_thread
    missing = machine_module.codex_session_confirmed_missing
    begin = machine._codex_forks.begin_delete
    abort = machine._codex_forks.abort_delete = AsyncMock()
    scans = 0

    def restart_proxy():
        if replacement:
            ctx.sdk.proc = SimpleNamespace(returncode=None)
        else:
            ctx.sdk.proc.returncode = 1

    async def read_thread(thread_id):
        if boundary == "probe":
            restart_proxy()
        raise CodexAppServerError({
            "code": -32600, "message": f"thread not found: {thread_id}",
        })

    async def restart_at_boundary(function, *args, **kwargs):
        nonlocal scans
        if function is missing:
            scans += 1
            if boundary == "scan" and scans == 2:
                restart_proxy()
        if function == begin and boundary == "journal":
            restart_proxy()
            return "delete_pending"
        if function is abort:
            return await abort(*args, **kwargs)
        return await original_to_thread(function, *args, **kwargs)

    ctx.sdk.read_thread_parent.side_effect = read_thread
    monkeypatch.setattr(machine_module.asyncio, "to_thread", restart_at_boundary)
    assert not await machine._prune_missing_codex_context(ctx)
    assert machine.sessions[SID] is ctx
    ctx.sdk.disconnect.assert_not_awaited()
    if boundary == "journal":
        abort.assert_awaited_once_with(SID)
    else:
        abort.assert_not_awaited()
