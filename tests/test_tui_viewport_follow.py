"""Only the viewport's pre-update bottom position controls auto-follow."""

import pytest
from textual.widgets.text_area import Selection

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp
from cc_remote.tui_state import Block
from tests.test_tui_send_jumps import client


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["reader", "draft"])
@pytest.mark.parametrize("bottom", [False, True])
async def test_follow_depends_only_on_viewport_not_focus_or_old_flag(focus, bottom):
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        editor = app.query_one(Composer)
        reader.move_cursor((20, 1))
        (editor if focus == "draft" else reader).focus()
        await pilot.pause()
        reader.scroll_to(y=reader.max_scroll_y if bottom else 10,
                         animate=False, immediate=True)
        view.follow = not bottom  # A stale flag must never override position.
        top = reader.scroll_y
        for number in range(3):
            view.put(Block(f"new-{number}", "assistant", "new output\n" * 30))
            app.paint()
            await pilot.pause()
            assert view.follow is bottom
            assert reader.scroll_y == (reader.max_scroll_y if bottom else top)
            assert app.focused is (editor if focus == "draft" else reader)


@pytest.mark.asyncio
async def test_bottom_selection_follows_without_moving_selection():
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.selection = Selection((20, 0), (20, 4))
        reader.set_mode("VISUAL")
        selection = reader.selection
        selected = reader.selected_text
        reader.scroll_end(animate=False, immediate=True)
        view.put(Block("new", "assistant", "output\n" * 40))
        app.paint()
        await pilot.pause()
        assert view.follow and reader.scroll_y == reader.max_scroll_y
        assert reader.selection == selection and reader.selected_text == selected


@pytest.mark.asyncio
async def test_one_row_above_bottom_is_not_bottom():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.scroll_to(y=reader.max_scroll_y - 1,
                         animate=False, immediate=True)
        top = reader.scroll_y
        c.workspace.view("s").put(Block("new", "assistant", "output\n" * 40))
        app.paint()
        await pilot.pause()
        assert not c.workspace.view("s").follow
        assert reader.scroll_y == top
