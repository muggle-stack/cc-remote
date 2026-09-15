"""Clipboard isolation and low-noise terminal status presentation."""

import asyncio
import base64
import io
import sys

import pytest
from PIL import Image
from rich.console import Console
from textual import events
from textual.widgets import Static

from cc_remote import tui_clipboard as cb
from cc_remote.tui_app import Composer, WorkspaceApp, WorkspaceClient
from cc_remote.tui_chrome import ACCENT, WARNING, settings_text, status_text
from cc_remote.tui_presentation import SessionPresentation
from cc_remote.tui_settings import TextValue


def png():
    stream = io.BytesIO()
    Image.new("RGB", (4, 4)).save(stream, format="PNG")
    return stream.getvalue()


def attachment():
    return {
        "name": "clipboard.png",
        "image": True,
        "content": {
            "media_type": "image/png",
            "data": base64.b64encode(png()).decode(),
        },
    }


def app():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    return WorkspaceApp(client, connect=False)


@pytest.mark.parametrize("value, expected", [(43.2055, "43%"), (99.8, "100%")])
def test_percentages_are_whole_numbers(value, expected):
    p = SessionPresentation()
    p.context = {"percentage": value}
    p.rates = {
        "codex": {
            "secondary": {
                "used_percent": 100 - value,
                "window_duration_mins": 10080,
            }
        }
    }
    label = p.usage_label()
    assert f"{expected} used" in label
    assert f"{expected} remaining" in label
    assert "." not in label


def test_settings_and_status_have_semantic_not_rainbow_styles():
    p = SessionPresentation()
    p.settings = {
        "model": {"model": "gpt-6-astra"},
        "effort": {"effort": "high"},
        "perm": {"mode": "never"},
        "permission_profile": {"profile": ":danger-full-access"},
        "web_search": {"mode": "live"},
        "collaboration_mode": {"mode": "default"},
        "fast": {"on": True},
    }
    rendered = settings_text(p)
    console = Console()
    assert rendered.plain == p.settings_label()
    assert rendered.get_style_at_offset(console, 0).bold
    assert (
        rendered.get_style_at_offset(
            console, rendered.plain.index("never")
        ).color.triplet.hex
        == WARNING
    )
    status = status_text(
        "DRAFT INSERT · idle · writable · queued 0 · completed · 11m 35s\n"
        "Space h: help · Shortcut: space …"
    )
    assert status.get_style_at_offset(console, 0).bold
    assert status.get_style_at_offset(console, status.plain.index("idle")).dim
    colors = {s.style for s in status.spans}
    assert colors <= {f"bold {ACCENT}", ACCENT, "dim"}


def test_provider_selection_is_display_scoped(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(cb.shutil, "which", lambda exe: "/bin/" + exe)
    assert cb.providers() == []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("DISPLAY", ":9")
    providers = cb.providers()
    assert providers[0][0] == ["/bin/wl-paste", "--list-types"]
    assert providers[1][0][-1] == "TARGETS"


@pytest.mark.asyncio
async def test_clipboard_reads_only_supported_mime_and_validates(monkeypatch):
    monkeypatch.setattr(cb, "providers", lambda: [(["list"], ["read"])])
    calls = []

    async def read(args, limit):
        calls.append(args)
        return b"text/plain\nimage/png\n" if args == ["list"] else png()

    monkeypatch.setattr(cb, "read_command", read)
    result = await cb.read_clipboard_image()
    assert result == attachment()
    assert calls == [["list"], ["read", "image/png"]]


@pytest.mark.asyncio
async def test_non_image_does_not_fall_back_to_another_desktop(monkeypatch):
    monkeypatch.setattr(cb, "providers", lambda: [(["one"], []), (["two"], [])])
    calls = []

    async def read(args, limit):
        calls.append(args)
        return b"text/plain\n"

    monkeypatch.setattr(cb, "read_command", read)
    with pytest.raises(cb.NoClipboardImage):
        await cb.read_clipboard_image()
    assert calls == [["one"]]


@pytest.mark.asyncio
async def test_clipboard_rejects_invalid_and_empty_images(monkeypatch):
    monkeypatch.setattr(cb, "providers", lambda: [(["list"], ["read"])])
    for content in (b"", b"not a PNG"):

        async def read(args, limit):
            return b"image/png" if args == ["list"] else content

        monkeypatch.setattr(cb, "read_command", read)
        with pytest.raises(ValueError):
            await cb.read_clipboard_image()


@pytest.mark.asyncio
async def test_clipboard_helper_output_exit_timeout_and_cancellation(
    monkeypatch,
):
    assert (
        await cb.read_command([sys.executable, "-c", "print('ok')"], 10)
        == b"ok\n"
    )
    with pytest.raises(ValueError, match="size limit"):
        await cb.read_command([sys.executable, "-c", "print('x' * 10000)"], 100)
    with pytest.raises(ValueError, match="unavailable"):
        await cb.read_command([sys.executable, "-c", "exit(1)"], 10)
    monkeypatch.setattr(cb, "TIMEOUT", 0.05)
    with pytest.raises(ValueError, match="timed out"):
        await cb.read_command(
            [sys.executable, "-c", "import time; time.sleep(5)"], 10
        )
    monkeypatch.setattr(cb, "TIMEOUT", 3)
    task = asyncio.create_task(
        cb.read_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            10,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_image_paste_stages_only_and_keeps_text_paste(monkeypatch):
    application = app()

    async def image():
        return attachment()

    monkeypatch.setattr("cc_remote.tui_app.read_clipboard_image", image)
    async with application.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "ctrl+v")
        await pilot.pause()
        view = application.client.workspace.view("s")
        assert len(view.attachments) == 1
        assert not application.client._outbox
        editor = application.query_one("#composer", Composer)
        application.post_message(events.Paste("ordinary pasted text"))
        await pilot.pause()
        assert editor.text == "ordinary pasted text"
        assert application.query_one("#settings", Static).render() is not None
        assert not application.clipboard_busy


@pytest.mark.asyncio
async def test_paste_stays_on_original_session_after_switch_and_rekey(
    monkeypatch,
):
    application = app()
    gate = asyncio.Event()

    async def image():
        await gate.wait()
        return attachment()

    monkeypatch.setattr("cc_remote.tui_app.read_clipboard_image", image)
    async with application.run_test() as pilot:
        await pilot.press("ctrl+j", "ctrl+v", "ctrl+v")
        application.client._handle(
            {
                "type": "session_rekey",
                "old_key": "s",
                "session_id": "renamed",
            }
        )
        application.client.attached_sid = "other"
        application.paint()
        gate.set()
        await pilot.pause()
        assert (
            len(application.client.workspace.view("renamed").attachments) == 1
        )
        assert not application.client.workspace.view("other").attachments
        assert not application.client._outbox


@pytest.mark.asyncio
async def test_clipboard_limits_and_missing_display_leave_draft_intact(
    monkeypatch,
):
    application = app()
    view = application.client.workspace.view("s")
    view.attachments = [attachment() for _ in range(8)]

    async def image():
        return attachment()

    monkeypatch.setattr("cc_remote.tui_app.read_clipboard_image", image)
    await application.paste_clipboard_image("s")
    assert len(view.attachments) == 8
    assert "too many" in application.client.notice
    monkeypatch.setattr(cb, "providers", lambda: [])
    with pytest.raises(ValueError, match="No desktop clipboard"):
        await cb.read_clipboard_image()


@pytest.mark.asyncio
async def test_modal_ctrl_v_keeps_textual_text_paste(monkeypatch):
    application = app()

    async def forbidden():
        raise AssertionError("A modal must not read the desktop clipboard")

    monkeypatch.setattr("cc_remote.tui_app.read_clipboard_image", forbidden)
    async with application.run_test() as pilot:
        application.push_screen(TextValue("Value", ""))
        await pilot.pause()
        application.copy_to_clipboard("local text")
        await pilot.press("i", "ctrl+v")
        assert application.screen.query_one(Composer).text == "local text"
