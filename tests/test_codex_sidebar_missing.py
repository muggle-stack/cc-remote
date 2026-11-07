"""Deleted native records must not leave pinned/cached sidebar ghosts."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import ListSessions, PinSession
from cc_remote.wrapper import codex_sessions, machine as machine_module
from tests.test_codex_profiles import _context, _machine


def _catalog(home: Path, rows=()):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.executemany("INSERT INTO threads VALUES (?, ?)", rows)


def _row(profile, sid, status="notLoaded"):
    return {
        "session_id": f"{profile}@{sid}", "native_session_id": sid,
        "codex_profile_id": profile, "codex_profile_label": profile,
        "status": status, "summary": "test session", "cwd": "/deleted-project",
    }


def test_confirm_deleted_catalog_and_stale_index_but_keep_other_history(tmp_path):
    home = tmp_path / "codex"
    gone = home / "sessions" / "rollout-gone.jsonl"
    live = home / "sessions" / "rollout-live.jsonl"
    live.parent.mkdir(parents=True)
    live.write_text("{}\n")
    archived = home / "archived_sessions" / "rollout-archived.jsonl"
    archived.parent.mkdir()
    archived.write_text("{}\n")
    _catalog(home, [
        ("gone", str(gone)), ("live", str(live)),
        ("archived", str(home / "sessions" / "rollout-archived.jsonl")),
        ("foreign", str(tmp_path / "another-account" / "rollout-foreign.jsonl")),
        ("unknown-path", None),
    ])
    ids = ["no-index", "gone", "live", "archived", "foreign", "unknown-path"]
    before = (home / "state_5.sqlite").read_bytes()
    assert codex_sessions.codex_missing_sidebar_sessions(
        ids, codex_home=home, unloaded_ids=ids,
    ) == {"no-index", "gone"}
    # A stale cached status is not sufficient evidence to discard an index row.
    assert codex_sessions.codex_missing_sidebar_sessions(
        ids, codex_home=home,
    ) == {"no-index"}
    assert (home / "state_5.sqlite").read_bytes() == before
    assert live.exists() and archived.exists()


@pytest.mark.parametrize("failure", ["missing-db", "corrupt-db", "schema", "permission", "symlink"])
def test_unavailable_native_state_never_proves_sidebar_deletion(tmp_path, monkeypatch, failure):
    home = tmp_path / "codex"
    home.mkdir()
    if failure == "corrupt-db":
        (home / "state_5.sqlite").write_bytes(b"corrupt")
    elif failure == "schema":
        with sqlite3.connect(home / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (other TEXT)")
    elif failure in {"permission", "symlink"}:
        _catalog(home)
        (home / "sessions").mkdir()
        if failure == "symlink":
            (home / "sessions" / "linked").symlink_to(tmp_path)
        else:
            def denied(*args, **kwargs):
                raise PermissionError("temporarily unreadable")
            monkeypatch.setattr(codex_sessions.os, "walk", denied)
    assert codex_sessions.codex_missing_sidebar_sessions(
        ["missing"], codex_home=home, unloaded_ids=["missing"],
    ) == set()


def test_catalog_materializing_during_scan_preserves_session(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    _catalog(home)
    (home / "sessions").mkdir()
    walk = codex_sessions.os.walk

    def materialize(*args, **kwargs):
        with sqlite3.connect(home / "state_5.sqlite") as db:
            db.execute("INSERT OR IGNORE INTO threads VALUES (?, ?)", ("new", None))
        yield from walk(*args, **kwargs)

    monkeypatch.setattr(codex_sessions.os, "walk", materialize)
    assert codex_sessions.codex_missing_sidebar_sessions(["new"], codex_home=home) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [True, False])
async def test_refresh_removes_deleted_rows_cache_and_pins_per_account(tmp_path, monkeypatch, listed):
    machine, transport = _machine(tmp_path)
    primary, stack = tmp_path / "primary", tmp_path / "stack"
    primary_rows = [("same-id", str(primary / "sessions" / "rollout-same-id.jsonl"))] if listed else []
    _catalog(primary, primary_rows)
    live = stack / "sessions" / "rollout-same-id.jsonl"
    live.parent.mkdir(parents=True)
    live.write_text("{}\n")
    _catalog(stack, [("same-id", str(live))])
    cached = [_row("primary", "same-id"), _row("stack", "same-id")]
    machine._codex_session_list_cache = (time.monotonic() - 10, cached, ())
    for row in cached:
        machine._session_pins.set_pinned("codex", row["session_id"], True)
    machine._session_pins.set_pinned("codex", "primary@off-page-deleted", True)

    async def native_list(_limit, *, codex_home=None):
        if Path(codex_home) == primary and not listed:
            return []
        return [{"session_id": "same-id", "status": "notLoaded", "cwd": "/deleted-project"}]

    monkeypatch.setattr(machine_module, "list_codex_sessions", native_list)
    first = await machine._list_codex_sessions(ListSessions(engine="codex", client_id="a"))
    second = await machine._list_codex_sessions(ListSessions(engine="codex", client_id="b"))
    assert [s.session_id for s in first.sessions] == ["stack@same-id"]
    assert [s.session_id for s in second.sessions] == ["stack@same-id"]
    assert [row["session_id"] for row in machine._codex_session_list_cache[1]] == ["stack@same-id"]
    assert machine._session_pins.ids("codex") == {"stack@same-id"}
    assert not [e for e in transport.sent if e.type == "error"]
    assert live.exists()  # Removing a project directory never removes its history.


@pytest.mark.asyncio
async def test_legacy_unpin_during_catalog_outage_keeps_cache_but_clears_pin(tmp_path, monkeypatch):
    machine, transport = _machine(tmp_path)
    _catalog(tmp_path / "primary", [("thread", str(tmp_path / "primary" / "sessions" / "gone.jsonl"))])
    cached = [_row("primary", "thread")]
    machine._codex_session_list_cache = (time.monotonic(), cached, ())
    machine._session_pins.set_pinned("codex", "primary@thread", True)
    # A failed catalog refresh alone cannot prove that native pins are absent.
    # This local-only fallback requires an authoritative legacy metadata read.
    machine._codex_rpc_for_wire = AsyncMock(return_value={"thread": {"id": "thread"}})
    monkeypatch.setattr(machine_module, "list_codex_sessions", AsyncMock(side_effect=RuntimeError("offline")))
    await machine._handle_pin_session(PinSession(
        session_id="primary@thread", engine="codex", pinned=False, client_id="a"))
    assert machine._session_pins.ids("codex") == set()
    machine._codex_rpc_for_wire.assert_awaited_once_with(
        "primary@thread", "thread/read", {"threadId": "primary@thread", "includeTurns": False})
    assert machine._codex_session_list_cache[1] == cached
    lists = [e for e in transport.sent if e.type == "session_list"]
    assert lists and all(not s.pinned for e in lists for s in e.sessions)
    assert not [e for e in transport.sent if e.type == "error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protection", ["active", "idle", "systemError", "resident", "hint", "work", "external"])
async def test_sidebar_cleanup_preserves_live_and_owned_sessions(tmp_path, monkeypatch, protection):
    machine, _ = _machine(tmp_path)
    _catalog(tmp_path / "primary")
    sid = "primary@thread"
    machine._session_pins.set_pinned("codex", sid, True)
    status = protection if protection in {"active", "idle", "systemError"} else "notLoaded"
    if protection == "resident":
        machine.sessions[sid] = _context(sid, "thread", "primary")
    elif protection == "hint":
        machine._codex_thread_started_hints[("primary", "thread")] = time.monotonic()
    elif protection == "external":
        machine._watch[sid] = {
            "engine": "codex", "external": True, "scan_complete": True,
        }
    elif protection == "work":
        store = machine._work.for_engine("codex")
        record = store.create_session(codex_profile_id="primary")
        store.bind_session(record.work_id, "thread", codex_profile_id="primary")
    raw = [_row("primary", "thread", status)]
    result = await machine._prune_missing_codex_sidebar_rows(
        raw, [], epoch=machine._codex_session_list_epoch, unloaded_sids={sid})
    assert result == raw
    assert machine._session_pins.ids("codex") == {sid}


@pytest.mark.asyncio
async def test_concurrent_resume_invalidates_sidebar_absence_check(tmp_path, monkeypatch):
    machine, _ = _machine(tmp_path)
    _catalog(tmp_path / "primary")
    sid = "primary@thread"
    raw = [_row("primary", "thread")]
    machine._session_pins.set_pinned("codex", sid, True)
    paused, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread

    async def pause_scan(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function is machine_module.codex_missing_sidebar_sessions:
            paused.set()
            await release.wait()
        return result

    monkeypatch.setattr(machine_module.asyncio, "to_thread", pause_scan)
    task = asyncio.create_task(machine._prune_missing_codex_sidebar_rows(
        raw, [], epoch=machine._codex_session_list_epoch, unloaded_sids={sid}))
    try:
        await asyncio.wait_for(paused.wait(), 2)
        machine.sessions[sid] = _context(sid, "thread", "primary")
        release.set()
        assert await task == raw
        assert machine._session_pins.ids("codex") == {sid}
    finally:
        release.set()
        await task
