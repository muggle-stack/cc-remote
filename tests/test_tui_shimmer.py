"""Activity animation must remain paint-only and follow real turn state."""

from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text
from textual.widgets.text_area import Selection

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp, WorkspaceClient
from cc_remote.tui_chrome import activity_sweep
from cc_remote.tui_state import Block


def test_sweep_moves_across_cjk_cells_without_changing_text_or_background():
    value = "[正在处理 1m 24s · Read ×2]"
    console = Console(color_system="truecolor")
    peaks = []
    for phase in (0.25, 0.5, 0.75):
        text = Text(value, style="on #123456")
        activity_sweep(text, 1, len(value) - 1, phase,
                       Style(color="#707070", bgcolor="#000000"),
                       Style(color="#f0f0f0", bgcolor="#ffffff"))
        assert text.plain == value
        styles = [text.get_style_at_offset(console, i) for i in range(len(text))]
        assert all(s.bgcolor.name == "#123456" for s in styles)
        assert styles[0].color is None and styles[-1].color is None
        levels = [s.color.get_truecolor().red for s in styles[1:-1]]
        peaks.append(levels.index(max(levels)))
    assert peaks[0] < peaks[1] < peaks[2]


def active_client():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    view = client.workspace.view("s")
    view.event(dict(type="user_msg", msg_id="t", prompt="Check the project", ts=1000))
    view.event(dict(type="delta", message_id="progress", turn_id="t",
                    channel="commentary", text="Reading project files.", ts=1001))
    view.event(dict(type="tool_use", tool_use_id="read", turn_id="t",
                    tool="Read", input={"file_path": "README.md"}, ts=1001))
    app = WorkspaceApp(client, connect=False)
    app.animation_level = "full"
    app.no_color = False
    return client, view, app


def visible_activity(reader):
    first = int(reader.scroll_y)
    rows = reader.wrapped_document._offset_to_line_info[
        first:first + reader.scrollable_content_region.height
    ]
    return tuple(
        tuple(reader.render_line(y)) for y, (row, _) in enumerate(rows)
        if row in reader.activity_ranges
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [44, 100])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_timer_sweeps_visible_rows_without_replacing_or_moving_text(monkeypatch, width, theme):
    clock = [0.3]
    monkeypatch.setattr("cc_remote.tui_app.time", SimpleNamespace(
        time=lambda: 1001, monotonic=lambda: clock[0],
    ))
    client, view, app = active_client()
    app.theme = theme
    async with app.run_test(size=(width, 35)) as pilot:
        reader = app.query_one(Transcript)
        app.query_one(Composer).focus()
        app.stop_following()
        row = reader.text[:reader.text.index("Reading project files.")].count("\n")
        reader.selection = Selection((row, 0), (row, 7))
        reader.scroll_home(animate=False, immediate=True)
        app.remember()
        await pilot.pause()
        initial = visible_activity(reader)
        assert initial
        user_style = reader.get_line(0).get_style_at_offset(app.console, 14)
        assert user_style.bgcolor.name == "#202a36"
        assert min(user_style.color.get_truecolor()) >= 160
        retained = (reader.text, reader.selection, reader.selected_text, reader.scroll_offset)
        mutations = []
        monkeypatch.setattr(reader, "load_text", lambda *_: mutations.append("load"))
        monkeypatch.setattr(reader, "replace", lambda *_, **__: mutations.append("replace"))
        clock[0] = 1.4
        await pilot.pause(0.2)
        assert visible_activity(reader) != initial
        assert (reader.text, reader.selection, reader.selected_text, reader.scroll_offset) == retained
        assert not mutations and not client._outbox and not reader.history.undo_stack
        assert view.presentation.turns["t"].status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype,is_error", [
    ("success", False), ("error_during_execution", True), ("error", True),
])
async def test_terminal_stops_sweep_even_with_stale_running_tool(subtype, is_error):
    client, view, app = active_client()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        assert reader.activity_ranges
        view.event(dict(type="turn_end", turn_id="t", ts=1002,
                        result={"subtype": subtype, "is_error": is_error}))
        app.paint()
        await pilot.pause()
        assert not reader.activity_ranges
        assert reader.activity_frame is None
        assert view.presentation.turns["t"].status != "running"
        assert not client._outbox


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["animation_level", "no_color"])
async def test_disabled_animation_clears_paint_and_does_not_refresh(monkeypatch, setting):
    client, _, app = active_client()
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        assert reader.activity_frame is not None
        setattr(app, setting, "none" if setting == "animation_level" else True)
        reader.refresh_activity()
        assert reader.activity_frame is None
        await pilot.pause()
        refreshes = []
        monkeypatch.setattr(reader, "refresh_lines", lambda *args: refreshes.append(args))
        reader.refresh_activity()
        assert not refreshes


@pytest.mark.asyncio
async def test_offscreen_activity_and_session_switch_do_not_animate_unrelated_text(monkeypatch):
    client, view, app = active_client()
    view.put(Block("answer", "assistant", "Processing is a word.\n" * 80, "t", "final"))
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert not visible_activity(reader)
        refreshes = []
        monkeypatch.setattr(reader, "refresh_lines", lambda *args: refreshes.append(args))
        reader.activity_frame = -1
        reader.refresh_activity()
        assert not refreshes
        client.workspace.view("other").put(Block("other", "assistant", "Processing is not a status."))
        client.attached_sid = "other"
        app.paint()
        assert not reader.activity_ranges
        assert not client._outbox
