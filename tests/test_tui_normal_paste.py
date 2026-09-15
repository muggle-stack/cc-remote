"""Terminal paste is text, never a Normal-mode command or submission."""

import pytest
from textual import events

from cc_remote.tui_app import Composer, Transcript
from cc_remote.tui_settings import TextValue
from tests.test_tui_editing import make_app


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["NORMAL", "INSERT"])
async def test_terminal_paste_preserves_mode_and_does_not_send(mode):
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        await pilot.press("ctrl+j")
        editor.load_text("before after")
        editor.move_cursor((0, 7))
        editor.set_mode(mode)
        app.post_message(events.Paste("hello\n/quit\ndd\n"))
        await pilot.pause()
        assert editor.text == "before hello\n/quit\ndd\nafter"
        assert editor.vim_mode == mode
        assert not app.client._outbox
        if mode == "INSERT":
            await pilot.press("escape")
        await pilot.press("u")
        assert editor.text == "before after"
        await pilot.press("ctrl+r")
        assert editor.text == "before hello\n/quit\ndd\nafter"


@pytest.mark.asyncio
async def test_paste_cancels_pending_operator_and_stays_a_separate_undo():
    app = make_app()
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        await pilot.press("ctrl+j", "i", "a", "b", "escape", "d")
        assert editor.prefix
        app.post_message(events.Paste("XYZ"))
        await pilot.pause()
        assert editor.text == "aXYZb"
        assert not editor.prefix
        await pilot.press("u")
        assert editor.text == "ab"
        await pilot.press("u")
        assert editor.text == ""


@pytest.mark.asyncio
async def test_locked_transcript_ignores_terminal_and_local_paste():
    app = make_app()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.focus()
        original = reader.text
        app.post_message(events.Paste("do not edit history"))
        await pilot.pause()
        app.copy_to_clipboard("also blocked")
        reader.action_paste()
        assert reader.text == original


@pytest.mark.asyncio
async def test_modal_normal_paste_uses_same_editable_boundary():
    app = make_app()
    async with app.run_test() as pilot:
        app.push_screen(TextValue("Value", ""))
        await pilot.pause()
        editor = app.screen.query_one(Composer)
        assert editor.vim_mode == "NORMAL"
        app.copy_to_clipboard("pasted value")
        await pilot.press("ctrl+v")
        assert editor.text == "pasted value"
        assert editor.vim_mode == "NORMAL"
        await pilot.press("u")
        assert editor.text == ""
