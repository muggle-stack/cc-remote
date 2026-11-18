"""Authenticated speech and single-recording Realtime transcription routes."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from cc_remote.relay.voice import DoubaoTranscription, DoubaoVoice, SPEECH_RATE, VoiceUpstreamError

_HEADERS = {"Cache-Control": "no-store"}
_SESSION = {
    "type": "transcription",
    "audio": {"input": {
        "format": {"type": "audio/pcm", "rate": SPEECH_RATE},
        "transcription": {"model": "gpt-live-transcribe"},
        "turn_detection": None,
    }},
}
_TRANSCRIPT = "conversation.item.input_audio_transcription"
_MAX_AUDIO_FRAME = 96_000
_MAX_RECORDING_SECONDS = 300


class VoiceInputError(ValueError):
    pass


def _error(message: str, status: int) -> JSONResponse:
    kind = "invalid_request_error" if status < 500 else "server_error"
    return JSONResponse({"error": {"message": message, "type": kind}}, status, headers=_HEADERS)


class _SpeechResponse(Response):
    """Own upstream and concurrency slot through disconnect and body completion."""

    def __init__(self, voice: DoubaoVoice, text: str, output: str, slots: asyncio.Semaphore):
        super().__init__()
        self.voice = voice
        self.text = text
        self.output = output
        self.slots = slots

    async def __call__(self, scope, receive, send):
        if self.slots.locked():
            await _error("语音服务繁忙，请稍后再试。", 429)(scope, receive, send)
            return
        async with self.slots, aclosing(self.voice.speech(self.text, self.output)) as chunks:
            try:
                first = await anext(chunks)
            except (VoiceUpstreamError, StopAsyncIteration):
                await _error("语音合成服务暂不可用。", 502)(scope, receive, send)
                return

            async def body():
                yield first
                async for chunk in chunks:
                    yield chunk

            media = "audio/pcm" if self.output == "pcm" else "audio/mpeg"
            await StreamingResponse(body(), media_type=media, headers=_HEADERS)(scope, receive, send)


async def _message(socket: WebSocket) -> dict:
    msg = await socket.receive()
    if msg["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(msg.get("code", 1000))
    text = msg.get("text")
    if not text or len(text) > 2 * _MAX_AUDIO_FRAME:
        raise VoiceInputError("无效的语音事件。")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise VoiceInputError("无效的语音事件。") from exc
    if not isinstance(obj, dict):
        raise VoiceInputError("无效的语音事件。")
    return obj


def _client_audio(event: dict) -> bytes:
    try:
        audio = base64.b64decode(event.get("audio", ""), validate=True)
    except (TypeError, ValueError) as exc:
        raise VoiceInputError("无效的 PCM 音频。") from exc
    if not audio or len(audio) % 2 or len(audio) > _MAX_AUDIO_FRAME:
        raise VoiceInputError("无效的 PCM 音频。")
    return audio


class _VoicePcm:
    """Convert continuous 24 kHz PCM to 16 kHz using 3:2 area averaging."""

    def __init__(self):
        self.tail = b""

    def convert(self, data: bytes, *, final: bool = False) -> bytes:
        data = self.tail + data
        aligned = len(data) - len(data) % 6
        self.tail = data[aligned:]
        converted = bytearray(aligned // 3 * 2)
        for i, (left, middle, right) in enumerate(struct.iter_unpack("<hhh", data[:aligned])):
            struct.pack_into("<hh", converted, i * 4, (2 * left + middle) // 3, (middle + 2 * right) // 3)
        if final and self.tail:
            if len(self.tail) == 4:
                left, right = struct.unpack("<hh", self.tail)
                converted.extend(struct.pack("<h", (2 * left + right) // 3))
            else:
                converted.extend(self.tail)
            self.tail = b""
        return bytes(converted)


async def _bridge(socket: WebSocket, provider: DoubaoTranscription, first_audio: bytes) -> None:
    item_id = f"item_{uuid4().hex}"
    committed = False

    async def event(kind: str, **data):
        await socket.send_json({"type": kind, "event_id": f"event_{uuid4().hex}", **data})

    async def send_audio():
        nonlocal committed
        pcm = _VoicePcm()
        pending = pcm.convert(first_audio)
        sent = len(first_audio)
        while True:
            message = await asyncio.wait_for(_message(socket), 15)
            if message.get("type") == "input_audio_buffer.commit":
                committed = True
                await event("input_audio_buffer.committed", item_id=item_id, previous_item_id=None)
                await provider.send(pending + pcm.convert(b"", final=True), final=True)
                return
            if message.get("type") != "input_audio_buffer.append":
                raise VoiceInputError("录音中只接受音频和结束事件。")
            audio = _client_audio(message)
            sent += len(audio)
            if sent > SPEECH_RATE * 2 * _MAX_RECORDING_SECONDS:
                raise VoiceInputError("本次录音已达到时长上限。")
            converted = pcm.convert(audio)
            if converted:
                if pending:
                    await provider.send(pending)
                pending = converted

    async def send_transcript():
        last = ""
        async for final, text in provider.updates():
            text = last if text is None else text
            if final:
                if not committed:
                    raise VoiceUpstreamError("transcription ended before commit")
                await event(f"{_TRANSCRIPT}.completed", item_id=item_id, content_index=0, transcript=text)
                return
            if text != last:
                delta = text[len(last):] if text.startswith(last) else ""
                # Doubao may revise an earlier word. The snapshot lets the composer
                # replace that draft while append-only consumers still get deltas.
                await event(f"{_TRANSCRIPT}.delta", item_id=item_id, content_index=0,
                            delta=delta, transcript=text)
                last = text

    sender = asyncio.create_task(send_audio())
    receiver = asyncio.create_task(send_transcript())
    try:
        done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        if sender in done:
            await sender
            await asyncio.wait_for(receiver, 15)
        else:
            await receiver
    finally:
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)


def voice_router(
    voice: DoubaoVoice | None,
    authorize: Callable[[Request | WebSocket], Awaitable[bool]],
    read_json: Callable[[Request, int], Awaitable[object]],
    max_clients: int,
) -> APIRouter:
    router = APIRouter()
    slots = asyncio.Semaphore(max_clients)

    @router.get("/api/voice-config")
    async def voice_config(req: Request):
        if not await authorize(req):
            return _error("请先登录。", 401)
        return JSONResponse(
            {"transcription": voice is not None, "speech": voice is not None}, headers=_HEADERS,
        )

    @router.post("/v1/audio/speech")
    async def speech(req: Request):
        if not await authorize(req):
            return _error("请先登录。", 401)
        if voice is None:
            return _error("语音服务未配置。", 503)
        try:
            body = await asyncio.wait_for(read_json(req, 32_768), 5)
        except (ClientDisconnect, TimeoutError, ValueError):
            return _error("无效的语音合成请求。", 400)
        if not isinstance(body, dict):
            return _error("无效的语音合成请求。", 400)
        output = body.get("response_format", "mp3")
        text = body.get("input")
        if (body.get("model") != "tts-1" or body.get("voice") != "alloy"
                or output not in ("mp3", "pcm") or body.get("speed", 1) != 1
                or body.get("stream_format", "audio") != "audio" or body.get("instructions")):
            return _error("当前只支持 tts-1、alloy 音色及 MP3 或 PCM 音频流。", 400)
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            return _error("朗读文字须为 1 至 4096 字符。", 400)
        return _SpeechResponse(voice, text, output, slots)

    @router.websocket("/v1/realtime")
    async def realtime(socket: WebSocket):
        if (voice is None or socket.query_params.get("intent") != "transcription"
                or not await authorize(socket) or slots.locked()):
            await socket.close(code=1008)
            return
        async with slots:
            await socket.accept()
            try:
                await socket.send_json({"type": "session.created", "session": _SESSION})
                async with asyncio.timeout(_MAX_RECORDING_SECONDS + 30):
                    message = await asyncio.wait_for(_message(socket), 10)
                    session = message.get("session")
                    if (message.get("type") != "session.update" or not isinstance(session, dict)
                            or session.get("type") != "transcription"):
                        raise VoiceInputError("需要初始化实时转写。")
                    audio = session.get("audio", {})
                    audio_input = audio.get("input", {}) if isinstance(audio, dict) else None
                    if (not isinstance(audio_input, dict) or audio_input.get("turn_detection") is not None
                            or audio_input.get("format", _SESSION["audio"]["input"]["format"])
                            != _SESSION["audio"]["input"]["format"]):
                        raise VoiceInputError("实时转写需要 24 kHz 单声道 PCM 和手动结束。")
                    await socket.send_json({"type": "session.updated", "session": _SESSION})
                    message = await asyncio.wait_for(_message(socket), 15)
                    if message.get("type") == "input_audio_buffer.commit":
                        return
                    if message.get("type") != "input_audio_buffer.append":
                        raise VoiceInputError("需要录音数据。")
                    first_audio = _client_audio(message)
                    async with voice.transcription() as provider:
                        await _bridge(socket, provider, first_audio)
            except WebSocketDisconnect:
                return
            except (VoiceInputError, TimeoutError, VoiceUpstreamError) as exc:
                message = str(exc) if isinstance(exc, VoiceInputError) else "实时转写暂不可用。"
                with suppress(WebSocketDisconnect, RuntimeError):
                    await socket.send_json({"type": "error", "error": {"message": message}})
            finally:
                with suppress(WebSocketDisconnect, RuntimeError):
                    await socket.close()

    return router
