"""Directory explorer is local UI state, not another control transport."""

import os
import json
import subprocess
import sys

import pytest

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, seed_demo, Composer
from cc_remote.tui_tree import SessionExplorer, SessionTree, TreeSearch, RenameDialog
from textual.widgets import TextArea
from cc_remote.tui_keys import KeyConfig


def make_app():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", None)
    seed_demo(client)
    return WorkspaceApp(client, connect=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 120])
async def test_tree_is_left_docked_toggleable_and_folders_expand(width):
    app = make_app()
    async with app.run_test(size=(width, 35)) as pilot:
        explorer = app.query_one(SessionExplorer)
        conversation = app.query_one("#conversation")
        assert not explorer.display
        await pilot.press("ctrl+p")
        assert not explorer.display
        await pilot.press("space", "e")
        assert explorer.display and len(app.screen_stack) == 1
        assert explorer.region.right == conversation.region.x
        assert conversation.region.width > 25
        tree = app.query_one(SessionTree)
        folder = tree.root.children[1]
        tree.move_cursor(folder)
        await pilot.press("enter")
        assert folder.is_expanded
        await pilot.press("h")
        assert not folder.is_expanded
        await pilot.press("l", "j", "enter")
        assert app.client.attached_sid == "demo-tests"
        assert explorer.display
        await pilot.press("space", "e")
        assert not explorer.display
        assert conversation.region.width == width
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_catalog_migration_delete_and_rename_update_existing_tree():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("space", "e")
        explorer = app.query_one(SessionExplorer)
        tree = app.query_one(SessionTree)
        row = app.client.workspace.catalog["demo-tests"]
        row.update(cwd="/new/place", summary="Renamed session")
        explorer.refresh_catalog()
        folders = {node.data[1]: node for node in tree.root.children}
        assert "/example/tests" not in folders
        assert folders["/new/place"].children[0].label.plain == "Renamed session"
        del app.client.workspace.catalog["demo-tests"]
        explorer.refresh_catalog()
        assert "/new/place" not in {node.data[1] for node in tree.root.children}


@pytest.mark.asyncio
async def test_tree_search_does_not_send_and_insert_keeps_space_e_literal():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "space", "e")
        draft = app.query_one(Composer)
        assert draft.text == " e"
        assert not app.query_one(SessionExplorer).display
        await pilot.press("escape", "space", "e", "slash", "z", "z")
        await pilot.press("ctrl+s", "ctrl+e", "ctrl+t", "enter")
        assert not app.client._outbox
        assert app.client.attached_sid == "demo-review"
        await pilot.press("escape")
        assert app.focused is app.query_one(SessionTree)
        assert not app.query_one(TreeSearch).display
        await pilot.press("escape", "ctrl+j")
        assert app.focused is draft and draft.text == " e"


def test_bare_tui_default_engine_is_codex():
    result = subprocess.run(
        [sys.executable, "-c", "from cc_remote.tui import ENGINE; print(ENGINE)"],
        env={k: v for k, v in os.environ.items() if k != "ENGINE"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "codex"


@pytest.mark.asyncio
async def test_tree_vim_counts_first_last_and_all_folders_are_local():
    app = make_app()
    for i in range(100):
        sid = f"extra-{i}"
        app.client.workspace.catalog[sid] = dict(
            session_id=sid, summary=sid, cwd="/example/many",
            engine="codex", space="code",
        )
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        sid = app.client.attached_sid
        assert all(n.is_expanded for n in tree.root.children)
        await pilot.press("G")
        last = tree.cursor_line
        assert last == tree.last_line and last > 100
        await pilot.press("5", "0", "k")
        assert tree.cursor_line == last - 50
        await pilot.press("g", "g")
        assert tree.cursor_line == 0
        await pilot.press("5", "0", "j")
        assert tree.cursor_line == 50
        await pilot.press("5", "0", "G")
        assert tree.cursor_line == 49
        await pilot.press("H")
        assert all(not n.is_expanded for n in tree.root.children)
        assert tree.cursor_node.data[0] == "folder"
        await pilot.press("L")
        assert all(n.is_expanded for n in tree.root.children)
        assert app.client.attached_sid == sid and not app.client._outbox
        await pilot.press("space", "h")
        assert len(app.screen_stack) == 2
        assert "Collapse all folders" in app.screen.query_one(TextArea).text


@pytest.mark.asyncio
async def test_tree_rename_targets_highlight_not_attached_session():
    app = make_app()
    app.client.demo = False
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        target = next(n for f in tree.root.children for n in f.children
                      if n.data[1] == "demo-tests")
        tree.move_cursor(target)
        await pilot.press("r")
        assert isinstance(app.screen, RenameDialog)
        editor = app.screen.query_one(TextArea)
        assert editor.read_only
        await pilot.press("i", "ctrl+a")
        editor.load_text("renamed")
        await pilot.press("escape", "enter")
        await pilot.pause()
        frames = [json.loads(raw) for raw, _ in app.client._outbox.values()]
        rename = next(f for f in frames if f["type"] == "rename_session")
        assert (rename["session_id"], rename["title"], rename["engine"],
                rename["space"]) == ("demo-tests", "renamed", "codex", "code")
        assert app.client.attached_sid == "demo-review"
        assert app.client.workspace.catalog["demo-tests"]["summary"] != "renamed"


@pytest.mark.asyncio
async def test_tree_search_keeps_vim_letters_literal_and_count_is_reset():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "5", "slash", "r", "H", "L", "G")
        search = app.query_one(TreeSearch)
        assert search.value == "rHLG"
        assert len(app.screen_stack) == 1
        await pilot.press("escape")
        tree = app.query_one(SessionTree)
        assert not tree.count and not tree.chord
        await pilot.press("g", "g", "r")
        assert len(app.screen_stack) == 1  # Folder is not a rename target.
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_tree_rename_validates_and_does_not_resurrect_deleted_target():
    app = make_app()
    app.client.demo = False
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L", "g", "g", "j", "r")
        dialog = app.screen
        assert isinstance(dialog, RenameDialog)
        sid = app.query_one(SessionTree).cursor_node.data[1]
        editor = dialog.query_one(TextArea)
        for name in ("", "a" * 201):
            editor.load_text(name)
            await pilot.press("enter")
            assert app.screen is dialog and not app.client._outbox
        del app.client.workspace.catalog[sid]
        editor.load_text("new title")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.client._outbox


def test_tree_chords_reject_unreachable_prefixes_and_count_keys():
    with pytest.raises(ValueError, match="prefix"):
        KeyConfig({"tree": {"rename": ["g"]}})
    with pytest.raises(ValueError, match="digits"):
        KeyConfig({"tree": {"rename": ["5"]}})
    keys = KeyConfig({"tree": {"rename": ["R"], "first": ["g t"]}})
    assert keys.layers["tree"]["rename"] == ("R",)
