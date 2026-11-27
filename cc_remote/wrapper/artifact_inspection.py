"""Inspect a session's visual artifacts through a read-only local MCP tool."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import stat
import sys
from uuid import uuid4

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from PIL import Image, ImageOps

from cc_remote.protocol import ARTIFACT_PREVIEW_MAX_BYTES
from cc_remote.wrapper.artifact_rendering import (
    VIEWPORT,
    WorkspaceAssets,
    render_document as _render_document,
    render_static_page,
    render_table,
)
from cc_remote.wrapper.artifact_tables import inspect_table
from cc_remote.wrapper.presentation_preview import prepare_word_preview, validate_presentation_preview
from cc_remote.wrapper.session_files import PreviewAuthorizationNeeded, read_session_file

SERVER_NAME = "cc_remote_artifact_preview"
TOOL_NAME = "inspect_file"
FILE_LINK_GUIDANCE = (
    "The user is reading your replies in cc-remote's browser. When sharing "
    "files or directories available on this machine, use clickable Markdown "
    "links with absolute paths, for example [Listen](</absolute/path/audio.wav>) "
    "or [Open folder](</absolute/path/output/>), rather than bare paths or "
    "file:// URLs. These links open cc-remote's file browser or preview, "
    "including audio playback. Link directly to existing audio files when "
    "the user asks to listen; no separate HTML page or web server is needed. "
    "Only link to real files and directories; preserve their actual paths."
)
_IMAGE_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF",
    ".avif": "AVIF", ".heic": "HEIF", ".heif": "HEIF", ".tif": "TIFF", ".tiff": "TIFF",
    ".bmp": "BMP",
}
_PAGED_DOCUMENTS = {
    ".pptx": "pptx", ".pdf": "pdf", ".docx": "docx",
    ".html": "html", ".htm": "html", ".md": "markdown", ".markdown": "markdown",
}
_DIAGRAMS = {".svg": "svg", ".mmd": "mermaid", ".mermaid": "mermaid"}
_TABLES = {".xlsx", ".csv", ".tsv"}
_SUPPORTED_SUFFIXES = frozenset({*_IMAGE_FORMATS, *_PAGED_DOCUMENTS, *_DIAGRAMS, *_TABLES})
_MAX_PIXELS = 24_000_000
_MAX_OUTPUT_BYTES = 1024 * 1024
_ROOT = Path(__file__).resolve().parents[2]


_Workspace = tuple[str, tuple[int, int]]
CodexArtifactScope = tuple[Path, str]


def _workspace(cwd: str) -> _Workspace:
    canonical = os.path.realpath(cwd)
    info = os.stat(canonical)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("The session workspace is unavailable.")
    return canonical, (info.st_dev, info.st_ino)


def prepare_codex_scope(
    state_dir: Path, codex_home: str, thread_id: str | None,
) -> CodexArtifactScope:
    """Reuse the same key across reconnects to a daemon-resident native thread."""
    directory = state_dir.resolve() / "artifact-inspection"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("The artifact tool state directory is not private.")
    database = directory / "codex-scopes.sqlite3"
    if database.is_symlink():
        raise ValueError("Invalid Codex artifact tool state file.")
    key = uuid4().hex
    with closing(sqlite3.connect(database, timeout=2)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS scopes (
                key TEXT PRIMARY KEY, account TEXT NOT NULL, thread_id TEXT,
                cwd TEXT, device TEXT, inode TEXT, UNIQUE (account, thread_id)
            )
        """)
        connection.execute(
            "INSERT OR IGNORE INTO scopes (key, account, thread_id) VALUES (?, ?, ?)",
            (key, codex_home, thread_id),
        )
        if thread_id is not None:
            key, = connection.execute(
                "SELECT key FROM scopes WHERE account = ? AND thread_id = ?",
                (codex_home, thread_id),
            ).fetchone()
    return database, key


def bind_codex_scope(scope: CodexArtifactScope, thread_id: str, cwd: str) -> None:
    database, key = scope
    root, (device, inode) = _workspace(cwd)
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=2)) as connection, connection:
        updated = connection.execute(
            "UPDATE scopes SET thread_id = ?, cwd = ?, device = ?, inode = ? "
            "WHERE key = ? AND (thread_id IS NULL OR thread_id = ?)",
            (thread_id, root, str(device), str(inode), key, thread_id),
        )
        if updated.rowcount != 1:
            raise ValueError("The Codex artifact tool does not belong to this session.")


def discard_pending_codex_scope(scope: CodexArtifactScope) -> None:
    database, key = scope
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=2)) as connection, connection:
        connection.execute("DELETE FROM scopes WHERE key = ? AND thread_id IS NULL", (key,))


def _codex_workspace(scope: CodexArtifactScope) -> _Workspace:
    database, key = scope
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            row = connection.execute(
                "SELECT cwd, device, inode FROM scopes "
                "WHERE key = ? AND thread_id IS NOT NULL AND cwd IS NOT NULL",
                (key,),
            ).fetchone()
        if row is not None:
            return row[0], (int(row[1]), int(row[2]))
    except (sqlite3.Error, ValueError, TypeError):
        pass
    raise ValueError("The Codex artifact tool's workspace is unavailable. Reconnect the session.")


def codex_mcp_config(scope: CodexArtifactScope) -> dict:
    """A per-thread native override with a workspace the Wrapper can rebind."""
    database, key = scope
    return {
        "command": sys.executable,
        "args": ["-I", "-B", str(_ROOT / "deploy" / "artifact_inspect_main.py"), str(database), key],
        "cwd": str(_ROOT),
        "startup_timeout_sec": 10,
        "tool_timeout_sec": 35,
    }


def _read_artifact(
    cwd: str, path: str, workspace_identity: tuple[int, int] | None,
) -> tuple[str, bytes]:
    try:
        relative, data, _, _ = read_session_file(
            cwd, path, allowed_suffixes=_SUPPORTED_SUFFIXES,
            max_bytes=ARTIFACT_PREVIEW_MAX_BYTES, allow_truncate=False,
            workspace_identity=workspace_identity,
        )
    except PreviewAuthorizationNeeded as exc:
        raise ValueError("Only files inside this session's working directory can be inspected.") from exc
    return relative, data


def render_raster_image(data: bytes, expected_format: str, frame: int = 1) -> tuple[bytes, str, int]:
    """Return a bounded PNG or JPEG, including frames of other raster formats."""
    if expected_format == "HEIF":
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise ValueError("HEIC inspection is unavailable. Reinstall the Wrapper dependencies.") from exc
        register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)

    try:
        with Image.open(BytesIO(data), formats=[expected_format]) as image:
            if image.width * image.height > _MAX_PIXELS:
                raise ValueError("Image exceeds the 24 megapixel inspection limit.")
            frame_count = getattr(image, "n_frames", 1)
            if frame > frame_count:
                raise ValueError(f"Frame {frame} is out of range. This file has {frame_count} frame(s).")
            if expected_format in {"GIF", "WEBP", "PNG"} and frame * image.width * image.height > 96_000_000:
                raise ValueError("Decoding this far into the animation exceeds the inspection limit.")
            image.seek(frame - 1)
            if image.width * image.height > _MAX_PIXELS:
                raise ValueError("Image exceeds the 24 megapixel inspection limit.")
            if (expected_format in {"PNG", "JPEG"} and frame_count == 1
                    and len(data) <= _MAX_OUTPUT_BYTES and image.getexif().get(274, 1) == 1
                    and image.width <= VIEWPORT["width"] and image.height <= VIEWPORT["height"]):
                image.load()
                mime_type = "image/png" if expected_format == "PNG" else "image/jpeg"
                return data, mime_type, frame_count
            oriented = ImageOps.exif_transpose(image)
            oriented.thumbnail((VIEWPORT["width"], VIEWPORT["height"]), Image.Resampling.LANCZOS)
            transparent = oriented.convert("RGBA")
            with BytesIO() as output:
                transparent.save(output, format="PNG")
                if output.tell() <= _MAX_OUTPUT_BYTES:
                    return output.getvalue(), "image/png", frame_count
                rgb = Image.new("RGB", transparent.size, "white")
                rgb.paste(transparent, mask=transparent.getchannel("A"))
                output.seek(0)
                output.truncate()
                rgb.save(output, format="JPEG", quality=85)
                if output.tell() > _MAX_OUTPUT_BYTES:
                    raise ValueError("Rendered image exceeds the inspection result limit.")
                return output.getvalue(), "image/jpeg", frame_count
    except (OSError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("The file is not a readable image of the requested format.") from exc


async def inspect_artifact(
    cwd: str, path: str, page: int = 1, *,
    frame: int = 1, sheet: str | None = None, cell_range: str | None = None, view: str = "visual",
    workspace_identity: tuple[int, int] | None = None,
) -> list[types.ContentBlock]:
    if type(page) is not int or not 1 <= page <= 4000:
        raise ValueError("page must be a page number from 1 to 4000.")
    if type(frame) is not int or not 1 <= frame <= 256:
        raise ValueError("frame must be a frame number from 1 to 256.")
    if view not in ("visual", "data"):
        raise ValueError("view must be visual or data.")
    if workspace_identity is None:
        cwd, workspace_identity = await asyncio.to_thread(_workspace, cwd)
    relative, data = await asyncio.to_thread(_read_artifact, cwd, path, workspace_identity)
    suffix = Path(relative).suffix.lower()
    if suffix not in _PAGED_DOCUMENTS and page != 1:
        raise ValueError("Page selection is available for PDF, PPTX, DOCX, HTML and Markdown.")
    if suffix not in _IMAGE_FORMATS and frame != 1:
        raise ValueError("Frame selection is available for raster images.")
    if suffix not in _TABLES and (sheet is not None or cell_range is not None or view == "data"):
        raise ValueError("sheet, range and data view are for tables.")

    summary = f"Viewed {relative}"
    if suffix in _IMAGE_FORMATS:
        image, mime_type, count = await asyncio.to_thread(render_raster_image, data, _IMAGE_FORMATS[suffix], frame)
        if count > 1:
            summary += f", frame {frame} of {count}"
    else:
        if suffix in _TABLES:
            table = await asyncio.to_thread(inspect_table, data, suffix, sheet, cell_range, visual=view == "visual")
            if view == "data":
                return [types.TextContent(type="text", text=table.as_json())]
            screenshot = await render_table(table.as_html())
            summary += f", {table.title} {table.region.address}"
        elif suffix in {".html", ".htm"}:
            assets = WorkspaceAssets(cwd, relative, workspace_identity)
            screenshot, count = await render_static_page(data, page, assets=assets)
            summary += f", viewport {page} of {count} (static HTML)"
        else:
            kind = _PAGED_DOCUMENTS.get(suffix) or _DIAGRAMS[suffix]
            if kind == "pptx":
                await asyncio.to_thread(validate_presentation_preview, data)
            elif kind == "docx":
                data = await asyncio.to_thread(prepare_word_preview, data)
            if kind == "markdown":
                screenshot, info = await _render_document(
                    data, kind, page, assets=WorkspaceAssets(cwd, relative, workspace_identity),
                )
            else:
                screenshot, info = await _render_document(data, kind, page)
            if kind in {"pptx", "pdf", "docx", "markdown"}:
                unit = "slide" if kind == "pptx" else "page"
                summary += f", {unit} {page} of {info['pageCount']}"
            if info.get("partial"):
                summary += " (some elements could not be rendered)"
            if kind == "docx":
                summary += " (browser layout)"
        image, mime_type, _ = await asyncio.to_thread(render_raster_image, screenshot, "PNG")
    return [
        types.TextContent(type="text", text=summary),
        types.ImageContent(type="image", data=base64.b64encode(image).decode("ascii"), mimeType=mime_type),
    ]


def _make_artifact_server(
    workspace: Callable[[], _Workspace], *, before_inspect: Callable[[], Awaitable[object]] | None = None,
) -> Server:
    server = Server(SERVER_NAME)
    limit = asyncio.Lock()

    @server.list_tools()
    async def list_tools():
        return [types.Tool(
            name=TOOL_NAME,
            description=(
                "Inspect files inside this session's workspace without opening a desktop app. "
                "Returns an image for common raster files, SVG, PPTX, PDF, DOCX, static HTML, Markdown, "
                "Mermaid, XLSX, CSV, and TSV. Choose a page, frame, or table region as appropriate. "
                "If an image is returned as a file URI, read that resource with the native file reader. "
                "Use data view for exact table cell values and saved formula text."
                " When sharing a file, link to its original absolute path in Markdown, "
                "using angle brackets around paths with spaces."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1, "description": "Workspace file path"},
                    "page": {
                        "type": "integer", "minimum": 1, "maximum": 4000,
                        "description": "One-based PPTX slide, PDF/DOCX page, or HTML/Markdown viewport. Defaults to 1.",
                    },
                    "frame": {
                        "type": "integer", "minimum": 1, "maximum": 256,
                        "description": "One-based raster image frame. Defaults to 1.",
                    },
                    "sheet": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "description": "XLSX worksheet name. Defaults to the first worksheet.",
                    },
                    "range": {
                        "type": "string", "minLength": 1, "maxLength": 32,
                        "description": "A1 table range, e.g. B3:H14. At most 32 rows and 12 columns.",
                    },
                    "view": {
                        "type": "string", "enum": ["visual", "data"],
                        "description": "Defaults to visual. Data view returns XLSX/CSV/TSV values and formulas as JSON.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
            ),
        )]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name != TOOL_NAME:
            raise ValueError("Unknown artifact inspection tool.")
        async with limit:
            if before_inspect is not None:
                await before_inspect()
            cwd, identity = await asyncio.to_thread(workspace)
            return await inspect_artifact(
                cwd, arguments["path"], arguments.get("page", 1),
                frame=arguments.get("frame", 1), sheet=arguments.get("sheet"),
                cell_range=arguments.get("range"), view=arguments.get("view", "visual"),
                workspace_identity=identity,
            )

    return server


def make_artifact_server(
    cwd: str, *, before_inspect: Callable[[], Awaitable[object]] | None = None,
) -> Server:
    try:
        scope = _workspace(cwd)
    except (OSError, ValueError):
        scope = None

    def workspace() -> _Workspace:
        if scope is None:
            raise ValueError("The session workspace is unavailable.")
        return scope

    return _make_artifact_server(workspace, before_inspect=before_inspect)


async def _serve(scope: CodexArtifactScope) -> None:
    server = _make_artifact_server(lambda: _codex_workspace(scope))
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="Wrapper-owned Codex workspace registry")
    parser.add_argument("scope_key")
    options = parser.parse_args()
    asyncio.run(_serve((options.database, options.scope_key)))
