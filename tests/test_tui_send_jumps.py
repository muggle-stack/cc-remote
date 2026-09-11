"""Draft submission and Vim jump-list regressions, without model calls."""

import json

import pytest

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp, WorkspaceClient
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_state import Block, SessionView


def client():
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    v = c.workspace.view("s")
    v.write_state = "writable"
    v.put(Block("user:t", "user", "question\n" * 80, "t"))
    v.put(Block("answer", "assistant", "answer\n" * 80, "t", "final"))
    return c


def test_typed_tool_replaces_commentary_scaffold_without_dumping_payload():
    v = SessionView(active_turn="t")
    v.put(Block("command", "assistant", "", "t", "commentary", expanded=True))
    v.event(dict(type="tool_use", tool_use_id="command", title="Run command",
                 input={"command": "private long command"}))
    v.event(dict(type="tool_result", tool_use_id="command", content="output"))
    text, _ = v.render()
    assert "1 个工具调用" in text and "succeeded" in text
    assert "── Progress" not in text
    assert "private long command" not in text and "output" not in text
    v.tool_groups["tools:command"].expanded = True
    assert "private long command" in v.render()[0]
    assert "output" in v.render()[0]


@pytest.mark.asyncio
async def test_send_preserves_scrolled_up_viewport_until_explicit_bottom_jump():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.press("g", "g", "ctrl+j", "i", "a", "enter", "b")
        editor = app.query_one(Composer)
        reader = app.query_one(Transcript)
        assert editor.text == "a\nb" and not c._outbox
        await pilot.press("ctrl+s")
        assert not c._outbox
        await pilot.press("escape", "enter")
        await pilot.pause()
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert len(frames) == 1 and frames[0]["prompt"] == "a\nb"
        assert editor.text == "" and editor.vim_mode == "NORMAL"
        assert not c.workspace.view("s").follow
        top = reader.scroll_y
        c.workspace.view("s").put(Block("next", "assistant", "new\n" * 80))
        app.paint()
        await pilot.pause()
        assert reader.scroll_y == top
        await pilot.press("ctrl+k", "G")
        await pilot.pause()
        assert c.workspace.view("s").follow
        assert reader.scroll_y == reader.max_scroll_y


@pytest.mark.asyncio
async def test_rejected_send_preserves_draft_and_reading_position():
    c = client()
    c.workspace.view("s").write_state = "read_only"
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "g", "ctrl+j", "i", "x", "escape", "enter")
        assert app.query_one(Composer).text == "x"
        assert not c._outbox and not c.workspace.view("s").follow


@pytest.mark.asyncio
async def test_jump_list_back_forward_tab_and_branching():
    c = client()
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("k")
        origin = reader.cursor_location
        await pilot.press("g", "g")
        first = reader.cursor_location
        await pilot.press("g", "a")
        answer = reader.cursor_location
        await pilot.press("ctrl+o")
        assert reader.cursor_location == first
        await pilot.press("ctrl+o")
        assert reader.cursor_location == origin
        await pilot.press("ctrl+i")
        assert reader.cursor_location == first
        await pilot.press("tab")
        assert reader.cursor_location == answer
        assert app.focused is reader
        await pilot.press("ctrl+o", "G")
        assert not c.workspace.view("s").jump_forward
        await pilot.press("ctrl+j", "i", "x", "ctrl+o")
        assert app.focused is app.query_one(Composer)
        assert app.query_one(Composer).text == "x"


def test_old_send_and_tab_focus_defaults_are_removed():
    keys = KeyConfig()
    assert not keys.global_keys["send"]
    assert not keys.global_keys["toggle_pane"]
    assert keys.normal_keys["jump_back"] == ("ctrl+o",)
    assert "tab" in keys.normal_keys["jump_forward"]
    assert "Draft Normal" in keys.help()
    assert "Send (Normal)  [draft.send]" in keys.help()


@pytest.mark.asyncio
async def test_send_rekey_clears_the_same_draft_and_keeps_follow(monkeypatch):
    c = client()
    view = c.workspace.view("s")

    async def accepted(text, *, queue=False):
        c.workspace.event(dict(type="session_rekey", old_key="s", session_id="real"))
        c.attached_sid = "real"
        view.follow = True
        return True

    monkeypatch.setattr(c, "submit", accepted)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("g", "g", "ctrl+j", "i", "x", "escape", "enter")
        await pilot.pause()
        assert "s" not in c.workspace.views
        assert c.workspace.view("real") is view and view.draft == ""
        assert app.query_one(Composer).text == ""
        assert view.follow
