"""Audio artifacts keep their original bytes and the existing preview boundary."""
from __future__ import annotations

import asyncio
import base64
import io
import wave

import pytest

from cc_remote.audio_preview import AUDIO_PREVIEW_MEDIA_TYPES
from cc_remote.protocol import (
    ARTIFACT_PREVIEW_MAX_BYTES, FilePreview, GetFilePreview,
    PreviewAuthorizationRequired, deserialize, is_downstream, serialize,
)
from cc_remote.wrapper.machine import WrapperMachine
from tests.test_multisession import _mk_ctx, _mk_machine


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0\0\0" * 480)
    return output.getvalue()


CONTAINERS = {
    "audio/wav": wav_bytes(),
    "audio/mpeg": b"\xff\xfb\x90\x00" + b"\0" * 32,
    "audio/mp4": b"\0\0\0\x18ftypM4A \0\0\0\0M4A isom",
    "audio/aac": b"\xff\xf1\x50\x80\x01\x7f\xfc\0",
    "audio/flac": b"fLaC" + b"\0" * 38,
    "audio/ogg": b"OggS\0" + b"\0" * 22,
    "audio/webm": b"\x1a\x45\xdf\xa3" + b"\0" * 16,
}


@pytest.mark.parametrize("suffix,media_type", AUDIO_PREVIEW_MEDIA_TYPES.items())
def test_audio_preview_identifies_container_and_preserves_bytes(tmp_path, suffix, media_type):
    path = tmp_path / ("sample" + suffix.upper())
    path.write_bytes(CONTAINERS[media_type])
    result = WrapperMachine._read_file_preview(str(tmp_path), path.name)
    assert result["format"] == "audio"
    assert result["media_type"] == media_type
    assert result["data"] == CONTAINERS[media_type]
    assert result["size"] == path.stat().st_size
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("suffix", AUDIO_PREVIEW_MEDIA_TYPES)
def test_audio_preview_rejects_non_audio_bytes(tmp_path, suffix):
    (tmp_path / ("fake" + suffix)).write_bytes(b"<html>not audio</html>")
    with pytest.raises(ValueError, match="音频格式不匹配"):
        WrapperMachine._read_file_preview(str(tmp_path), "fake" + suffix)


def test_audio_preview_rejects_video_riff_and_does_not_truncate(tmp_path):
    path = tmp_path / "video.wav"
    path.write_bytes(b"RIFF\0\0\0\0AVI ")
    with pytest.raises(ValueError, match="音频格式不匹配"):
        WrapperMachine._read_file_preview(str(tmp_path), path.name)
    with path.open("wb") as stream:
        stream.write(wav_bytes())
        stream.truncate(ARTIFACT_PREVIEW_MAX_BYTES + 1)
    with pytest.raises(ValueError, match="8 MiB"):
        WrapperMachine._read_file_preview(str(tmp_path), path.name)


def test_work_audio_preview_retains_external_file_authorization_for_symlink_escapes(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "private.wav"
    outside.write_bytes(wav_bytes())
    (root / "link.wav").symlink_to(outside)
    with pytest.raises(ValueError, match="外部文件需要确认"):
        WrapperMachine._read_file_preview(str(root), "link.wav")

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-a", session_id="session-a")
        ctx.space = "work"
        ctx.cwd = str(root)
        machine.sessions[ctx.key] = ctx
        response = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key, client_id="client-a", request_id="audio-request",
            path=str(outside),
        ))
        assert isinstance(response, PreviewAuthorizationRequired)
        assert response.format == "audio" and response.to == "client-a"
        assert not any(isinstance(event, FilePreview) for event in transport.sent)

    asyncio.run(run())


def test_audio_preview_is_private_one_shot_and_protocol_roundtrips(tmp_path):
    data = wav_bytes()
    (tmp_path / "sample.wav").write_bytes(data)

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-a", session_id="session-a")
        ctx.cwd = str(tmp_path)
        machine.sessions[ctx.key] = ctx
        response = await machine._handle_get_file_preview(GetFilePreview(
            sid=ctx.key, client_id="client-a", request_id="audio-request",
            path="sample.wav",
        ))
        assert isinstance(response, FilePreview) and response.error is None
        assert response.to == "client-a" and response.sid == ctx.key
        assert response.request_id == "audio-request"
        assert response.format == "audio" and response.media_type == "audio/wav"
        assert base64.b64decode(response.data) == data
        assert not is_downstream(response)
        assert deserialize(serialize(response)) == response
        assert transport.sent[-1] == response

    asyncio.run(run())
