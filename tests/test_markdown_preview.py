"""Bounded, cwd-confined Markdown preview regressions."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import stat
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import cc_remote.wrapper.machine as machine_module
import cc_remote.wrapper.preview_capabilities as preview_capabilities
from cc_remote.protocol import (
    AuthorizePreview,
    FileSaveResult,
    FilePreview,
    GetFilePreview,
    GetPreviewAsset,
    FILE_PREVIEW_MAX_BYTES,
    PREVIEW_ASSET_MAX_BYTES,
    ProcessEvent,
    PreviewAuthorizationRequired,
    PreviewAuthorizationResult,
    PreviewAsset,
    SaveMarkdown,
    ToolResult,
    ToolUse,
)
from cc_remote.wrapper.git_diff import read_git_diff
from cc_remote.wrapper.preview_capabilities import PreviewCapabilityStore
from tests.test_multisession import _mk_ctx, _mk_machine


def test_markdown_preview_reads_utf8_and_normalizes_paths(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    readme = docs / "README.md"
    readme.write_text("\ufeff# 标题\n\n正文", encoding="utf-8", newline="")
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
    source.write_text(
        "def answer():\n    return 42\n", encoding="utf-8", newline="")
    machine, _ = _mk_machine()

    result = machine._read_text_preview(str(tmp_path), "module.py")

    assert result[0] == "module.py"
    assert result[1] == "def answer():\n    return 42\n"
    assert result[5] == "text"
    assert result[6] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_markdown_save_is_atomic_and_preserves_bom_crlf_and_mode(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"\xef\xbb\xbf# old\r\nline\r\n")
    path.chmod(0o640)
    before = path.stat()
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    machine, _ = _mk_machine()

    result = machine._write_markdown_file(
        str(tmp_path), "README.md", "# new\nline\n",
        before.st_size, before.st_mtime_ns, revision,
    )

    assert result[0] == "README.md"
    assert path.read_bytes() == b"\xef\xbb\xbf# new\r\nline\r\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert result[1] == path.stat().st_size
    assert result[2] == path.stat().st_mtime_ns
    assert result[3] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_markdown_save_conflict_never_overwrites_newer_content(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("old", encoding="utf-8")
    before = path.stat()
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("newer external content", encoding="utf-8")
    machine, _ = _mk_machine()

    with pytest.raises(RuntimeError, match="修改"):
        machine._write_markdown_file(
            str(tmp_path), "README.md", "editor draft",
            before.st_size, before.st_mtime_ns, revision,
        )

    assert path.read_text(encoding="utf-8") == "newer external content"


def test_markdown_save_rejects_non_markdown_and_symlink(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    source = root / "note.txt"
    source.write_text("text", encoding="utf-8")
    machine, _ = _mk_machine()
    source_stat = source.stat()

    with pytest.raises(ValueError, match="Markdown"):
        machine._write_markdown_file(
            str(root), "note.txt", "draft", source_stat.st_size,
            source_stat.st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())

    link = root / "link.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise
    link_stat = outside.stat()
    with pytest.raises(ValueError, match="符号链接"):
        machine._write_markdown_file(
            str(root), "link.md", "draft", link_stat.st_size,
            link_stat.st_mtime_ns, hashlib.sha256(outside.read_bytes()).hexdigest())


def test_source_preview_rejects_binary_content(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"text\0binary")
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="UTF-8 文本"):
        machine._read_text_preview(str(tmp_path), "binary.dat")


def test_rendered_artifacts_read_html_images_and_pdf_without_persistence(tmp_path):
    (tmp_path / "page.html").write_text(
        "<h1>Report</h1><script>window.bad = true</script>", encoding="utf-8")
    svg_bytes = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<rect width="10" height="10"/></svg>'
    )
    (tmp_path / "diagram.svg").write_bytes(svg_bytes)
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\npreview")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.7\npreview")
    machine, _ = _mk_machine()

    html = machine._read_file_preview(str(tmp_path), "page.html")
    svg = machine._read_file_preview(str(tmp_path), "diagram.svg")
    image = machine._read_file_preview(str(tmp_path), "image.png")
    pdf = machine._read_file_preview(str(tmp_path), "report.pdf")

    assert html["format"] == "html" and "<h1>Report</h1>" in html["content"]
    assert svg["format"] == "image"
    assert svg["media_type"] == "image/svg+xml"
    assert svg["data"] == svg_bytes
    assert image["format"] == "image" and image["media_type"] == "image/png"
    assert image["data"] == b"\x89PNG\r\n\x1a\npreview"
    assert pdf["format"] == "pdf" and pdf["media_type"] == "application/pdf"
    assert pdf["data"] == b"%PDF-1.7\npreview"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "diagram.svg", "image.png", "page.html", "report.pdf",
    ]


def test_rendered_artifact_rejects_mismatched_content_type(tmp_path):
    (tmp_path / "fake.png").write_text("<script>alert(1)</script>", encoding="utf-8")
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="格式不匹配"):
        machine._read_file_preview(str(tmp_path), "fake.png")


def test_office_preview_converts_inside_ephemeral_sandbox(tmp_path, monkeypatch):
    source = tmp_path / "deck.pptx"
    source.write_bytes(b"office-source")
    machine, _ = _mk_machine()

    monkeypatch.setattr("cc_remote.wrapper.machine.shutil.which",
                        lambda name: f"/usr/bin/{name}")

    def fake_convert(cls, command):
        assert "--unshare-all" in command
        assert "--unshare-net" not in command
        assert "--clearenv" in command
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--setenv") + 1:][:2] == [
            "PATH", "/usr/bin:/bin",
        ]
        for variable in (
            "HOME", "TMPDIR", "LANG", "LC_ALL",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
        ):
            assert variable in command
        mount = Path(command[command.index("--bind") + 1])
        (mount / "out" / "input.pdf").write_bytes(b"%PDF-1.7\nconverted")

    monkeypatch.setattr(
        type(machine), "_run_office_conversion", classmethod(fake_convert))

    preview = machine._read_file_preview(str(tmp_path), "deck.pptx")

    assert preview["format"] == "pdf"
    assert preview["converted_from"] == "pptx"
    assert preview["data"] == b"%PDF-1.7\nconverted"
    assert preview["size"] == len(b"office-source")
    assert list(tmp_path.iterdir()) == [source]


def test_office_converter_process_receives_only_minimal_environment(monkeypatch):
    captured = {}

    class Process:
        pid = 123

        @staticmethod
        def wait(timeout=None):
            return 0

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("WRAPPER_TOKEN", "relay-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "model-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setattr(machine_module.subprocess, "Popen", popen)

    machine_module.WrapperMachine._run_office_conversion(
        ["/usr/bin/bwrap", "--", "/usr/bin/true"])

    assert captured["env"] == {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    assert not {
        "WRAPPER_TOKEN", "ANTHROPIC_API_KEY", "HTTPS_PROXY",
    }.intersection(captured["env"])


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
    with pytest.raises(ValueError, match="当前工作目录|确认"):
        machine._read_markdown_preview(str(tmp_path), path)


def test_markdown_preview_rejects_absolute_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="确认"):
        machine._read_markdown_preview(str(root), str(outside))

    try:
        (root / "escape.md").symlink_to(outside)
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise
    with pytest.raises(ValueError, match="超出"):
        machine._read_markdown_preview(str(root), "escape.md")


def test_preview_rejects_a_resolved_path_too_large_for_the_wire():
    root = "/" + "/".join(["abcdefghij"] * 270)
    relative = "../" + "b" * 1500 + ".md"
    machine, _ = _mk_machine()

    with pytest.raises(ValueError, match="路径过长"):
        machine._read_text_preview(root, relative)


def test_capability_rejects_a_canonical_path_that_expands_past_the_bound(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# artifact", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    oversized = "/" + "x" * preview_capabilities.PREVIEW_PATH_MAX_BYTES
    monkeypatch.setattr(
        preview_capabilities.os.path,
        "realpath",
        lambda _path: oversized,
    )

    with pytest.raises(
        preview_capabilities.PreviewCapabilityError,
        match="路径过长",
    ):
        store.grant_path(
            "claude",
            "code",
            "session-1",
            str(artifact),
            mode="read",
            source="user_approved",
        )


def test_successful_write_grants_exact_cross_cwd_preview_and_edit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# created", encoding="utf-8")
    neighbor = tmp_path / "neighbor.md"
    neighbor.write_text("secret", encoding="utf-8")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        await machine._observe_preview_path_event(ctx, ToolUse(
            message_id="message-1",
            tool_use_id="write-1",
            tool="Write",
            input={"file_path": str(outside), "content": "# created"},
        ))
        await machine._observe_preview_path_event(ctx, ToolResult(
            tool_use_id="write-1",
            content=f"File created successfully at: {outside}",
            is_error=False,
            status="succeeded",
        ))

        preview = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-external",
        ))
        assert preview.error is None
        assert preview.path == str(outside.resolve())
        assert preview.content == "# created"

        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(neighbor),
            request_id="preview-neighbor",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)
        assert required.resolved_path == str(neighbor.resolve())

        saved = await machine._handle_save_markdown(SaveMarkdown(
            sid=ctx.key,
            client_id="client-1",
            path=preview.path,
            request_id="save-external",
            content="# edited",
            expected_size=preview.size,
            expected_mtime_ns=preview.mtime_ns,
            expected_revision=preview.revision,
        ))
        assert saved.status == "saved"
        assert saved.path == str(outside.resolve())
        assert outside.read_text(encoding="utf-8") == "# edited"
        refreshed = machine._preview_capabilities(ctx, require_write=True)
        assert refreshed[str(outside.resolve())].matches(outside.stat())

    asyncio.run(run())


def test_failed_write_never_grants_cross_cwd_preview(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session-1", session_id="session-1")
    ctx.cwd = str(root)

    asyncio.run(machine._observe_preview_path_event(ctx, ToolUse(
        message_id="message-1",
        tool_use_id="write-1",
        tool="Write",
        input={"file_path": str(outside), "content": "not written"},
    )))
    asyncio.run(machine._observe_preview_path_event(ctx, ToolResult(
        tool_use_id="write-1",
        content="permission denied",
        is_error=True,
        status="failed",
    )))

    with pytest.raises(ValueError, match="确认"):
        machine._read_markdown_preview(
            str(root), str(outside),
        machine._preview_capabilities(ctx),
    )


def test_history_write_replay_never_rebinds_a_replaced_external_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# original", encoding="utf-8")
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session-1", session_id="session-1")
    ctx.cwd = str(root)
    machine.sessions[ctx.key] = ctx

    asyncio.run(machine._observe_preview_path_event(ctx, ToolUse(
        message_id="message-live",
        tool_use_id="write-live",
        tool="Write",
        input={"file_path": str(outside), "content": "# original"},
    )))
    asyncio.run(machine._observe_preview_path_event(ctx, ToolResult(
        tool_use_id="write-live",
        content="written",
        is_error=False,
        status="succeeded",
    )))
    original_capability = machine._preview_capabilities(ctx)[
        str(outside.resolve())
    ]

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# unrelated replacement", encoding="utf-8")
    os.replace(replacement, outside)
    assert not original_capability.matches(outside.stat())

    historical_use = ToolUse(
        message_id="message-history",
        tool_use_id="write-history",
        tool="Write",
        input={"file_path": str(outside), "content": "# old transcript"},
    )
    assert machine._normalize_preview_write_event(historical_use) == (
        str(outside),
    )
    assert historical_use.input["file_paths"] == [str(outside)]
    rebound = machine._preview_capabilities(ctx)[str(outside.resolve())]
    assert rebound.device == original_capability.device
    assert rebound.inode == original_capability.inode
    assert not rebound.matches(outside.stat())

    async def require_confirmation():
        result = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-replaced",
        ))
        assert isinstance(result, PreviewAuthorizationRequired)
        assert machine._preview_capabilities(ctx) == {}

    asyncio.run(require_confirmation())


def test_unknown_external_preview_requires_explicit_read_only_authorization(
        tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "generated.md"
    outside.write_text("# generated", encoding="utf-8")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-external",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)
        assert required.to == "client-1"
        assert required.sid == "session-1"
        assert required.request_id == "preview-external"
        assert required.path == str(outside)
        assert required.resolved_path == str(outside.resolve())
        assert required.operation == "file_preview"
        assert required.preview_id is None
        assert transport.sent[-1] == required
        assert all(not isinstance(item, FilePreview) for item in transport.sent)
        assert required.seq is None
        assert ctx.buffer.tail_seq == 0

        granted = await machine._handle_authorize_preview(AuthorizePreview(
            sid=ctx.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert isinstance(granted, PreviewAuthorizationResult)
        assert granted.status == "granted"
        assert granted.to == "client-1"
        assert granted.request_id == "preview-external"

        preview = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-external",
        ))
        assert isinstance(preview, FilePreview)
        assert preview.content == "# generated"
        assert preview.writable is False

        before = outside.stat()
        denied_save = await machine._handle_save_markdown(SaveMarkdown(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="save-external",
            content="# must not write",
            expected_size=before.st_size,
            expected_mtime_ns=str(before.st_mtime_ns),
            expected_revision=hashlib.sha256(outside.read_bytes()).hexdigest(),
        ))
        assert denied_save.status == "error"
        assert denied_save.error and "编辑" in denied_save.error
        assert outside.read_text(encoding="utf-8") == "# generated"

    asyncio.run(run())


def test_preview_authorization_is_client_session_and_identity_bound(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "generated.md"
    outside.write_text("# first", encoding="utf-8")

    async def run():
        machine, _ = _mk_machine()
        first = _mk_ctx("session-1", session_id="session-1")
        first.cwd = str(root)
        second = _mk_ctx("session-2", session_id="session-2")
        second.cwd = str(root)
        machine.sessions[first.key] = first
        machine.sessions[second.key] = second

        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=first.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-1",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)

        wrong_client = await machine._handle_authorize_preview(AuthorizePreview(
            sid=first.key,
            client_id="client-2",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert isinstance(wrong_client, PreviewAuthorizationResult)
        assert wrong_client.status == "expired"
        assert wrong_client.to == "client-2"

        wrong_session = await machine._handle_authorize_preview(AuthorizePreview(
            sid=second.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert isinstance(wrong_session, PreviewAuthorizationResult)
        assert wrong_session.status == "expired"
        assert wrong_session.sid == "session-2"

        granted = await machine._handle_authorize_preview(AuthorizePreview(
            sid=first.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert granted.status == "granted"

        replacement = tmp_path / "replacement.md"
        replacement.write_text("# replaced", encoding="utf-8")
        os.replace(replacement, outside)

        changed = await machine._handle_get_file_preview(GetFilePreview(
            sid=first.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-2",
        ))
        assert isinstance(changed, PreviewAuthorizationRequired)
        assert changed.authorization_id != required.authorization_id

        other_session = await machine._handle_get_file_preview(GetFilePreview(
            sid=second.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-other",
        ))
        assert isinstance(other_session, PreviewAuthorizationRequired)

    asyncio.run(run())


def test_preview_authorization_expires_and_deny_never_inspects_file(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "generated.md"
    outside.write_text("# generated", encoding="utf-8")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        store = machine._preview_capability_store
        original_inspect = store.inspect_path

        def unexpected_inspection(*args, **kwargs):
            raise AssertionError("challenge creation must not inspect the file")

        monkeypatch.setattr(store, "inspect_path", unexpected_inspection)
        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-denied",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)
        denied = await machine._handle_authorize_preview(AuthorizePreview(
            sid=ctx.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="deny",
        ))
        assert denied.status == "denied"

        monkeypatch.setattr(store, "inspect_path", original_inspect)
        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-expired",
        ))
        challenge = machine._preview_challenges[required.authorization_id]
        machine._preview_challenges[required.authorization_id] = replace(
            challenge,
            created_at=(
                challenge.created_at
                - machine.PREVIEW_AUTHORIZATION_TTL
                - 1
            ),
        )
        expired = await machine._handle_authorize_preview(AuthorizePreview(
            sid=ctx.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert expired.status == "expired"
        assert machine._preview_capabilities(ctx) == {}

    asyncio.run(run())


def test_preview_authorization_ttl_uses_a_monotonic_clock(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "generated.md"
    outside.write_text("# generated", encoding="utf-8")
    wall = [1_000.0]
    monotonic = [100.0]
    monkeypatch.setattr(machine_module.time, "time", lambda: wall[0])
    monkeypatch.setattr(
        machine_module.time,
        "monotonic",
        lambda: monotonic[0],
    )

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-monotonic",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)

        wall[0] = -1_000.0
        monotonic[0] += machine.PREVIEW_AUTHORIZATION_TTL + 1
        expired = await machine._handle_authorize_preview(AuthorizePreview(
            sid=ctx.key,
            client_id="client-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        ))
        assert expired.status == "expired"
        assert machine._preview_capabilities(ctx) == {}

    asyncio.run(run())


def test_authorize_preview_duplicate_replays_granted_result(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "generated.md"
    outside.write_text("# generated", encoding="utf-8")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            request_id="preview-1",
        ))
        command = AuthorizePreview(
            sid=ctx.key,
            client_id="client-1",
            cmd_id="authorize-command-1",
            authorization_id=required.authorization_id,
            request_id=required.request_id,
            decision="allow",
        )

        await machine._process_command(command)
        await machine._process_command(command)

        results = [
            message for message in transport.sent
            if isinstance(message, PreviewAuthorizationResult)
        ]
        assert [result.status for result in results] == [
            "granted", "granted",
        ]

    asyncio.run(run())


def test_preview_capability_store_persists_exact_identity_and_mode(tmp_path):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# one", encoding="utf-8")

    store = PreviewCapabilityStore(state)
    granted = store.grant_path(
        "claude", "code", "session-1", str(artifact),
        mode="read", source="user_approved",
    )
    assert granted.path == str(artifact.resolve())
    assert granted.mode == "read"

    restored = PreviewCapabilityStore(state)
    read_caps = restored.snapshot(
        "claude", "code", "session-1", require_write=False)
    write_caps = restored.snapshot(
        "claude", "code", "session-1", require_write=True)
    assert read_caps[str(artifact.resolve())].matches(artifact.stat())
    assert write_caps == {}

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# two", encoding="utf-8")
    os.replace(replacement, artifact)
    assert not read_caps[str(artifact.resolve())].matches(artifact.stat())

    restored.rekey("claude", "code", "session-1", "session-real")
    assert restored.snapshot("claude", "code", "session-1") == {}
    assert str(artifact.resolve()) in restored.snapshot(
        "claude", "code", "session-real")
    restored.remove_session("claude", "session-real")
    assert restored.snapshot("claude", "code", "session-real") == {}


def test_ephemeral_preview_capability_rekey_stays_memory_only(tmp_path):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# one", encoding="utf-8")

    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "btw-temp", str(artifact),
        mode="read_write", source="structured_write", persist=False,
    )
    store.rekey(
        "claude", "code", "btw-temp", "btw-real", persist=False)

    assert str(artifact.resolve()) in store.snapshot(
        "claude", "code", "btw-real")
    assert PreviewCapabilityStore(state).snapshot(
        "claude", "code", "btw-real") == {}
    assert not (state / ".preview-capabilities.invalidated").exists()


def test_failed_preview_revoke_stays_revoked_after_restart(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(first),
        mode="read", source="user_approved")
    store.grant_path(
        "codex", "code", "session-2", str(second),
        mode="read", source="user_approved")

    def broken_connect():
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_connect", broken_connect)
    store.revoke("claude", "code", "session-1", str(first))

    assert store.snapshot("claude", "code", "session-1") == {}
    assert store.snapshot("codex", "code", "session-2") == {}
    assert (state / ".preview-capabilities.invalidated").exists()

    recovered = PreviewCapabilityStore(state)
    assert recovered.snapshot("claude", "code", "session-1") == {}
    assert recovered.snapshot("codex", "code", "session-2") == {}
    assert not (state / ".preview-capabilities.invalidated").exists()


def test_preview_marker_cleanup_failure_keeps_restart_fail_closed(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(artifact),
        mode="read", source="user_approved")

    monkeypatch.setattr(
        store,
        "_clear_invalidation_locked",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(OSError("directory fsync failed")),
    )
    store.remove_session("claude", "session-1")

    assert (state / ".preview-capabilities.invalidated").exists()
    recovered = PreviewCapabilityStore(state)
    assert recovered.snapshot("claude", "code", "session-1") == {}
    assert not (state / ".preview-capabilities.invalidated").exists()


def test_legacy_preview_invalidation_marker_recovers_fail_closed(tmp_path):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(artifact),
        mode="read", source="user_approved")
    marker = state / ".preview-capabilities.invalidated"
    marker.write_text("preview capabilities invalidated\n", encoding="ascii")
    marker.chmod(0o600)

    recovered = PreviewCapabilityStore(state)

    assert recovered.snapshot("claude", "code", "session-1") == {}
    assert not marker.exists()


def test_destructive_retry_recovers_an_existing_invalidation_marker(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(first),
        mode="read", source="user_approved")
    store.grant_path(
        "codex", "code", "session-2", str(second),
        mode="read", source="user_approved")
    original_connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: (_ for _ in ()).throw(OSError("temporary write failure")),
    )
    store.revoke("claude", "code", "session-1", str(first))
    assert (state / ".preview-capabilities.invalidated").exists()

    monkeypatch.setattr(store, "_connect", original_connect)
    store.rekey("claude", "code", "session-1", "session-3")

    assert store._persistent is True
    assert not (state / ".preview-capabilities.invalidated").exists()
    restored = PreviewCapabilityStore(state)
    assert restored.snapshot("claude", "code", "session-1") == {}
    assert restored.snapshot("claude", "code", "session-3") == {}
    assert restored.snapshot("codex", "code", "session-2") == {}


def test_preview_epoch_invalidates_an_overlapping_store_process(tmp_path):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact", encoding="utf-8")
    first = PreviewCapabilityStore(state)
    first.grant_path(
        "claude", "code", "session-1", str(artifact),
        mode="read", source="user_approved")
    overlapping = PreviewCapabilityStore(state)
    assert str(artifact.resolve()) in overlapping.snapshot(
        "claude", "code", "session-1")

    first.revoke("claude", "code", "session-1", str(artifact))

    assert overlapping.snapshot("claude", "code", "session-1") == {}


def test_concurrent_preview_revokes_preserve_unrelated_grants(tmp_path):
    state = tmp_path / "state"
    artifacts = [tmp_path / f"artifact-{index}.md" for index in range(3)]
    for index, artifact in enumerate(artifacts):
        artifact.write_text(str(index), encoding="utf-8")
    first = PreviewCapabilityStore(state)
    for index, artifact in enumerate(artifacts):
        first.grant_path(
            "claude", "code", f"session-{index}", str(artifact),
            mode="read", source="user_approved")
    second = PreviewCapabilityStore(state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                store.revoke,
                "claude", "code", f"session-{index}", str(artifacts[index]),
            )
            for index, store in enumerate((first, second))
        ]
        for future in futures:
            future.result(timeout=3)

    restored = PreviewCapabilityStore(state)
    assert restored.snapshot("claude", "code", "session-0") == {}
    assert restored.snapshot("claude", "code", "session-1") == {}
    assert str(artifacts[2].resolve()) in restored.snapshot(
        "claude", "code", "session-2")


def test_preview_lock_refuses_symlink_without_chmodding_target(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target"
    target.write_text("do not touch", encoding="utf-8")
    target.chmod(0o640)
    before = target.stat().st_mode & 0o777
    (state / ".preview-capabilities.lock").symlink_to(target)

    store = PreviewCapabilityStore(state)

    assert store._persistent is False
    assert target.stat().st_mode & 0o777 == before


def test_preview_lock_wait_is_bounded(tmp_path, monkeypatch):
    store = PreviewCapabilityStore(tmp_path / "state")
    clock = iter((0.0, 0.0, 6.0))

    def busy_lock(_descriptor, operation):
        if operation & preview_capabilities.fcntl.LOCK_NB:
            raise BlockingIOError

    monkeypatch.setattr(
        preview_capabilities.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        preview_capabilities.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(preview_capabilities.fcntl, "flock", busy_lock)

    with pytest.raises(TimeoutError, match="acquisition timed out"):
        with store._exclusive_lock():
            raise AssertionError("busy lock must not be entered")


def test_preview_revoke_lock_timeout_leaves_restart_deny_marker(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("artifact", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(artifact),
        mode="read", source="user_approved")
    original_lock = store._exclusive_lock

    def lock_timeout():
        raise TimeoutError("preview capability lock acquisition timed out")

    monkeypatch.setattr(store, "_exclusive_lock", lock_timeout)
    store.revoke("claude", "code", "session-1", str(artifact))

    marker = state / ".preview-capabilities.invalidated"
    assert marker.exists()
    assert store.snapshot("claude", "code", "session-1") == {}

    monkeypatch.setattr(store, "_exclusive_lock", original_lock)
    recovered = PreviewCapabilityStore(state)
    assert recovered.snapshot("claude", "code", "session-1") == {}
    assert not marker.exists()


def test_preview_grant_storage_wait_does_not_block_event_loop(
    tmp_path, monkeypatch,
):
    async def run():
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        started = threading.Event()
        release = threading.Event()
        original = machine._preview_capability_store.grant_path

        def blocked_grant(*args, **kwargs):
            started.set()
            assert release.wait(timeout=2)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            machine._preview_capability_store, "grant_path", blocked_grant)
        await machine._observe_preview_path_event(ctx, ToolUse(
            message_id="message-1",
            tool_use_id="write-1",
            tool="Write",
            input={"file_path": str(outside), "content": "outside"},
        ))
        task = asyncio.create_task(machine._observe_preview_path_event(
            ctx,
            ToolResult(
                tool_use_id="write-1",
                content="written",
                is_error=False,
                status="succeeded",
            ),
        ))
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())


def test_preview_epoch_rejects_oversized_state_file(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / ".preview-capabilities.epoch").write_bytes(b"a" * 65)

    store = PreviewCapabilityStore(state)

    assert store._persistent is False
    assert store._entries == {}


def test_revoke_recovers_after_an_earlier_grant_persistence_failure(
    tmp_path, monkeypatch,
):
    state = tmp_path / "state"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "session-1", str(first),
        mode="read", source="user_approved")
    original_connect = store._connect
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: (_ for _ in ()).throw(OSError("temporary write failure")),
    )
    store.grant_path(
        "claude", "code", "session-1", str(second),
        mode="read", source="user_approved")
    assert store._persistent is False

    monkeypatch.setattr(store, "_connect", original_connect)
    store.revoke("claude", "code", "session-1", str(first))

    assert store._persistent is True
    restored = PreviewCapabilityStore(state)
    assert str(first.resolve()) not in restored.snapshot(
        "claude", "code", "session-1")


def test_preview_capability_store_bounds_survive_rekey_and_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(preview_capabilities, "_SESSION_CAP", 2)
    monkeypatch.setattr(preview_capabilities, "_GLOBAL_CAP", 4)
    state = tmp_path / "state"
    files = []
    for index in range(4):
        artifact = tmp_path / f"artifact-{index}.md"
        artifact.write_text(f"# {index}", encoding="utf-8")
        files.append(artifact)

    store = PreviewCapabilityStore(state)
    for artifact in files[:2]:
        store.grant_path(
            "claude", "code", "temp-session", str(artifact),
            mode="read", source="user_approved",
        )
    for artifact in files[2:]:
        store.grant_path(
            "claude", "code", "real-session", str(artifact),
            mode="read_write", source="structured_write",
        )

    store.rekey(
        "claude", "code", "temp-session", "real-session")
    assert store.snapshot("claude", "code", "temp-session") == {}
    assert len(store.snapshot("claude", "code", "real-session")) == 2

    restored = PreviewCapabilityStore(state)
    assert restored.snapshot("claude", "code", "temp-session") == {}
    assert len(restored.snapshot(
        "claude", "code", "real-session")) == 2


def test_preview_capability_mode_never_crosses_a_changed_file_identity(tmp_path):
    state = tmp_path / "state"
    artifact = tmp_path / "artifact.md"
    artifact.write_text("# original", encoding="utf-8")
    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "real-session", str(artifact),
        mode="read_write", source="structured_write",
    )

    replacement = tmp_path / "replacement.md"
    replacement.write_text("# replacement", encoding="utf-8")
    os.replace(replacement, artifact)
    current = store.grant_path(
        "claude", "code", "temp-session", str(artifact),
        mode="read", source="user_approved",
    )
    store.rekey(
        "claude", "code", "temp-session", "real-session")

    merged = store.snapshot(
        "claude", "code", "real-session")[str(artifact.resolve())]
    assert merged.mode == "read"
    assert merged.matches(artifact.stat())
    assert merged.device == current.device and merged.inode == current.inode

    # A same-sid re-grant after replacement also cannot inherit the previous
    # inode's write capability.
    second = tmp_path / "second.md"
    second.write_text("# second", encoding="utf-8")
    os.replace(second, artifact)
    refreshed = store.grant_path(
        "claude", "code", "real-session", str(artifact),
        mode="read", source="user_approved",
    )
    assert refreshed.mode == "read"


def test_session_cleanup_deletes_durable_capability_evicted_from_memory(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(preview_capabilities, "_SESSION_CAP", 1)
    monkeypatch.setattr(preview_capabilities, "_GLOBAL_CAP", 1)
    state = tmp_path / "state"
    durable = tmp_path / "durable.md"
    ephemeral = tmp_path / "ephemeral.md"
    durable.write_text("# durable", encoding="utf-8")
    ephemeral.write_text("# ephemeral", encoding="utf-8")

    store = PreviewCapabilityStore(state)
    store.grant_path(
        "claude", "code", "durable-session", str(durable),
        mode="read", source="user_approved",
    )
    store.grant_path(
        "claude", "code", "btw-session", str(ephemeral),
        mode="read", source="user_approved", persist=False,
    )
    assert store.snapshot("claude", "code", "durable-session") == {}

    store.remove_session("claude", "durable-session")
    restored = PreviewCapabilityStore(state)
    assert restored.snapshot("claude", "code", "durable-session") == {}


def test_machine_preview_rekey_and_delete_migrate_then_clear_state(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# outside", encoding="utf-8")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("temp-session", session_id=None)
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine._preview_capability_store.grant_path(
            "claude", "code", ctx.key, str(outside),
            mode="read", source="user_approved",
        )

        # A second external path leaves a live requester-bound challenge.
        second = tmp_path / "second.md"
        second.write_text("# second", encoding="utf-8")
        required = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path=str(second),
            request_id="preview-2",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)

        await machine._rekey_preview_session(ctx, ctx.key, "real-session")
        assert machine._preview_capabilities(ctx) == {}
        ctx.session_id = "real-session"
        assert str(outside.resolve()) in machine._preview_capabilities(ctx)
        assert machine._preview_challenges[
            required.authorization_id
        ].session_key == "real-session"

        # Resident-pool eviction is not session deletion. Recreating the
        # runtime for the same durable sid must retain its exact capabilities.
        machine.sessions.pop(ctx.key)
        resumed = _mk_ctx("real-session", session_id="real-session")
        resumed.cwd = str(root)
        machine.sessions[resumed.key] = resumed
        assert str(outside.resolve()) in machine._preview_capabilities(resumed)

        await machine._drop_preview_session("claude", "real-session")
        assert machine._preview_capabilities(resumed) == {}
        assert required.authorization_id not in machine._preview_challenges

    asyncio.run(run())


def test_successful_codex_patch_grants_multiple_exact_cross_cwd_paths(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    first = tmp_path / "one.txt"
    second = tmp_path / "two.md"
    first.write_text("1\n", encoding="utf-8", newline="")
    second.write_text("# two\n", encoding="utf-8", newline="")
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session-1", session_id="session-1")
    ctx.cwd = str(root)
    use = ToolUse(
        message_id="message-1",
        tool_use_id="patch-1",
        tool="apply_patch",
        category="file",
        input={"changes": [
            {"path": str(first), "kind": "add"},
            {"path": str(second), "kind": "add"},
        ]},
    )

    asyncio.run(machine._observe_preview_path_event(ctx, use))
    assert use.input["file_paths"] == [str(first), str(second)]
    assert ctx.preview_write_candidates["patch-1"] == (
        str(first), str(second))
    asyncio.run(machine._observe_preview_path_event(ctx, ToolResult(
        tool_use_id="patch-1",
        content="ok",
        is_error=False,
        status="succeeded",
    )))

    allowed = machine._preview_capabilities(ctx)
    assert set(allowed) == {str(first.resolve()), str(second.resolve())}
    assert machine._read_text_preview(
        str(root), str(first), allowed)[1] == "1\n"
    assert machine._read_markdown_preview(
        str(root), str(second), allowed)[1] == "# two\n"
    diff = asyncio.run(machine._git_diff(str(root), str(first), allowed))
    assert "--- /dev/null" in diff
    assert "+1" in diff
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("changed\n", encoding="utf-8")
    os.replace(replacement, first)
    with pytest.raises(ValueError, match="no longer matches"):
        asyncio.run(machine._git_diff(str(root), str(first), allowed))
    with pytest.raises(ValueError, match="outside the session repository"):
        asyncio.run(machine._git_diff(
            str(root), str(tmp_path / "not-authorized.txt"), allowed))


def test_external_diff_never_reopens_capability_checked_path(
    tmp_path, monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL_SNAPSHOT\n", encoding="utf-8")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("SECRET_AFTER_CHECK\n", encoding="utf-8")
    capability = PreviewCapabilityStore(tmp_path / "state").grant_path(
        "codex",
        "code",
        "session-1",
        str(outside),
        mode="read_write",
        source="structured_write",
    )
    outside_path = str(outside.resolve())
    command_calls: list[tuple[str, ...]] = []
    original_lstat = os.lstat
    original_open = os.open
    replaced = False
    target_lstat_calls = 0

    def replace_path_once() -> None:
        nonlocal replaced
        if replaced:
            return
        os.replace(replacement, outside)
        replaced = True

    def racing_lstat(path, *args, **kwargs):
        nonlocal target_lstat_calls
        file_stat = original_lstat(path, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == outside_path:
            target_lstat_calls += 1
            # First call belongs to realpath(). The vulnerable implementation's
            # second call is its capability check immediately before Git.
            if target_lstat_calls >= 2:
                replace_path_once()
        return file_stat

    def racing_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.path.abspath(os.fspath(path)) == outside_path:
            replace_path_once()
        return descriptor

    # The old implementation races after lstat; the fixed one races after
    # open. Only an already-open descriptor keeps ORIGINAL_SNAPSHOT in both
    # cases without letting the later diff step reopen the replaced path.
    monkeypatch.setattr(os, "lstat", racing_lstat)
    monkeypatch.setattr(os, "open", racing_open)

    async def replacing_runner(argv: tuple[str, ...], _max_bytes: int) -> str:
        command_calls.append(argv)
        if "rev-parse" in argv:
            return f"{root}\n"
        return (
            f"diff --git a/{outside.name} b/{outside.name}\n"
            "--- /dev/null\n"
            f"+++ b/{outside.name}\n"
            "@@ -0,0 +1 @@\n"
            f"+{outside.read_text(encoding='utf-8')}"
        )

    diff = asyncio.run(read_git_diff(
        str(root),
        str(outside),
        allowed_external_paths={outside_path: capability},
        max_bytes=64 * 1024,
        source_max_bytes=64 * 1024,
        run_command=replacing_runner,
    ))

    assert "ORIGINAL_SNAPSHOT" in diff
    assert "SECRET_AFTER_CHECK" not in diff
    assert len(command_calls) == 1


def test_external_binary_diff_never_decodes_capability_snapshot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"\0PRIVATE_BINARY_PAYLOAD")
    capability = PreviewCapabilityStore(tmp_path / "state").grant_path(
        "claude",
        "code",
        "session-1",
        str(outside),
        mode="read",
        source="user_approved",
    )

    async def root_only_runner(argv: tuple[str, ...], _max_bytes: int) -> str:
        if "rev-parse" in argv:
            return f"{root}\n"
        raise AssertionError("external diff must not reopen the source")

    diff = asyncio.run(read_git_diff(
        str(root),
        str(outside),
        allowed_external_paths={str(outside.resolve()): capability},
        max_bytes=64 * 1024,
        source_max_bytes=64 * 1024,
        run_command=root_only_runner,
    ))

    assert "Binary files /dev/null" in diff
    assert "PRIVATE_BINARY_PAYLOAD" not in diff


def test_markdown_preview_rejects_special_files_without_blocking(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows has no FIFO special files")
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
    vector = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4">'
        b'<circle cx="2" cy="2" r="2"/></svg>'
    )
    (tmp_path / "vector.svg").write_bytes(vector)
    (tmp_path / "invalid.svg").write_text("<html/>", encoding="utf-8")
    (tmp_path / "large.webp").write_bytes(b"x" * (PREVIEW_ASSET_MAX_BYTES + 1))
    machine, _ = _mk_machine()

    path, media_type, data = machine._read_preview_asset(
        str(tmp_path), "image.png")
    assert (path, media_type, data) == ("image.png", "image/png", b"png")
    assert machine._read_preview_asset(str(tmp_path), "vector.svg") == (
        "vector.svg", "image/svg+xml", vector,
    )
    with pytest.raises(ValueError, match="SVG"):
        machine._read_preview_asset(str(tmp_path), "invalid.svg")
    with pytest.raises(ValueError, match="4 MiB"):
        machine._read_preview_asset(str(tmp_path), "large.webp")


@pytest.mark.parametrize("tool", ["Read", "view_image"])
def test_successful_external_image_read_serves_an_immutable_snapshot(
    tmp_path, tool,
):
    root = tmp_path / "root"
    root.mkdir()
    original = b"\x89PNG\r\n\x1a\noriginal"
    replacement = b"\x89PNG\r\n\x1a\nreplacement"
    outside = tmp_path / "outside.png"
    outside.write_bytes(original)
    neighbor = tmp_path / "neighbor.png"
    neighbor.write_bytes(b"\x89PNG\r\n\x1a\nneighbor")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        await machine._emit(ctx, ToolUse(
            message_id="message-1",
            tool_use_id="image-read-1",
            tool=tool,
            input={"file_path": str(outside)}
            if tool == "Read" else {"path": str(outside)},
        ))
        await machine._emit(ctx, ToolResult(
            tool_use_id="image-read-1",
            content="image loaded",
            is_error=False,
            status="succeeded",
        ))

        # The browser must receive the exact successful-read snapshot, not
        # whatever later replaced the reusable /tmp-style path.
        outside.write_bytes(replacement)
        preview = await machine._handle_get_preview_asset(GetPreviewAsset(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            preview_id="preview-1",
            request_id="asset-1",
        ))
        required = await machine._handle_get_preview_asset(GetPreviewAsset(
            sid=ctx.key,
            client_id="client-1",
            path=str(neighbor),
            preview_id="preview-1",
            request_id="asset-2",
        ))
        assert preview.error is None
        assert preview.media_type == "image/png"
        assert preview.data == "iVBORw0KGgpvcmlnaW5hbA=="
        assert isinstance(required, PreviewAuthorizationRequired)
        assert required.resolved_path == str(neighbor.resolve())
        assert machine._preview_capabilities(ctx) == {}

    asyncio.run(run())


def test_failed_external_image_read_never_grants_a_snapshot(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nsecret")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        await machine._emit(ctx, ToolUse(
            message_id="message-1",
            tool_use_id="image-read-1",
            tool="Read",
            input={"file_path": str(outside)},
        ))
        await machine._emit(ctx, ToolResult(
            tool_use_id="image-read-1",
            content="permission denied",
            is_error=True,
            status="failed",
        ))
        required = await machine._handle_get_preview_asset(GetPreviewAsset(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            preview_id="preview-1",
            request_id="asset-1",
        ))
        assert isinstance(required, PreviewAuthorizationRequired)
        assert required.resolved_path == str(outside.resolve())
        assert machine._preview_capabilities(ctx) == {}

    asyncio.run(run())


def test_codex_image_view_process_uses_its_item_snapshot_not_reused_path(
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    first = b"\x89PNG\r\n\x1a\nfirst"
    second = b"\x89PNG\r\n\x1a\nsecond"
    outside.write_bytes(first)

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        await machine._emit(ctx, ProcessEvent(
            item_id="image-view-1",
            kind="server_tool",
            phase="start",
            status="running",
            turn_id="turn-1",
            title="查看图片",
            tool="view_image",
            input={"file_path": str(outside)},
        ))
        assert "preview_id" not in transport.sent[-1].input

        await machine._emit(ctx, ProcessEvent(
            item_id="image-view-1",
            kind="server_tool",
            phase="end",
            status="succeeded",
            turn_id="turn-1",
            title="查看图片",
            tool="view_image",
            input={"file_path": str(outside)},
        ))

        assert transport.sent[-1].input["preview_id"] == "image-view-1"
        outside.write_bytes(second)
        preview = await machine._handle_get_preview_asset(GetPreviewAsset(
            sid=ctx.key,
            client_id="client-1",
            path=str(outside),
            preview_id="image-view-1",
            request_id="asset-1",
        ))
        assert preview.error is None
        assert preview.data == "iVBORw0KGgpmaXJzdA=="

    asyncio.run(run())


def test_failed_codex_image_view_never_advertises_a_preview(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nfailed")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx

        for phase, status in (("start", "running"), ("end", "failed")):
            await machine._emit(ctx, ProcessEvent(
                item_id="image-view-failed",
                kind="server_tool",
                phase=phase,
                status=status,
                turn_id="turn-1",
                title="查看图片",
                tool="view_image",
                input={"file_path": str(outside)},
            ))
            assert "preview_id" not in transport.sent[-1].input

        assert (
            ctx.preview_snapshot_token,
            "image-view-failed",
        ) not in machine._preview_image_snapshots

    asyncio.run(run())


def test_external_image_snapshots_are_entry_bounded_and_session_purge_isolated():
    machine, _ = _mk_machine()
    machine.PREVIEW_IMAGE_SNAPSHOT_SESSION_ENTRIES = 2
    machine.PREVIEW_IMAGE_SNAPSHOT_GLOBAL_ENTRIES = 3

    machine._store_preview_image_snapshot("session-a", "/tmp/a.png", "image/png", b"")
    machine._store_preview_image_snapshot("session-a", "/tmp/b.png", "image/png", b"")
    machine._store_preview_image_snapshot("session-a", "/tmp/c.png", "image/png", b"")
    assert list(machine._preview_image_snapshots) == [
        ("session-a", "/tmp/b.png"),
        ("session-a", "/tmp/c.png"),
    ]

    machine._store_preview_image_snapshot("session-b", "/tmp/d.png", "image/png", b"")
    machine._store_preview_image_snapshot("session-c", "/tmp/e.png", "image/png", b"")
    assert list(machine._preview_image_snapshots) == [
        ("session-a", "/tmp/c.png"),
        ("session-b", "/tmp/d.png"),
        ("session-c", "/tmp/e.png"),
    ]

    machine._purge_preview_image_snapshots("session-b")
    assert list(machine._preview_image_snapshots) == [
        ("session-a", "/tmp/c.png"),
        ("session-c", "/tmp/e.png"),
    ]
    assert machine._preview_image_snapshot_bytes == 0


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
        assert preview.revision == hashlib.sha256(b"# hello").hexdigest()
        assert isinstance(asset, PreviewAsset)
        assert asset.to == "client-1" and asset.sid == "session-1"
        assert asset.preview_id == "preview-1" and asset.request_id == "asset-1"
        assert transport.sent[-2:] == [preview, asset]

    asyncio.run(run())


def test_rendered_preview_response_is_requester_routed(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\npreview")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(tmp_path)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        preview = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key,
            client_id="client-1",
            path="image.png",
            request_id="preview-image",
        ))

        assert isinstance(preview, FilePreview)
        assert preview.to == "client-1" and preview.sid == "session-1"
        assert preview.format == "image" and preview.media_type == "image/png"
        assert preview.data == "iVBORw0KGgpwcmV2aWV3"
        assert transport.sent[-1] == preview

    asyncio.run(run())


def test_markdown_save_response_is_correlated_and_duplicate_is_not_reexecuted(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("# old", encoding="utf-8")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        ctx.cwd = str(tmp_path)
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key
        before = path.stat()
        command = SaveMarkdown(
            sid=ctx.key,
            client_id="client-1",
            cmd_id="save-command-1",
            path="README.md",
            request_id="save-1",
            content="# saved",
            expected_size=before.st_size,
            expected_mtime_ns=str(before.st_mtime_ns),
            expected_revision=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

        await machine._process_command(command)
        saved_stat = path.stat()
        await machine._process_command(command)

        results = [message for message in transport.sent
                   if isinstance(message, FileSaveResult)]
        assert len(results) == 2
        assert all(result.status == "saved" for result in results)
        assert all(result.to == "client-1" for result in results)
        assert path.read_text(encoding="utf-8") == "# saved"
        assert path.stat().st_mtime_ns == saved_stat.st_mtime_ns
        assert [message.type for message in transport.sent[-2:]] == [
            "file_save_result", "command_ack",
        ]

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
