"""Rejected queued prompts survive transport ACK and newer drafts."""

import json

import pytest

from cc_remote.tui_app import Composer, WorkspaceApp
from tests.test_tui_workspace import client, emit


@pytest.mark.asyncio
@pytest.mark.parametrize("correlation", ["msg_id", "request_id"])
async def test_queued_rejection_restores_full_text_after_ack(correlation):
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        prompt = "private queue text " * 5000
        assert await c.submit(prompt, queue=True)
        frame = json.loads(next(iter(c._outbox.values()))[0])
        emit(c, "command_ack", client_id=c.client_id, cmd_id=frame["cmd_id"])
        editor.load_text("newer draft")
        emit(c, "error", message="queue full", **{
            correlation: frame["msg_id" if correlation == "msg_id" else "cmd_id"],
        })
        app.paint()
        assert editor.text == "newer draft\n\n" + prompt
        assert not c.workspace.view("s").pending_queued_text
        assert prompt not in c.workspace.view("s").render()[0]
        await pilot.pause()
        assert editor.text.count(prompt) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["query_queue", "user_msg"])
async def test_queue_receipt_released_only_on_authoritative_delivery(kind):
    c = client()
    assert await c.submit("private", queue=True)
    frame = json.loads(next(iter(c._outbox.values()))[0])
    view = c.workspace.view("s")
    emit(c, "command_ack", client_id=c.client_id, cmd_id=frame["cmd_id"])
    assert view.pending_queued_text
    emit(c, kind, **(dict(items=[dict(msg_id=frame["msg_id"])])
                    if kind == "query_queue" else
                    dict(msg_id=frame["msg_id"], prompt="private")))
    assert not view.pending_queued_text


@pytest.mark.asyncio
async def test_fast_queue_rejection_does_not_clear_the_original_draft():
    c = client()

    async def send(command):
        emit(c, "error", request_id=command.cmd_id, message="queue full")
        return True

    c._send = send
    assert not await c.submit("retain this", queue=True)
    assert not c.workspace.view("s").pending_queued_text
