"""The same Vim command must work in chat, reports, forms and search."""

import pytest
from textual.keys import _character_to_key

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, Transcript
from cc_remote.tui_widgets import Composer
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import (
    ActionForm,
    ActionPicker,
    PanelReader,
    DetailPanel,
)


def make_app():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    client.workspace.view("s").write_state = "writable"
    return WorkspaceApp(client, connect=False)


async def open_surface(app, surface):
    if surface == "chat":
        widget = app.query_one(Transcript)
    elif surface == "draft":
        widget = app.query_one(Composer)
    elif surface == "report":
        await app.push_screen(DetailPanel(app.client, "s", "Reports"))
        widget = app.screen.query_one(PanelReader)
    elif surface == "form":
        await app.push_screen(ActionForm(app.client, "s", "set_goal"))
        widget = app.screen.query_one(ModalEditor)
    else:
        await app.push_screen(ActionPicker(app.client, "s"))
        widget = app.screen.query_one(ModalEditor)
    widget.focus()
    return widget


CASES = [
    ("call(one two)", 7, "yi(", "one two"),
    ("one two three", 5, "yaw", "two "),
    ("one two three", 5, "yiw", "two"),
    ("foo.bar baz", 2, "yiW", "foo.bar"),
    ("(outer(inner)tail)", 9, "2yi(", "outer(inner)tail"),
    ('say "你好 world"', 7, 'ya"', '"你好 world"'),
    ("one two three four", 0, "y2aw", "one two "),
    ("one two three four", 0, "2y2w", "one two three four"),
    ("one two three", 0, "ye", "one"),
    ("one:two:end", 0, "yf:", "one:"),
    ("one:two:end", 0, "yt:", "one"),
    ("a\nb\nc\nd", 0, "2yy", "a\nb\n"),
    ("(hello [world])", 0, "y%", "(hello [world])"),
    ("one two\nthree\n\nfour", 5, "yip", "one two\nthree"),
    ("call(one two)", 7, "vi(y", "one two"),
    ("abcd", 0, "vy", "a"),
    ("abcd", 3, "vy", "d"),
    ("abcd", 1, "vy", "b"),
    ("abcd", 0, "vlly", "abc"),
    ("abcd", 2, "vhhy", "abc"),
    ("abcd", 1, "vlhhy", "ab"),
    ("中文字符", 0, "vlly", "中文字"),
    ("one two", 0, "vey", "one"),
    ("abc\ndef", 1, "vjy", "bc\nde"),
    ("abcd", 0, "v$y", "abcd"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["chat", "draft", "report", "form", "search"]
)
async def test_identical_vim_objects_counts_and_motions(surface):
    app = make_app()
    async with app.run_test() as pilot:
        widget = await open_surface(app, surface)
        for text, cursor, keys, expected in CASES:
            widget.load_text(text)
            widget.move_cursor(widget.document.get_location_from_index(cursor))
            widget.set_mode("NORMAL")
            app.copy_to_clipboard("")
            await pilot.press(*map(_character_to_key, keys))
            assert app.clipboard == expected, (surface, keys, app.clipboard)
            assert widget.text == text
            assert not widget.prefix
            assert widget.vim_mode == "NORMAL", (surface, keys)
            assert not app.client._outbox
            assert widget.cursor_location == (
                widget.document.get_location_from_index(text.index(expected))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["chat", "report"])
async def test_read_only_objects_never_delete_insert_or_undo(surface):
    app = make_app()
    async with app.run_test() as pilot:
        widget = await open_surface(app, surface)
        text = "call(one two)"
        widget.load_text(text)
        widget.move_cursor((0, 7))
        for command in ["di(", "ciw", "2dd", "x", "p", "u", "A", "O"]:
            await pilot.press(*map(_character_to_key, command))
            assert widget.text == text
            assert widget.read_only
            assert widget.vim_mode != "INSERT"
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_pending_commands_cancel_and_do_not_cross_panes_or_panels():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.load_text("one two")
        reader.move_cursor((0, 0))
        await pilot.press("y", "i", "escape")
        assert not reader.prefix and not app.clipboard
        await pilot.press("y", "ctrl+j")
        assert not reader.prefix
        editor = app.query_one(Composer)
        editor.load_text("draft words")
        await pilot.press("y", "ctrl+k", "w")
        assert not editor.prefix
        assert reader.cursor_location == (0, 4)
        assert not app.clipboard
        await pilot.press("space", "g")
        before = dict(app.client._outbox)
        panel = app.screen
        widget = panel.query_one(PanelReader)
        widget.load_text("one two")
        widget.move_cursor((0, 0))
        await pilot.press("y", "a", "w")
        assert app.screen is panel and app.clipboard == "one "
        assert dict(app.client._outbox) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["draft", "form"])
async def test_counted_change_and_delete_share_undo(surface):
    # Search opens directly in Insert and Esc dismisses it; editable forms
    # and drafts retain the full Vim change/undo interaction.
    app = make_app()
    async with app.run_test() as pilot:
        widget = await open_surface(app, surface)
        widget.load_text("one two three four")
        widget.move_cursor((0, 0))
        await pilot.press("c", "2", "w", "X", "escape")
        assert widget.text == "X three four"
        await pilot.press("u")
        assert widget.text == "one two three four"
        widget.move_cursor((0, 0))
        await pilot.press("d", "2", "a", "w")
        assert widget.text == "three four"
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_find_repeat_word_ends_and_counted_motion_in_chat():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.load_text("one:two:end\nnext line")
        reader.move_cursor((0, 0))
        await pilot.press("f", "colon", "semicolon")
        assert reader.cursor_location == (0, 7)
        await pilot.press("comma")
        assert reader.cursor_location == (0, 3)
        await pilot.press("0", "2", "w")
        assert reader.cursor_location == (0, 4)
        await pilot.press("e")
        assert reader.cursor_location == (0, 6)
        await pilot.press("2", "g", "g")
        assert reader.cursor_location == (1, 0)


@pytest.mark.asyncio
async def test_yank_in_chat_replaces_register_and_lands_at_object_start():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        editor = app.query_one(Composer)
        editor.load_text("stale")
        await pilot.press("ctrl+j", "d", "i", "w", "ctrl+k")
        reader.load_text("call(fresh)")
        reader.move_cursor((0, 7))
        await pilot.press("y", "i", "left_parenthesis", "ctrl+j", "p")
        assert editor.text == "fresh"
        assert reader.cursor_location == (0, 5)
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_multiline_change_undo_redo_and_final_lines_delete():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("a\nb\nc")
        editor.move_cursor((1, 0))
        await pilot.press("ctrl+j", "2", "c", "c", "X", "enter", "Y", "escape")
        assert editor.text == "a\nX\nY"
        await pilot.press("u")
        assert editor.text == "a\nb\nc"
        await pilot.press("ctrl+r")
        assert editor.text == "a\nX\nY"
        editor.move_cursor((1, 0))
        await pilot.press("2", "d", "d")
        assert editor.text == "a"
        await pilot.press("u")
        assert editor.text == "a\nX\nY"
