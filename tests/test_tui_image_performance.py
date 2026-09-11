"""Structural performance budgets: scrolling/panning must not upload pixels."""

from types import SimpleNamespace

import pytest
from PIL import Image
from rich.console import Console

from cc_remote.tui_app import Transcript
from cc_remote.tui_graphics import StableTGPImage
from cc_remote.tui_image_viewer import ImageCanvas
from cc_remote.tui_inline_images import TranscriptViewport
from cc_remote.tui_preview import PreviewRef
from cc_remote.tui_preview_views import FilePreviewScreen
from cc_remote.tui_state import Block
from tests.test_tui_inline_images import setup
from tests.test_tui_image_viewer import large_reply
from tests.test_tui_preview import make_app


@pytest.mark.asyncio
async def test_pan_and_zoom_send_only_placement_metadata(monkeypatch):
    sent = []
    monkeypatch.setattr("textual_image.renderable.tgp._send_tgp_message",
                        lambda **kw: sent.append(kw))
    app = make_app()
    source = Image.new("RGB", (1980, 2052), "green")
    async with app.run_test(size=(100, 40)) as pilot:
        canvas = ImageCanvas(StableTGPImage, source)
        await app.mount(canvas)
        # Keep the production pan/zoom path, with deterministic viewport size.
        canvas.styles.width = 80
        canvas.styles.height = 20
        await pilot.pause()
        canvas.draw_frame()
        console = Console(width=80, height=20)
        list(console.render(canvas.picture.render()))
        image_id = canvas.picture._renderable.terminal_image_id
        sent.clear()

        def no_resampling(*args, **kwargs):
            pytest.fail("Pan/zoom must not resample or re-encode source pixels")

        monkeypatch.setattr(Image.Image, "resize", no_resampling)
        for n in range(60):
            canvas.zoom(1 if n % 10 < 5 else -1)
            canvas.pan(1 if n % 2 else -1, 1 if n % 2 else -1)
            canvas.draw_frame()
            list(console.render(canvas.picture.render()))
        assert canvas.frame is None
        assert sent and all(m["a"] == "p" and "payload" not in m for m in sent)
        assert all(m["i"] == image_id and m["p"] == 1 for m in sent)
        assert all(m["w"] > 0 and m["h"] > 0 for m in sent)
        assert len(sent) <= 60
        assert sum(len(str(m)) for m in sent) < 15000


@pytest.mark.asyncio
async def test_scroll_out_and_back_keeps_the_same_uploaded_image(monkeypatch):
    app, view, calls = setup(monkeypatch)
    app.graphics = StableTGPImage
    sent = []
    monkeypatch.setattr("textual_image.renderable.tgp._send_tgp_message",
                        lambda **kw: sent.append(kw))
    view.put(Block("b", "assistant", "\n".join(
        f"line {n}" for n in range(70)
    ), "t2"))
    async with app.run_test(size=(80, 28)) as pilot:
        app.stop_following()
        reader = app.query_one(Transcript)
        viewport = app.query_one(TranscriptViewport)
        reader.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        await pilot.pause()
        picture = next(iter(viewport.pictures.values()))[1]
        console = Console(width=80, height=28)
        list(console.render(picture.picture.render()))
        renderable = picture.picture._renderable
        sent.clear()
        for _ in range(5):
            reader.scroll_end(animate=False, immediate=True)
            viewport.sync()
            assert not viewport.pictures and viewport.parked
            assert not picture.display
            reader.scroll_home(animate=False, immediate=True)
            viewport.sync()
            assert next(iter(viewport.pictures.values()))[1] is picture
            list(console.render(picture.picture.render()))
        assert picture.picture._renderable is renderable
        assert not sent and len(calls) == 1
        # Real removal must still release the parked terminal image.
        reader.scroll_end(animate=False, immediate=True)
        viewport.sync()
        view.blocks.clear()
        view.version += 1
        app.paint()
        await pilot.pause()
        assert not viewport.parked and not viewport.pictures
        assert any(m.get("a") == "d" for m in sent)


def test_offscreen_cache_eviction_releases_widgets_and_pixels():
    viewport = TranscriptViewport(Transcript())
    images = {n: Image.new("RGB", (20, 10)) for n in range(12)}
    viewport.cache = images.copy()
    removed = []
    viewport.pictures = {10: None, 11: None}
    viewport.parked = {
        n: (None, SimpleNamespace(remove=lambda n=n: removed.append(n)))
        for n in range(10)
    }
    viewport.trim_cache()
    assert len(viewport.cache) == viewport.MAX_CACHE
    assert removed == [0, 1, 2, 3]
    assert 10 in viewport.cache and 11 in viewport.cache
    for n in removed:
        with pytest.raises(ValueError):
            images[n].getpixel((0, 0))
    for image in viewport.cache.values():
        image.close()


@pytest.mark.asyncio
async def test_keyboard_pan_reaches_kitty_without_manual_render(monkeypatch):
    sent = []
    monkeypatch.setattr("textual_image.renderable.tgp._send_tgp_message",
                        lambda **kw: sent.append(kw))

    async def request(*args, **kwargs):
        return large_reply()

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    app = make_app()
    async with app.run_test(size=(100, 40)) as pilot:
        await app.push_screen(FilePreviewScreen(
            app.client, "s", PreviewRef("board.png", "image"), StableTGPImage,
        ))
        await pilot.pause()
        await pilot.press("z", "i")
        await pilot.pause()
        before = [m for m in sent if m.get("a") == "p"][-1]
        sent.clear()
        await pilot.press("j")
        await pilot.pause()
        updates = [m for m in sent if m.get("a") == "p"]
        assert updates and updates[-1]["y"] > before["y"]
        assert not any("payload" in m for m in sent)
