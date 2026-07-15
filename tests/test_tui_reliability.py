"""The interactive TUI must retry commands with the same identity."""

import asyncio
import json

import pytest

from cc_remote.protocol import Query
from cc_remote.tui import Tui, _safe_remote_text, _validate_relay_url


class _Ws:
    def __init__(self):
        self.frames: list[str] = []

    async def send(self, raw: str) -> None:
        self.frames.append(raw)


def test_tui_retries_same_command_until_matching_ack():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
        tui._line = lambda _line: None
        first = _Ws()
        tui.ws = first

        assert await tui._send(Query(prompt="hello", msg_id="m1", sid="s1"))
        sent = json.loads(first.frames[-1])
        assert sent["client_id"] == tui.client_id
        assert sent["cmd_id"]
        assert len(tui._outbox) == 1

        second = _Ws()
        tui.ws = second
        await tui._flush_outbox()
        assert json.loads(second.frames[-1]) == sent

        # An ACK for another client must not delete our command.
        tui._handle({"type": "command_ack", "client_id": "other",
                     "cmd_id": sent["cmd_id"]})
        assert len(tui._outbox) == 1
        tui._handle({"type": "command_ack", "client_id": tui.client_id,
                     "cmd_id": sent["cmd_id"]})
        assert len(tui._outbox) == 0
        assert tui._outbox_bytes == 0

    asyncio.run(run())


def test_tui_plain_reconnect_does_not_request_or_render_history_twice():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
        lines: list[str] = []
        tui._line = lines.append
        first = _Ws()
        tui.ws = first

        assert await tui._request_history("s1")
        request = next(
            json.loads(raw) for raw in first.frames
            if json.loads(raw)["type"] == "get_history"
        )
        tui._handle({
            "type": "history", "session_id": "s1", "events": [
                {"type": "user_msg", "prompt": "once"},
            ],
        })
        assert sum("once" in line for line in lines) == 1
        tui._handle({
            "type": "command_ack", "client_id": tui.client_id,
            "cmd_id": request["cmd_id"],
        })

        second = _Ws()
        tui.ws = second
        await tui._recovery_preamble()
        assert not await tui._request_history("s1")
        assert [
            json.loads(raw)["type"] for raw in second.frames
        ] == ["hello", "switch_session"]
        assert sum("once" in line for line in lines) == 1

    asyncio.run(run())


def test_tui_recovery_switches_then_flushes_each_session_in_order():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s2")
        tui._line = lambda _line: None
        tui.session_engines = {"s1": "claude", "s2": "codex"}
        first = _Ws()
        tui.ws = first
        assert await tui._send(Query(prompt="one", msg_id="m1", sid="s1"))
        assert await tui._send(Query(prompt="two", msg_id="m2", sid="s2"))

        second = _Ws()
        tui.ws = second
        await tui._recovery_preamble()
        frames = [json.loads(raw) for raw in second.frames]

        assert [(frame["type"], frame.get("sid"), frame.get("session_id"))
                for frame in frames] == [
            ("hello", None, None),
            ("switch_session", None, "s1"),
            ("query", "s1", None),
            ("switch_session", None, "s2"),
            ("query", "s2", None),
        ]
        assert frames[1]["engine"] == "claude"
        assert frames[3]["engine"] == "codex"

    asyncio.run(run())


def test_tui_rebuild_or_generation_change_forces_one_history_refresh():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
        tui._line = lambda _line: None
        tui._history_loaded.add("s1")
        tui._wrapper_generation = "generation-old"
        ws = _Ws()
        tui.ws = ws

        tui._handle({
            "type": "replay_start", "sid": "s1", "generation": "generation-new",
            "from_seq": 1, "to_seq": 4, "truncated": False, "rebuild": True,
        })
        await tui._flush_history_refreshes()
        assert not any(json.loads(raw)["type"] == "get_history" for raw in ws.frames)

        tui._handle({
            "type": "replay_end", "sid": "s1", "to_seq": 4,
            "truncated": False,
        })
        await tui._flush_history_refreshes()
        assert sum(
            json.loads(raw)["type"] == "get_history" for raw in ws.frames
        ) == 1

        # ReplayEnd repeats its truncation marker, but an outstanding reliable
        # request suppresses a second full-history pull.
        tui._handle({
            "type": "replay_end", "sid": "s1", "to_seq": 4,
            "truncated": True,
        })
        await tui._flush_history_refreshes()
        assert sum(
            json.loads(raw)["type"] == "get_history" for raw in ws.frames
        ) == 1

    asyncio.run(run())


def test_tui_same_generation_delta_replay_does_not_refresh_history():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
        tui._line = lambda _line: None
        tui._history_loaded.add("s1")
        tui._wrapper_generation = "generation-1"
        ws = _Ws()
        tui.ws = ws

        tui._handle({
            "type": "replay_start", "sid": "s1", "generation": "generation-1",
            "from_seq": 8, "to_seq": 9, "truncated": False, "rebuild": False,
        })
        tui._handle({
            "type": "replay_end", "sid": "s1", "to_seq": 9,
            "truncated": False,
        })
        await tui._flush_history_refreshes()
        assert not any(json.loads(raw)["type"] == "get_history" for raw in ws.frames)

    asyncio.run(run())


def test_tui_wrapper_process_generation_change_refreshes_attached_history():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
        tui._line = lambda _line: None
        tui._history_loaded.add("s1")
        tui._wrapper_generation = "generation-old"
        ws = _Ws()
        tui.ws = ws

        tui._handle({
            "type": "wrapper_reconnected", "generation": "generation-new",
            "state": "idle",
        })
        await tui._flush_history_refreshes()
        assert sum(
            json.loads(raw)["type"] == "get_history" for raw in ws.frames
        ) == 1

    asyncio.run(run())


def test_tui_explicit_attach_refreshes_even_an_already_loaded_session():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", None)
        tui._line = lambda _line: None
        tui._history_loaded.add("s1")
        ws = _Ws()
        tui.ws = ws

        await tui._attach("s1", "claude")
        assert [json.loads(raw)["type"] for raw in ws.frames] == [
            "switch_session", "get_history",
        ]

    asyncio.run(run())


def test_tui_drops_overlap_and_stale_cached_events_before_rendering():
    tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
    lines: list[str] = []
    chunks: list[str] = []
    tui._line = lines.append
    tui._write = chunks.append

    tui._handle({
        "type": "snapshot", "sid": "s1", "cc_session_id": "s1",
        "generation": "g1", "state": "running", "tail_text": "",
    })
    tui._handle({"type": "delta", "sid": "s1", "seq": 1, "text": "X"})
    # Relay registration can let the live seq=1 frame precede this Hello replay.
    tui._handle({
        "type": "replay_start", "sid": "s1", "generation": "g1",
        "from_seq": 1, "to_seq": 2, "truncated": False,
        "rebuild": False,
    })
    tui._handle({"type": "delta", "sid": "s1", "seq": 1, "text": "X"})
    tui._handle({"type": "model", "sid": "s1", "seq": 2, "model": "new"})
    tui._handle({
        "type": "replay_end", "sid": "s1", "to_seq": 2,
        "truncated": False,
    })
    # A cached response for an older reliable command must not roll the UI back.
    tui._handle({"type": "model", "sid": "s1", "seq": 1, "model": "old"})

    assert chunks == ["X"]
    assert sum("[model: new]" in line for line in lines) == 1
    assert not any("[model: old]" in line for line in lines)


def test_tui_rebuild_accepts_lower_sequence_then_restores_dedup():
    tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
    chunks: list[str] = []
    tui._line = lambda _line: None
    tui._write = chunks.append

    tui._handle({
        "type": "snapshot", "sid": "s1", "cc_session_id": "s1",
        "generation": "g1", "state": "running", "tail_text": "",
    })
    tui._handle({"type": "delta", "sid": "s1", "seq": 10, "text": "old"})
    chunks.clear()
    tui._handle({
        "type": "replay_start", "sid": "s1", "generation": "g1",
        "from_seq": 1, "to_seq": 1, "truncated": False,
        "rebuild": True,
    })
    tui._handle({
        "type": "delta", "sid": "s1", "seq": 1, "text": "rebuilt",
    })
    assert chunks == []  # authoritative History will render this narrative once
    tui._handle({
        "type": "replay_end", "sid": "s1", "to_seq": 1,
        "truncated": False,
    })
    tui._handle({
        "type": "history", "session_id": "s1",
        "events": [{"type": "delta", "text": "rebuilt"}],
    })
    tui._handle({
        "type": "delta", "sid": "s1", "seq": 1, "text": "stale",
    })

    assert chunks == ["rebuilt"]
    assert tui.cursors["s1"] == 1


def test_tui_truncated_replay_does_not_print_again_before_history():
    tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
    chunks: list[str] = []
    tui._line = lambda _line: None
    tui._write = chunks.append

    tui._handle({
        "type": "replay_start", "sid": "s1", "generation": "g1",
        "from_seq": 4, "to_seq": 5, "truncated": True,
        "rebuild": False,
    })
    tui._handle({
        "type": "delta", "sid": "s1", "seq": 5, "text": "only once",
    })
    tui._handle({
        "type": "replay_end", "sid": "s1", "to_seq": 5,
        "truncated": True,
    })
    assert chunks == []

    tui._handle({
        "type": "history", "session_id": "s1",
        "events": [{"type": "delta", "text": "only once"}],
    })
    assert chunks == ["only once"]


def test_tui_matching_user_echo_is_removed_from_dedup_set():
    tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
    lines: list[str] = []
    tui._line = lines.append
    tui.sent_msg_ids.add("mine")
    tui.cursors["s1"] = 1  # reconnect overlap is stale but still proves delivery

    tui._handle({
        "type": "user_msg", "sid": "s1", "seq": 1,
        "msg_id": "mine", "prompt": "already echoed",
    })

    assert "mine" not in tui.sent_msg_ids
    assert not any("already echoed" in line for line in lines)


def test_tui_strips_terminal_and_bidi_controls_from_remote_text():
    dangerous = (
        "safe\nnext\tcolumn\x1b]52;c;U0VDUkVU\x07\r\b"
        "\u009b31m\u061c\u200f\u202e\u2066done"
    )
    sanitized = _safe_remote_text(dangerous)

    assert sanitized == "safe\nnext    column]52;c;U0VDUkVU31mdone"
    assert "\n" in sanitized
    assert all(
        char == "\n" or not (
            ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        )
        for char in sanitized
    )

    tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "s1")
    chunks: list[str] = []
    tui._write = chunks.append
    tui._line = lambda _line: None
    tui._handle({
        "type": "delta", "sid": "s1", "seq": 1, "text": dangerous,
    })
    assert chunks == [sanitized]


def test_tui_keeps_and_answers_ask_user_per_session():
    async def run() -> None:
        tui = Tui("ws://127.0.0.1:8765/ws", "password", "", "claude", "A")
        lines: list[str] = []
        tui._line = lines.append
        ws = _Ws()
        tui.ws = ws

        tui._handle({
            "type": "ask_user", "sid": "A", "seq": 1,
            "ask_id": "ask-a", "question": "Question A?",
            "options": [{"label": "A1"}, {"label": "A2"}],
        })
        tui._handle({
            "type": "ask_user", "sid": "B", "seq": 1,
            "ask_id": "ask-b", "question": "Question B?",
            "options": [{"label": "B1"}, {"label": "B2"}],
        })
        assert "A" in tui.pending_asks and "B" in tui.pending_asks
        assert not any("Question B?" in line for line in lines)

        await tui._attach("B")
        assert any("Question B?" in line for line in lines)
        await tui._answer_ask(2)

        answers = [
            json.loads(raw) for raw in ws.frames
            if json.loads(raw)["type"] == "answer_question"
        ]
        assert len(answers) == 1
        assert answers[0]["sid"] == "B"
        assert answers[0]["ask_id"] == "ask-b"
        assert answers[0]["answer"] == "B2"
        assert "A" in tui.pending_asks and "B" not in tui.pending_asks

        # A terminal event clears only the matching background session's ask.
        tui._handle({
            "type": "turn_end", "sid": "A", "seq": 2,
            "result": {"subtype": "success", "duration_ms": 1,
                       "is_error": False},
        })
        assert tui.pending_asks == {}

    asyncio.run(run())


@pytest.mark.parametrize("url", [
    "ws://relay.example.com/ws",
    "wss://user:secret@relay.example.com/ws",
    "wss://relay.example.com/not-ws",
    "wss://relay.example.com/ws?token=secret",
])
def test_tui_rejects_unsafe_relay_url_before_login(url):
    with pytest.raises(ValueError):
        _validate_relay_url(url)


@pytest.mark.parametrize("url", [
    "ws://127.0.0.1:8765/ws",
    "ws://[::1]:8765/ws",
    "ws://localhost:8765/ws",
    "wss://relay.example.com/ws",
])
def test_tui_accepts_tls_or_loopback_relay_url(url):
    assert _validate_relay_url(url) == url


def test_tui_rejects_plain_ws_public_ip_unless_allow_insecure_http(monkeypatch):
    import cc_remote.tui as tui_module

    url = "ws://198.51.100.10:8765/ws"
    monkeypatch.setattr(tui_module, "ALLOW_INSECURE_HTTP", False)
    with pytest.raises(ValueError):
        _validate_relay_url(url)

    monkeypatch.setattr(tui_module, "ALLOW_INSECURE_HTTP", True)
    assert _validate_relay_url(url) == url
