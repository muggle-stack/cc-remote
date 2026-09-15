"""Stop is a configurable, session-scoped command, not a local state edit."""

import json

import pytest

from cc_remote.tui_app import Composer, WorkspaceApp, WorkspaceClient
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_settings import TextValue


def make_app(config=None):
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    client.keys = KeyConfig(config)
    client.workspace.catalog["s"] = dict(
        session_id="s", engine="codex", space="code", cwd="/project",
        summary="Session",
    )
    view = client.workspace.view("s")
    view.write_state = "writable"
    view.state = "running"
    return WorkspaceApp(client, connect=False)


def interrupts(app):
    return [frame for raw, _ in app.client._outbox.values()
            if (frame := json.loads(raw))["type"] == "interrupt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["reader", "draft_normal", "draft_insert"])
@pytest.mark.parametrize("engine", ["codex", "claude"])
async def test_stop_targets_current_turn_and_preserves_workspace(focus, engine):
    app = make_app()
    app.client.engine = engine
    app.client.session_engines["s"] = engine
    app.client.workspace.catalog["s"]["engine"] = engine
    view = app.client.workspace.view("s")
    view.queue = [dict(msg_id="queued", prompt_preview="later", kind="queue",
                       image_count=0, file_count=0)]
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", *"keep draft", "escape")
        if focus == "reader":
            await pilot.press("ctrl+k")
        elif focus == "draft_insert":
            await pilot.press("i")
        editor = app.query_one(Composer)
        before = editor.text, editor.cursor_location, app.mode, app.focused
        view.attachments = [{"name": "note", "image": False, "content": {}}]
        await pilot.press("ctrl+x")
        assert len(interrupts(app)) == 1
        assert interrupts(app)[0]["sid"] == "s"
        assert (editor.text, editor.cursor_location, app.mode, app.focused) == before
        assert view.state == "running"  # Wait for the authoritative terminal.
        assert view.queue[0]["msg_id"] == "queued"
        assert view.attachments[0]["name"] == "note"
        assert "Stop requested" in app.client.notice


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [["ctrl+y"], []])
async def test_stop_can_be_rebound_or_disabled_without_default_fallback(keys):
    app = make_app({"keys": {"stop": keys}})
    async with app.run_test() as pilot:
        await pilot.press("ctrl+x")
        assert not interrupts(app)
        if keys:
            await pilot.press("ctrl+y")
            assert len(interrupts(app)) == 1
        row = next(r for r in app.client.keys.index() if r["id"] == "keys.stop")
        assert row["keys"] == keys
        assert row["enabled"] == bool(keys)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["modal", "tree", "search"])
async def test_stop_does_not_escape_nested_key_scopes(scope):
    app = make_app()
    async with app.run_test() as pilot:
        if scope == "modal":
            app.push_screen(TextValue("Rename", "value"))
            await pilot.pause()
        else:
            await pilot.press("space", "e")
            if scope == "search":
                await pilot.press("slash")
        await pilot.press("ctrl+x")
        assert not interrupts(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["idle", "read_only", "no_session"])
async def test_stop_requires_a_running_writable_session(state):
    app = make_app()
    if state == "idle":
        app.client.workspace.view("s").state = "idle"
    elif state == "read_only":
        app.client.workspace.view("s").write_state = "read_only"
    else:
        app.client.attached_sid = None
    async with app.run_test() as pilot:
        await pilot.press("ctrl+x")
        assert not interrupts(app)


@pytest.mark.asyncio
async def test_failed_stop_does_not_claim_success_or_change_running_state():
    app = make_app()

    async def reject(message):
        app.client.notice = "Disconnected; try again"
        return False

    app.client._send = reject
    async with app.run_test() as pilot:
        await pilot.press("ctrl+x")
        assert app.client.notice == "Disconnected; try again"
        assert app.client.workspace.view("s").state == "running"
