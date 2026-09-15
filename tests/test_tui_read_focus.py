"""Focus-only navigation must not pull the viewport to an obsolete cursor."""

import pytest
from textual.geometry import Offset

from cc_remote.tui_app import Transcript, WorkspaceApp, WorkspaceClient
from cc_remote.tui_state import Block


def make_app():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    client.workspace.view("s").put(
        Block("long", "assistant", "a long answer line\n" * 300, "t", "final")
    )
    return WorkspaceApp(client, connect=False)


def cursor_visible(reader):
    position = reader.wrapped_document.location_to_offset(
        reader.cursor_location
    )
    return (
        reader.scroll_y
        <= position.y
        < (reader.scroll_y + reader.scrollable_content_region.height)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bottom", [False, True])
@pytest.mark.parametrize("cursor_at_end", [False, True])
async def test_ctrl_k_preserves_viewport_then_vim_moves_locally(
    bottom, cursor_at_end
):
    app = make_app()
    async with app.run_test(size=(80, 26)) as pilot:
        reader = app.query_one(Transcript)
        await pilot.press(*(["G"] if cursor_at_end else ["g", "g"]))
        await pilot.press("ctrl+j", "i", "x")
        target = reader.max_scroll_y if bottom else reader.max_scroll_y // 2
        reader.scroll_to(y=target, animate=False, immediate=True, force=True)
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert reader.scroll_y == target
        assert cursor_visible(reader)
        await pilot.press("k", "j")
        await pilot.pause()
        assert abs(reader.scroll_y - target) <= 2
        if bottom:
            assert app.client.workspace.view("s").follow


@pytest.mark.asyncio
async def test_already_focused_ctrl_k_reconciles_mouse_scrolled_cursor():
    app = make_app()
    async with app.run_test(size=(80, 26)) as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("g", "g")
        reader.scroll_to(y=150, animate=False, immediate=True, force=True)
        await pilot.pause()
        await pilot.press("ctrl+k")
        assert reader.scroll_y == 150 and cursor_visible(reader)


@pytest.mark.asyncio
async def test_focus_retains_visible_cursor_with_wrapped_lines():
    app = make_app()
    app.client.workspace.view("s").put(
        Block("long", "assistant", "宽行 words " * 1000, "t", "final")
    )
    async with app.run_test(size=(42, 26)) as pilot:
        reader = app.query_one(Transcript)
        app.stop_following()
        target = reader.wrapped_document.offset_to_location(Offset(3, 60))
        reader.move_cursor(target)
        await pilot.pause()
        top = reader.scroll_y
        await pilot.press("ctrl+j", "i", "x", "ctrl+k", "ctrl+k")
        assert reader.cursor_location == target
        assert reader.scroll_y == top


@pytest.mark.asyncio
@pytest.mark.parametrize("draft", [False, True])
async def test_replaced_history_anchor_keeps_reading_row(draft):
    app = make_app()
    async with app.run_test(size=(80, 26)) as pilot:
        reader = app.query_one(Transcript)
        app.stop_following()
        reader.move_cursor((180, 0))
        await pilot.pause()
        if draft:
            await pilot.press("ctrl+j")
        top = reader.scroll_y
        view = app.client.workspace.view("s")
        # Model an authoritative history page replacing an obsolete block ID.
        view.blocks.clear()
        view.put(
            Block("canonical", "assistant", "new answer\n" * 300, "t", "final")
        )
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == top
        assert reader.cursor_location == (180, 0)
        await pilot.press("ctrl+k", "k")
        assert abs(reader.scroll_y - top) <= 1
