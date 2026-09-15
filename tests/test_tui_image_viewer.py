"""Full-resolution image viewing, scoped chords, and bounded local rendering."""

import base64
import io

import pytest
from PIL import Image

from cc_remote.tui_image_viewer import ImageCanvas, ImageView
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_preview import PreviewRef, decode_image
from cc_remote.tui_preview_views import FilePreviewScreen
from tests.test_tui_preview import FakeImage, make_app


def large_reply():
    stream = io.BytesIO()
    with Image.new("RGB", (1980, 2052), "green") as image:
        image.save(stream, format="PNG")
    return dict(type="file_preview", format="image", media_type="image/png",
                data=base64.b64encode(stream.getvalue()).decode())


def test_original_resolution_and_thumbnail_limits():
    reply = large_reply()
    with decode_image(reply) as thumbnail:
        assert thumbnail.width <= 1024 and thumbnail.height <= 768
    with decode_image(reply, thumbnail=False) as original:
        assert original.size == (1980, 2052)


def test_pan_bounds_keep_source_visible_at_every_zoom():
    view = ImageView(1980, 2052)
    assert view.bounds((800, 600))[0] == (0, 0, 1980, 2052)
    view.zoom = 4
    for dx, dy in ((-100, -100), (100, 100), (1, -1)):
        view.pan(dx, dy, (800, 600))
        box, _ = view.bounds((800, 600))
        assert 0 <= box[0] < box[2] <= view.width
        assert 0 <= box[1] < box[3] <= view.height


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_image_zoom_pan_fit_and_close_are_local(monkeypatch, custom):
    app = make_app()
    if custom:
        app.client.keys = KeyConfig({"image": {
            "zoom_in": ["x l"], "zoom_out": ["x o"], "fit": ["x f"],
        }})
    calls = []

    async def request(*args, **kwargs):
        calls.append(args)
        return large_reply()

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    async with app.run_test(size=(100, 40)) as pilot:
        await app.push_screen(FilePreviewScreen(
            app.client, "s", PreviewRef("board.png", "image"), FakeImage,
        ))
        await pilot.pause()
        canvas = app.screen.query_one(ImageCanvas)
        assert canvas.source.size == (1980, 2052)
        assert canvas.size.height > 9
        chord = "x" if custom else "z"
        zoom_key = "l" if custom else "i"
        await pilot.press(chord, zoom_key, chord, zoom_key, chord, zoom_key)
        assert canvas.view.zoom == 1.5 ** 3
        center = (canvas.view.x, canvas.view.y)
        await pilot.press("h", "j")
        assert canvas.view.x < center[0] and canvas.view.y > center[1]
        await pilot.press("l", "k")
        assert canvas.view.x == pytest.approx(center[0])
        assert canvas.view.y == pytest.approx(center[1])
        await pilot.press(chord, "o")
        assert canvas.view.zoom == 1.5 ** 2
        await pilot.resize_terminal(55, 24)
        await pilot.pause()
        assert canvas.view.zoom == 1.5 ** 2
        assert canvas.frame.width <= 1600 and canvas.frame.height <= 1200
        await pilot.press(chord, "f")
        assert canvas.view.zoom == 1 and canvas.view.x == 0.5
        assert len(calls) == 1 and not app.client._outbox
        # One Esc closes even while a zoom chord is incomplete.
        await pilot.press(chord, "escape")
        assert len(app.screen_stack) == 1
        assert canvas.picture.image is None
        with pytest.raises(ValueError):
            canvas.source.getpixel((0, 0))


def test_image_chords_are_indexed_and_conflict_checked():
    keys = KeyConfig()
    assert "Image preview" in keys.help()
    assert "zoom_in" in keys.help()
    with pytest.raises(ValueError, match="prefix"):
        KeyConfig({"image": {"fit": ["z"]}})


@pytest.mark.asyncio
async def test_image_authorization_is_explicit(monkeypatch):
    app = make_app()
    calls = []

    async def request(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("challenge"):
            return dict(status="granted")
        if len(calls) == 1:
            return dict(type="preview_authorization_required",
                        authorization_id="exact-file", resolved_path="/x.png",
                        request_id="tui-preview-image")
        return large_reply()

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    async with app.run_test() as pilot:
        await app.push_screen(FilePreviewScreen(
            app.client, "s", PreviewRef("/x.png", "image"), FakeImage,
        ))
        await pilot.pause()
        await pilot.press("z", "i")
        assert len(calls) == 1 and not app.screen.query(ImageCanvas)
        await pilot.press("a")
        await pilot.pause()
        assert calls == [{}, {"challenge": {
            "authorization_id": "exact-file",
            "request_id": "tui-preview-image",
        }}, {}]
        assert app.screen.query(ImageCanvas)
