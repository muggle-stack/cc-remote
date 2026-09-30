"""Late native writes must not turn private side chats into public sessions."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from claude_agent_sdk._internal.sessions import _sanitize_path

from cc_remote.config import WrapperConfig
from cc_remote.protocol import Error, GetHistory, ListSessions, SessionList, SwitchSession
from cc_remote.wrapper.machine import WrapperMachine
from tests.test_multisession import _mk_machine, _StubTransport


SID = "11111111-1111-4111-8111-111111111111"
CWD = "/workspace/project"


def write_transcript(root, sid):
    path = root / "projects" / _sanitize_path(CWD) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "user", "uuid": str(uuid4()), "parentUuid": None,
        "sessionId": sid, "cwd": CWD,
        "message": {"role": "user", "content": "a real conversation"},
    }) + "\n")
    return path


@pytest.mark.parametrize("explicit", [False, True])
def test_late_full_transcript_stays_private_after_cleanup_and_restart(
    tmp_path, monkeypatch, explicit,
):
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected))
    private_file = write_transcript(selected, SID)
    normal_sid = str(uuid4())
    normal_file = write_transcript(selected, normal_sid)
    normal_bytes = normal_file.read_bytes()
    # Native UUIDs are only unique within their account namespace.
    other_file = write_transcript(other, SID)
    other_bytes = other_file.read_bytes()
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = cfg.state_dir / "work" / "claude"
    cfg.codex_work_root = cfg.state_dir / "work" / "codex"
    if explicit:
        cfg.claude_profiles_json = json.dumps({
            "selected": {"label": "Selected", "config_dir": str(selected), "default": True},
            "other": {"label": "Other", "config_dir": str(other)},
        })
    transport = _StubTransport()
    machine = WrapperMachine(cfg, transport)
    profile = machine._claude_profiles.default
    wire_sid = machine._claude_wire_sid(profile, SID)

    async def run():
        machine._remember_private_btw(wire_sid, CWD)
        assert await machine._delete_private_btw(wire_sid, CWD)
        assert not private_file.exists()

        # Simulate a writer that survives unlink and flushes a complete native
        # conversation, not merely an empty metadata stub.
        write_transcript(selected, SID)
        restarted = WrapperMachine(cfg, transport)
        await restarted._cleanup_private_btw_sessions()
        assert restarted._private_btw_sessions[wire_sid]["retired"] is True
        assert not restarted.sessions  # Receipts are not resident sessions.
        monkeypatch.setattr(restarted, "_bg_blocked_session_ids", lambda *_: set())

        async def forbidden_spawn(**_kwargs):
            raise AssertionError("a retired private id must never be resumed")

        monkeypatch.setattr(restarted, "_spawn", forbidden_spawn)
        for client in ("owner", "other-client"):
            listing = await restarted._handle_list_sessions(ListSessions(client_id=client))
            assert isinstance(listing, SessionList)
            listed = {row.session_id for row in listing.sessions}
            assert wire_sid not in listed
            assert restarted._claude_wire_sid(profile, normal_sid) in listed
            if explicit:
                other_profile = restarted._claude_profile("other")
                assert restarted._claude_wire_sid(other_profile, SID) in listed
            for command in (
                SwitchSession(session_id=wire_sid, client_id=client),
                GetHistory(session_id=wire_sid, client_id=client),
            ):
                before = len(transport.sent)
                await restarted._process_command(command)
                errors = [event for event in transport.sent[before:] if isinstance(event, Error)]
                assert len(errors) == 1
                error = errors[0]
                assert error.code == "auth" and error.sid == wire_sid
                assert error.to == client
        assert not restarted.sessions

    asyncio.run(run())
    assert normal_file.read_bytes() == normal_bytes
    assert other_file.read_bytes() == other_bytes


def test_receipt_compaction_failure_keeps_the_pending_privacy_guard(monkeypatch):
    machine, transport = _mk_machine()
    machine._remember_private_btw(SID, CWD)
    monkeypatch.setattr(machine, "_claude_catalog_delete_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(machine, "_persist_private_btw_sessions", lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(RuntimeError("fixture write failure"))
    ))

    async def run():
        assert await machine._delete_private_btw(SID, CWD)
        assert machine._private_btw_sessions[SID]["cwd"] == CWD
        restarted = WrapperMachine(machine.cfg, transport)
        assert SID in restarted._private_btw_sessions

    asyncio.run(run())


def test_broker_catalog_cannot_reintroduce_a_retired_private_id(monkeypatch):
    machine, _ = _mk_machine()
    machine._remember_private_btw(SID, CWD)
    machine._retire_private_btw(SID)
    normal_sid = str(uuid4())

    async def broker_list():
        return {"sessions": [
            {"id": sid, "cwd": CWD, "running": True}
            for sid in (SID, normal_sid)
        ]}

    machine._claude_broker_enabled = True
    machine._claude_broker = SimpleNamespace(list=broker_list)
    monkeypatch.setattr(machine, "_claude_catalog_list_sessions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(machine, "_bg_blocked_session_ids", lambda *_: set())

    async def run():
        listing = await machine._handle_list_sessions(ListSessions(client_id="viewer"))
        assert isinstance(listing, SessionList)
        assert [row.session_id for row in listing.sessions] == [normal_sid]

    asyncio.run(run())


def test_retired_capacity_refuses_new_ids_without_forgetting_old_ones(monkeypatch):
    machine, transport = _mk_machine()
    monkeypatch.setattr(machine, "PRIVATE_BTW_RETIRED_CAP", 1)
    machine._remember_private_btw(SID, CWD)
    machine._retire_private_btw(SID)
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        machine._remember_private_btw(str(uuid4()), CWD)
    restarted = WrapperMachine(machine.cfg, transport)
    assert restarted._private_btw_sessions[SID]["retired"] is True


@pytest.mark.parametrize("retired", ["true", 1, None])
def test_invalid_receipt_refuses_fail_open_startup(retired):
    machine, transport = _mk_machine()
    machine._private_btw_file().write_text(json.dumps({
        SID: {"created_at": 1, "retired": retired},
    }))
    with pytest.raises(RuntimeError, match="refusing fail-open"):
        WrapperMachine(machine.cfg, transport)
