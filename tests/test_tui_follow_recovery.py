"""Follow intent survives asynchronous wheel scrolling and transcript reflow."""

import pytest

from cc_remote.tui_app import Transcript, WorkspaceApp
from cc_remote.tui_state import Block
from tests.test_tui_send_jumps import client


@pytest.mark.asyncio
async def test_wheel_arrives_at_bottom_after_initial_callback():
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("k")
        reader.scroll_to(y=reader.max_scroll_y - 5, animate=False, immediate=True)
        reader.on_mouse_scroll_down()
        await pilot.pause()
        assert not view.follow  # Scroll animation has not reached its target.
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert view.follow
        view.put(Block("new", "assistant", "new output\n" * 30))
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == reader.max_scroll_y


@pytest.mark.asyncio
async def test_downward_navigation_resumes_at_viewport_bottom_not_last_line():
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("k", "k", "k")
        app.paint()
        assert view.follow  # The reading cursor is not the scroll position.
        assert reader.cursor_location != reader.document.end
        reader.scroll_end(animate=False, immediate=True)
        await pilot.press("j")
        assert view.follow


@pytest.mark.asyncio
async def test_following_reflow_overrides_transient_middle_bookmark():
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        app.remember()
        # Text/image re-projection can temporarily reset the scroll offset
        # before a resize captures it. Follow intent remains authoritative.
        bookmark = ("s", view.anchor, view.selection, ("user:t", 0),
                    False, True, True)
        reader.resize_bookmark = bookmark
        view.put(Block("new", "assistant", "new output\n" * 30))
        app.paint()
        reader.restore_resize(bookmark)
        await pilot.pause()
        assert view.follow
        assert reader.scroll_y == reader.max_scroll_y


@pytest.mark.asyncio
async def test_ignored_read_key_does_not_disable_stream_follow():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+f8")
        assert c.workspace.view("s").follow


@pytest.mark.asyncio
async def test_stale_wheel_callback_is_ignored_but_bottom_selection_follows():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        view = c.workspace.view("s")
        app.stop_following()
        reader.scroll_end(animate=False, immediate=True)
        app.resume_following_at_bottom(False, "another-session")
        assert not view.follow
        old_revision = reader.follow_revision
        app.stop_following()  # A new upward gesture cancels an older callback.
        app.resume_following_at_bottom(False, "s", old_revision)
        assert not view.follow
        await pilot.press("v", "k")
        reader.on_mouse_scroll_down()
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert view.follow and reader.selected_text
