from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

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
from tests.test_multisession import _mk_ctx, _mk_machine


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


def test_session_pin_store_persists_and_unpins(tmp_path):
    store = SessionPinStore(tmp_path)
    store.set_pinned("claude", "claude-1", True)
    store.set_pinned("codex", "codex-1", True)
    assert SessionPinStore(tmp_path).ids("claude") == {"claude-1"}
    assert SessionPinStore(tmp_path).ids("codex") == {"codex-1"}
    if sys.platform != "win32":
        assert oct(os.stat(tmp_path / "session-pins.json").st_mode & 0o777) == "0o600"

    store.set_pinned("claude", "claude-1", False)
    assert SessionPinStore(tmp_path).ids("claude") == set()


def test_session_pin_store_rejects_malformed_state(tmp_path):
    (tmp_path / "session-pins.json").write_text('{"claude":"bad"}')
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
