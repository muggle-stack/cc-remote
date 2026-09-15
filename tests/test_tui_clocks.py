"""Elapsed time must advance without detail fetches or transcript reloads."""

import pytest
from textual.widgets.text_area import Selection

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, Transcript
from cc_remote.tui_state import Block
from cc_remote.tui_presentation import TurnDisplay


@pytest.mark.asyncio
@pytest.mark.parametrize("expanded", [False, True])
async def test_elapsed_ticks_without_events_fetches_or_document_reload(
    monkeypatch, expanded
):
    now = [1009.0]
    monkeypatch.setattr("cc_remote.tui_app.time.time", lambda: now[0])
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    view = client.workspace.view("s")
    for tid in ("first", "second"):
        view.presentation.turns[tid] = TurnDisplay(started=1000)
        view.put(
            Block(tid, "detail", "body text\nmore text", tid, expanded=expanded)
        )
    app = WorkspaceApp(client, connect=False)
    async with app.run_test():
        reader = app.query_one(Transcript)
        assert reader.text.count("Processing · 9s") == 2
        app.stop_following()
        row, _ = reader.document.get_location_from_index(app.starts[1][0])
        reader.selection = Selection((row + 1, 1), (row + 1, 5))
        chosen, selection = reader.selected_text, reader.selection
        app.remember()
        original_render = view.render
        reloads = []
        original_load = reader.load_text

        def load(text):
            reloads.append(text)
            original_load(text)

        monkeypatch.setattr(reader, "load_text", load)
        for value in (1010.0, 1011.0, 1060.0, 1121.0):
            now[0] = value
            app.paint()
            expected, starts = original_render()
            assert reader.text == expected
            assert app.starts == starts
            assert reader.selection == selection
            assert reader.selected_text == chosen
        assert "Processing · 2m 1s" in reader.text
        assert not reloads and not client._outbox
        assert not reader.history.undo_stack
        # The next real data update uses the rebased block anchors, too.
        view.version += 1
        app.paint()
        assert reader.selected_text == chosen


@pytest.mark.asyncio
async def test_clock_stops_at_terminal_and_only_ticks_once_per_second(
    monkeypatch,
):
    now = [1010.0]
    monkeypatch.setattr("cc_remote.tui_app.time.time", lambda: now[0])
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    view = client.workspace.view("s")
    turn = TurnDisplay(started=1000)
    view.presentation.turns["turn"] = turn
    view.put(Block("d", "detail", "body", "turn"))
    app = WorkspaceApp(client, connect=False)
    async with app.run_test():
        reader = app.query_one(Transcript)
        calls = []
        original = view.block_header

        def header(*args, **kwargs):
            calls.append(kwargs.get("now"))
            return original(*args, **kwargs)

        monkeypatch.setattr(view, "block_header", header)
        for value in (1011.0, 1011.1, 1011.9):
            now[0] = value
            app.paint()
        assert len(calls) == 1
        assert "Processing · 11s" in reader.text
        turn.status, turn.ended = "completed", 1012.0
        view.version += 1
        app.paint()
        assert "completed · 12s" in reader.text
        calls.clear()
        now[0] = 9999.0
        app.paint()
        assert "completed · 12s" in reader.text
        assert not calls


@pytest.mark.asyncio
async def test_timer_callback_refreshes_without_manual_paint(monkeypatch):
    now = [1001.0]
    monkeypatch.setattr("cc_remote.tui_app.time.time", lambda: now[0])
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    view = client.workspace.view("s")
    view.presentation.turns["turn"] = TurnDisplay(started=1000)
    view.put(Block("d", "detail", "body", "turn"))
    app = WorkspaceApp(client, connect=False)
    async with app.run_test() as pilot:
        now[0] = 1012.0
        await pilot.pause(0.2)
        assert "Processing · 12s" in app.query_one(Transcript).text
        assert not client._outbox
