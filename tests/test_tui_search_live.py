"""Search opens ready to type; directory results follow remote disk changes."""

import asyncio

import pytest
from textual.widgets import OptionList

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient
from cc_remote.tui_directories import DirectoryPicker
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import ActionPicker, ShortcutPicker
from cc_remote.tui_settings import ValuePicker
from cc_remote.tui_buffers import BufferPicker
from cc_remote.tui_keys import KeyConfig


def app_client():
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    c.buffers.open("s")
    return WorkspaceApp(c, connect=False), c


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["actions", "shortcuts", "values", "buffers"])
async def test_search_immediately_types_navigates_and_closes_once(kind):
    app, c = app_client()
    picker = {
        "actions": lambda: ActionPicker(c, "s"),
        "shortcuts": lambda: ShortcutPicker(c),
        "values": lambda: ValuePicker("Pick", [("one", 1), ("two", 2)]),
        "buffers": lambda: BufferPicker(c),
    }[kind]()
    async with app.run_test() as pilot:
        app.push_screen(picker)
        await pilot.pause()
        editor = picker.query_one(ModalEditor)
        assert editor.vim_mode == "INSERT" and app.focused is editor
        await pilot.press("ctrl+j", "ctrl+k", "x")
        assert editor.text == "x" and app.focused is editor
        await pilot.press("escape")
        assert len(app.screen_stack) == 1 and not c._outbox


@pytest.mark.asyncio
async def test_search_close_can_be_remapped_while_typing():
    app, c = app_client()
    c.keys = KeyConfig({"picker": {"close": ["ctrl+y"]}})
    async with app.run_test() as pilot:
        app.push_screen(ValuePicker("Pick", [("one", 1)]))
        await pilot.pause()
        await pilot.press("o", "ctrl+y")
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_directory_recursion_and_live_creation_preserve_search(monkeypatch):
    app, c = app_client()
    pages = {"/root": ["/root/repo"], "/root/repo": [],
             "/root/repo/deep": ["/root/repo/deep/new-project"]}
    reads = []

    async def listing(path, *, refresh=False):
        reads.append((path, refresh))
        return {"path": path, "dirs": [{"path": p} for p in pages.get(path, [])]}

    async def rank(paths, query):
        return [p for p in paths if query in p]

    monkeypatch.setattr(c, "list_directories", listing)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    monkeypatch.setattr(DirectoryPicker, "REFRESH_SECONDS", 0.3)
    async with app.run_test() as pilot:
        picker = DirectoryPicker(c, "/root")
        app.push_screen(picker)
        await pilot.pause(0.2)
        await pilot.press(*"new-project")
        pages["/root/repo"] = ["/root/repo/deep"]
        for _ in range(20):
            await pilot.pause(0.1)
            if picker.matches == ["/root/repo/deep/new-project"]:
                break
        assert picker.matches == ["/root/repo/deep/new-project"]
        assert picker.query_one(ModalEditor).text == "new-project"
        assert all(refresh for _, refresh in reads)
        await pilot.press("escape")
        assert len(app.screen_stack) == 1 and not c._outbox


@pytest.mark.asyncio
async def test_recursive_scan_is_bounded_and_cancels_pending_reads(monkeypatch):
    app, c = app_client()
    count = 0
    hanging = False
    cancelled = asyncio.Event()

    async def listing(path, **kwargs):
        nonlocal count
        count += 1
        if hanging and path != "/root":
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return {"path": path, "dirs": [{"path": path + "/child"}]}

    async def rank(paths, query):
        return paths

    monkeypatch.setattr(c, "list_directories", listing)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    monkeypatch.setattr(DirectoryPicker, "MAX_READS", 6)
    async with app.run_test() as pilot:
        picker = DirectoryPicker(c, "/root")
        app.push_screen(picker)
        await pilot.pause(0.6)
        assert count == 6 and "limit reached" in picker.scan_note
        hanging = True
        picker.action_refresh()
        await pilot.pause(0.1)
        await pilot.press("escape")
        await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.asyncio
async def test_symlink_cycles_and_outside_roots_are_not_traversed(monkeypatch):
    app, c = app_client()
    reads = []

    async def listing(path, **kwargs):
        reads.append(path)
        if path.endswith("/escape"):
            return {"path": "/outside", "dirs": [{"path": "/outside/deep"}]}
        return {"path": "/root", "dirs": [
            {"path": "/root/loop"}, {"path": "/root/escape"}]}

    monkeypatch.setattr(c, "list_directories", listing)
    # This tests traversal, not the optional host fzf executable. CI need not
    # have it installed; real ranking has a separate capability-gated test.
    async def rank(paths, query):
        return paths

    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test() as pilot:
        picker = DirectoryPicker(c, "/root")
        app.push_screen(picker)
        await pilot.pause()
        await picker.load_worker.wait()
        await picker.filter_worker.wait()
        await pilot.pause()
        assert reads == ["/root", "/root/loop", "/root/escape"]
        assert "/outside/deep" not in picker.paths
        assert picker.query_one(OptionList).option_count


@pytest.mark.asyncio
async def test_background_directory_refresh_keeps_highlight(monkeypatch):
    app, c = app_client()

    async def listing(path, **kwargs):
        return {"path": path, "dirs": [
            {"path": "/root/a"}, {"path": "/root/b"}
        ] if path == "/root" else []}

    async def rank(paths, query):
        return paths

    monkeypatch.setattr(c, "list_directories", listing)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test() as pilot:
        picker = DirectoryPicker(c, "/root")
        app.push_screen(picker)
        await pilot.pause(0.25)
        await pilot.press("ctrl+j", "ctrl+j")
        assert picker.selected_path() == "/root/b"
        picker.refresh_if_idle()
        await pilot.pause(0.25)
        assert picker.selected_path() == "/root/b"
