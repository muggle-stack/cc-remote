"""Zero-model tests for attachment validation limits."""
from __future__ import annotations

import base64
import asyncio
import binascii
import os
import re
import struct
import sys
import time
import zlib

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
    if sys.platform != "win32":
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
            raise RuntimeError("synthetic failure")

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        sdk = FailingSdk()
        ctx.sdk = sdk
        ctx.state = "running"

        await machine._run_turn(
            ctx, "inspect", files=[{"filename": "note.txt", "data": _b64(8)}]
        )

        assert ctx.state == "idle"
        assert sdk.path is not None
        assert not os.path.exists(os.path.dirname(sdk.path))

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
