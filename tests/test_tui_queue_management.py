"""Drive the real queue keys, not only the action methods."""

import json

import pytest
from textual.widgets import OptionList, Static

from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import QueueCancel, QueueEdit, QueuePanel
from tests.test_tui_parity import app_client


def queue(client, ids="abc"):
    client._handle({"type": "query_queue", "sid": "s", "items": [
        {"msg_id": mid, "kind": "queue", "prompt_preview": mid,
         "image_count": 0, "file_count": 0, "retained_bytes": 5}
        for mid in ids
    ], "total_count": len(ids), "total_bytes": 5 * len(ids)})


def frames(client):
    return [json.loads(raw) for raw, _ in client._outbox.values()]


@pytest.mark.asyncio
async def test_queue_edit_failed_preflight_and_save_retains_attachments():
    app, client = app_client()
    queue(client)
    async with app.run_test() as pilot:
        await pilot.press("space", "l", "j", "i")
        assert isinstance(app.screen, QueueEdit)
        screen = app.screen
        client._handle({
            "type": "queued_query_detail", "sid": "s", "msg_id": "b",
            "request_id": screen.request_id, "prompt": "complete prompt",
            "error": "preflight failed", "image_count": 2,
        })
        await pilot.pause(0.2)
        editor = screen.query_one(ModalEditor)
        assert editor.vim_mode == "INSERT" and not editor.locked
        editor.load_text("updated prompt")
        await pilot.press("escape", "enter")
        sent = frames(client)[-1]
        assert sent["type"] == "update_queued_query"
        assert sent["msg_id"] == "b" and sent["prompt"] == "updated prompt"
        assert "images" not in sent and "files" not in sent
        client._handle({
            "type": "queued_query_updated", "sid": "s", "msg_id": "b",
            "request_id": sent["cmd_id"], "updated": False,
            "error": "Already started",
        })
        await pilot.pause(0.2)
        assert editor.text == "updated prompt"
        assert "Already started" in str(screen.query_one("#queue-result", Static).render())
        await pilot.press("escape")
        assert isinstance(app.screen, QueuePanel)
        assert app.screen.selected_id() == "b"


@pytest.mark.asyncio
async def test_queue_cancel_confirmation_is_bound_to_selected_message():
    app, client = app_client()
    queue(client)
    async with app.run_test() as pilot:
        await pilot.press("space", "l", "j", "d")
        assert isinstance(app.screen, QueueCancel)
        await pilot.press("n")
        assert not any(f["type"] == "cancel_queued_query" for f in frames(client))
        await pilot.press("d")
        queue(client, "ac")  # The list changes while confirmation is open.
        await pilot.press("y")
        sent = frames(client)[-1]
        assert sent["type"] == "cancel_queued_query" and sent["msg_id"] == "b"
        assert isinstance(app.screen, QueuePanel)


@pytest.mark.asyncio
async def test_queue_reorder_uses_server_echo_and_stable_highlight():
    app, client = app_client()
    queue(client)
    async with app.run_test() as pilot:
        await pilot.press("space", "l", "j", "K")
        sent = frames(client)[-1]
        assert sent["type"] == "reorder_queued_queries"
        assert sent["expected"] == list("abc") and sent["order"] == list("bac")
        assert [q["msg_id"] for q in client.workspace.view("s").queue] == list("abc")
        queue(client, "bac")
        await pilot.pause(0.3)
        assert app.screen.selected_id() == "b"
        assert app.screen.query_one(OptionList).highlighted == 0
        await pilot.press("J")
        assert frames(client)[-1]["order"] == list("abc")


@pytest.mark.asyncio
async def test_queue_keys_are_configurable_and_indexed():
    app, client = app_client()
    client.keys = KeyConfig({"queue": {"delete": ["x"], "move_up": ["u"]}})
    assert "x" in client.keys.layer_help("queue", {"delete"})
    queue(client)
    async with app.run_test() as pilot:
        await pilot.press("space", "l", "j", "u")
        assert frames(client)[-1]["order"] == list("bac")
        await pilot.press("d")
        assert isinstance(app.screen, QueuePanel)
        await pilot.press("x")
        assert isinstance(app.screen, QueueCancel)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [True, False])
async def test_fast_reorder_reply_is_not_overwritten(monkeypatch, reject):
    app, client = app_client()
    queue(client)
    async with app.run_test() as pilot:
        await pilot.press("space", "l", "j")
        screen = app.screen

        async def immediate(command):
            if reject:
                client._handle({
                    "type": "error", "sid": "s", "code": "queue_changed",
                    "request_id": command.cmd_id, "message": "Queue changed",
                })
            else:
                queue(client, "bac")
            screen.paint()
            return True

        monkeypatch.setattr(client, "_send", immediate)
        await pilot.press("K")
        status = str(screen.query_one("#queue-status", Static).render())
        assert ("Queue changed" if reject else "Server confirmed") in status
        assert "waiting" not in status
