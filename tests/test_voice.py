from __future__ import annotations

import asyncio
import base64
import gzip
import json
import struct
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from cc_remote.config import RelayConfig
from cc_remote.relay import server
from cc_remote.relay import voice as doubao
from cc_remote.relay.voice import _decode_transcription, DoubaoVoice, VoiceUpstreamError

ORIGIN = "https://voice.example"
SPEECH = {"input": "按住说话", "model": "tts-1", "voice": "alloy", "response_format": "pcm"}
REALTIME = "wss://voice.example/v1/realtime?intent=transcription"
UPDATE = {"type": "session.update", "session": {"type": "transcription"}}
TRANSCRIPT = "conversation.item.input_audio_transcription"


class FakeVoice:
    def __init__(self):
        self.calls = []
        self.audio = []
        self.closed = False

    async def speech(self, text, output):
        self.calls.append((text, output))
        yield b"\x01"
        yield b"\x02\x03\x04"

    @asynccontextmanager
    async def transcription(self):
        self.calls.append("transcription")
        self.queue = asyncio.Queue()
        try:
            yield self
        finally:
            self.closed = True

    async def send(self, audio, *, final=False):
        self.audio.append((audio, final))
        suggestions = {1: "按驻", 2: "按住"}
        await self.queue.put((final, "按住说话" if final else suggestions[len(self.audio)]))

    async def updates(self):
        while True:
            yield await self.queue.get()


def make_client(tmp_path, *, configured=False):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("test app")
    config = RelayConfig(
        login_password="voice-test-password",
        session_secret="s" * 48,
        wrapper_token="w" * 48,
        public_origin=ORIGIN,
        static_dir=str(static),
        device_db_path=str(tmp_path / "devices.sqlite3"),
        doubao_voice_api_key="fixture-project-key" if configured else "",
    )
    return TestClient(server.create_app(config), base_url=ORIGIN, headers={"origin": ORIGIN})


def login(client):
    assert client.post("/api/login", json={"password": "voice-test-password"}).status_code == 200


def send_audio(socket, *samples):
    data = struct.pack(f"<{len(samples)}h", *samples)
    socket.send_json({
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(data).decode(),
    })


def init(socket):
    assert socket.receive_json()["type"] == "session.created"
    socket.send_json(UPDATE)
    assert socket.receive_json()["type"] == "session.updated"


def test_voice_is_opt_in(tmp_path):
    with make_client(tmp_path) as client:
        assert "microphone=()" in client.get("/").headers["permissions-policy"]
        login(client)
        assert client.get("/api/voice-config").json() == {"transcription": False, "speech": False}
        assert client.post("/v1/audio/speech", json=SPEECH).status_code == 503
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(REALTIME):
            pass


def test_speech_requires_origin_and_session_and_returns_stream(tmp_path, monkeypatch):
    voice = FakeVoice()
    monkeypatch.setattr(server, "DoubaoVoice", lambda _key: voice)
    with make_client(tmp_path, configured=True) as client:
        assert client.get("/").headers["permissions-policy"] == "camera=(), microphone=(self), geolocation=()"
        assert client.get("/api/voice-config").status_code == 401
        assert client.post("/v1/audio/speech", json=SPEECH).status_code == 401
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(REALTIME):
            pass
        login(client)
        assert client.get("/api/voice-config").json() == {"transcription": True, "speech": True}
        foreign = {"origin": "https://other.example"}
        assert client.post("/v1/audio/speech", json=SPEECH, headers=foreign).status_code == 401
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(REALTIME, headers=foreign):
            pass
        assert voice.calls == []
        response = client.post("/v1/audio/speech", json=SPEECH)
        assert response.status_code == 200
        assert response.content == b"\x01\x02\x03\x04"
        assert response.headers["content-type"] == "audio/pcm"
        assert response.headers["cache-control"] == "no-store"
        assert voice.calls == [("按住说话", "pcm")]


def test_realtime_returns_revisable_interim_and_final_only_after_commit(tmp_path, monkeypatch):
    voice = FakeVoice()
    monkeypatch.setattr(server, "DoubaoVoice", lambda _key: voice)
    with make_client(tmp_path, configured=True) as client:
        login(client)
        with client.websocket_connect(REALTIME) as socket:
            init(socket)
            send_audio(socket, 3, 6, 9, 12)
            send_audio(socket, 15, 18)
            first = socket.receive_json()
            assert first["type"] == f"{TRANSCRIPT}.delta"
            assert first["transcript"] == first["delta"] == "按驻"
            send_audio(socket, 21, 24, 27)
            revision = socket.receive_json()
            assert revision["type"] == f"{TRANSCRIPT}.delta"
            assert revision["delta"] == ""
            assert revision["transcript"] == "按住"
            socket.send_json({"type": "input_audio_buffer.commit"})
            committed = socket.receive_json()
            assert committed["type"] == "input_audio_buffer.committed"
            final = socket.receive_json()
            assert final["type"] == f"{TRANSCRIPT}.completed"
            assert final["item_id"] == committed["item_id"] == first["item_id"]
            assert final["transcript"] == "按住说话"
        assert voice.closed
        assert voice.audio == [
            (struct.pack("<hh", 4, 8), False),
            (struct.pack("<hh", 13, 17), False),
            (struct.pack("<hh", 22, 26), True),
        ]


def test_empty_commit_skips_vendor(tmp_path, monkeypatch):
    voice = FakeVoice()
    monkeypatch.setattr(server, "DoubaoVoice", lambda _key: voice)
    with make_client(tmp_path, configured=True) as client:
        login(client)
        with client.websocket_connect(REALTIME) as socket:
            init(socket)
            socket.send_json({"type": "input_audio_buffer.commit"})
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
    assert voice.calls == []


def test_doubao_response_decodes_gzip_and_optional_sequence():
    compressed = gzip.compress(json.dumps({"result": {"text": "完成"}}).encode())
    response = bytes((0x11, 0x93, 0x11, 0)) + struct.pack(">iI", -2, len(compressed)) + compressed
    assert _decode_transcription(response) == (True, "完成")
    with pytest.raises(VoiceUpstreamError):
        _decode_transcription(response[:-1])


@pytest.mark.asyncio
async def test_doubao_transcription_wire_uses_pcm_and_final_packet(monkeypatch):
    async def upstream(socket):
        assert socket.request.headers["X-Api-Resource-Id"] == "volc.seedasr.sauc.duration"
        request = await socket.recv()
        assert request[:4] == b"\x11\x10\x11\x00"
        assert json.loads(gzip.decompress(request[8:]))["audio"]["rate"] == 16000
        audio = await socket.recv()
        assert audio[:4] == b"\x11\x22\x01\x00"
        assert gzip.decompress(audio[8:]) == b"\x20\x00"
        result = json.dumps({"result": {"text": "语音"}}).encode()
        await socket.send(b"\x11\x92\x10\x00" + struct.pack(">I", len(result)) + result)

    async with serve(upstream, "127.0.0.1", 0) as server_socket:
        port = server_socket.sockets[0].getsockname()[1]
        monkeypatch.setattr(doubao, "_ASR_URL", f"ws://127.0.0.1:{port}")
        async with asyncio.timeout(5), DoubaoVoice("fixture-key").transcription() as transcription:
            await transcription.send(b"\x20\x00", final=True)
            assert [result async for result in transcription.updates()] == [(True, "语音")]


@pytest.mark.asyncio
async def test_doubao_speech_decodes_chunks_until_success(monkeypatch):
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Resource-Id"] == "seed-tts-2.0"
        params = json.loads(request.content)["req_params"]
        assert params["text"] == "读一段"
        assert params["speaker"] == "zh_female_vv_uranus_bigtts"
        assert params["audio_params"] == {"format": "pcm", "sample_rate": 24000}
        return httpx.Response(200, text='{"code":0,"data":"AQI="}\n{"code":20000000}\n')

    client_class = httpx.AsyncClient
    monkeypatch.setattr(doubao.httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(upstream), **kw,
    ))
    assert [data async for data in DoubaoVoice("fixture-key").speech("读一段", "pcm")] == [b"\x01\x02"]
