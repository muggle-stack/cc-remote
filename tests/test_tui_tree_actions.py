"""Tree mutations act on the highlighted session, not the open transcript."""

import json

import pytest
from textual.widgets import OptionList, TextArea

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, seed_demo
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_tree import DeleteDialog, RenameDialog, SessionTree, TreeSearch
from cc_remote.tui_tree import tree_nodes


def make_app(config=None):
    client = WorkspaceClient("ws://localhost/ws", "", "", "codex", None)
    seed_demo(client)
    client.demo = False
    client.keys = KeyConfig(config)
    return WorkspaceApp(client, connect=False)


def sent(client, kind):
    return [frame for raw, _ in client._outbox.values()
            if (frame := json.loads(raw))["type"] == kind]


async def select_target(app, pilot):
    await pilot.press("space", "e", "L")
    tree = app.query_one(SessionTree)
    tree.move_cursor(next(n for f in tree.root.children for n in f.children
                          if n.data[1] == "demo-tests"))


async def confirm_archive(client, pilot):
    await pilot.pause()
    commands = sent(client, "archive_session")
    assert len(commands) == 1 and not sent(client, "delete_session")
    command = commands[0]
    client._handle(dict(
        type="session_list", engine="codex", space="code",
        to=client.client_id, request_id=command["cmd_id"],
        sessions=[dict(client.workspace.catalog["demo-tests"], tag="archived")],
    ))
    assert not sent(client, "delete_session")
    client._handle(dict(type="command_ack", client_id=client.client_id,
                        to=client.client_id, cmd_id=command["cmd_id"]))
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("d", "y"), ("D",)])
async def test_tree_closes_side_chat_without_native_deletion(keys):
    app = make_app()
    app.client._handle(dict(
        type="btw_opened", btw_sid="btw-side", parent_sid="demo-review",
        engine="codex", revision=1, generation="g",
    ))
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        tree.move_cursor(next(n for n in tree_nodes(tree.root)
                              if n.data == ("session", "btw-side")))
        await pilot.press(*keys)
        assert sent(app.client, "close_btw")[0]["sid"] == "btw-side"
        assert not sent(app.client, "delete_session")
        assert not sent(app.client, "archive_session")
        assert "btw-side" in app.client.workspace.catalog
        app.client._handle(dict(type="btw_closed", btw_sid="btw-side",
                                generation="g", revision=2))
        assert "btw-side" not in app.client.workspace.catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["enter", "n", "escape"])
async def test_delete_defaults_to_no_and_cancellation_never_sends(cancel):
    app = make_app()
    async with app.run_test() as pilot:
        await select_target(app, pilot)
        await pilot.press("d")
        assert isinstance(app.screen, DeleteDialog)
        assert app.screen.sid == "demo-tests"
        assert app.screen.query_one(OptionList).highlighted == 0
        await pilot.press(cancel)
        assert len(app.screen_stack) == 1
        assert not sent(app.client, "delete_session")
        assert not sent(app.client, "archive_session")
        assert app.focused is app.query_one(SessionTree)


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("d", "y"), ("d", "j", "enter"), ("D",)])
async def test_confirmed_and_direct_delete_only_target_highlight(keys):
    app = make_app()
    async with app.run_test() as pilot:
        await select_target(app, pilot)
        await pilot.press(*keys)
        await confirm_archive(app.client, pilot)
        commands = sent(app.client, "delete_session")
        assert len(commands) == 1
        command = commands[0]
        assert (command["session_id"], command["engine"], command["space"]) == (
            "demo-tests", "codex", "code",
        )
        assert len(app.screen_stack) == 1
        assert app.client.attached_sid == "demo-review"
        assert "demo-tests" in app.client.workspace.catalog
        app.client._handle(dict(
            type="error", code="busy", message="Session is running",
            request_id=command["cmd_id"], to=app.client.client_id,
        ))
        assert "demo-tests" in app.client.workspace.catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remove", "space", "cursor"])
async def test_delete_dialog_pins_identity_and_revalidates_scope(change):
    app = make_app()
    async with app.run_test() as pilot:
        await select_target(app, pilot)
        await pilot.press("d")
        if change == "remove":
            del app.client.workspace.catalog["demo-tests"]
        elif change == "space":
            app.client.workspace.catalog["demo-tests"]["space"] = "work"
        else:
            tree = app.query_one(SessionTree)
            tree.move_cursor(tree.root.children[0].children[0])
        await pilot.press("y")
        await pilot.pause()
        commands = sent(app.client, "delete_session")
        if change == "cursor":
            await confirm_archive(app.client, pilot)
            commands = sent(app.client, "delete_session")
            assert len(commands) == 1
            assert commands[0]["session_id"] == "demo-tests"
        else:
            assert not commands
            assert not sent(app.client, "archive_session")


@pytest.mark.asyncio
async def test_folder_and_search_text_never_delete_sessions():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "g", "g", "d", "D", "r")
        assert len(app.screen_stack) == 1
        await pilot.press("slash", "d", "D", "r")
        assert app.query_one(TreeSearch).value == "dDr"
        assert not sent(app.client, "delete_session")
        assert not sent(app.client, "rename_session")


@pytest.mark.asyncio
async def test_tree_mutations_and_confirmation_are_rebindable():
    app = make_app({
        "tree": {"rename": ["R"], "delete": ["x"], "delete_direct": ["X"]},
        "confirmation": {"yes": ["v"], "close": ["b"]},
    })
    async with app.run_test() as pilot:
        await select_target(app, pilot)
        await pilot.press("r", "d", "D")
        assert len(app.screen_stack) == 1
        assert not sent(app.client, "delete_session")
        await pilot.press("R")
        assert isinstance(app.screen, RenameDialog)
        await pilot.press("escape", "x", "y", "n", "escape")
        assert isinstance(app.screen, DeleteDialog)
        assert not sent(app.client, "delete_session")
        await pilot.press("b", "x", "v")
        await confirm_archive(app.client, pilot)
        assert len(sent(app.client, "delete_session")) == 1


@pytest.mark.asyncio
async def test_rename_result_refreshes_visible_tree_without_focus_change():
    app = make_app()
    client = app.client
    client.session_catalog_requests[client.scope] = "initial"
    async with app.run_test() as pilot:
        await select_target(app, pilot)
        await pilot.press("r")
        app.screen.query_one(TextArea).load_text("New session title")
        await pilot.press("enter")
        await pilot.pause()
        command = sent(client, "rename_session")[0]
        rows = [dict(row) for row in client.visible_catalog().values()]
        for row in rows:
            if row["session_id"] == "demo-tests":
                row["summary"] = "New session title"
        listing = dict(type="session_list", engine="codex", space="code",
                       sessions=rows, request_id=command["cmd_id"])
        client._handle(listing)
        client._handle(dict(type="command_ack", client_id=client.client_id,
                            to=client.client_id, cmd_id=command["cmd_id"]))
        await client._flush_history_refreshes()
        client._handle(dict(listing, request_id=
                            client.session_catalog_requests[client.scope]))
        app.paint()
        tree = app.query_one(SessionTree)
        target = next(n for f in tree.root.children for n in f.children
                      if n.data[1] == "demo-tests")
        assert target.label.plain == "New session title"
        assert client.attached_sid == "demo-review"
