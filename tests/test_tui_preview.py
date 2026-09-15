"""Model-free preview discovery, file hints, Vim, RPC and image isolation."""

import asyncio
import base64
import io

import pytest
from PIL import Image
from textual.widgets import Static

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, Composer
from cc_remote.tui_state import Block
from cc_remote.tui_preview import (
    PreviewRef,
    references,
    local_reference,
    decode_image,
    preview_request,
    route_preview,
    detect_graphics,
)
from cc_remote.tui_preview_views import (
    FileHints,
    FilePreviewScreen,
    MarkdownReader,
)
from cc_remote.tui_inline_images import TranscriptViewport, InlinePicture


def make_app():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    return WorkspaceApp(client, connect=False)


def image_reply():
    output = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(output, format="PNG")
    return {
        "type": "file_preview",
        "format": "image",
        "media_type": "image/png",
        "data": base64.b64encode(output.getvalue()).decode(),
    }


def test_detect_markdown_images_spaces_and_deduplicate():
    refs = references(
        "[Plan](</work/a plan.md>) and `docs/b.markdown`\n"
        "![image](/work/image.png)\n/work/a.md:12\n"
        "[again](/work/image.png)"
    )
    assert [r.path for r in refs] == [
        "/work/a plan.md",
        "docs/b.markdown",
        "/work/image.png",
        "/work/a.md",
    ]
    assert len(references(" ".join(f"file{i}.md" for i in range(100)))) == 64


@pytest.mark.parametrize(
    "path",
    [
        "https://host/a.md",
        "file:///a.md",
        "//host/a.png",
        "javascript:x.md",
        "%2f%2fhost/a.md",
        "a%00.md",
        "a\x1b.md",
        "foo.txt",
    ],
)
def test_only_local_bounded_paths(path):
    assert local_reference(path) is None


def test_no_requests_for_remote_links():
    assert not references("https://host/a.md ![pic](https://host/x.png)")
    assert [r.path for r in references("[fake.md](real.md) (other.md)")] == [
        "real.md",
        "other.md",
    ]


@pytest.mark.asyncio
async def test_real_image_widget_integrates_with_textual(monkeypatch):
    from textual_image.widget import HalfcellImage

    monkeypatch.delenv("NO_COLOR", raising=False)
    app = make_app()
    app.graphics = HalfcellImage

    async def request(*args, **kwargs):
        return image_reply()

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", request)
    app.client.workspace.view("s").put(
        Block("image", "assistant", "result.png")
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(HalfcellImage)) == 1
        assert app.query_one(TranscriptViewport).cache
        picture = app.query_one(HalfcellImage)
        strips = picture.render_lines(picture.size.region)
        pixels = [segment for strip in strips for segment in strip
                  if "▀" in segment.text]
        assert pixels and all(s.style.color.triplet.red == 255 for s in pixels)
        assert not any("SIXEL IMAGE" in strip.text for strip in strips)
        assert "▀" in app.export_screenshot()


@pytest.mark.asyncio
async def test_automatic_external_image_waits_for_user_authorization(
    monkeypatch,
):
    app = make_app()
    app.graphics = FakeImage
    calls = []

    async def request(*args, **kwargs):
        calls.append(kwargs)
        return {"type": "preview_authorization_required"}

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", request)
    app.client.workspace.view("s").put(
        Block("image", "assistant", "result.png")
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert calls == [{}]
        viewport = app.query_one(TranscriptViewport)
        assert not viewport.cache
        assert "authorization" in next(iter(viewport.errors.values()))


def test_decode_bounded_image_without_filesystem(monkeypatch):
    decoded = decode_image(image_reply())
    assert decoded.size == (20, 10)
    decoded.close()
    with pytest.raises(ValueError):
        decode_image({"format": "image", "data": "invalid!"})
    with pytest.raises(ValueError, match="SVG"):
        decode_image({"format": "image", "media_type": "image/svg+xml"})
    monkeypatch.setattr("cc_remote.tui_preview.MAX_IMAGE_BYTES", 1)
    with pytest.raises(ValueError, match="limit"):
        decode_image(image_reply())


def test_no_terminal_probe_when_not_tty(monkeypatch):
    class Pipe:
        def isatty(self):
            return False

    monkeypatch.setattr("cc_remote.tui_preview.sys.__stdin__", Pipe())
    assert detect_graphics() is None


@pytest.mark.asyncio
async def test_preview_rpc_rejects_other_sessions_and_cleans_cancelled_reads():
    client = make_app().client
    task = asyncio.create_task(preview_request(client, "s", "doc.md"))
    await asyncio.sleep(0)
    rid = next(iter(client.preview_waiters))
    event = {
        "type": "file_preview",
        "request_id": rid,
        "sid": "other",
        "content": "private",
        "format": "markdown",
    }
    assert route_preview(client, event) and not task.done()
    client._on_event({**event, "sid": "s"})
    assert (await task)["content"] == "private"
    assert not client.preview_waiters
    assert not client.workspace.view("s").presentation.reports
    task = asyncio.create_task(preview_request(client, "s", "doc.md"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client.preview_waiters


@pytest.mark.asyncio
async def test_numbered_hints_immediate_digit_and_paging():
    app = make_app()
    refs = [PreviewRef(f"doc{i}.md", "markdown") for i in range(13)]
    async with app.run_test() as pilot:
        chosen = []
        await app.push_screen(FileHints(refs), chosen.append)
        await pilot.press("l", "4")
        assert chosen == [refs[12]]
        await app.push_screen(FileHints(refs), chosen.append)
        await pilot.press("2")
        assert chosen[-1] == refs[1]
        assert not app.client._outbox


@pytest.mark.asyncio
async def test_multiple_docs_shortcut_uses_numbered_selection(monkeypatch):
    app = make_app()
    app.client.workspace.view("s").put(
        Block("a", "assistant", "`a.md` and `b.md`")
    )
    calls = []

    async def request(client, sid, path=None, **kwargs):
        calls.append((sid, path))
        return {
            "type": "file_preview",
            "format": "markdown",
            "content": "# Hello",
        }

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    async with app.run_test() as pilot:
        await pilot.press("space", "v")
        assert isinstance(app.screen, FileHints)
        assert not calls
        await pilot.press("2")
        await pilot.pause()
        assert isinstance(app.screen, FilePreviewScreen)
        assert calls == [("s", "b.md")]
        assert "Hello" in app.screen.query_one(MarkdownReader).text
        await pilot.press("escape")
        assert isinstance(app.screen, FileHints)
        assert app.screen.page == 0
        from textual.widgets import OptionList
        assert app.screen.query_one(OptionList).highlighted == 1
        await pilot.press("1")
        await pilot.pause()
        assert calls[-1] == ("s", "a.md")
        await pilot.press("escape", "escape")
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_markdown_readonly_vim_no_save_and_no_external_asset_fetch(
    monkeypatch,
):
    app = make_app()

    async def request(*args, **kwargs):
        return {
            "type": "file_preview",
            "format": "markdown",
            "content": "# Heading\n\ncall(one two)\n\n![remote](https://host/img.png)",
        }

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    async with app.run_test() as pilot:
        await app.push_screen(
            FilePreviewScreen(app.client, "s", PreviewRef("a.md", "markdown"))
        )
        await pilot.pause()
        reader = app.screen.query_one(MarkdownReader)
        assert "# Heading" not in reader.text and "Heading" in reader.text
        index = reader.text.index("one two")
        reader.move_cursor(reader.document.get_location_from_index(index))
        await pilot.press("y", "i", "left_parenthesis")
        assert app.clipboard == "one two"
        original = reader.text
        await pilot.press("c", "i", "w", "ctrl+s", "ctrl+e")
        assert reader.text == original and reader.read_only
        assert not app.client._outbox
        await pilot.press("escape")
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_outside_file_requires_explicit_a_never_auto_grants(monkeypatch):
    app = make_app()
    calls = []

    async def request(client, sid, path=None, **kwargs):
        calls.append(kwargs)
        if kwargs.get("challenge"):
            return {"type": "preview_authorization_result", "status": "granted"}
        if len(calls) == 1:
            return {
                "type": "preview_authorization_required",
                "authorization_id": "auth",
                "request_id": "tui-preview-original",
                "resolved_path": "/outside/a.md",
            }
        return {
            "type": "file_preview",
            "format": "markdown",
            "content": "allowed",
        }

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    async with app.run_test() as pilot:
        await app.push_screen(
            FilePreviewScreen(
                app.client, "s", PreviewRef("/outside/a.md", "markdown")
            )
        )
        await pilot.pause()
        assert len(calls) == 1
        await pilot.press("a")
        await pilot.pause()
        assert calls == [{}, {"challenge": {
            "authorization_id": "auth",
            "request_id": "tui-preview-original",
        }}, {}]


@pytest.mark.asyncio
async def test_authorization_reuses_original_correlated_read_id():
    client = make_app().client
    sent = []

    async def send(command):
        sent.append(command)
        response = {
            "sid": "s", "request_id": command.request_id,
            "authorization_id": "challenge",
        }
        if command.type == "get_file_preview":
            response["type"] = "preview_authorization_required"
        else:
            assert command.request_id == sent[0].request_id
            assert command.authorization_id == "challenge"
            route_preview(client, {
                **response, "type": "preview_authorization_required",
            })
            assert not client.preview_waiters[command.request_id][1].done()
            route_preview(client, {
                **response, "type": "preview_authorization_result",
                "authorization_id": "different", "status": "granted",
            })
            assert not client.preview_waiters[command.request_id][1].done()
            response.update(type="preview_authorization_result",
                            status="granted")
        route_preview(client, response)
        return True

    client._send = send
    challenge = await preview_request(client, "s", "/outside/a.md")
    assert not client.preview_waiters
    result = await preview_request(client, "s", challenge=challenge)
    assert result["status"] == "granted"
    assert not client.preview_waiters


class FakeImage(Static):
    def __init__(self, image):
        super().__init__("[image pixels]")
        self.image = image


@pytest.mark.asyncio
async def test_auto_image_does_not_steal_focus_and_clears_on_session_switch(
    monkeypatch,
):
    app = make_app()
    app.graphics = FakeImage
    calls = []

    async def request(client, sid, path=None, **kwargs):
        calls.append((sid, path))
        return image_reply()

    monkeypatch.setattr("cc_remote.tui_inline_images.preview_request", request)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "x")
        editor = app.query_one(Composer)
        app.client.workspace.view("s").put(
            Block("image", "assistant", "![pic](result.png)")
        )
        app.paint()
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        assert viewport.cache and len(viewport.query(InlinePicture)) == 1
        assert app.focused is editor and editor.text == "x"
        app.paint()
        await pilot.pause()
        assert calls == [("s", "result.png")]
        app.client.attached_sid = "new"
        app.paint()
        await pilot.pause()
        assert not viewport.cache and not viewport.query(InlinePicture)


@pytest.mark.asyncio
async def test_unsupported_terminal_never_automatically_reads_images():
    app = make_app()
    app.client.workspace.view("s").put(
        Block("image", "assistant", "result.png")
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query_one(TranscriptViewport).projection.slots
        assert not app.client._outbox
