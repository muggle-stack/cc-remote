"""Inline pixels must share the transcript viewport, not persistent chrome."""

import asyncio

import pytest
from textual.events import MouseScrollDown
from textual.widgets.text_area import Selection

from cc_remote.tui_app import Transcript
from cc_remote.tui_inline_images import TranscriptViewport, InlinePicture
from cc_remote.tui_preview import detect_graphics
from cc_remote.tui_state import Block
from tests.test_tui_preview import make_app, image_reply, FakeImage


def setup(monkeypatch, text="![A](a.png)\n\nAfter image."):
    app = make_app()
    app.graphics = FakeImage
    calls = []

    async def request(client, sid, path, **kwargs):
        calls.append((sid, path, kwargs))
        return image_reply()

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", request)
    view = app.client.workspace.view("s")
    view.put(Block("a", "assistant", text, "turn"))
    return app, view, calls


@pytest.mark.parametrize("supported", [True, False])
def test_tmux_never_chooses_its_sixel_placeholder(monkeypatch, supported):
    from textual_image.widget import HalfcellImage
    from cc_remote.tui_graphics import StableTGPImage

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr("cc_remote.tui_preview.sys.__stdin__", Tty())
    monkeypatch.setattr("cc_remote.tui_preview.sys.__stdout__", Tty())
    monkeypatch.setenv("TMUX", "/test/tmux")
    monkeypatch.setattr(
        "textual_image.renderable.tgp.query_terminal_support", lambda: supported
    )
    assert detect_graphics() is (StableTGPImage if supported else HalfcellImage)


@pytest.mark.asyncio
async def test_inline_paragraph_positions_and_source_copy(monkeypatch):
    app, view, calls = setup(monkeypatch,
                            "First ![A](a.png).\n\nSecond `b.png`.\n\nEnd.")
    async with app.run_test(size=(100, 55)) as pilot:
        await pilot.pause()
        await pilot.pause()
        reader = app.query_one(Transcript)
        viewport = app.query_one(TranscriptViewport)
        slots = viewport.projection.slots
        assert [s.path for s in slots] == ["a.png", "b.png"]
        first, second, end = [reader.text.index(w)
                              for w in ("First", "Second", "End.")]
        assert first < slots[0].start < second < slots[1].start < end
        assert len(viewport.query(InlinePicture)) == 2
        for _ in range(3):
            await pilot.pause(0.12)
            assert reader.size.height == viewport.content_size.height
            assert len(viewport.query(InlinePicture)) == 2
        assert not app.query("#image-shelf")
        assert [c[1] for c in calls] == ["a.png", "b.png"]
        source, source_starts = view.render()
        visible = source.replace("![A](a.png)", "A (a.png)").replace(
            "`b.png`", "b.png"
        )
        reader.selection = Selection((0, 0), reader.document.end)
        assert reader.selected_text == visible
        reader.operate("y", (0, 0), reader.document.end)
        assert app.clipboard == visible
        assert view.render()[0] == source
        for word in ("First", "Second", "End."):
            pos = reader.text.index(word)
            anchor = reader.projection.locate(view, pos, app.starts)
            assert anchor == view.locate(source.index(word), source_starts)
            assert reader.projection.resolve(
                view, anchor, app.starts, len(reader.text)
            ) == pos
            assert reader.projection.resolve(
                view, ("missing", 0), app.starts, len(reader.text),
                fallback=pos,
            ) == pos


@pytest.mark.asyncio
async def test_reference_image_starts_below_the_complete_rendered_paragraph(
    monkeypatch,
):
    app, view, calls = setup(
        monkeypatch, "![photo][ref]\n\n[ref]: photo.png"
    )
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        reader = app.query_one(Transcript)
        viewport = app.query_one(TranscriptViewport)
        assert "photo (photo.png)\n" in reader.text
        slot, = viewport.projection.slots
        assert slot.start > reader.text.index("photo.png)") + len("photo.png)")
        assert reader.text[slot.start - 1] == "\n"
        assert [call[1] for call in calls] == ["photo.png"]


@pytest.mark.asyncio
async def test_scroll_clips_images_and_never_covers_input(monkeypatch):
    app, view, _ = setup(monkeypatch)
    view.put(Block("b", "assistant", "\n".join(
        f"line {n}" for n in range(60)
    ), "t2"))
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        reader = app.query_one(Transcript)
        # History offscreen is not read merely to paint the latest turn.
        assert not viewport.cache
        app.stop_following()
        reader.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        await pilot.pause()
        assert viewport.cache and viewport.pictures
        picture = viewport.query_one(InlinePicture)
        region = app.screen.find_widget(picture).visible_region
        assert region.y >= viewport.region.y
        assert region.bottom <= viewport.region.bottom
        before = reader.scroll_y
        picture.picture.post_message(MouseScrollDown(
            picture.picture, 1, 1, 0, 0, 0, False, False, False
        ))
        await pilot.pause()
        assert reader.scroll_y > before
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert not viewport.pictures
        reader.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        assert viewport.pictures  # cached image reappears in its original spot


@pytest.mark.asyncio
async def test_image_resize_preserves_reader_anchor_and_draft(monkeypatch):
    app, view, _ = setup(monkeypatch)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        reader = app.query_one(Transcript)
        app.stop_following()
        pos = reader.text.index("After")
        reader.move_cursor(reader.document.get_location_from_index(pos))
        app.remember()
        anchor = view.anchor
        await pilot.press("ctrl+j", "i", "x")
        await pilot.resize_terminal(32, 30)
        await pilot.pause()
        assert view.anchor == anchor
        assert reader.document.get_text_range(
            reader.cursor_location,
            (reader.cursor_location[0], reader.cursor_location[1] + 5)
        ) == "After"
        assert app.focused.id == "composer" and app.focused.text == "x"
        picture = app.query_one(InlinePicture)
        assert picture.size.width <= reader.content_size.width


@pytest.mark.asyncio
async def test_switch_and_cwd_change_discard_inflight_image(monkeypatch):
    app, view, _ = setup(monkeypatch)
    waiting = asyncio.Event()
    cancelled = []

    async def request(*args):
        try:
            await waiting.wait()
            return image_reply()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", request)
    async with app.run_test() as pilot:
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        assert viewport.pending
        app.client.workspace.catalog["s"] = {"cwd": "/new"}
        app.paint()
        await pilot.pause()
        assert cancelled and viewport.identity == ("s", "/new", 0)
        app.client.attached_sid = "other"
        app.paint()
        await pilot.pause()
        waiting.set()
        await pilot.pause()
        assert not viewport.cache and not viewport.pictures


@pytest.mark.asyncio
async def test_no_auto_read_from_tool_code_or_remote_link(monkeypatch):
    app, view, calls = setup(monkeypatch,
                            "```\na.png\n```\n\n![web](https://host/b.png)")
    view.put(Block("tool", "tool", "c.png"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not calls
        assert not app.query_one(TranscriptViewport).projection.slots


@pytest.mark.asyncio
async def test_folded_history_and_modal_remove_pixels(monkeypatch):
    app, view, _ = setup(monkeypatch)
    async with app.run_test(size=(90, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        assert viewport.pictures
        await pilot.press("space", "h")
        await pilot.pause(0.15)
        assert not viewport.pictures
        await pilot.press("escape")
        await pilot.pause()
        assert viewport.pictures
        view.blocks.clear()
        view.version += 1
        app.paint()
        await pilot.pause()
        assert not viewport.projection.slots and not viewport.pictures
