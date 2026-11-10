"""Zero-model tests for attachment validation limits."""
from __future__ import annotations

import base64
import asyncio
import binascii
import os
import re
import struct
import time
import zlib
from pathlib import Path

from cc_remote.attachments import (
    MAX_ATTACHMENT_COUNT,
    MAX_SINGLE_ATTACHMENT_BYTES,
    validate_attachments,
)
from cc_remote.protocol import ERR_BAD_PROMPT, Query, Steer, TurnSteered
from tests.test_multisession import _mk_ctx, _mk_machine


def _b64(size: int) -> str:
    return base64.b64encode(b"x" * size).decode()


def _png(width: int = 1, height: int = 1, extra: bytes = b"") -> str:
    header = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(
        ">II", width, height)
    return base64.b64encode(header + extra).decode()


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", checksum))


def _complete_png(*extra_chunks: tuple[bytes, bytes]) -> str:
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + b"".join(_png_chunk(kind, payload) for kind, payload in extra_chunks)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(data).decode()


def test_valid_small_image_and_file():
    assert validate_attachments(
        [{"media_type": "image/png", "data": _png()}],
        [{"filename": "note.txt", "data": _b64(64)}],
    ) is None


def test_rejects_invalid_base64_type_count_and_size():
    assert "base64" in validate_attachments(
        [], [{"filename": "x", "data": "%%%"}])
    assert "unsupported" in validate_attachments(
        [{"media_type": "image/svg+xml", "data": _b64(2)}], [])
    assert "too many" in validate_attachments(
        [], [{"filename": str(i), "data": _b64(1)}
             for i in range(MAX_ATTACHMENT_COUNT + 1)])
    assert "6 MiB" in validate_attachments(
        [], [{"filename": "big", "data": _b64(MAX_SINGLE_ATTACHMENT_BYTES + 1)}])


def test_rejects_total_size_and_bad_filename():
    four_mib = 4 * 1024 * 1024
    assert "8 MiB" in validate_attachments([], [
        {"filename": "a", "data": _b64(four_mib)},
        {"filename": "b", "data": _b64(four_mib)},
        {"filename": "c", "data": _b64(1)},
    ])
    assert "filename" in validate_attachments(
        [], [{"filename": "", "data": _b64(1)}])


def test_rejects_mime_mismatch_pixel_bomb_and_utf8_filename_overflow():
    assert "declared type" in validate_attachments(
        [{"media_type": "image/png", "data": _b64(32)}], [])
    assert "dimensions" in validate_attachments(
        [{"media_type": "image/png", "data": _png(8192, 8192)}], [])
    assert "filename" in validate_attachments(
        [], [{"filename": "界" * 100, "data": _b64(1)}])


def test_png_animation_detection_uses_chunk_boundaries():
    static_with_text = _complete_png(
        (b"tEXt", b"Comment\x00a static image may mention acTL"),
    )
    animated = _complete_png((b"acTL", struct.pack(">II", 1, 0)))

    assert validate_attachments(
        [{"media_type": "image/png", "data": static_with_text}], []) is None
    assert "declared type" in validate_attachments(
        [{"media_type": "image/png", "data": animated}], [])


def test_invalid_query_attachment_is_rejected_before_turn_claim():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        machine.sessions["sid-1"] = ctx
        machine.focused_sid = "sid-1"

        await machine._handle_query(Query(
            sid="sid-1", prompt="read it", msg_id="msg-1",
            files=[{"filename": "bad.txt", "data": "%%%"}],
        ))

        assert ctx.state == "idle" and ctx.turn_task is None
        assert transport.sent[-1].type == "error"
        assert transport.sent[-1].code == ERR_BAD_PROMPT
        assert transport.sent[-1].message == "附件不符合要求，请调整后重试。"
        assert "base64" not in transport.sent[-1].message

    asyncio.run(run())


def test_stashed_files_are_private_unique_and_path_safe(tmp_path):
    machine, _ = _mk_machine()
    turn_dir = tmp_path / "turn"
    turn_dir.mkdir(mode=0o700)
    prompt = machine._stash_files("inspect", [
        {"filename": "../same.txt", "data": _b64(3)},
        {"filename": "same.txt", "data": _b64(4)},
    ], str(turn_dir))

    paths = re.findall(r"@([^\s]+)", prompt)
    assert len(paths) == 2 and len(set(paths)) == 2
    assert all(os.path.dirname(path) == str(turn_dir) for path in paths)
    assert [open(path, "rb").read() for path in paths] == [b"x" * 3, b"x" * 4]
    assert all((os.stat(path).st_mode & 0o777) == 0o600 for path in paths)


def test_turn_temp_directory_is_removed_when_query_fails():
    class FailingSdk:
        effort = "high"
        applied_effort = "high"

        def __init__(self):
            self.path = None

        async def query(self, prompt):
            match = re.search(r"@([^\s]+)", prompt)
            assert match is not None
            self.path = match.group(1)
            assert os.path.isfile(self.path)
            assert self.ctx.transient_turn_attachment_dir == os.path.dirname(self.path)
            raise RuntimeError("synthetic failure")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        sdk = FailingSdk()
        sdk.ctx = ctx
        ctx.sdk = sdk
        ctx.state = "running"

        await machine._run_turn(
            ctx, "inspect", files=[{"filename": "note.txt", "data": _b64(8)}]
        )

        assert ctx.state == "idle"
        assert sdk.path is not None
        assert not os.path.exists(os.path.dirname(sdk.path))
        assert ctx.transient_turn_attachment_dir is None

    asyncio.run(run())


def test_work_turn_persists_files_and_images_inside_private_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    class FailingSdk:
        effort = "high"
        applied_effort = "high"

        async def query(self, messages):
            payloads = [item async for item in messages]
            text = payloads[0]["message"]["content"][0]["text"]
            assert f"@{workspace}/uploads/work-message/00-note.txt" in text
            assert (workspace / "uploads" / "work-message" /
                    "image-00.png").is_file()
            raise RuntimeError("synthetic failure")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("tmp-work", None)
        ctx.cwd = str(workspace)
        ctx.space = "work"
        ctx.sdk = FailingSdk()
        ctx.state = "running"
        ctx.active_msg_id = "work-message"

        await machine._run_turn(
            ctx,
            "inspect both",
            images=[{"media_type": "image/png", "data": _png()}],
            files=[{"filename": "note.txt", "data": _b64(8)}],
        )

        upload_dir = workspace / "uploads" / "work-message"
        assert (upload_dir / "00-note.txt").read_bytes() == b"x" * 8
        assert (upload_dir / "image-00.png").read_bytes().startswith(b"\x89PNG")
        assert upload_dir.stat().st_mode & 0o777 == 0o700
        assert not (workspace.parent / "uploads").exists()

    asyncio.run(run())


def test_claude_code_image_reaches_native_sdk_without_media_interception():
    image_data = _png()

    class FailingSdk:
        effort = "high"
        applied_effort = "high"

        async def query(self, messages):
            [payload] = [item async for item in messages]
            content = payload["message"]["content"]
            assert content == [
                {"type": "text", "text": "inspect image"},
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_data,
                }},
            ]
            raise RuntimeError("synthetic failure after native query boundary")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("tmp-code-image", None)
        ctx.engine = "claude"
        ctx.state = "running"
        ctx.active_msg_id = "image-message"
        ctx.sdk = FailingSdk()

        await machine._run_turn(
            ctx,
            "inspect image",
            images=[{"media_type": "image/png", "data": image_data}],
        )

    asyncio.run(run())


def test_history_strips_legacy_and_workspace_work_attachment_paths():
    machine, _ = _mk_machine()
    roots = (
        "/Users/test/.codex/cc-remote/work/chats/work-a1b2/uploads/message-1",
        "/Users/test/.codex/cc-remote/work/chats/work-a1b2/"
        "workspace/uploads/message-1",
    )

    for root in roots:
        codex_prompt = (
            "inspect\n\n[用户附件,请用工具读取以下文件]:\n"
            f"{root}/00-report.md"
        )
        assert machine._strip_attachment_paths(codex_prompt) == (
            "inspect", [{"filename": "report.md"}],
        )
        claude_prompt = f"inspect\n\n@{root}/00-report.md"
        assert machine._strip_attachment_paths(claude_prompt) == (
            "inspect", [{"filename": "report.md"}],
        )


def test_work_attachment_write_stays_bound_after_directory_symlink_swap(
        monkeypatch, tmp_path):
    machine, _ = _mk_machine()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    relocated = workspace / "relocated-message"
    workspace.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    original = machine._stash_files

    def swap_then_stash(
        prompt, files, temp_dir, engine="claude", *, dir_fd=None,
    ):
        Path(temp_dir).rename(relocated)
        Path(temp_dir).symlink_to(outside, target_is_directory=True)
        return original(
            prompt, files, temp_dir, engine, dir_fd=dir_fd)

    monkeypatch.setattr(machine, "_stash_files", swap_then_stash)

    machine._stash_work_attachments(
        "inspect",
        [{"filename": "note.txt", "data": _b64(8)}],
        None,
        str(workspace),
        "message-1",
        "claude",
    )

    assert not (outside / "00-note.txt").exists()
    assert (relocated / "00-note.txt").read_bytes() == b"x" * 8


def test_work_upload_refuses_symlinked_or_unmanaged_root(tmp_path):
    machine, _ = _mk_machine()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    uploads = workspace / "uploads"
    uploads.symlink_to(outside, target_is_directory=True)

    for setup in ("symlink", "unmanaged"):
        try:
            machine._stash_work_attachments(
                "inspect",
                [{"filename": "note.txt", "data": _b64(8)}],
                None,
                str(workspace),
                f"message-{setup}",
                "claude",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe Work upload root was accepted")
        if setup == "symlink":
            uploads.unlink()
            uploads.mkdir(mode=0o700)
            (uploads / "user-file.txt").write_text(
                "must remain user-owned", encoding="utf-8")

    assert (uploads / "user-file.txt").read_text(encoding="utf-8") == (
        "must remain user-owned")
    assert not list(outside.iterdir())


def test_rejected_work_steer_keeps_persistent_uploads(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)

    class RejectingSdk:
        turn_id = "turn-1"
        turn_active = True

        async def steer(self, *_args, **_kwargs):
            raise RuntimeError("synthetic rejection")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("work-session", "work-session")
        ctx.cwd = str(workspace)
        ctx.space = "work"
        ctx.engine = "codex"
        ctx.sdk = RejectingSdk()
        ctx.state = "running"
        machine.sessions[ctx.key] = ctx

        command = Steer(
            sid=ctx.key,
            cmd_id="steer-command",
            client_id="client-1",
            msg_id="steer-message",
            prompt="inspect this",
            files=[{"filename": "note.txt", "data": _b64(8)}],
        )
        await machine._handle_steer(command)

        upload = workspace / "uploads" / "steer-message" / "00-note.txt"
        assert upload.read_bytes() == b"x" * 8

    asyncio.run(run())


def test_startup_cleanup_only_removes_owned_attachment_names(monkeypatch, tmp_path):
    machine, _ = _mk_machine()
    turn = tmp_path / "cc-remote-turn-AbC123"
    unrelated = tmp_path / "cc-remote-fix-test.keep"
    legacy = tmp_path / "cc-remote-1700000000-note.txt"
    turn.mkdir()
    unrelated.mkdir()
    legacy.write_bytes(b"old")
    old = time.time() - 2 * 24 * 3600
    for path in (turn, unrelated, legacy):
        os.utime(path, (old, old))
    monkeypatch.setattr("cc_remote.wrapper.machine.tempfile.gettempdir", lambda: str(tmp_path))

    machine._cleanup_tmp()

    assert not turn.exists()
    assert not legacy.exists()
    assert unrelated.is_dir()


def test_accepted_codex_steer_attachments_live_until_native_turn_terminal(
        monkeypatch, tmp_path):
    class Sdk:
        turn_id = "native-turn"
        turn_active = True

        def __init__(self):
            self.file_path = None
            self.image_path = None

        async def steer(
            self, prompt, images=None, *, client_user_message_id=None,
        ):
            assert client_user_message_id == "steer-message"
            self.file_path = prompt.rsplit("\n", 1)[-1]
            self.image_path = images[0]
            assert os.path.isfile(self.file_path)
            assert os.path.isfile(self.image_path)
            return self.turn_id

    async def run():
        machine, _transport = _mk_machine()
        created = tmp_path / "cc-remote-turn-lifetime"

        def make_temp_dir(*, prefix):
            assert prefix == "cc-remote-turn-"
            created.mkdir(mode=0o700)
            return str(created)

        monkeypatch.setattr(
            "cc_remote.wrapper.machine.tempfile.mkdtemp", make_temp_dir)
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.engine = "codex"
        ctx.state = "running"
        sdk = Sdk()
        ctx.sdk = sdk
        machine.sessions[ctx.key] = ctx

        event = await machine._handle_steer(Steer(
            sid=ctx.key,
            cmd_id="steer-command",
            client_id="client-1",
            prompt="inspect both",
            msg_id="steer-message",
            images=[{"media_type": "image/png", "data": _png()}],
            files=[{"filename": "note.txt", "data": _b64(8)}],
        ))

        assert isinstance(event, TurnSteered)
        assert event.files == [{"filename": "note.txt"}]
        assert event.images and event.images[0]["data"] == _png()
        assert created.is_dir()
        assert os.path.isfile(sdk.file_path)
        assert os.path.isfile(sdk.image_path)
        assert ctx.codex_steer_attachment_dirs == [str(created)]

        await machine._set_idle_after_managed_turn(ctx)

        assert not created.exists()
        assert ctx.codex_steer_attachment_dirs == []
        assert ctx.state == "idle"

    asyncio.run(run())


def test_rejected_codex_steer_removes_staged_attachments_immediately(
        monkeypatch, tmp_path):
    class Sdk:
        turn_id = "native-turn"
        turn_active = True

        async def steer(self, prompt, images=None, **_kwargs):
            assert os.path.isfile(prompt.rsplit("\n", 1)[-1])
            assert os.path.isfile(images[0])
            raise RuntimeError("synthetic steer rejection")

    async def run():
        machine, _transport = _mk_machine()
        created = tmp_path / "cc-remote-turn-rejected"

        def make_temp_dir(*, prefix):
            assert prefix == "cc-remote-turn-"
            created.mkdir(mode=0o700)
            return str(created)

        monkeypatch.setattr(
            "cc_remote.wrapper.machine.tempfile.mkdtemp", make_temp_dir)
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.sdk = Sdk()
        machine.sessions[ctx.key] = ctx

        result = await machine._handle_steer(Steer(
            sid=ctx.key,
            cmd_id="steer-command",
            client_id="client-1",
            prompt="inspect both",
            msg_id="steer-message",
            images=[{"media_type": "image/png", "data": _png()}],
            files=[{"filename": "note.txt", "data": _b64(8)}],
        ))

        assert result.type == "error"
        assert not created.exists()
        assert ctx.codex_steer_attachment_dirs == []

    asyncio.run(run())
