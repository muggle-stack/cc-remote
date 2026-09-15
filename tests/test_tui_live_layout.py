"""Live tail, terminal folding and narrow terminal regressions (no model)."""

import pytest
from textual.widgets import Static
from textual.widgets.text_area import Selection

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, Transcript, location
from cc_remote.tui_chrome import status_text
from cc_remote.tui_presentation import TurnDisplay
from cc_remote.tui_state import Block, SessionView, WorkspaceState


def client():
    return WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")


def turn(view):
    view.event(dict(type="user_msg", msg_id="t", prompt="question", ts=1000))
    view.event(dict(type="delta", message_id="p", channel="commentary",
                    text="progress explanation"))
    view.event(dict(type="tool_use", tool_use_id="tool", tool="command",
                    input={"command": "echo test"}))
    view.event(dict(type="tool_result", tool_use_id="tool", content="result"))


@pytest.mark.parametrize("terminal", ["final", "success", "error_during_execution"])
def test_live_processes_fold_without_losing_data(terminal):
    view = SessionView()
    turn(view)
    assert "progress explanation" in view.render()[0]
    if terminal == "final":
        view.event(dict(type="delta", message_id="answer", channel="final",
                        text="final answer"))
        # Async questions also use final; the phase is not a terminal.
        assert "progress explanation" in view.render()[0]
        assert "── Turn details" in view.render()[0]
        view.event(dict(type="turn_end", turn_id="t",
                        result={"subtype": "success"}))
    else:
        view.event(dict(type="turn_end", turn_id="t",
                        result={"subtype": terminal}, ts=1010))
    text, starts = view.render()
    assert text.count("── Turn details") == 1
    assert "progress explanation" in text
    assert "echo test" not in text
    detail = next(b for _, b in starts if b.role == "detail")
    assert detail.expanded
    group = next(b for _, b in starts if b.role == "tool_group")
    assert not group.expanded
    group.expanded = True
    text, _ = view.render()
    assert "progress explanation" in text and "echo test" in text
    view.event(dict(type="tool_delta", tool_use_id="tool", delta="late tail"))
    assert "late tail" in view.render()[0]
    if terminal == "final":
        assert "final answer" in text
    detail.expanded = False
    text, starts = view.render()
    assert view.resolve(("p", 30), starts, len(text)) == next(
        start for start, b in starts if b.role == "detail"
    )


def test_fetched_details_do_not_fold_recursively_or_duplicate_live_tools():
    state = WorkspaceState()
    view = state.view("s")
    turn(view)
    view.event(dict(type="turn_end", turn_id="t", result={}))
    view.revision = "r"
    state.event(dict(type="turn_detail", session_id="s", revision="r", turn_id="t",
                     events=[dict(type="tool_use", tool_use_id="tool",
                                  tool="command", input={}),
                             dict(type="turn_end", turn_id="t", result={})]))
    text, starts = view.render()
    assert sum(b.role == "detail" for _, b in starts) == 1
    assert "1 个工具调用" in text
    group = next(b for _, b in starts if b.role == "tool_group")
    group.expanded = True
    assert view.render()[0].count("── Tool") == 1
    assert "Enter: show this turn's details" not in text


@pytest.mark.asyncio
async def test_follow_tail_after_append_resize_and_return_from_reading():
    c = client()
    view = c.workspace.view("s")
    view.put(Block("p", "assistant", "line\n" * 100))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(90, 30)) as pilot:
        reader = app.query_one(Transcript)
        await pilot.pause()
        assert reader.scroll_y == reader.max_scroll_y
        view.put(Block("p", "assistant", "new output\n" * 30), append=True)
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == reader.max_scroll_y
        await pilot.resize_terminal(42, 24)
        await pilot.pause()
        assert reader.scroll_y == reader.max_scroll_y
        await pilot.press("g", "g")
        top = reader.scroll_y
        assert not view.follow
        view.put(Block("p", "assistant", "more\n" * 30), append=True)
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == top
        reader.scroll_end(animate=False, force=True)
        reader.on_mouse_scroll_down()
        await pilot.pause()
        assert view.follow
        view.put(Block("p", "assistant", "last\n" * 30), append=True)
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == reader.max_scroll_y


@pytest.mark.asyncio
async def test_selection_at_bottom_follows_without_clearing_selection():
    c = client()
    view = c.workspace.view("s")
    view.put(Block("p", "assistant", "line\n" * 100))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        app.stop_following()
        reader.selection = Selection((95, 0), (95, 4))
        reader.scroll_end(animate=False, force=True)
        reader.on_mouse_up()
        await pilot.pause()
        assert view.follow and reader.selected_text == "line"


@pytest.mark.asyncio
async def test_keyboard_bottom_resumes_and_stale_buffer_callback_is_ignored():
    c = client()
    view = c.workspace.view("s")
    view.put(Block("p", "assistant", "line\n" * 60))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("k")
        app.paint()
        assert view.follow  # Cursor moved, but the viewport remains at bottom.
        await pilot.press("j")
        await pilot.pause()
        assert view.follow
        app.stop_following()
        reader.move_cursor((0, 0))
        app.follow_tail("another-buffer")
        assert reader.cursor_location == (0, 0)


@pytest.mark.asyncio
async def test_local_fold_opens_and_closes_from_body_without_network():
    c = client()
    view = c.workspace.view("s")
    turn(view)
    view.event(dict(type="turn_end", turn_id="t", result={}))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        start = next(i for i, b in app.starts if b.role == "detail")
        app.stop_following()  # As a user cursor/mouse navigation would do.
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        assert not view.local_details["t"].expanded
        app.paint()  # Drive the periodic renderer without depending on its tick.
        await pilot.pause()
        assert "progress explanation" not in reader.text
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert view.local_details["t"].expanded
        assert "progress explanation" in reader.text
        await pilot.press("j", "j", "enter")
        assert not view.local_details["t"].expanded
        app.paint()
        await pilot.pause()
        assert "progress explanation" not in reader.text
        assert not c._outbox


def test_footer_strips_ansi_and_preserves_two_bounded_lines():
    c = client()
    c._line("\x1b[2mtest\x1b[0m")
    assert c.notice == "test"
    text = status_text("READ NORMAL · running · " + "long " * 100
                       + "\nSpace h: help · message\nextra line", width=32)
    lines = text.split("\n")
    assert len(lines) == 2
    assert all(line.cell_len <= 32 for line in lines)


@pytest.mark.asyncio
async def test_narrow_footer_keeps_time_and_moves_shortcuts_to_help():
    c = client()
    view = c.workspace.view("s")
    view.state = "running"
    view.presentation.active = "t"
    view.presentation.turns["t"] = TurnDisplay(
        duration_ms=573000, activity="Hook · postToolUse · " + "command " * 60
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(44, 26)) as pilot:
        assert not list(app.query("#key-hints"))
        text = app.query_one("#status", Static).content
        assert "9m 33s" in text.plain
        assert "queued 0" not in text.plain
        assert all(line.cell_len <= 44 for line in text.split("\n"))
        await pilot.press("space", "h")
        help_text = app.screen.query_one("TextArea").text
        for key in ("Space e", "Space m", "Space p", "Ctrl+v"):
            assert key in help_text
