"""Retain private attachments until authoritative delivery or rejection."""

import json

import pytest

from cc_remote.tui_attachments import read_attachment
from tests.test_tui_workspace import client, emit


@pytest.mark.asyncio
@pytest.mark.parametrize("queue", [False, True])
@pytest.mark.parametrize("correlation", ["msg_id", "request_id"])
async def test_rejected_delivery_restores_files_after_ack(
    tmp_path, queue, correlation,
):
    c = client()
    view = c.workspace.view("s")
    path = tmp_path / "notes.txt"
    path.write_text("private attachment")
    attachment = read_attachment(str(path))
    view.attachments.append(attachment)
    assert await c.submit("with file", queue=queue)
    frame = json.loads(next(iter(c._outbox.values()))[0])
    assert not view.attachments and view.pending_attachments
    # ACK drains transport storage, not the retained delivery payload.
    c._outbox.clear()
    newer = {**attachment, "name": "new draft attachment"}
    view.attachments.append(newer)
    emit(c, "error", code="read_only", message="ownership changed", **{
        correlation: frame["msg_id" if correlation == "msg_id" else "cmd_id"],
    })
    assert view.attachments == [newer, attachment]
    assert not view.pending_attachments


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user_msg", "turn_steered", "query_queue"])
async def test_authoritative_delivery_releases_only_sent_attachments(
    tmp_path, kind,
):
    c = client()
    path = tmp_path / "notes.txt"
    path.write_text("attachment")
    view = c.workspace.view("s")
    attachment = read_attachment(str(path))
    view.attachments.append(attachment)
    assert await c.submit("with file", queue=kind == "query_queue")
    frame = json.loads(next(iter(c._outbox.values()))[0])
    view.attachments.append(attachment)
    payload = (dict(items=[dict(msg_id=frame["msg_id"])])
               if kind == "query_queue" else
               dict(msg_id=frame["msg_id"], prompt="with file"))
    emit(c, kind, **payload)
    assert not view.pending_attachments
    assert view.attachments == [attachment]


@pytest.mark.asyncio
async def test_fast_rejection_during_send_does_not_erase_restored_files(tmp_path):
    c = client()
    path = tmp_path / "notes.txt"
    path.write_text("attachment")
    attachment = read_attachment(str(path))
    c.workspace.view("s").attachments.append(attachment)

    async def send(command):
        emit(c, "error", code="busy", message="rejected", msg_id=command.msg_id)
        return True

    c._send = send
    assert await c.submit("with file")
    assert c.workspace.view("s").attachments == [attachment]
