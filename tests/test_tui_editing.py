"""Draft Vim modes and searchable session-picker regressions."""

import pytest

from textual.keys import _character_to_key

from cc_remote.tui_app import (
    Composer,
    Transcript,
    WorkspaceApp,
    WorkspaceClient,
)
from cc_remote.tui_tree import TreeSearch as SearchInput, SessionTree, SessionExplorer


def matches(app):
    return [
        n
        for folder in app.query_one(SessionTree).root.children
        for n in folder.children
    ]


def make_app():
    client = WorkspaceClient("ws://localhost:8765/ws", "", "", "codex", "s")
    client.workspace.view("s").write_state = "writable"
    client.workspace.view("s").event(
        {
            "type": "user_msg",
            "msg_id": "m",
            "prompt": "reading position\nsecond line",
        }
    )
    client.workspace.catalog = {
        "s": {"summary": "Review changes", "cwd": "/work/alpha"},
        "target-123": {"summary": "Kernel TESTS", "cwd": "/work/linux"},
        "chinese-session": {"summary": "驱动检查", "cwd": "/work/中文"},
    }
    for order, (sid, row) in enumerate(client.workspace.catalog.items()):
        row.update(session_id=sid, engine="codex", last_modified=str(3 - order))
    return WorkspaceApp(client, connect=False)


@pytest.mark.asyncio
async def test_pane_switch_and_insert_are_independent():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        editor = app.query_one(Composer)
        await pilot.press("g", "u", "j", "i")
        position = reader.cursor_location
        assert app.focused is reader  # i no longer switches panes.
        await pilot.press("ctrl+j")
        assert app.focused is editor and editor.vim_mode == "NORMAL"
        await pilot.press("i", "a", "b", "c", "escape")
        assert app.focused is editor and editor.vim_mode == "NORMAL"
        assert editor.text == "abc"
        await pilot.press("h", "x")
        assert editor.text == "ac"
        await pilot.press("u")
        assert editor.text == "abc"
        await pilot.press("ctrl+r")
        assert editor.text == "ac"
        await pilot.press("ctrl+k")
        assert app.focused is reader and reader.cursor_location == position


@pytest.mark.asyncio
async def test_draft_word_motion_change_delete_and_paste():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("one two three\nnext line")
        await pilot.press("ctrl+j", "g", "g", "w", "d", "w")
        assert editor.text == "one three\nnext line"
        await pilot.press("0", "c", "w", "N", "E", "W", "escape")
        assert editor.text == "NEW three\nnext line"
        assert editor.vim_mode == "NORMAL"
        await pilot.press("j", "y", "y", "p")
        assert editor.text.endswith("next line\nnext line")
        assert app.focused is editor


@pytest.mark.asyncio
async def test_draft_visual_delete_and_open_line():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("abc\ndef")
        await pilot.press("ctrl+j", "g", "g", "v", "l", "l", "d")
        assert editor.text == "\ndef"
        await pilot.press("o", "x", "escape")
        assert editor.text == "\nx\ndef"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("d", "d"), "def"),
        (("G", "d", "d"), "abc"),
        (("c", "c", "x", "escape"), "x\ndef"),
        (("w", "c", "w", "x", "escape"), "abc\nx"),
        (("A", "x", "escape"), "abcx\ndef"),
        (("G", "I", "x", "escape"), "abc\nxdef"),
        (("O", "x", "escape"), "x\nabc\ndef"),
        (("l", "d", "dollar_sign"), "a\ndef"),
    ],
)
async def test_draft_line_editing_boundaries(keys, expected):
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("abc\ndef")
        await pilot.press("ctrl+j", "g", "g", *keys)
        assert editor.text == expected
        assert app.focused is editor and editor.vim_mode == "NORMAL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search", "expected"),
    [
        ("TESTS", "target-123"),
        ("/work/linux", "target-123"),
        ("123", "target-123"),
        ("kernel linux", "target-123"),
        ("中文", "chinese-session"),
    ],
)
async def test_session_picker_filters_title_cwd_id_and_unicode(search, expected):
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "slash")
        assert app.query_one(SessionExplorer).display
        assert isinstance(app.focused, SearchInput)
        app.screen.query_one(SearchInput).value = search
        await pilot.pause()
        assert len(matches(app)) == 1
        assert matches(app)[0].data == ("session", expected)
        await pilot.press("enter")
        assert app.client.attached_sid == expected
        assert app.focused is app.query_one(Transcript)


@pytest.mark.asyncio
async def test_empty_search_results_cannot_attach_and_cancel_preserves_editor():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        await pilot.press("ctrl+j", "i", "a", "b", "c", "escape", "h")
        position = editor.cursor_location
        await pilot.press("space", "e", "slash", "z", "z", "z", "enter")
        assert app.query_one(SessionExplorer).display
        assert app.client.attached_sid == "s"
        await pilot.press("ctrl+s", "ctrl+e", "ctrl+t")
        assert not app.client._outbox  # Search never submits a draft or answer.
        await pilot.press("escape", "escape")
        assert app.focused is editor
        assert editor.text == "abc" and editor.cursor_location == position
        assert editor.vim_mode == "NORMAL"


@pytest.mark.asyncio
async def test_search_accepts_j_as_text_and_arrows_move_selection():
    app = make_app()
    app.client.workspace.catalog["j"] = {
        "session_id": "j",
        "summary": "job",
        "cwd": "/j",
        "engine": "codex",
    }
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "slash", "j")
        assert app.screen.query_one(SearchInput).value == "j"
        assert len(matches(app)) == 1
        app.query_one(SearchInput).value = "target-123"
        await pilot.pause()
        await pilot.press("enter")
        assert app.client.attached_sid == "target-123"


@pytest.mark.asyncio
async def test_picker_cancels_pending_vim_operator():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("one two")
        await pilot.press("ctrl+j", "d", "escape", "space", "e", "escape", "w")
        assert editor.text == "one two"
        assert editor.cursor_location == (0, 4)


@pytest.mark.asyncio
async def test_quote_then_vim_edit_then_read_keeps_reading_anchor():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("g", "u", "j", "V", "space", "q")
        position = reader.cursor_location
        await pilot.press("ctrl+j", "i", "w", "h", "y", "escape", "h", "ctrl+k")
        assert "why" in app.query_one(Composer).text
        assert reader.cursor_location == position
        assert not app.client._outbox


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "cursor", "keys", "expected"),
    [
        ("call(old)", 6, "ci(new", "call(new)"),
        ("one two three", 5, "daw", "one three"),
        ("one two three", 5, "ciwNEW", "one NEW three"),
        ('say "old"', 6, 'ci"new', 'say "new"'),
        ("one [old] rest", 6, "da[", "one  rest"),
        ("{outer(inner)}", 8, "cibX", "{outer(X)}"),
        ("one two", 5, "viwd", "one "),
        ("call()", 4, "ci(x", "call(x)"),
        ("one <two>", 6, "da<", "one "),
        ("one two", 0, "di(", "one two"),
    ],
)
async def test_text_object_keyboard_commands(text, cursor, keys, expected):
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text(text)
        editor.move_cursor((0, cursor))
        await pilot.press("ctrl+j", *(_character_to_key(k) for k in keys))
        assert editor.text == expected
        assert app.focused is editor
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_text_object_yank_cancel_and_undo():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("one two")
        await pilot.press("ctrl+j", "y", "i", "w")
        assert editor.register == app.clipboard == "one"
        assert editor.text == "one two"
        await pilot.press("c", "i", "escape", "w")
        assert editor.text == "one two" and editor.cursor_location == (0, 4)
        await pilot.press("d", "a", "w", "u")
        assert editor.text == "one two"


@pytest.mark.asyncio
async def test_directional_focus_keys_are_idempotent_and_preserve_text():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        editor = app.query_one(Composer)
        await pilot.press("g", "u", "j")
        position = reader.cursor_location
        await pilot.press("ctrl+j", "i", "a", "b", "ctrl+j", "enter", "c")
        assert editor.text == "ab\nc" and editor.vim_mode == "INSERT"
        await pilot.press("ctrl+k", "ctrl+k")
        assert app.focused is reader and reader.cursor_location == position
        assert editor.text == "ab\nc"
        await pilot.press("ctrl+j")
        assert app.focused is editor and editor.vim_mode == "NORMAL"
        assert editor.cursor_location == (1, 1)


@pytest.mark.asyncio
async def test_search_ctrl_j_k_and_arrows_share_selection():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "slash")
        listing = app.query_one(SessionTree)
        start = listing.cursor_line
        await pilot.press("ctrl+j")
        assert listing.cursor_line == start + 1
        await pilot.press("down")
        assert listing.cursor_line == start + 2
        await pilot.press("ctrl+k", "up")
        assert listing.cursor_line == start
        assert isinstance(app.focused, SearchInput)
        assert app.screen.query_one(SearchInput).value == ""
        app.query_one(SearchInput).value = "target-123"
        await pilot.pause()
        await pilot.press("enter")
        assert app.client.attached_sid == "target-123"


@pytest.mark.asyncio
async def test_empty_search_ctrl_j_k_never_leaves_picker_or_sends():
    app = make_app()
    async with app.run_test() as pilot:
        await pilot.press(
            "space", "e", "slash", "z", "z", "z", "ctrl+j", "ctrl+k", "enter"
        )
        assert app.query_one(SessionExplorer).display
        assert not matches(app)
        assert not app.client._outbox
