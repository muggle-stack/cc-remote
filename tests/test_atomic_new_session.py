"""Zero-token tests for atomic new-session first queries."""
from __future__ import annotations
from unittest.mock import AsyncMock

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.wrapper import machine as machine_module
from cc_remote.protocol import (
    CreateWorkSchedule,
    ERR_INVALID_CWD,
    NewSession,
    SessionFocus,
    TurnBinding,
    UserMsg,
    deserialize,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine

_PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


def test_new_session_query_and_turn_binding_roundtrip():
    msg = NewSession(
        request_id="req-1",
        cwd="/tmp/project",
        engine="codex",
        model="gpt-test",
        effort="high",
        collaboration_mode="plan",
        permission_mode="on-request",
        service_tier="fast",
        prompt="hello",
        msg_id="msg-1",
        images=[{"media_type": "image/png", "data": _PNG_1X1}],
        files=[{"filename": "note.txt", "data": "ZmlsZQ=="}],
    )
    assert deserialize(serialize(msg)) == msg
    assert NewSession().prompt is None  # blank-session creation stays supported
    with pytest.raises(ValidationError):
        NewSession(prompt="missing message id")
    with pytest.raises(ValidationError):
        NewSession(engine="claude", collaboration_mode="plan")
    with pytest.raises(ValidationError):
        NewSession(engine="claude", permission_mode="on-request")
    with pytest.raises(ValidationError):
        NewSession(engine="claude", service_tier="fast")
    work = NewSession(
        engine="codex", space="work", codex_profile_id="stack",
        service_tier="fast",
    )
    assert deserialize(serialize(work)) == work
    with pytest.raises(ValidationError):
        NewSession(engine="claude", space="work", codex_profile_id="stack")

    schedule = CreateWorkSchedule(
        engine="codex", codex_profile_id="stack", title="report",
        prompt="build it", next_run_at=1,
    )
    assert deserialize(serialize(schedule)) == schedule
    with pytest.raises(ValidationError):
        CreateWorkSchedule(
            engine="claude", codex_profile_id="stack", title="report",
            prompt="build it", next_run_at=1,
        )

    binding = TurnBinding(msg_id="browser-message", turn_id="native-turn")
    assert deserialize(serialize(binding)) == binding


@pytest.mark.parametrize("permission", [None, "default", "acceptEdits", "plan", "auto", "bypassPermissions"])
def test_new_session_starts_initial_query_on_the_new_ctx(permission):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-new", None)
        old_ctx = _mk_ctx("old-session", "old-session")
        machine.sessions["old-session"] = old_ctx
        machine.focused_sid = "old-session"
        captured = {}

        original_send = transport.send

        async def send_with_late_focus(msg):
            await original_send(msg)
            if isinstance(msg, SessionFocus):
                # Simulate an unrelated focus winning while the create response
                # is in flight. The embedded query must still target tmp-new.
                machine.focused_sid = "old-session"

        transport.send = send_with_late_focus

        async def fake_spawn(**kwargs):
            captured["spawn"] = kwargs
            machine.sessions["tmp-new"] = ctx
            return ctx

        async def fake_run(turn_ctx, prompt, images=None, files=None):
            captured["turn"] = (turn_ctx, prompt, images, files)
            # The real _run_turn emits this only after preflight/reconnect. Keep
            # that boundary in the stub while asserting the explicit new ctx.
            await machine._emit(turn_ctx, UserMsg(
                msg_id="msg-new",
                prompt=prompt,
                images=images,
                files=[{"filename": item["filename"]} for item in files or []],
            ))

        machine._spawn = fake_spawn
        machine._run_turn = fake_run
        cmd = NewSession(
            request_id="req-new",
            cwd="/tmp",
            model="claude-test",
            effort="high",
            permission_mode=permission,
            prompt="first prompt",
            msg_id="msg-new",
            images=[{"media_type": "image/png", "data": _PNG_1X1}],
            files=[{"filename": "note.txt", "data": "ZmlsZQ=="}],
        )

        await machine._handle_new_session(cmd)
        assert ctx.turn_task is not None
        await ctx.turn_task

        focus = next(msg for msg in transport.sent if isinstance(msg, SessionFocus))
        user = next(msg for msg in transport.sent if msg.type == "user_msg")
        assert focus.session_id == "tmp-new"
        assert focus.request_id == "req-new"
        assert user.sid == "tmp-new" and user.msg_id == "msg-new"
        assert machine.focused_sid == "old-session"
        assert [msg.type for msg in transport.sent].index("session_focus") < [
            msg.type for msg in transport.sent
        ].index("user_msg")
        assert captured["spawn"] == {
            "resume_id": None,
            "cwd": "/tmp",
            "engine": "claude",
            "model": "claude-test",
            "effort": "high",
            "auto_compact_mode": None,
            "auto_compact_threshold_tokens": None,
            "collaboration_mode": None,
            "permission_mode": permission,
            "permission_profile": None,
            "web_search": None,
            "service_tier": None,
            "space": "code",
            "work_id": None,
            "raise_on_failure": True,
        }
        turn_ctx, prompt, images, files = captured["turn"]
        assert turn_ctx is ctx and prompt == "first prompt"
        assert images == cmd.images and files == cmd.files

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "auto", "bypassPermissions"])
def test_new_session_claude_permissions_are_engine_and_space_scoped(mode):
    command = NewSession(engine="claude", permission_mode=mode)
    assert deserialize(serialize(command)) == command
    with pytest.raises(ValidationError, match="selected engine"):
        NewSession(engine="codex", permission_mode=mode)
    work = NewSession(engine="claude", space="work", permission_mode=mode)
    assert deserialize(serialize(work)) == work


@pytest.mark.parametrize("mode", ["never", "on-request", "untrusted"])
def test_new_session_codex_permissions_remain_engine_scoped(mode):
    command = NewSession(engine="codex", permission_mode=mode)
    assert deserialize(serialize(command)) == command
    with pytest.raises(ValidationError, match="selected engine"):
        NewSession(engine="claude", permission_mode=mode)


def test_invalid_new_session_cwd_is_single_correlated_error(tmp_path):
    async def run():
        machine, transport = _mk_machine()
        old_ctx = _mk_ctx("old-session", "old-session")
        machine.sessions[old_ctx.key] = old_ctx
        machine.focused_sid = old_ctx.key

        await machine._handle_new_session(NewSession(
            request_id="req-invalid-cwd",
            client_id="browser-one",
            cwd=str(tmp_path / "deleted"),
            engine="codex",
        ))

        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert error.type == "error"
        assert error.code == ERR_INVALID_CWD
        assert error.request_id == "req-invalid-cwd"
        assert error.to == "browser-one"
        assert error.sid is None
        assert machine.focused_sid == "old-session"
        assert old_ctx.buffer.tail_seq == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["preflight", "cap", "connect"])
def test_new_session_sync_failures_never_emit_to_focused_session(
        failure, monkeypatch, tmp_path):
    async def run():
        machine, transport = _mk_machine()
        old_ctx = _mk_ctx("old-session", "old-session")
        old_ctx.state = "running"
        machine.sessions[old_ctx.key] = old_ctx
        machine.focused_sid = old_ctx.key
        machine.cfg.max_concurrent_sessions = 1 if failure == "cap" else 2
        engine = "codex"

        if failure == "preflight":
            engine = "claude"

            def fail_preflight(_binary):
                raise RuntimeError("missing claude")

            monkeypatch.setattr(
                machine_module.SdkHandle, "preflight",
                staticmethod(fail_preflight),
            )
        elif failure == "connect":
            class FailingCodex:
                approval = "never"
                effort = None
                applied_effort = None
                model = None
                service_tier = None
                collaboration_mode = "default"

                def __init__(self, *_args, **_kwargs):
                    pass

                async def connect(self, **_kwargs):
                    raise RuntimeError("connect exploded")

            monkeypatch.setattr(machine_module, "CodexHandle", FailingCodex)

        await machine._handle_new_session(NewSession(
            request_id=f"req-{failure}", client_id="browser-one",
            cwd=str(tmp_path), engine=engine,
        ))

        assert len(transport.sent) == 1
        error = transport.sent[0]
        assert error.type == "error"
        assert error.request_id == f"req-{failure}"
        assert error.to == "browser-one"
        assert error.sid is None
        assert "connect exploded" not in error.message
        assert "missing claude" not in error.message
        assert "connect failed" not in error.message
        assert old_ctx.buffer.tail_seq == 0

    asyncio.run(run())


def test_spawn_resume_missing_session_keeps_legacy_focused_error(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        old_ctx = _mk_ctx("old-session", "old-session")
        machine.sessions[old_ctx.key] = old_ctx
        machine.focused_sid = old_ctx.key
        monkeypatch.setattr(
            machine_module.SdkHandle, "preflight", staticmethod(lambda _bin: None))
        monkeypatch.setattr(
            machine_module, "get_session_info", lambda _sid: None)

        ctx = await machine._spawn(
            resume_id="missing-session", engine="claude")

        assert ctx is None
        errors = [msg for msg in transport.sent if msg.type == "error"]
        assert len(errors) == 1
        assert errors[0].sid == "old-session"
        assert errors[0].request_id is None

    asyncio.run(run())


def test_spawn_bootstrap_missing_session_still_falls_back_to_fresh(
        monkeypatch):
    class FreshClaude:
        permission_mode = "bypassPermissions"
        effort = None
        applied_effort = None
        model = None

        def __init__(self, *_args, **_kwargs):
            self.connect_args = None

        @staticmethod
        def preflight(_binary):
            return None

        async def connect(self, **kwargs):
            self.connect_args = kwargs

        async def disconnect(self):
            return None

    async def run():
        machine, transport = _mk_machine()
        monkeypatch.setattr(machine_module, "SdkHandle", FreshClaude)
        monkeypatch.setattr(
            machine_module, "get_session_info", lambda _sid: None)

        ctx = await machine._spawn(
            resume_id="missing-bootstrap", engine="claude", bootstrap=True)

        assert ctx is not None
        assert ctx.session_id is None
        assert ctx.sdk.connect_args["resume_id"] is None
        assert not [msg for msg in transport.sent if msg.type == "error"]

    asyncio.run(run())


def test_spawn_bootstrap_disconnects_failed_resume_before_fresh_retry(
        monkeypatch, tmp_path):
    class RetryClaude:
        permission_mode = "bypassPermissions"
        effort = None
        applied_effort = None
        model = None

        def __init__(self, *_args, **_kwargs):
            self.connect_args = []
            self.disconnects = 0

        @staticmethod
        def preflight(_binary):
            return None

        async def connect(self, **kwargs):
            self.connect_args.append(kwargs)
            if kwargs["resume_id"] is not None:
                raise RuntimeError("partial resume failed")

        async def disconnect(self):
            self.disconnects += 1

    async def run():
        machine, transport = _mk_machine()
        monkeypatch.setattr(machine_module, "SdkHandle", RetryClaude)
        monkeypatch.setattr(
            machine_module,
            "get_session_info",
            lambda _sid: type("Info", (), {"cwd": str(tmp_path)})(),
        )
        monkeypatch.setattr(machine_module, "save_session_id", lambda *_args: None)
        machine._watch_session = AsyncMock(side_effect=lambda _sid: None)
        machine._prime_claude_ownership = lambda _sid: asyncio.sleep(0)
        machine._load_history = lambda *_args: asyncio.sleep(0)

        ctx = await machine._spawn(
            resume_id="resume-bootstrap",
            engine="claude",
            bootstrap=True,
        )

        assert ctx is not None
        assert ctx.session_id is None
        assert [call["resume_id"] for call in ctx.sdk.connect_args] == [
            "resume-bootstrap", None,
        ]
        assert ctx.sdk.disconnects == 1
        assert not [message for message in transport.sent
                    if message.type == "error"]

    asyncio.run(run())


def test_blank_new_session_does_not_start_a_turn():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("tmp-blank", None)

        async def fake_spawn(**kwargs):
            machine.sessions["tmp-blank"] = ctx
            return ctx

        machine._spawn = fake_spawn
        await machine._handle_new_session(NewSession(request_id="req-blank"))

        assert ctx.turn_task is None
        assert [msg.type for msg in transport.sent] == [
            "snapshot", "session_focus", "auto_compact", "perm"]
        assert transport.sent[1].request_id == "req-blank"
        assert transport.sent[2].mode == "inherit"
        assert transport.sent[3].mode == "bypassPermissions"

    asyncio.run(run())
