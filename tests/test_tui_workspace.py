"""Model-free terminal interaction and control-link regressions."""

import asyncio
import json

import pytest
from textual.widgets import OptionList, Static
from textual.widgets.text_area import Selection
from websockets.asyncio.server import serve

from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.tui_app import (
    Composer,
    Transcript,
    WorkspaceApp,
    WorkspaceClient,
    location,
    offset,
    seed_demo,
)
from cc_remote.tui_state import Block, MAX_BLOCKS, SessionView, WorkspaceState


def client(sid="s"):
    c = WorkspaceClient("ws://localhost:8765/ws", "", "", "codex", sid)
    if sid:
        c.workspace.view(sid).write_state = "writable"
    return c


def emit(c, kind, **values):
    c._handle({"type": kind, "v": PROTOCOL_VERSION, "sid": "s", **values})


def test_workspace_reuses_replay_dedup_before_projection():
    c = client()
    emit(c, "delta", seq=1, message_id="a", text="once")
    emit(c, "delta", seq=1, message_id="a", text="once")
    assert c.workspace.view("s").blocks[0].text == "once"


def test_background_events_do_not_steal_focus():
    c = client()
    emit(c, "user_msg", sid="other", msg_id="m", prompt="background")
    emit(c, "session_focus", session_id="other")
    assert c.attached_sid == "s"
    assert c.workspace.view("other").blocks[0].text == "background"
    assert not c.workspace.view("s").blocks


def test_background_error_is_projected_without_replacing_focused_notice():
    c = client()
    c.notice = "Focused task"
    emit(c, "error", sid="other", code="busy", message="Background failure")
    assert c.notice == "Focused task"
    assert "Background failure" in c.workspace.view("other").render()[0]
    emit(c, "error", code="busy", message="Focused failure")
    assert "Focused failure" in c.notice
    emit(c, "error", sid=None, code="offline", message="Connection failure")
    assert "Connection failure" in c.notice


def test_protocol_mismatch_is_terminal_not_a_refresh_loop():
    c = client()
    c._handle({"type": "snapshot", "v": PROTOCOL_VERSION - 1, "sid": "s"})
    assert c._quitting and c.protocol_error
    assert "Protocol mismatch" in c.notice
    assert not c.workspace.view("s").blocks


def test_control_revision_and_snapshot_are_authoritative():
    c = client()
    emit(
        c,
        "snapshot",
        state="running",
        control={"generation": "g", "revision": 4, "write_state": "read_only"},
    )
    emit(
        c, "session_control", generation="g", revision=3, write_state="writable"
    )
    view = c.workspace.view("s")
    assert view.state == "running"
    assert view.write_state == "read_only"


def test_history_summary_merges_newer_live_text_without_duplicates():
    view = SessionView()
    view.event(
        {
            "type": "delta",
            "message_id": "a",
            "turn_id": "m",
            "seq": 8,
            "text": "new answer",
        }
    )
    view.history(
        {
            "revision": "r",
            "live_seq": 5,
            "turns": [
                {
                    "id": "m",
                    "prompt": "question",
                    "blocks": [
                        {
                            "kind": "text",
                            "message_id": "a",
                            "text": "old answer",
                        }
                    ],
                }
            ],
        }
    )
    assert [b.id for b in view.blocks] == ["user:m", "a"]
    assert view.blocks[-1].text == "new answer"


def test_newest_history_does_not_move_older_live_turn_after_latest():
    view = SessionView()
    view.event(
        {"type": "user_msg", "msg_id": "old", "seq": 2, "prompt": "older"}
    )
    view.history(
        {
            "revision": "r",
            "live_seq": 8,
            "turns": [{"id": "new", "prompt": "latest"}],
        }
    )
    assert [b.text for b in view.blocks] == ["older", "latest"]


def test_unchanged_summary_keeps_an_expanded_detail_page():
    view = SessionView(revision="r", details={"m": None})
    view.put(Block("detail:m", "detail", "expanded output", "m"))
    view.history(
        {"revision": "r", "turns": [{"id": "m", "detailEventCount": 10}]}
    )
    assert view.blocks[0].text == "expanded output"


def test_history_error_and_stale_page_cannot_erase_projection():
    view = SessionView()
    view.history(
        {
            "revision": "r",
            "generation": "g",
            "build_seq": 4,
            "turns": [{"id": "m", "prompt": "keep"}],
        }
    )
    for change in (
        {"error": "unavailable"},
        {"authoritative": False},
        {"build_seq": 3},
        {"before": "m", "revision": "old"},
    ):
        view.history(
            {
                "revision": "r",
                "generation": "g",
                "build_seq": 4,
                "turns": [],
                **change,
            }
        )
    assert view.blocks[0].text == "keep"


def test_client_message_alias_deduplicates_history_and_keeps_anchor():
    view = SessionView()
    view.event(
        {"type": "user_msg", "msg_id": "client", "seq": 8, "prompt": "question"}
    )
    text, starts = view.render()
    anchor = view.locate(text.index("question"), starts)
    view.history(
        {
            "revision": "r",
            "live_seq": 5,
            "turns": [
                {"id": "native", "clientMsgId": "client", "prompt": "question"}
            ],
        }
    )
    text, starts = view.render()
    assert text.count("question") == 1
    assert text[view.resolve(anchor, starts, len(text)) :].startswith(
        "question"
    )


def test_paging_keeps_stable_anchor_and_oldest_cursor():
    view = SessionView()
    view.history(
        {
            "revision": "r",
            "oldest_id": "b",
            "has_more": True,
            "turns": [{"id": "b", "prompt": "newer"}],
        }
    )
    text, starts = view.render()
    anchor = view.locate(text.index("newer"), starts)
    view.history(
        {
            "revision": "r",
            "before": "b",
            "oldest_id": "a",
            "has_more": False,
            "turns": [{"id": "a", "prompt": "older"}],
        }
    )
    text, starts = view.render()
    assert text[view.resolve(anchor, starts, len(text)) :].startswith("newer")
    assert view.oldest == "a" and not view.has_more


def test_reset_removes_rolled_back_turns_but_not_draft():
    view = SessionView(draft="do not lose")
    view.put(Block("old", "assistant", "removed"))
    view.history({"reset": True, "revision": "new", "turns": []})
    assert not view.blocks
    assert view.draft == "do not lose"


def test_output_controls_are_not_interpreted_and_size_is_bounded():
    view = SessionView()
    view.put(Block("a", "assistant", "\x1b]52;c;secret\x07[bold]hello\u202e"))
    text, _ = view.render()
    assert "\x1b" not in text and "\u202e" not in text
    assert "[bold]" in text
    view.put(Block("large", "assistant", "x" * 100000))
    assert len(view.blocks[-1].text) == 65536


def test_rekey_preserves_session_draft():
    state = WorkspaceState()
    state.view("tmp").draft = "draft"
    state.event(
        {"type": "session_rekey", "old_key": "tmp", "session_id": "real"}
    )
    assert state.view("real").draft == "draft"
    assert "tmp" not in state.views


@pytest.mark.asyncio
async def test_demo_never_sends_or_authenticates():
    c = client()
    seed_demo(c)
    assert not await c.submit("do not send")
    assert not await c.submit("do not queue", queue=True)
    assert not c._outbox
    assert c.cookie == "" and c.ws is None


def test_bounded_live_tail_never_evicts_a_reading_selection():
    view = SessionView(follow=False, anchor=("keep", 0), selection=("keep", 0))
    view.put(Block("keep", "user", "selected text"))
    for index in range(MAX_BLOCKS * 2):
        view.put(Block(str(index), "tool", "output"))
    assert view.blocks[0].id == "keep"
    assert len(view.blocks) == MAX_BLOCKS
    assert view.tail_hidden


@pytest.mark.asyncio
async def test_queue_ownership_and_reliable_retry_identity():
    c = client()
    assert await c.submit("deferred", queue=True)
    assert len(c._outbox) == 1
    raw, _ = next(iter(c._outbox.values()))
    frame = json.loads(raw)
    assert frame["delivery"] == "queue" and frame["sid"] == "s"
    assert not c.workspace.view("s").blocks
    sent = []

    async def send(raw):
        sent.append(raw)
        return True

    c._send_raw = send
    await c._flush_outbox()
    await c._flush_outbox()
    assert sent == [raw, raw]
    c._handle(
        {
            "type": "command_ack",
            "cmd_id": frame["cmd_id"],
            "client_id": c.client_id,
        }
    )
    assert not c._outbox


@pytest.mark.asyncio
async def test_read_only_cannot_send_or_queue():
    c = client()
    c.workspace.view("s").write_state = "read_only"
    assert not await c.submit("no")
    assert not await c.submit("no", queue=True)
    assert not c._outbox


@pytest.mark.asyncio
async def test_question_options_and_free_text_are_exactly_scoped():
    c = client()
    emit(
        c,
        "ask_user",
        ask_id="ask",
        question="why?",
        allow_text=True,
        options=[{"label": "Yes"}, {"label": "No"}],
    )
    assert await c.answer("2")
    frame = json.loads(next(iter(c._outbox.values()))[0])
    assert (frame["sid"], frame["ask_id"], frame["answer"]) == (
        "s",
        "ask",
        "No",
    )
    c._outbox.clear()
    emit(
        c,
        "ask_user",
        ask_id="text",
        question="why?",
        allow_text=True,
        options=[],
    )
    assert await c.answer("explanation")
    assert (
        json.loads(next(iter(c._outbox.values()))[0])["answer"] == "explanation"
    )


@pytest.mark.asyncio
async def test_all_blocking_questions_remain_answerable_after_first_reply():
    from cc_remote.tui_modal import ModalEditor
    from cc_remote.tui_panels import QuestionDialog

    c = client()
    for identity in ("first", "second"):
        emit(c, "ask_user", ask_id=identity, question=identity,
             allow_text=True, options=[])
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        for identity in ("first", "second"):
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, QuestionDialog)
            assert app.screen.ask["ask_id"] == identity
            app.screen.query_one("#answer", ModalEditor).load_text("answer")
            await pilot.press("enter")
            await pilot.pause()
        sent = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert [f["ask_id"] for f in sent] == ["first", "second"]
        assert c._pending_ask_for_attached() is None


@pytest.mark.asyncio
async def test_normal_insert_and_quote_keep_independent_cursors():
    c = client()
    emit(
        c, "user_msg", msg_id="m", prompt="first line\nsecond line\nthird line"
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.press("g", "u", "j", "v", "l", "l", "l")
        reader = app.query_one(Transcript)
        end = reader.cursor_location
        selected = reader.selected_text
        assert selected
        await pilot.press("space", "q")
        assert reader.cursor_location == end
        assert app.focused is reader and app.mode == "NORMAL"
        assert "> " + selected in app.query_one(Composer).text
        await pilot.press("ctrl+j", "i", "w", "h", "y", "escape", "ctrl+k")
        assert reader.cursor_location == end
        assert "why" in app.query_one(Composer).text
        assert not c._outbox  # Quotes are not commands.


@pytest.mark.asyncio
async def test_stream_and_resize_do_not_move_visual_selection():
    c = client()
    emit(c, "user_msg", msg_id="m", prompt="汉字 code\n" * 30)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.press("g", "u", "j", "v", "l", "l")
        reader = app.query_one(Transcript)
        selection = reader.selection
        chosen = reader.selected_text
        emit(c, "delta", seq=2, message_id="a", text="streaming\n" * 20)
        await pilot.pause()
        await pilot.resize_terminal(70, 25)
        await pilot.pause()
        assert reader.selection == selection
        assert reader.selected_text == chosen
        assert app.mode == "VISUAL"


@pytest.mark.asyncio
async def test_switch_restores_reading_position_and_each_draft():
    c = client()
    emit(c, "user_msg", msg_id="m", prompt="one\ntwo\nthree")
    emit(c, "user_msg", sid="b", msg_id="n", prompt="another")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press(
            "g", "u", "j", "ctrl+j", "i", "d", "r", "a", "f", "t", "escape", "ctrl+k"
        )
        reader = app.query_one(Transcript)
        position = reader.cursor_location
        c.attached_sid = "b"
        app.paint()
        await pilot.pause()
        assert app.query_one(Composer).text == ""
        await pilot.press("ctrl+j", "i", "b", "escape", "ctrl+k")
        c.attached_sid = "s"
        app.paint()
        await pilot.pause()
        assert app.query_one(Composer).text == "draft"
        assert reader.cursor_location == position
        assert c.workspace.view("b").draft == "b"


@pytest.mark.asyncio
async def test_copy_does_not_focus_composer_and_latest_message_jumps():
    c = client()
    emit(c, "user_msg", msg_id="m", prompt="question")
    emit(c, "delta", message_id="a", text="answer", channel="final")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "a", "j", "v", "l", "l", "y")
        reader = app.query_one(Transcript)
        assert app.focused is reader and app.mode == "NORMAL"
        assert app.clipboard == "ans"
        await pilot.press("g", "u")
        assert reader.cursor_location == (0, 0)
        await pilot.press("right_square_bracket", "m")
        assert reader.cursor_location[0] > 0


@pytest.mark.asyncio
async def test_ui_queue_keeps_editor_if_send_rejected_then_submits_once():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "a")
        c.workspace.view("s").write_state = "read_only"
        await pilot.press("ctrl+e")
        assert app.query_one(Composer).text == "a"
        c.workspace.view("s").write_state = "writable"
        await pilot.press("ctrl+e", "ctrl+e")
        assert app.query_one(Composer).text == ""
        assert len(c._outbox) == 1


@pytest.mark.asyncio
async def test_tool_expansion_and_command_escape_preserve_draft():
    c = client()
    emit(c, "tool_use", tool_use_id="t", tool="shell", input={"cmd": "pwd"})
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        start = next(i for i, b in app.starts if b.role == "tool_group")
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "pwd" in app.query_one(Transcript).text
        await pilot.press(
            "ctrl+j",
            "i",
            "d",
            "escape",
            "ctrl+k",
            "colon",
            "s",
            "t",
            "o",
            "p",
            "escape",
            "escape",
        )
        assert app.query_one(Composer).text == "d"
        assert not c._outbox


def test_unicode_location_roundtrip():
    text = "hello\n汉字🙂\n"
    for index in range(len(text) + 1):
        assert offset(text, location(text, index)) == index


def test_rollback_barrier_rejects_late_history_without_waiting_for_reset():
    state = WorkspaceState()
    state.event(
        {
            "type": "history",
            "session_id": "s",
            "revision": "old",
            "turns": [{"id": "m", "prompt": "removed"}],
        }
    )
    state.event(
        {"type": "history_invalidated", "session_id": "s", "revision": "new"}
    )
    state.event(
        {
            "type": "history",
            "session_id": "s",
            "revision": "old",
            "turns": [{"id": "m", "prompt": "removed"}],
        }
    )
    assert not state.view("s").blocks


@pytest.mark.asyncio
@pytest.mark.parametrize("machine", ["test-device", "default"])
async def test_real_websocket_reuses_device_route_and_summary_protocol(machine):
    received = []
    complete = asyncio.Event()

    async def server(ws):
        assert ws.request.path == f"/ws?machine={machine}"
        async for raw in ws:
            frame = json.loads(raw)
            received.append(frame)
            if frame["type"] == "list_sessions":
                matches = (frame["engine"], frame["space"]) == ("codex", "code")
                await ws.send(json.dumps({
                    "type": "session_list", "v": PROTOCOL_VERSION,
                    "engine": frame["engine"], "space": frame["space"],
                    "request_id": frame["cmd_id"],
                    "sessions": [dict(session_id="s", engine="codex",
                                      space="code")] if matches else [],
                }))
            if frame["type"] == "get_history":
                switch = next(f for f in received if f["type"] == "switch_session")
                assert (switch["engine"], switch["space"]) == ("codex", "code")
                await ws.send(
                    json.dumps(
                        {
                            "type": "history",
                            "v": PROTOCOL_VERSION,
                            "session_id": "s",
                            "revision": "r",
                            "detail": "summary",
                            "turns": [{"id": "m", "prompt": "wire history"}],
                        }
                    )
                )
                complete.set()

    async with serve(server, "127.0.0.1", 0) as socket:
        port = socket.sockets[0].getsockname()[1]
        c = WorkspaceClient(
            f"ws://127.0.0.1:{port}/ws",
            "",
            "",
            "codex",
            "s",
            machine_id=machine,
        )
        task = asyncio.create_task(c._connection_loop())
        try:
            await asyncio.wait_for(complete.wait(), 3)
            for _ in range(100):
                if c.workspace.view("s").blocks:
                    break
                await asyncio.sleep(0.01)
            assert c.workspace.view("s").blocks[0].text == "wire history"
            assert received[0]["type"] == "hello"
            assert received[0]["machine_id"] == machine
            assert (
                next(f for f in received if f["type"] == "get_history")[
                    "detail"
                ]
                == "summary"
            )
        finally:
            c._quitting = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_visual_line_selects_whole_next_line():
    c = client()
    emit(c, "user_msg", msg_id="m", prompt="short\nlonger line here")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "u", "j", "V", "j")
        assert (
            app.query_one(Transcript).selected_text == "short\nlonger line here"
        )


@pytest.mark.asyncio
async def test_rekey_while_editing_does_not_recreate_temporary_view():
    c = client("tmp")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "d", "r", "a", "f", "t")
        emit(c, "session_rekey", old_key="tmp", session_id="real")
        app.paint()
        assert app.query_one(Composer).text == "draft"
        assert c.workspace.view("real").draft == "draft"
        assert "tmp" not in c.workspace.views


@pytest.mark.asyncio
async def test_session_picker_navigation_and_attach_updates_title():
    c = client(None)
    emit(
        c,
        "session_list",
        engine="codex",
        sessions=[
            {
                "session_id": "a",
                "cwd": "/a",
                "summary": "first",
                "engine": "codex",
            },
            {
                "session_id": "b",
                "cwd": "/b",
                "summary": "second",
                "engine": "codex",
            },
        ],
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(110, 35)) as pilot:
        heading = app.query_one("#session-title", Static)
        assert "Space e" in heading.content.plain
        assert not app.query(OptionList)
        await pilot.press("space", "e", "j", "l", "j", "enter")
        await pilot.pause()
        assert c.attached_sid == "b"
        assert app.focused is app.query_one(Transcript)
        assert heading.content.plain == "Codex / Code · Session: second"
        assert not app.query(OptionList)
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert any(
            f["type"] == "switch_session" and f["session_id"] == "b"
            for f in frames
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("demo", [False, True])
@pytest.mark.parametrize("width", [60, 110, 180])
async def test_workspace_has_full_width_conversation_and_one_line_title(
    demo, width
):
    c = client()
    if demo:
        seed_demo(c)
    else:
        emit(
            c,
            "session_list",
            engine="codex",
            sessions=[
                {"session_id": "s", "summary": "当前工作", "engine": "codex"}
            ],
        )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(width, 35)) as pilot:
        await pilot.pause()
        heading = app.query_one("#session-title", Static)
        expected = "Review a change" if demo else "当前工作"
        assert heading.content.plain == "Codex / Code · Session: " + expected
        assert heading.region.height == 1
        assert not app.query("#sessions")
        assert not app.query(OptionList)
        for widget in (app.query_one(Transcript), app.query_one(Composer)):
            assert widget.region.x == 0
            assert widget.region.width == width
        # Resizing must never restore a list or reserve a sidebar's width.
        await pilot.resize_terminal(width + 15, 35)
        await pilot.pause()
        assert heading.region.height == 1
        assert app.query_one(Transcript).region.width == width + 15
        assert not app.query(OptionList)


@pytest.mark.asyncio
async def test_title_updates_from_catalog_without_moving_editor(monkeypatch):
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(60, 30)) as pilot:
        heading = app.query_one("#session-title", Static)
        assert heading.content.plain == "Codex / Code · Session: s"
        await pilot.press("ctrl+j", "i", "d", "r", "a", "f", "t")
        editor = app.query_one(Composer)
        cursor = editor.cursor_location
        emit(
            c,
            "session_list",
            engine="codex",
            sessions=[
                {
                    "session_id": "s",
                    "summary": "[bold]新标题\n\x1b[31m第二行\u202e " * 30,
                    "engine": "codex",
                },
                {
                    "session_id": "other",
                    "summary": "Background title",
                    "engine": "codex",
                },
            ],
        )
        app.paint()
        await pilot.pause()
        content = heading.content
        assert content.plain.startswith("Codex / Code · Session: [bold]新标题")
        assert not content.spans
        assert not any(c in content.plain for c in "\n\x1b\u202e")
        assert "Background title" not in content.plain
        assert heading.styles.text_wrap == "nowrap"
        assert heading.styles.text_overflow == "ellipsis"
        assert heading.region.height == 1
        assert heading.render_line(0).text.rstrip().endswith("…")
        assert app.focused is editor
        assert editor.text == "draft" and editor.cursor_location == cursor
        assert editor.vim_mode == "INSERT"
        updates = []
        monkeypatch.setattr(heading, "update", updates.append)
        for _ in range(10):
            app.paint()
        assert not updates


@pytest.mark.asyncio
async def test_title_falls_back_to_prompt_and_tracks_rekey():
    c = client("tmp")
    c.workspace.catalog["tmp"] = {
        "session_id": "tmp",
        "summary": None,
        "first_prompt": "first line\nsecond line",
    }
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        heading = app.query_one("#session-title", Static)
        assert heading.content.plain == "Codex / Code · Session: first line second line"
        emit(c, "session_rekey", old_key="tmp", session_id="real")
        app.paint()
        await pilot.pause()
        assert c.attached_sid == "real"
        assert heading.content.plain == "Codex / Code · Session: first line second line"
        c.workspace.catalog["real"]["first_prompt"] = None
        app.paint()
        assert heading.content.plain == "Codex / Code · Session: real"


@pytest.mark.asyncio
async def test_half_page_keys_and_idle_ticks_do_not_repaint(monkeypatch):
    c = client()
    emit(c, "user_msg", msg_id="m", prompt="line\n" * 80)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "g", "ctrl+d")
        assert app.query_one(Transcript).cursor_location[0] > 0
        await pilot.press("ctrl+u")
        assert app.query_one(Transcript).cursor_location == (0, 0)
        app.paint()
        updates = []
        monkeypatch.setattr(app.query_one("#status"), "update", updates.append)
        monkeypatch.setattr(
            app.query_one("#question"), "update", updates.append
        )
        for _ in range(10):
            app.paint()
        assert not updates


@pytest.mark.asyncio
async def test_return_to_evicted_tail_requests_summary_not_a_model_turn():
    c = client()
    view = c.workspace.view("s")
    view.tail_hidden = True
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("G")
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert [f["type"] for f in frames] == ["get_history"]
        assert frames[0]["detail"] == "summary"
        # A hidden tail is not the real bottom. Exercise idle paints while the
        # request is pending, then assert follow after its summary arrives.
        # The transient flag immediately after G depends on timer scheduling.
        for _ in range(3):
            app.paint()
            await pilot.pause()
        emit(c, "history", session_id="s", revision="fresh", turns=[{
            "id": "newest", "prompt": "latest user message",
            "blocks": [{"kind": "text", "message_id": "answer",
                        "channel": "final", "text": "latest answer\n" * 60}],
        }])
        app.paint()
        await pilot.pause()
        assert not view.tail_hidden
        assert view.follow
        reader = app.query_one(Transcript)
        assert reader.scroll_y == reader.max_scroll_y
        assert "latest answer" in reader.text


@pytest.mark.asyncio
async def test_pagination_preserves_selected_text_in_ui():
    c = client()
    emit(
        c,
        "history",
        session_id="s",
        revision="r",
        turns=[{"id": "m", "prompt": "original line"}],
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "u", "j", "v", "l", "l")
        reader = app.query_one(Transcript)
        chosen = reader.selected_text
        emit(
            c,
            "history",
            session_id="s",
            revision="r",
            before="m",
            turns=[{"id": "old", "prompt": "older line"}],
        )
        app.paint()
        await pilot.pause()
        assert reader.selected_text == chosen
        assert reader.selection != Selection.cursor((0, 0))
