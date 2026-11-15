"""Cold catalog replies must stay independent of rollout reconciliation."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import ListSessions, SessionListInvalidated
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_sessions import catalog_index_only, codex_catalog_read_mode
from tests.test_codex_profiles import _machine


def native_row(sid):
    return {"session_id": sid, "summary": sid, "cwd": "/project"}


@pytest.mark.asyncio
async def test_first_reply_precedes_native_reconciliation(tmp_path, monkeypatch):
    machine, transport = _machine(tmp_path)
    monkeypatch.setattr(machine, "_prime_codex_sidebar_watches", AsyncMock())
    monkeypatch.setattr(machine, "_claim_legacy_presentation_from_codex_catalog", AsyncMock())
    started, release = asyncio.Event(), asyncio.Event()
    reads = []

    async def native_list(_limit, *, codex_home=None):
        indexed = catalog_index_only()
        reads.append((Path(codex_home).name, indexed))
        if not indexed:
            started.set()
            await release.wait()
        if Path(codex_home).name != "primary":
            return []
        return [native_row("indexed")] if indexed else [
            native_row("indexed"), native_row("native-cli"),
        ]

    monkeypatch.setattr(machine_module, "list_codex_sessions", native_list)
    request = asyncio.create_task(machine._list_codex_sessions(ListSessions(
        engine="codex", client_id="first", cmd_id="first-list")))
    try:
        reply = await asyncio.wait_for(request, 2)
        assert [row.session_id for row in reply.sessions] == ["primary@indexed"]
        assert reply.to == "first" and reply.request_id == "first-list"
        await asyncio.wait_for(started.wait(), 2)
        assert transport.sent == [reply]
        release.set()
        await asyncio.wait_for(machine._codex_catalog_reconcile_task, 2)
        hints = [event for event in transport.sent if isinstance(event, SessionListInvalidated)]
        assert {(event.engine, event.space) for event in hints} == {
            ("codex", "code"), ("codex", "work"),
        }
        refreshed = await machine._list_codex_sessions(ListSessions(
            engine="codex", client_id="second", cmd_id="second-list"))
        assert {row.session_id for row in refreshed.sessions} == {
            "primary@indexed", "primary@native-cli",
        }
        assert reads.count(("primary", True)) == 1
        assert reads.count(("primary", False)) == 1
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        background = getattr(machine, "_codex_catalog_reconcile_task", None)
        if background is not None:
            await asyncio.gather(background, return_exceptions=True)


@pytest.mark.asyncio
async def test_empty_index_waits_for_native_discovery(tmp_path, monkeypatch):
    machine, _ = _machine(tmp_path)
    monkeypatch.setattr(machine, "_prime_codex_sidebar_watches", AsyncMock())
    monkeypatch.setattr(machine, "_claim_legacy_presentation_from_codex_catalog", AsyncMock())
    reads = []

    async def native_list(_limit, *, codex_home=None):
        reads.append(catalog_index_only())
        if catalog_index_only() or Path(codex_home).name != "primary":
            return []
        return [native_row("native-cli")]

    monkeypatch.setattr(machine_module, "list_codex_sessions", native_list)
    reply = await machine._list_codex_sessions(ListSessions(engine="codex"))
    assert [row.session_id for row in reply.sessions] == ["primary@native-cli"]
    assert True in reads and False in reads


@pytest.mark.asyncio
async def test_native_failure_keeps_indexed_rows_and_reports_profile_error(tmp_path, monkeypatch):
    machine, transport = _machine(tmp_path)
    monkeypatch.setattr(machine, "_prime_codex_sidebar_watches", AsyncMock())
    monkeypatch.setattr(machine, "_claim_legacy_presentation_from_codex_catalog", AsyncMock())

    async def native_list(_limit, *, codex_home=None):
        if not catalog_index_only():
            raise RuntimeError("native catalog unavailable")
        return [native_row("indexed")]

    monkeypatch.setattr(machine_module, "list_codex_sessions", native_list)
    first = await machine._list_codex_sessions(ListSessions(engine="codex"))
    await machine._codex_catalog_reconcile_task
    refreshed = await machine._list_codex_sessions(ListSessions(engine="codex"))
    assert {row.session_id for row in refreshed.sessions} == {
        row.session_id for row in first.sessions
    } == {"primary@indexed", "stack@indexed"}
    assert all(profile.error for profile in refreshed.codex_profiles)
    assert sum(isinstance(event, SessionListInvalidated) for event in transport.sent) == 2


@pytest.mark.asyncio
async def test_index_reads_share_without_waiting_for_full_catalog(tmp_path, monkeypatch):
    machine, _ = _machine(tmp_path)
    gates = {True: asyncio.Event(), False: asyncio.Event()}
    started = {True: asyncio.Event(), False: asyncio.Event()}
    calls = []

    async def read_catalog():
        indexed = catalog_index_only()
        calls.append(indexed)
        started[indexed].set()
        await gates[indexed].wait()
        sid = "indexed" if indexed else "native-cli"
        return ([{"session_id": f"primary@{sid}", "native_session_id": sid,
                  "codex_profile_id": "primary"}], ())

    monkeypatch.setattr(machine, "_read_codex_profile_catalog", read_catalog)

    async def refresh(indexed):
        with codex_catalog_read_mode(index_only=indexed):
            return await machine._refresh_codex_session_catalog()

    full = asyncio.create_task(refresh(False))
    indexed = []
    try:
        await asyncio.wait_for(started[False].wait(), 2)
        indexed = [asyncio.create_task(refresh(True)) for _ in range(2)]
        await asyncio.wait_for(started[True].wait(), 2)
        gates[True].set()
        replies = await asyncio.wait_for(asyncio.gather(*indexed), 2)
        assert calls.count(True) == 1 and calls.count(False) == 1
        assert all(rows[0]["session_id"] == "primary@indexed" for rows in replies)
        assert not full.done()
        gates[False].set()
        await asyncio.wait_for(full, 2)
        assert machine._codex_session_list_cache[1][0]["session_id"] == "primary@native-cli"
        assert machine._codex_session_list_reads == {}
    finally:
        for gate in gates.values():
            gate.set()
        await asyncio.gather(full, *indexed, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_index_reply_cannot_replace_completed_reconciliation(tmp_path, monkeypatch):
    machine, _ = _machine(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def read_catalog():
        indexed = catalog_index_only()
        if indexed:
            started.set()
            await release.wait()
        sid = "indexed" if indexed else "native-cli"
        return ([{"session_id": f"primary@{sid}", "native_session_id": sid,
                  "codex_profile_id": "primary"}], ())

    monkeypatch.setattr(machine, "_read_codex_profile_catalog", read_catalog)

    async def indexed_read():
        with codex_catalog_read_mode(index_only=True):
            return await machine._refresh_codex_session_catalog()

    task = asyncio.create_task(indexed_read())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await machine._refresh_codex_session_catalog()
        release.set()
        rows = await asyncio.wait_for(task, 2)
        assert rows[0]["session_id"] == "primary@native-cli"
        assert machine._codex_session_list_cache[1] == rows
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
