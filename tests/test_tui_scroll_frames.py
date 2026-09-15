"""Observe intermediate scroll offsets and frames, not just settled positions."""

import asyncio

import pytest

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp
from cc_remote.tui_state import Block
from tests.test_tui_send_jumps import client


def watch_frames(monkeypatch, app):
    frames = []
    original = app._display

    def display(screen, renderable):
        if renderable is not None:
            reader = app.query_one(Transcript)
            frames.append((reader.scroll_y, reader.max_scroll_y))
        original(screen, renderable)

    monkeypatch.setattr(app, "_display", display)
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize("draft_focus", [False, True])
@pytest.mark.parametrize("chunk", ["new line\n" * 4, "新内容 wrapped words " * 20])
async def test_stream_never_scrolls_up_between_bottom_frames(
    monkeypatch, draft_focus, chunk
):
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    offsets, frames = [], []
    watch = Transcript.watch_scroll_y
    display = app._display
    recording = False

    def observe(reader, old, new):
        if recording:
            offsets.append((old, new))
        watch(reader, old, new)

    def frame(screen, renderable):
        if recording and renderable is not None:
            reader = app.query_one(Transcript)
            frames.append((reader.scroll_y, reader.max_scroll_y))
        display(screen, renderable)

    monkeypatch.setattr(Transcript, "watch_scroll_y", observe)
    monkeypatch.setattr(app, "_display", frame)
    async with app.run_test(size=(80, 25)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        # Retain an older reading cursor while the viewport follows the tail.
        reader.move_cursor((25, 1))
        if draft_focus:
            app.query_one(Composer).focus()
        await pilot.pause()
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        recording = True
        for n in range(5):
            view.put(Block("stream", "assistant", f"{n}: {chunk}"), append=True)
            app.paint()
            await pilot.pause()
        recording = False
        assert reader.scroll_y == reader.max_scroll_y
        assert offsets and frames
        assert all(new >= old for old, new in offsets), offsets
        assert all(y == bottom for y, bottom in frames), frames


@pytest.mark.asyncio
async def test_scrolled_up_stream_has_no_intermediate_viewport_movement(monkeypatch):
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    offsets = []
    async with app.run_test() as pilot:
        await pilot.press("g", "g", "j", "j", "ctrl+j")
        reader = app.query_one(Transcript)
        reader.scroll_to(y=20, animate=False, immediate=True)
        await pilot.pause()
        original = reader.watch_scroll_y

        def watch(old, new):
            offsets.append((old, new))
            original(old, new)

        monkeypatch.setattr(reader, "watch_scroll_y", watch)
        for n in range(5):
            view.put(Block("stream", "assistant", f"chunk {n}\n" * 4), append=True)
            app.paint()
            await pilot.pause()
        assert reader.scroll_y == 20
        assert all(old == new == 20 for old, new in offsets), offsets


@pytest.mark.asyncio
async def test_resize_frames_stay_at_bottom(monkeypatch):
    c = client()
    c.workspace.view("s").put(Block("wide", "assistant", "wide words " * 300))
    app = WorkspaceApp(c, connect=False)
    frames = []
    async with app.run_test(size=(90, 30)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((10, 0))
        await pilot.press("ctrl+j")
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        original = app._display

        def frame(screen, renderable):
            if renderable is not None:
                frames.append((reader.scroll_y, reader.max_scroll_y))
            original(screen, renderable)

        monkeypatch.setattr(app, "_display", frame)
        for size in [(42, 22), (105, 40), (65, 27)]:
            await pilot.resize_terminal(*size)
            await pilot.pause()
        assert frames and all(y == bottom for y, bottom in frames), frames


@pytest.mark.asyncio
async def test_unchanged_projection_does_not_reload_the_document(monkeypatch):
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)

        def forbidden(text):
            raise AssertionError("An unchanged projection must not reload")

        monkeypatch.setattr(reader, "load_text", forbidden)
        for _ in range(3):
            c.workspace.view("s").version += 1
            app.paint()
            await pilot.pause()


@pytest.mark.asyncio
async def test_completion_fold_has_no_intermediate_cursor_frame(monkeypatch):
    c = client()
    view = c.workspace.view("s")
    view.event(dict(type="user_msg", msg_id="live", prompt="work"))
    view.put(Block("progress", "assistant", "progress\n" * 100,
                   "live", "commentary"))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((25, 0))
        await pilot.press("ctrl+j")
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        frames = watch_frames(monkeypatch, app)
        view.put(Block("final", "assistant", "finished", "live", "final"))
        view.event(dict(type="turn_end", turn_id="live", result={}))
        app.paint()
        await pilot.pause()
        assert frames and all(y == bottom for y, bottom in frames), frames
        assert "progress\n" in reader.text  # Completion keeps the outer open.


@pytest.mark.asyncio
async def test_elapsed_clock_updates_never_scroll_to_the_reading_cursor(monkeypatch):
    from cc_remote.tui_presentation import TurnDisplay

    now = [1009.0]
    monkeypatch.setattr("cc_remote.tui_app.time.time", lambda: now[0])
    c = client()
    view = c.workspace.view("s")
    view.presentation.turns["t"] = TurnDisplay(started=1000)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((20, 0))
        await pilot.press("ctrl+j")
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        frames = watch_frames(monkeypatch, app)
        for value in (1010.0, 1011.0, 1060.0):
            now[0] = value
            app.paint()
            await pilot.pause()
        assert frames and all(y == bottom for y, bottom in frames), frames


@pytest.mark.asyncio
async def test_async_inline_image_expansion_keeps_every_frame_at_bottom(monkeypatch):
    from cc_remote.tui_inline_images import TranscriptViewport
    from tests.test_tui_preview import FakeImage, image_reply

    release = asyncio.Event()

    async def image_request(*args, **kwargs):
        await release.wait()
        return image_reply()

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", image_request)
    c = client()
    view = c.workspace.view("s")
    view.put(Block("image", "assistant", "![preview](preview.png)"))
    app = WorkspaceApp(c, connect=False)
    app.graphics = FakeImage
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((20, 0))
        await pilot.press("ctrl+j")
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        frames = watch_frames(monkeypatch, app)
        release.set()
        viewport = app.query_one(TranscriptViewport)
        tasks = list(viewport.pending.values())
        assert tasks
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        await pilot.pause()
        assert viewport.cache
        assert frames and all(y == bottom for y, bottom in frames), frames
