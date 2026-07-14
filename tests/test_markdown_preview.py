"""Bounded, cwd-confined Markdown preview regressions."""
from __future__ import annotations

import asyncio
import os

import pytest

from cc_remote.protocol import (
    FilePreview,
    GetFilePreview,
    GetPreviewAsset,
    FILE_PREVIEW_MAX_BYTES,
    PREVIEW_ASSET_MAX_BYTES,
    PreviewAsset,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def test_markdown_preview_reads_utf8_and_normalizes_paths(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    readme = docs / "README.md"
    readme.write_text("\ufeff# 标题\n\n正文", encoding="utf-8")
    machine, _ = _mk_machine()

    relative = machine._read_markdown_preview(str(tmp_path), "docs/README.md")
    absolute = machine._read_markdown_preview(str(tmp_path), str(readme))

    assert relative[0] == absolute[0] == "docs/README.md"
    assert relative[1] == absolute[1] == "# 标题\n\n正文"
    assert relative[2] == readme.stat().st_size
    assert relative[3] is False
    assert relative[4] == readme.stat().st_mtime_ns


def test_source_preview_reads_utf8_and_reports_text_format(tmp_path):
    source = tmp_path / "module.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    machine, _ = _mk_machine()

    result = machine._read_text_preview(str(tmp_path), "module.py")

    assert result[0] == "module.py"
    assert result[1] == "def answer():\n    return 42\n"
    assert result[5] == "text"


def test_source_preview_rejects_binary_content(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"text\0binary")
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="UTF-8 文本"):
        machine._read_text_preview(str(tmp_path), "binary.dat")


def test_markdown_preview_truncates_without_breaking_split_utf8(tmp_path):
    path = tmp_path / "large.md"
    path.write_bytes(b"a" * (FILE_PREVIEW_MAX_BYTES - 1) + "界".encode() + b"tail")
    machine, _ = _mk_machine()

    relative, content, size, truncated, _ = machine._read_markdown_preview(
        str(tmp_path), "large.md")

    assert relative == "large.md"
    assert content == "a" * (FILE_PREVIEW_MAX_BYTES - 1)
    assert size > FILE_PREVIEW_MAX_BYTES
    assert truncated is True


@pytest.mark.parametrize("path", ["../outside.md", "~/.secret.md"])
def test_markdown_preview_rejects_paths_outside_cwd(tmp_path, path):
    machine, _ = _mk_machine()
    with pytest.raises(ValueError, match="当前工作目录|超出"):
        machine._read_markdown_preview(str(tmp_path), path)


def test_markdown_preview_rejects_absolute_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="超出"):
        machine._read_markdown_preview(str(root), str(outside))
    with pytest.raises(ValueError, match="超出"):
        machine._read_markdown_preview(str(root), "escape.md")


def test_markdown_preview_rejects_special_files_without_blocking(tmp_path):
    fifo = tmp_path / "pipe.md"
    os.mkfifo(fifo)
    machine, _ = _mk_machine()

    async def run():
        with pytest.raises(ValueError, match="普通文件"):
            await asyncio.wait_for(asyncio.to_thread(
                machine._read_markdown_preview, str(tmp_path), "pipe.md"), 1)

    asyncio.run(run())


def test_markdown_preview_rejects_invalid_utf8(tmp_path):
    (tmp_path / "invalid.md").write_bytes(b"\xff\xfe")
    machine, _ = _mk_machine()

    with pytest.raises(UnicodeDecodeError, match="UTF-8"):
        machine._read_markdown_preview(str(tmp_path), "invalid.md")


def test_preview_asset_is_type_limited_and_bounded(tmp_path):
    (tmp_path / "image.png").write_bytes(b"png")
    (tmp_path / "vector.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "large.webp").write_bytes(b"x" * (PREVIEW_ASSET_MAX_BYTES + 1))
    machine, _ = _mk_machine()

    path, media_type, data = machine._read_preview_asset(
        str(tmp_path), "image.png")
    assert (path, media_type, data) == ("image.png", "image/png", b"png")
    with pytest.raises(ValueError, match="PNG"):
        machine._read_preview_asset(str(tmp_path), "vector.svg")
    with pytest.raises(ValueError, match="4 MiB"):
        machine._read_preview_asset(str(tmp_path), "large.webp")


def test_preview_responses_are_requester_routed_and_correlated(tmp_path):
    (tmp_path / "README.md").write_text("# hello", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"png")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(tmp_path)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        preview = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path="README.md",
            request_id="preview-1",
        ))
        asset = await machine._handle_get_preview_asset(GetPreviewAsset(
            sid=ctx.key,
            client_id="client-1",
            path="image.png",
            preview_id="preview-1",
            request_id="asset-1",
        ))

        assert isinstance(preview, FilePreview)
        assert preview.to == "client-1" and preview.sid == "session-1"
        assert preview.request_id == "preview-1" and preview.content == "# hello"
        assert preview.format == "markdown"
        assert isinstance(asset, PreviewAsset)
        assert asset.to == "client-1" and asset.sid == "session-1"
        assert asset.preview_id == "preview-1" and asset.request_id == "asset-1"
        assert transport.sent[-2:] == [preview, asset]

    asyncio.run(run())


def test_preview_unknown_sid_never_reads_focused_session(tmp_path):
    (tmp_path / "README.md").write_text("focused secret", encoding="utf-8")

    async def run():
        machine, transport = _mk_machine()
        focused = _mk_ctx("focused", session_id="focused")
        focused.cwd = str(tmp_path)
        machine.sessions[focused.key] = focused
        machine.focused_sid = focused.key

        response = await machine._handle_get_file_preview(GetFilePreview(
            sid="missing-session",
            client_id="client-1",
            path="README.md",
            request_id="preview-1",
        ))

        assert response.error and not response.content
        assert response.sid == "missing-session" and response.to == "client-1"
        assert transport.sent[-1] == response

    asyncio.run(run())


def test_preview_payload_is_not_retained_in_command_dedupe_cache(tmp_path):
    (tmp_path / "README.md").write_text("# hello", encoding="utf-8")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(tmp_path)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key
        command = GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            cmd_id="command-1",
            path="README.md",
            request_id="preview-1",
        )

        await machine._process_command(command)

        assert machine._processed_commands["client-1"]["command-1"] == ()
        assert [message.type for message in transport.sent[-2:]] == [
            "file_preview", "command_ack",
        ]

    asyncio.run(run())
