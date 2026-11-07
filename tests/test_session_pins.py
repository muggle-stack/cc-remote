from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import (
    ListSessions,
    PinSession,
    PROTOCOL_VERSION,
    SessionInfo,
    SessionList,
    deserialize,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.session_pins import SessionPinStore, SessionPinStoreError
from tests.test_codex_profiles import _machine as _profile_machine
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.fixture(autouse=True)
def legacy_pin_control(monkeypatch):
    # This module exercises legacy/local and ownership fallbacks. Native 0.167
    # RPC choices are covered independently in test_codex_0167_adaptation.
    monkeypatch.setattr(machine_module.WrapperMachine, "_set_native_codex_pin",
                        AsyncMock(return_value=False))


def test_pin_command_and_session_info_roundtrip():
    command = deserialize(serialize(PinSession(
        session_id="session-1", pinned=True, engine="codex", space="work")))
    assert command.type == "pin_session"
    assert command.session_id == "session-1" and command.pinned is True
    info = deserialize(serialize(SessionList(
        engine="claude", sessions=[SessionInfo(
            session_id="session-1", pinned=True)])))
    assert info.sessions[0].pinned is True


def test_relay_roundtrip_null_engine_does_not_reject_claude_pin():
    async def run():
        raw = (
            f'{{"v":{PROTOCOL_VERSION},"ts":1,"type":"pin_session",'
            '"session_id":"claude-1","pinned":true}'
        )
        command = deserialize(serialize(deserialize(raw)))
        assert command.engine is None
        assert "engine" in command.model_fields_set

        machine, transport = _mk_machine()
        machine.sessions = {"claude-1": _mk_ctx("claude-1", "claude-1")}

        async def refresh(_cmd):
            return None

        machine._handle_list_sessions = refresh
        await machine._handle_pin_session(command)
        assert machine._session_pins.ids("claude") == {"claude-1"}
        assert not [message for message in transport.sent
                    if message.type == "error"]

    asyncio.run(run())


def test_explicit_wrong_engine_still_rejects_pin():
    async def run():
        machine, transport = _mk_machine()
        machine.sessions = {"claude-1": _mk_ctx("claude-1", "claude-1")}
        command = PinSession(
            session_id="claude-1", pinned=True, engine="codex")

        await machine._handle_pin_session(command)
        assert machine._session_pins.ids("claude") == set()
        errors = [message for message in transport.sent
                  if message.type == "error"]
        assert len(errors) == 1
        assert errors[0].code == "auth"

    asyncio.run(run())


def _native_catalog(home, session_id=None, *, archived=False):
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)")
        if session_id:
            db.execute("INSERT INTO threads VALUES (?, ?)",
                       (session_id, int(archived)))


@pytest.mark.parametrize("pinned", [True, False])
@pytest.mark.parametrize("archived", [True, False])
@pytest.mark.parametrize("engine", ["codex", None])
def test_cold_codex_pin_uses_exact_catalog_without_loading_session(
    tmp_path, monkeypatch, pinned, archived, engine,
):
    async def run():
        machine, transport = _profile_machine(tmp_path)
        _native_catalog(tmp_path / "primary", "catalog-only", archived=archived)
        _native_catalog(tmp_path / "stack", "catalog-only")
        sid = "primary@catalog-only"
        machine._session_pins.set_pinned("codex", sid, not pinned)
        machine._session_pins.set_pinned("codex", "stack@catalog-only", True)
        refreshed = []

        async def refresh(cmd):
            refreshed.append(cmd)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Pinning must not load a session or use Claude")

        monkeypatch.setattr(machine, "_list_codex_sessions", refresh)
        monkeypatch.setattr(machine, "_spawn", forbidden)
        monkeypatch.setattr(machine, "_claude_catalog_session_info", forbidden)
        command = PinSession(session_id=sid, engine=engine, pinned=pinned)
        await machine._handle_pin_session(command)

        assert (sid in machine._session_pins.ids("codex")) is pinned
        assert "stack@catalog-only" in machine._session_pins.ids("codex")
        assert machine._session_pins.ids("claude") == set()
        assert not machine.sessions
        assert not [msg for msg in transport.sent if msg.type == "error"]
        assert refreshed == [command]

    asyncio.run(run())


@pytest.mark.parametrize("unavailable", [False, True])
def test_cold_codex_pin_never_uses_another_accounts_catalog(
    tmp_path, monkeypatch, unavailable,
):
    async def run():
        machine, transport = _profile_machine(tmp_path)
        if not unavailable:
            _native_catalog(tmp_path / "primary")
        _native_catalog(tmp_path / "stack", "sibling-only")
        sid = "primary@sibling-only"
        machine._session_pins.set_pinned("codex", sid, True)
        machine._session_pins.set_pinned("codex", "stack@sibling-only", True)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("Uncertain Codex ownership must not query Claude")

        monkeypatch.setattr(machine, "_claude_catalog_session_info", forbidden)
        result = await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", pinned=True,
            client_id="client-1", cmd_id="unpin-1"))

        assert result.code == ("busy" if unavailable else "not_running")
        assert result.sid == sid and result.to == "client-1"
        assert result.request_id == "unpin-1"
        assert transport.sent == [result]
        assert machine._session_pins.ids("codex") == {
            sid, "stack@sibling-only",
        }

    asyncio.run(run())


@pytest.mark.parametrize("catalog", ["missing", "deleted", "corrupt"])
@pytest.mark.parametrize("legacy", [False, True])
def test_unpin_only_removes_own_preference_when_native_state_is_gone(
    tmp_path, monkeypatch, catalog, legacy,
):
    async def run():
        machine, transport = _profile_machine(tmp_path)
        if catalog == "deleted":
            _native_catalog(tmp_path / "primary")
        elif catalog == "corrupt":
            (tmp_path / "primary").mkdir()
            (tmp_path / "primary" / "state_5.sqlite").write_bytes(b"not sqlite")
        _native_catalog(tmp_path / "stack", "same-id")
        sid = "primary@same-id"
        for pinned_sid in (sid, "stack@same-id"):
            machine._session_pins.set_pinned("codex", pinned_sid, True)
        machine._session_pins.set_pinned("claude", "same-id", True)
        refresh = AsyncMock()
        monkeypatch.setattr(machine, "_list_codex_sessions", refresh)

        def forbidden(*args, **kwargs):
            raise AssertionError("Unpin must not depend on native session files")

        monkeypatch.setattr(machine, "_is_codex_session", forbidden)
        monkeypatch.setattr(machine_module, "codex_session_presence", forbidden)
        await machine._handle_pin_session(PinSession(
            session_id=sid, engine=None if legacy else "codex", pinned=False))
        # Repeated explicit unpin is harmless and succeeds without native data.
        await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", pinned=False))
        assert machine._session_pins.ids("codex") == {"stack@same-id"}
        assert machine._session_pins.ids("claude") == {"same-id"}
        assert SessionPinStore(tmp_path / "state").ids("codex") == {"stack@same-id"}
        assert refresh.await_count == 2
        assert not transport.sent

    asyncio.run(run())


def test_unpin_rejects_unknown_account_and_wrong_work_space(tmp_path, monkeypatch):
    async def run():
        machine, _ = _profile_machine(tmp_path)
        monkeypatch.setattr(machine, "_list_codex_sessions", AsyncMock())
        machine._session_pins.set_pinned("codex", "unknown@native", True)
        rejected = await machine._handle_pin_session(PinSession(
            session_id="unknown@native", engine="codex", pinned=False))
        assert rejected.code == "auth"
        assert "unknown@native" in machine._session_pins.ids("codex")
        store = machine._work.for_engine("codex")
        record = store.create_session(codex_profile_id="primary")
        store.bind_session(record.work_id, "work-thread", codex_profile_id="primary")
        sid = "primary@work-thread"
        machine._session_pins.set_pinned("codex", sid, True)
        rejected = await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", space="code", pinned=False))
        assert rejected.code == "auth"
        assert sid in machine._session_pins.ids("codex")
        await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", space="work", pinned=False))
        assert sid not in machine._session_pins.ids("codex")

    asyncio.run(run())


def test_claude_unpin_does_not_require_transcript(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine._session_pins.set_pinned("claude", "deleted", True)
        monkeypatch.setattr(machine, "_handle_list_sessions", AsyncMock())

        def forbidden(*args, **kwargs):
            raise AssertionError("Unpin must not inspect transcripts")

        monkeypatch.setattr(machine, "_claude_catalog_session_info", forbidden)
        await machine._handle_pin_session(PinSession(
            session_id="deleted", engine="claude", pinned=False))
        assert not machine._session_pins.ids("claude")
        assert not transport.sent

    asyncio.run(run())


def test_catalog_only_codex_work_pin_keeps_space_guard(tmp_path, monkeypatch):
    async def run():
        machine, transport = _profile_machine(tmp_path)
        _native_catalog(tmp_path / "primary", "work-thread")
        store = machine._work.for_engine("codex")
        record = store.create_session(codex_profile_id="primary")
        store.bind_session(record.work_id, "work-thread", codex_profile_id="primary")
        sid = "primary@work-thread"

        async def refresh(_cmd):
            return None

        monkeypatch.setattr(machine, "_list_codex_sessions", refresh)
        rejected = await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", space="code", pinned=True))
        assert rejected.code == "auth" and "Work/Code" in rejected.message
        assert machine._session_pins.ids("codex") == set()

        transport.sent.clear()
        await machine._handle_pin_session(PinSession(
            session_id=sid, engine="codex", space="work", pinned=True))
        assert machine._session_pins.ids("codex") == {sid}
        assert not transport.sent

    asyncio.run(run())


def test_session_pin_store_persists_and_unpins(tmp_path):
    store = SessionPinStore(tmp_path)
    store.set_pinned("claude", "claude-1", True)
    store.set_pinned("codex", "codex-1", True)
    assert SessionPinStore(tmp_path).ids("claude") == {"claude-1"}
    assert SessionPinStore(tmp_path).ids("codex") == {"codex-1"}
    assert oct(os.stat(tmp_path / "session-pins.json").st_mode & 0o777) == "0o600"

    store.set_pinned("claude", "claude-1", False)
    assert SessionPinStore(tmp_path).ids("claude") == set()


def test_session_pin_store_rejects_malformed_state(tmp_path):
    (tmp_path / "session-pins.json").write_text('{"claude":"bad"}')
    with pytest.raises(SessionPinStoreError, match="unreadable"):
        SessionPinStore(tmp_path)


def test_pin_profile_revisions_round_trip_consistently(tmp_path):
    store = SessionPinStore(tmp_path)
    store.set_pinned("codex", "old@native", True)

    assert store.migrate_codex_profile_sessions(
        lambda session_id: session_id.replace("old@", "new@", 1),
        profile_revision=7,
    ) == 1

    payload = json.loads(
        (tmp_path / "session-pins.json").read_text(encoding="utf-8")
    )
    assert payload["profile_revision"] == 7
    assert payload["profile_revisions"] == {"claude": 0, "codex": 7}
    assert SessionPinStore(tmp_path).ids("codex") == {"new@native"}


def test_pin_store_rejects_inconsistent_profile_revisions(tmp_path):
    (tmp_path / "session-pins.json").write_text(
        json.dumps({
            "claude": [],
            "codex": [],
            "profile_revision": 3,
            "profile_revisions": {"claude": 0, "codex": 4},
        }),
        encoding="utf-8",
    )
    with pytest.raises(SessionPinStoreError, match="unreadable"):
        SessionPinStore(tmp_path)


def test_claude_session_list_includes_durable_pin(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine._session_pins.set_pinned("claude", "claude-1", True)
        monkeypatch.setattr(machine_module, "list_sessions", lambda limit=200: [
            SimpleNamespace(
                session_id="claude-1", summary="pinned", custom_title=None,
                last_modified=10, first_prompt=None, git_branch=None,
                cwd="/repo", tag=None,
            ),
        ])

        await machine._handle_list_sessions(ListSessions(client_id="client-1"))
        listing = next(message for message in transport.sent
                       if isinstance(message, SessionList))
        assert len(listing.sessions) == 1
        assert listing.sessions[0].pinned is True

    asyncio.run(run())


def test_claude_session_list_uses_stable_title_not_latest_prompt(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        monkeypatch.setattr(machine_module, "list_sessions", lambda limit=200: [
            SimpleNamespace(
                session_id="named", summary="latest question",
                custom_title="official title", first_prompt="first question",
                last_modified=20, git_branch=None, cwd="/repo", tag=None,
            ),
            SimpleNamespace(
                session_id="unnamed", summary="latest follow-up",
                custom_title=None, first_prompt="initial question",
                last_modified=10, git_branch=None, cwd="/repo", tag=None,
            ),
        ])

        await machine._handle_list_sessions(ListSessions(client_id="client-1"))
        listing = next(message for message in transport.sent
                       if isinstance(message, SessionList))
        assert [session.summary for session in listing.sessions] == [
            "official title", "initial question",
        ]

    asyncio.run(run())
