"""Archived sessions live under a virtual root without losing cwd identity."""

import pytest

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient
from cc_remote.tui_tree import SessionExplorer, SessionTree, TreeSearch, tree_nodes


def setup():
    c = WorkspaceClient("ws://localhost/ws", "", "", "codex", "live")
    for sid, cwd, archived in [
        ("live", "/one/abc", False),
        ("a", "/one/abc", True),
        ("b", "/two/abc", True),
    ]:
        c.workspace.catalog[sid] = dict(
            session_id=sid, cwd=cwd, tag="archived" if archived else None,
            engine="codex", space="code", summary=sid,
        )
    return WorkspaceApp(c, connect=False), c


def node(tree, identity):
    return next(n for n in tree_nodes(tree.root) if n.data == identity)


@pytest.mark.asyncio
async def test_archive_is_collapsed_root_with_distinct_original_folders():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "e")
        tree = app.query_one(SessionTree)
        assert [n.label.plain for n in tree.root.children] == ["abc", "Archived"]
        archive = node(tree, ("archive", ""))
        assert not archive.is_expanded
        assert [n.label.plain for n in archive.children] == ["abc", "abc"]
        assert {n.data for n in archive.children} == {
            ("archived_folder", "/one/abc"), ("archived_folder", "/two/abc")}
        assert node(tree, ("session", "a")).parent.parent is archive
        assert node(tree, ("session", "live")).parent.parent is tree.root
        tree.move_cursor(archive)
        await pilot.press("r", "d", "D")
        assert len(app.screen_stack) == 1 and not c._outbox


@pytest.mark.asyncio
async def test_archive_search_opens_all_ancestors_and_clearing_restores_folds():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "slash")
        search = app.query_one(TreeSearch)
        search.value = "archived /two/"
        await pilot.pause()
        tree = app.query_one(SessionTree)
        target = node(tree, ("session", "b"))
        assert target.parent.is_expanded and target.parent.parent.is_expanded
        assert tree.cursor_node is target
        assert len(tree.root.children) == 1
        await pilot.press("escape")
        assert not node(tree, ("archive", "")).is_expanded
        assert c.attached_sid == "live"


@pytest.mark.asyncio
async def test_nested_folds_survive_catalog_refresh_and_H_has_visible_cursor():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        tree.move_cursor(node(tree, ("session", "b")))
        c.workspace.catalog["b"]["summary"] = "Renamed"
        app.query_one(SessionExplorer).refresh_catalog()
        target = node(tree, ("session", "b"))
        assert target.label.plain == "Renamed"
        assert target.parent.is_expanded and target.parent.parent.is_expanded
        await pilot.press("H")
        assert tree.cursor_node.data == ("archive", "")
        assert tree.cursor_line >= 0
        assert all(not n.is_expanded for n in tree.root.children)
        await pilot.press("L", "G")
        assert tree.cursor_node.data == ("session", "b")
        assert c.attached_sid == "live" and not c._outbox


@pytest.mark.asyncio
async def test_archive_move_and_unarchive_preserve_native_identity():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        tree.move_cursor(node(tree, ("session", "a")))
        c.workspace.catalog["a"]["cwd"] = "/new/location"
        app.query_one(SessionExplorer).refresh_catalog()
        assert node(tree, ("session", "a")).parent.data == (
            "archived_folder", "/new/location")
        assert not any(n.data == ("archived_folder", "/one/abc")
                       for n in tree_nodes(tree.root))
        c.workspace.catalog["a"]["tag"] = None
        app.query_one(SessionExplorer).refresh_catalog()
        assert node(tree, ("session", "a")).parent.data == (
            "folder", "/new/location")
        assert len([n for n in tree_nodes(tree.root)
                    if n.data == ("session", "a")]) == 1


@pytest.mark.asyncio
async def test_archive_folder_rename_and_delete_target_the_leaf():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "L")
        tree = app.query_one(SessionTree)
        tree.move_cursor(node(tree, ("session", "a")))
        await pilot.press("r")
        assert app.screen.value == "a"
        await pilot.press("escape", "d")
        assert app.screen.sid == "a" and not app.screen.archive
        await pilot.press("n")
        assert not c._outbox
