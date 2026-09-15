"""Terminal reflow must preserve the bottom or the source being read."""

import pytest
from textual.geometry import Offset
from textual.widgets.text_area import Selection

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, Transcript
from cc_remote.tui_state import Block


def make_app():
    client = WorkspaceClient("ws://localhost/ws", "", "", "codex", "s")
    view = client.workspace.view("s")
    view.put(Block("a", "assistant", "\n".join(
        f"line {n}: " + "中 English words " * 12 for n in range(80)
    )))
    return WorkspaceApp(client, connect=False), view


@pytest.mark.asyncio
@pytest.mark.parametrize("follow", [False, True])
async def test_bottom_stays_bottom_even_when_not_following(follow):
    app, view = make_app()
    async with app.run_test(size=(100, 35)) as pilot:
        reader = app.query_one(Transcript)
        await pilot.pause()
        view.follow = follow
        if not follow:
            reader.move_cursor((30, 3))
        reader.scroll_end(animate=False, immediate=True)
        cursor = reader.cursor_location
        await pilot.press("ctrl+j", "i", "x")
        view.follow = follow
        for width, height in [(43, 25), (125, 40), (30, 20), (100, 35)]:
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            assert reader.scroll_y == reader.max_scroll_y
            assert view.follow
            assert reader.cursor_location == cursor
            assert app.focused.id == "composer" and app.focused.text == "x"


@pytest.mark.asyncio
async def test_middle_keeps_source_at_top_not_old_screen_row():
    app, view = make_app()
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        app.stop_following()
        reader.selection = Selection((40, 3), (40, 8))
        selection = reader.selection
        reader.scroll_to(y=77, animate=False, immediate=True)
        point = reader.wrapped_document.offset_to_location(Offset(0, 77))
        for width, height in [(43, 25), (125, 40), (30, 20), (100, 35)]:
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            expected = reader.wrapped_document.location_to_offset(point).y
            assert reader.scroll_y == expected
            assert reader.selection == selection
            assert not view.follow
            # Reflow can move the first character to a different wrapped row;
            # the next resize preserves that row's visible source start.
            point = reader.wrapped_document.offset_to_location(
                reader.scroll_offset
            )
