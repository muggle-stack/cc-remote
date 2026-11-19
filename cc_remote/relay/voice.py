"""Doubao Voice 2.0 transport. Audio is streamed and kept out of chat history."""
from __future__ import annotations

import base64
import gzip
import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

ASR_RATE = 16_000
SPEECH_RATE = 24_000
_ASR_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
_SPEECH_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
_SPEAKER = "zh_female_vv_uranus_bigtts"


class VoiceUpstreamError(RuntimeError):
    """An upstream failure without credentials, transcript or provider body."""


def _frame(kind: int, payload: bytes, *, final: bool = False) -> bytes:
    compressed = gzip.compress(payload)
    flags = 2 if final else 0
    serialization = 0x11 if kind == 1 else 0x01
    return bytes((0x11, kind << 4 | flags, serialization, 0)) + struct.pack(
        ">I", len(compressed)
    ) + compressed


def _decode_transcription(frame: str | bytes) -> tuple[bool, str | None]:
    if not isinstance(frame, bytes) or len(frame) < 8 or frame[0] >> 4 != 1:
        raise VoiceUpstreamError("invalid transcription frame")
    offset = (frame[0] & 15) * 4
    kind, flags = frame[1] >> 4, frame[1] & 15
    if offset < 4:
        raise VoiceUpstreamError("invalid transcription header")
    if flags & 1:
        offset += 4  # signed sequence number
    if kind == 15:
        raise VoiceUpstreamError("transcription rejected by provider")
    if kind != 9:
        return False, None
    if len(frame) < offset + 4:
        raise VoiceUpstreamError("invalid transcription response")
    size, = struct.unpack_from(">I", frame, offset)
    payload = frame[offset + 4:]
    if len(payload) != size:
        raise VoiceUpstreamError("truncated transcription response")
    try:
        if frame[2] & 15 == 1 and payload:
            payload = gzip.decompress(payload)
        body = json.loads(payload) if payload else {}
        result = body.get("result") or {}
        text = result.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("invalid provider text")
    except (AttributeError, EOFError, OSError, ValueError) as exc:
        raise VoiceUpstreamError("invalid transcription payload") from exc
    return bool(flags & 2), text


class DoubaoTranscription:
    def __init__(self, socket: ClientConnection):
        self._socket = socket

    async def send(self, audio: bytes, *, final: bool = False) -> None:
        await self._socket.send(_frame(2, audio, final=final))

    async def updates(self) -> AsyncIterator[tuple[bool, str | None]]:
        async for frame in self._socket:
            final, text = _decode_transcription(frame)
            yield final, text
            if final:
                return
        raise VoiceUpstreamError("transcription ended before completion")


class DoubaoVoice:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _headers(self, resource: str) -> dict[str, str]:
        return {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": resource,
            "X-Api-Request-Id": str(uuid4()),
        }

    @asynccontextmanager
    async def transcription(self) -> AsyncIterator[DoubaoTranscription]:
        headers = {**self._headers("volc.seedasr.sauc.duration"), "X-Api-Sequence": "-1"}
        request = {
            "user": {"uid": "cc-remote"},
            "audio": {"format": "pcm", "codec": "raw", "rate": ASR_RATE, "bits": 16, "channel": 1},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
        }
        try:
            async with connect(
                _ASR_URL, additional_headers=headers, open_timeout=8, close_timeout=1,
            ) as socket:
                await socket.send(_frame(1, json.dumps(request).encode()))
                yield DoubaoTranscription(socket)
        except (OSError, WebSocketException) as exc:
            raise VoiceUpstreamError("transcription connection failed") from exc

    async def speech(self, text: str, output_format: str) -> AsyncIterator[bytes]:
        request = {
            "user": {"uid": "cc-remote"},
            "req_params": {
                "text": text,
                "speaker": _SPEAKER,
                "audio_params": {"format": output_format, "sample_rate": SPEECH_RATE},
            },
        }
        received = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8)) as client:
                async with client.stream(
                    "POST", _SPEECH_URL, headers=self._headers("seed-tts-2.0"), json=request,
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        code = event.get("code")
                        if code not in (0, 20_000_000):
                            raise VoiceUpstreamError("speech rejected by provider")
                        if event.get("data"):
                            audio = base64.b64decode(event["data"], validate=True)
                            received += len(audio)
                            if audio:
                                yield audio
                        if code == 20_000_000:
                            if not received or (output_format == "pcm" and received % 2):
                                raise VoiceUpstreamError("invalid speech audio")
                            return
        except (httpx.HTTPError, AttributeError, ValueError, TypeError) as exc:
            raise VoiceUpstreamError("speech stream failed") from exc
        raise VoiceUpstreamError("speech ended before completion")
