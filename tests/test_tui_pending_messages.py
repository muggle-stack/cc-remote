"""Immediate, non-authoritative send receipts across delayed echo/rebuild."""

import asyncio
import json

import pytest

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp
from tests.test_tui_send_jumps import client


def frame(c):
    return json.loads(next(iter(c._outbox.values()))[0])


@pytest.mark.asyncio
async def test_pending_message_is_visible_before_socket_send_finishes():
    c = client()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_socket(raw):
        started.set()
        await release.wait()
        return True

    c._send_raw = slow_socket
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.focus()
        editor.load_text("not swallowed")
        task = asyncio.create_task(app.send(False))
        try:
            await asyncio.wait_for(started.wait(), 2)
            app.paint()
            await pilot.pause()
            reader = app.query_one(Transcript)
            assert "not swallowed" in reader.text
            assert "awaiting confirmation" in reader.text
            assert reader.scroll_y == reader.max_scroll_y
            msg = frame(c)
            c._handle(dict(type="user_msg", sid="s", msg_id=msg["msg_id"],
                           prompt=msg["prompt"]))
        finally:
            release.set()
            await task
        app.paint()
        await pilot.pause()
        assert reader.text.count("not swallowed") == 1
        assert "awaiting confirmation" not in reader.text
        assert editor.text == ""


@pytest.mark.asyncio
async def test_ack_does_not_confirm_delivery_but_echo_deduplicates():
    c = client()
    v = c.workspace.view("s")
    before = v.active_turn
    assert await c.submit("pending prompt")
    msg = frame(c)
    assert v.active_turn == before  # No invented native task.
    c._handle(dict(type="command_ack", client_id=c.client_id,
                   cmd_id=msg["cmd_id"]))
    assert v.pending_messages
    for _ in range(2):
        c._handle(dict(type="user_msg", sid="s", msg_id=msg["msg_id"],
                       prompt=msg["prompt"]))
    assert not v.pending_messages
    assert v.render()[0].count("pending prompt") == 1


@pytest.mark.asyncio
async def test_history_first_rebuild_and_rekey_reconcile_pending_identity():
    c = client()
    assert await c.submit("pending prompt")
    msg = frame(c)
    c.workspace.event(dict(type="session_rekey", old_key="s", session_id="new"))
    v = c.workspace.view("new")
    v.history(dict(type="history", revision="r", generation="g", build_seq=1,
                   turns=[], reset=True))
    assert "pending prompt" in v.render()[0]
    v.history(dict(type="history", revision="r", generation="g", build_seq=2,
                   turns=[dict(id="native", clientMsgId=msg["msg_id"],
                               prompt="pending prompt", done=True, blocks=[])]))
    v.event(dict(type="user_msg", msg_id=msg["msg_id"], prompt="pending prompt"))
    assert not v.pending_messages
    assert v.render()[0].count("pending prompt") == 1


@pytest.mark.asyncio
async def test_failed_send_retains_text_and_queue_is_not_projected_as_sent():
    c = client()
    v = c.workspace.view("s")
    assert await c.submit("will be rejected")
    msg = frame(c)
    v.event(dict(type="error", msg_id=msg["msg_id"], code="busy", message="Busy"))
    assert not v.pending_messages
    failed = next(b for b in v.blocks if b.id == "user:" + msg["msg_id"])
    assert failed.data["status"] == "failed"
    assert "will be rejected" in v.render()[0]
    assert await c.submit("queued only", queue=True)
    assert "queued only" not in v.render()[0]
    v.state = "running"
    assert await c.submit("steering")
    steering = json.loads(list(c._outbox.values())[-1][0])
    assert steering["type"] == "steer"
    v.event(dict(type="turn_steered", msg_id=steering["msg_id"],
                 turn_id="native", prompt="steering"))
    assert v.render()[0].count("steering") == 1


@pytest.mark.asyncio
async def test_locally_rejected_send_removes_receipt_without_losing_draft():
    c = client()
    v = c.workspace.view("s")
    v.draft = "keep draft"

    async def reject(message):
        return False

    c._send = reject
    assert not await c.submit(v.draft)
    assert v.draft == "keep draft" and not v.pending_messages
