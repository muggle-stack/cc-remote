"""Zero-token tests for atomic new-session first queries."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    PROTOCOL_VERSION,
    NewSession,
    SessionFocus,
    UserMsg,
    deserialize,
    serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine

_PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


def test_protocol_v10_new_session_query_roundtrip_and_validation():
    assert PROTOCOL_VERSION == 10
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


def test_new_session_starts_initial_query_on_the_new_ctx():
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
            "collaboration_mode": None,
            "permission_mode": None,
            "service_tier": None,
        }
        turn_ctx, prompt, images, files = captured["turn"]
        assert turn_ctx is ctx and prompt == "first prompt"
        assert images == cmd.images and files == cmd.files

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
            "snapshot", "session_focus", "perm"]
        assert transport.sent[1].request_id == "req-blank"
        assert transport.sent[2].mode == "bypassPermissions"

    asyncio.run(run())
