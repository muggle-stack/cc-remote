"""Bounded preview discovery/reads on the existing authenticated control link."""

import asyncio
import base64
import io
import os
import re
import sys
import uuid
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from markdown_it import MarkdownIt
from PIL import Image

from cc_remote.protocol import GetFilePreview, AuthorizePreview

MAX_REFS = 64
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}
PATH = re.compile(
    r"[^\s`<>\[\]{}\"']+\.(?:markdown|md|png|jpe?g|gif|webp|avif)(?=$|[\s),;。:#!?])",
    re.I,
)


@dataclass(frozen=True)
class PreviewRef:
    path: str
    kind: str


def local_reference(raw):
    raw = unquote(raw.strip()).split("#", 1)[0].split("?", 1)[0]
    raw = re.sub(r":\d+(?::\d+)?$", "", raw)
    if (
        not raw
        or len(raw.encode()) > 4096
        or raw.startswith("//")
        or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw)
        or any(ord(c) < 32 or ord(c) == 127 for c in raw)
    ):
        return None
    suffix = PurePosixPath(raw).suffix.lower()
    kind = (
        "markdown"
        if suffix in MARKDOWN_SUFFIXES
        else ("image" if suffix in IMAGE_SUFFIXES else None)
    )
    return PreviewRef(raw, kind) if kind else None


def references(text):
    """Links, inline code and plain paths; no URL fetching or disk discovery."""
    found = {}

    def add(raw):
        ref = local_reference(raw)
        if ref and len(found) < MAX_REFS:
            found.setdefault(ref.path, ref)

    def walk(tokens):
        in_link = False
        for token in tokens:
            if token.type == "link_close":
                in_link = False
            if token.type in {"link_open", "image"}:
                add(token.attrGet("href") or token.attrGet("src") or "")
                in_link = token.type == "link_open"
            if token.type in {"code_inline", "fence", "code_block"}:
                # Quoted/code paths may contain spaces.
                if local_reference(token.content):
                    add(token.content)
                else:
                    for match in PATH.finditer(token.content):
                        add(match[0])
            elif token.type == "text" and not in_link:
                for match in PATH.finditer(token.content):
                    add(match[0].lstrip("("))
            if token.children and token.type != "image":
                walk(token.children)

    walk(MarkdownIt("commonmark").parse(text[:65536]))
    return list(found.values())


async def preview_request(client, sid, path=None, *, challenge=None):
    """One correlated, cancellable private read. Never synthesize a model turn."""
    request_id = (
        challenge["request_id"] if challenge
        else "tui-preview-" + uuid.uuid4().hex
    )
    if request_id in client.preview_waiters:
        raise ValueError("This preview request is already pending")
    future = asyncio.get_running_loop().create_future()
    authorization_id = challenge["authorization_id"] if challenge else None
    client.preview_waiters[request_id] = (sid, future, authorization_id)
    command = (
        AuthorizePreview(
            sid=sid,
            authorization_id=challenge["authorization_id"],
            request_id=request_id,
            decision="allow",
            client_id=client.client_id,
        )
        if challenge
        else GetFilePreview(
            sid=sid,
            path=path,
            request_id=request_id,
            client_id=client.client_id,
        )
    )
    try:
        if not await client._send(command):
            raise ValueError(client.notice)
        response = await asyncio.wait_for(future, 20)
        if response.get("error") or response.get("type") == "error":
            raise ValueError(response.get("error") or response.get("message"))
        return response
    except TimeoutError as exc:
        raise ValueError("Preview timed out; use Reload file") from exc
    finally:
        client.preview_waiters.pop(request_id, None)


def route_preview(client, event):
    request_id = event.get("request_id") or ""
    if not request_id.startswith("tui-preview-"):
        return False
    entry = client.preview_waiters.get(request_id)
    if entry:
        sid, future, authorization_id = entry
        expected = client.workspace.rekeys.get(sid, sid)
        kind = event.get("type")
        expected_reply = (
            kind == "preview_authorization_result"
            and event.get("authorization_id") == authorization_id
            if authorization_id else kind in {
                "file_preview", "preview_authorization_required",
            }
        )
        if (
            event.get("sid") == expected
            and not future.done()
            and (kind == "error" or expected_reply)
        ):
            future.set_result(event)
    # Late or mismatched binary replies never enter transcript/report storage.
    return True


def decode_image(event, *, thumbnail=True):
    data = event.get("data") or ""
    if (
        event.get("format") != "image"
        or len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4
    ):
        raise ValueError("Image exceeds the terminal preview limit")
    if event.get("media_type") == "image/svg+xml":
        raise ValueError("SVG preview requires the Web UI")
    try:
        raw = base64.b64decode(data, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.width * source.height > 16_000_000:
                    raise ValueError(
                        "Image dimensions exceed the preview limit"
                    )
                source.seek(0)
                if thumbnail:
                    source.thumbnail((1024, 768))
                return source.convert("RGB")
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("Cannot decode this image safely") from exc


def detect_graphics():
    """Probe once BEFORE Textual owns stdin; never infer support from TERM."""
    if not (
        sys.__stdin__
        and sys.__stdout__
        and sys.__stdin__.isatty()
        and sys.__stdout__.isatty()
    ):
        return None
    try:
        # tmux advertises SIXEL for its own parser even when the attached
        # terminal cannot display it (Kitty, for example). That produces
        # tmux's "SIXEL IMAGE" / '+' placeholders. Probe the end-to-end Kitty
        # transport instead; never change the user's tmux configuration.
        if os.environ.get("TMUX"):
            from textual_image.renderable.tgp import query_terminal_support
            from textual_image.widget import HalfcellImage
            from cc_remote.tui_graphics import StableTGPImage

            return StableTGPImage if query_terminal_support() else HalfcellImage
        from textual_image.renderable import Image as Renderer
        from textual_image.widget import Image as Widget

        if Renderer.__module__.rsplit(".", 1)[-1] == "tgp":
            from cc_remote.tui_graphics import StableTGPImage

            return StableTGPImage
        if Renderer.__module__.rsplit(".", 1)[-1] == "sixel":
            return Widget
    except (
        ImportError,
        OSError,
        RuntimeError,
        ValueError,
        TimeoutError,
        ZeroDivisionError,
    ):
        pass
    return None
