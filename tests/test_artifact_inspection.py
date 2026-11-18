from __future__ import annotations

import base64
import json
from io import BytesIO
import os
from pathlib import Path
from zipfile import ZipFile

import pytest
from claude_agent_sdk._internal.sdk_mcp_bridge import SdkMcpBridge
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from cc_remote.wrapper import artifact_inspection as preview
from cc_remote.wrapper.codex_stream import _legacy_mcp_result, _mcp_result_content


def png_bytes() -> bytes:
    pixels = BytesIO()
    Image.new("RGB", (48, 24), "blue").save(pixels, format="PNG")
    return pixels.getvalue()


async def call_file_tool(bridge, **arguments):
    return (await bridge.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": preview.TOOL_NAME, "arguments": arguments},
    }))["result"]


@pytest.mark.asyncio
async def test_claude_bridge_returns_image_and_keeps_its_workspace(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    original = png_bytes()
    (root / "view.png").write_bytes(original)
    other = tmp_path / "outside.png"
    other.write_bytes(original)
    (root / "escape.png").symlink_to(other)
    (root / "values.tsv").write_text("包名\t版本\ndsh\t0.123456789012345\n")
    bridge = SdkMcpBridge(preview.SERVER_NAME, preview.make_artifact_server(str(root)))

    try:
        await bridge.handle({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "artifact-test", "version": "1"},
            },
        })
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = await call_file_tool(bridge, path="view.png")
        assert [(item["type"], item.get("mimeType")) for item in response["content"]] == [
            ("text", None), ("image", "image/png"),
        ]
        assert base64.b64decode(response["content"][1]["data"]) == original
        table = await call_file_tool(bridge, path="values.tsv", range="B2", view="data")
        assert json.loads(table["content"][0]["text"])["cells"] == {"B2": {"value": "0.123456789012345"}}
        assert (await call_file_tool(bridge, path="escape.png"))["isError"]

        # A tool stays tied to the original workspace even if its path is replaced.
        moved = root.rename(tmp_path / "old-workspace")
        root.mkdir()
        (root / "view.png").write_bytes(original)
        assert (await call_file_tool(bridge, path="view.png"))["isError"]
        assert (moved / "view.png").read_bytes() == original
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_codex_tool_retargets_the_running_mcp_server_after_migration(tmp_path: Path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    for directory in (old, new):
        directory.mkdir()
        (directory / "view.png").write_bytes(png_bytes())

    scope = preview.prepare_codex_scope(tmp_path, "primary", None)
    preview.bind_codex_scope(scope, "native-thread", str(old))
    config = preview.codex_mcp_config(scope)
    params = StdioServerParameters(command=config["command"], args=config["args"], cwd=config["cwd"])
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            assert not (await client.call_tool(preview.TOOL_NAME, {"path": str(old / "view.png")})).isError

            # A new Wrapper handle and the already-loaded native server share one scope.
            resumed = preview.prepare_codex_scope(tmp_path, "primary", "native-thread")
            preview.bind_codex_scope(resumed, "native-thread", str(new))
            assert not (await client.call_tool(preview.TOOL_NAME, {"path": str(new / "view.png")})).isError
            assert (await client.call_tool(preview.TOOL_NAME, {"path": str(old / "view.png")})).isError


@pytest.mark.asyncio
async def test_pptx_selects_requested_slide_and_checks_source(tmp_path: Path, monkeypatch):
    calls = []

    async def render(data, kind, page):
        calls.append((data, kind, page))
        return png_bytes(), {"pageCount": 5, "partial": True}

    monkeypatch.setattr(preview, "_render_document", render)
    pptx = tmp_path / "slides.pptx"
    with ZipFile(pptx, "w") as zipped:
        zipped.writestr("[Content_Types].xml", "<Types/>")
        zipped.writestr("ppt/presentation.xml", "<presentation/>")

    result = await preview.inspect_artifact(os.fspath(tmp_path), pptx.name, page=3)
    assert calls == [(pptx.read_bytes(), "pptx", 3)]
    assert "slide 3 of 5 (some elements" in result[0].text

    pptx.write_bytes(b"not a pptx")
    with pytest.raises(ValueError, match="PPTX"):
        await preview.inspect_artifact(os.fspath(tmp_path), pptx.name)
    assert len(calls) == 1


def test_codex_artifact_replay_omits_image_bodies():
    content = {"content": [
        {"type": "text", "text": "Viewed slide 2"},
        {"type": "image", "data": "image contents", "mimeType": "image/png"},
    ]}
    assert _mcp_result_content({
        "server": preview.SERVER_NAME, "tool": preview.TOOL_NAME, "result": content,
    }) == "Viewed slide 2"
    assert _legacy_mcp_result({"Ok": content}, artifact_inspection=True) == (
        "Viewed slide 2", False,
    )


@pytest.mark.asyncio
async def test_animation_selects_a_single_frame(tmp_path):
    path = tmp_path / "animation.gif"
    Image.new("RGB", (16, 10), "red").save(
        path, save_all=True, append_images=[Image.new("RGB", (16, 10), "blue")],
    )
    result = await preview.inspect_artifact(str(tmp_path), path.name, frame=2)
    assert "frame 2 of 2" in result[0].text
    with Image.open(BytesIO(base64.b64decode(result[1].data))) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (0, 0, 255)
    with pytest.raises(ValueError, match="out of range"):
        await preview.inspect_artifact(str(tmp_path), path.name, frame=3)


@pytest.mark.asyncio
async def test_word_validation_is_applied_before_headless_render(tmp_path, monkeypatch):
    path = tmp_path / "doc.docx"
    with ZipFile(path, "w") as zipped:
        zipped.writestr("[Content_Types].xml", "<Types/>")
        zipped.writestr("word/document.xml", "<document/>")

    async def render(data, kind, page):
        assert kind == "docx" and page == 2
        with ZipFile(BytesIO(data)) as zipped:
            assert zipped.read("word/document.xml") == b"<document/>"
        return png_bytes(), {"pageCount": 3}

    monkeypatch.setattr(preview, "_render_document", render)
    result = await preview.inspect_artifact(str(tmp_path), path.name, page=2)
    assert "page 2 of 3 (browser layout)" in result[0].text
    path.write_bytes(b"not a document")
    with pytest.raises(ValueError, match="DOCX"):
        await preview.inspect_artifact(str(tmp_path), path.name, page=2)
