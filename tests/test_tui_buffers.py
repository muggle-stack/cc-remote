"""Local tab navigation, picker keys and history cache regression tests."""

import json

import pytest
from textual.widgets import OptionList, Static

from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.tui_app import (
    Composer,
    Transcript,
    WorkspaceApp,
    WorkspaceClient,
    seed_demo,
)
from cc_remote.tui_buffers import BufferPicker, SessionBuffers, tab_line
from cc_remote.tui_modal import ModalEditor


def client():
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", None)
    frames = []

    async def send(raw):
        frames.append(json.loads(raw))
        return True

    c._send_raw = send
    return c, frames


def event(c, kind, **data):
    c._handle({"v": PROTOCOL_VERSION, "type": kind, **data})


def history(c, sid, **data):
    event(c, "history", session_id=sid, turns=[], revision="r", **data)


def reads(frames):
    return [f["session_id"] for f in frames if f["type"] == "get_history"]


def test_order_rekey_dedup_wrap_and_local_close():
    tabs = SessionBuffers()
    for sid in ("a", "tmp-b", "a", "c", "b"):
        tabs.open(sid)
    tabs.rekey("tmp-b", "b")
    assert tabs.ids == ["a", "b", "c"]
    assert tabs.neighbor("a", -1) == "c"
    assert tabs.neighbor("c", 1) == "a"
    assert tabs.close("b") == "c"
    assert tabs.close("c") == "a"
    assert tabs.close("a") is None
    assert tabs.neighbor(None, 1) is None


@pytest.mark.asyncio
async def test_cached_switch_keeps_projection_and_draft_without_history_read():
    c, frames = client()
    await c._attach("a")
    history(c, "a")
    view = c.workspace.view("a")
    view.draft = "keep draft"
    view.anchor = ("answer", 2)
    await c._attach("b")
    history(c, "b")
    event(c, "delta", sid="a", message_id="answer", text="background")
    await c._attach("a")
    assert reads(frames) == ["a", "b"]
    assert c.workspace.view("a") is view
    assert view.draft == "keep draft" and view.anchor == ("answer", 2)
    assert any(b.text == "background" for b in view.blocks)
    assert not any(f["type"] in {"query", "interrupt"} for f in frames)


@pytest.mark.asyncio
async def test_error_and_stale_history_do_not_cache():
    c, frames = client()
    await c._attach("a")
    history(c, "a", error="unavailable", authoritative=False)
    await c._attach("a")
    assert reads(frames) == ["a", "a"]
    event(c, "history_invalidated", session_id="a", revision="new")
    history(c, "a")  # Old page must not undo the invalidation barrier.
    assert "a" not in c.cached_history
    await c._attach("a")
    assert reads(frames) == ["a", "a", "a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["rebuild", "truncated", "generation"])
async def test_replay_gaps_and_new_wrapper_invalidate_cached_tabs(reset):
    c, frames = client()
    await c._attach("a")
    history(c, "a", generation="old")
    await c._attach("b")
    history(c, "b", generation="old")
    if reset == "generation":
        event(c, "snapshot", sid="b", generation="new")
        assert not c.cached_history
    else:
        event(c, "replay_start", sid="a", **{reset: True})
        assert "a" not in c.cached_history
        event(c, "replay_end", sid="a", to_seq=0)
    await c._attach("a")
    assert reads(frames) == ["a", "b", "a"]


@pytest.mark.asyncio
async def test_reconnect_retains_views_but_revalidates_background_tab():
    c, frames = client()
    await c._attach("a")
    history(c, "a")
    view = c.workspace.view("a")
    await c._attach("b")
    history(c, "b")
    # Commands were acknowledged before the connection dropped.
    c._outbox.clear()
    c._outbox_bytes = 0
    await c._recovery_preamble()
    assert not c.cached_history
    assert c.workspace.view("a") is view
    await c._attach("a")
    assert reads(frames)[-1] == "a" and reads(frames).count("a") == 2


def test_rekey_updates_tab_and_cache_identity_without_duplicates():
    c, _ = client()
    c.buffers.open("tmp-a")
    c.buffers.open("a")
    c.cached_history.add("tmp-a")
    c.attached_sid = "tmp-a"
    c.workspace.view("tmp-a").draft = "draft"
    event(c, "session_rekey", old_key="tmp-a", session_id="a")
    assert c.buffers.ids == ["a"]
    assert c.attached_sid == "a"
    assert c.cached_history == {"a"}
    assert c.workspace.view("a").draft == "draft"


@pytest.mark.asyncio
async def test_catalog_eviction_revalidates_but_resident_tabs_stay_cached():
    c, frames = client()
    for sid in ("a", "b"):
        await c._attach(sid)
        history(c, sid)
    event(
        c,
        "session_list",
        engine="codex",
        sessions=[
            {"session_id": "a", "state": None},
            {"session_id": "b", "state": "idle"},
        ],
    )
    assert c.cached_history == {"b"}
    await c._attach("b")
    assert reads(frames) == ["a", "b"]
    await c._attach("a")
    assert reads(frames) == ["a", "b", "a"]


@pytest.mark.asyncio
async def test_tab_keys_picker_search_and_close_preserve_session_and_draft():
    c, frames = client()
    seed_demo(c)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(100, 32)) as pilot:
        assert c.buffers.ids == ["demo-review"]
        assert app.query_one("#session-tabs", Static).size.height == 1
        await pilot.press("ctrl+j", "i", "H", "L", "escape")
        assert app.query_one(Composer).text == "HL"  # Insert is not navigation.
        await pilot.press("ctrl+k", "g", "g", "j", "j")
        reading_cursor = app.query_one(Transcript).cursor_location
        await app.attach_session("demo-codex-work")
        await pilot.press("H")
        assert c.attached_sid == "demo-review"
        assert app.query_one(Composer).text == "HL"
        assert app.query_one(Transcript).cursor_location == reading_cursor
        await pilot.press("L")
        assert c.attached_sid == "demo-codex-work"
        await pilot.press("space", "comma")
        assert isinstance(app.screen, BufferPicker)
        search = app.screen.query_one(ModalEditor)
        assert search.vim_mode == "INSERT" and app.focused is search
        listing = app.screen.query_one(OptionList)
        assert listing.option_count == 2  # Not all demo catalog sessions.
        await pilot.press("ctrl+j")
        assert listing.highlighted == 1
        await pilot.press("ctrl+k")
        assert listing.highlighted == 0
        await pilot.press("down", "up")
        assert listing.highlighted == 0
        await pilot.press(*"demo-review", "enter")
        assert len(app.screen_stack) == 1
        assert c.attached_sid == "demo-review"
        assert app.query_one(Transcript).vim_mode == "NORMAL"
        await pilot.press("space", "b", "d")
        assert c.buffers.ids == ["demo-codex-work"]
        assert c.attached_sid == "demo-codex-work"
        assert c.workspace.view("demo-review").draft == "HL"
        assert c.workspace.view("demo-review").state == "running"
        await pilot.press("space", "b", "d")
        assert c.attached_sid is None and c.buffers.ids == []
        app.paint()
        assert not c.buffers.ids
        await pilot.press("space", "comma", *"missing", "enter")
        assert isinstance(app.screen, BufferPicker)
        assert app.screen.query_one(OptionList).option_count == 0
        assert not frames and not c._outbox


@pytest.mark.asyncio
async def test_close_running_tab_only_sends_neighbor_focus_not_mutations():
    c, frames = client()
    seed_demo(c)
    c.demo = False
    c.buffers.open("demo-codex-work")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        current = c.workspace.view("demo-review")
        current.queue = [{"msg_id": "queued", "preview": "keep queued"}]
        await pilot.press("space", "b", "d")
        assert c.attached_sid == "demo-codex-work"
        assert current.state == "running"
        assert current.queue[0]["msg_id"] == "queued"
        assert {f["type"] for f in frames} == {
            "switch_session",
            "get_history",
        }
        frames.clear()
        await pilot.press("space", "b", "d")
        assert not frames  # Last-tab close is entirely local.


@pytest.mark.asyncio
async def test_rekey_keeps_visible_draft_without_reopening_temporary_tab():
    c, _ = client()
    seed_demo(c)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        editor = app.query_one(Composer)
        editor.load_text("draft not yet painted")
        event(c, "session_rekey", old_key="demo-review", session_id="new-id")
        app.paint()
        await pilot.pause()
        assert c.buffers.ids == ["new-id"]
        assert "demo-review" not in c.workspace.views
        assert editor.text == "draft not yet painted"
        assert c.workspace.view("new-id").draft == editor.text


def test_many_tabs_keep_active_visible_and_titles_safe():
    c, _ = client()
    for n in range(50):
        sid = f"s{n}"
        c.buffers.open(sid)
        c.workspace.catalog[sid] = {"summary": f"项目{n}\n[bold]"}
    c.attached_sid = "s49"
    line = tab_line(c, 80)
    assert "项目49" in line.plain and "\n" not in line.plain
    assert any(span.style == "bold white on #334466" for span in line.spans)


def tab_client(titles, active=0):
    c, _ = client()
    for index, title in enumerate(titles):
        sid = f"tab-{index}"
        c.buffers.open(sid)
        c.workspace.catalog[sid] = {"summary": title}
    c.attached_sid = f"tab-{active}"
    return c


def test_short_tabs_use_available_cells_not_a_fixed_slot_count():
    titles = ["cc-remote", "airmux", "opensbi ipi", "mail", "codeg", "notes"]
    c = tab_client(titles, active=1)
    line = tab_line(c, 100)
    assert all(title in line.plain for title in titles)
    assert line.cell_len <= 100
    assert "‹" not in line.plain and "›" not in line.plain


@pytest.mark.parametrize("width", [0, 1, 5, 8, 20, 40, 80, 120, 200])
@pytest.mark.parametrize("active", [0, 7, 11])
def test_tab_width_accounts_for_unicode_indexes_status_and_overflow(width, active):
    c = tab_client([f"项目{i} · cafe\u0301 · 🐟" for i in range(12)], active)
    for sid in c.buffers.ids:
        c.workspace.view(sid).state = "running"
    line = tab_line(c, width)
    assert line.cell_len <= width
    if width:
        assert (line.style == "bold white on #334466"
                or any(s.style == "bold white on #334466" for s in line.spans))
    if width >= 40:
        assert f" {active + 1} 项目{active}" in line.plain
        assert " ●" in line.plain


def test_long_titles_are_ellipsized_and_neighbor_windows_stay_contiguous():
    c = tab_client([f"session-{i}-" + "long" * 50 for i in range(8)], active=4)
    line = tab_line(c, 100)
    assert "…" in line.plain and " 5 session-4-" in line.plain
    assert line.plain.startswith("‹ ") and line.plain.endswith(" ›")
    assert line.cell_len <= 100
    narrow = tab_line(c, 16)
    assert " 5 " in narrow.plain and narrow.cell_len <= 16
    assert narrow.plain.startswith("‹ ") and narrow.plain.endswith(" ›")


@pytest.mark.asyncio
async def test_tab_bar_reflows_after_terminal_resize_without_navigation():
    c, frames = client()
    seed_demo(c)
    for i in range(10):
        sid = f"short-{i}"
        c.buffers.open(sid)
        c.workspace.catalog[sid] = {"summary": f"tab{i}"}
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(60, 30)) as pilot:
        tabs = app.query_one("#session-tabs", Static)
        narrow = tabs.content.plain
        await pilot.resize_terminal(160, 30)
        await pilot.pause()
        app.paint()
        wide = tabs.content.plain
        assert wide.count("│") > narrow.count("│")
        assert all(f"tab{i}" in wide for i in range(10))
        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        app.paint()
        assert tabs.content.cell_len <= 40
        assert any(s.style == "bold white on #334466" for s in tabs.content.spans)
        assert c.attached_sid == "demo-review"
        assert not frames and not c._outbox
