"""Scoped startup, navigation races and configurable keyboard regressions."""

import asyncio
import json

import pytest
from textual.widgets import TextArea

from cc_remote.protocol import ListSessions, PROTOCOL_VERSION
from cc_remote.tui_app import WorkspaceApp, WorkspaceClient, seed_demo, Composer
from cc_remote.tui_keys import KeyConfig, load_keys
from cc_remote.tui_navigation import activity, scoped_catalog, select_session
from cc_remote.tui_tree import SessionExplorer, SessionTree


def client(sid=None):
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", sid)
    frames = []

    async def send(raw):
        frames.append(json.loads(raw))
        return True

    c._send_raw = send
    return c, frames


async def catalog(c, rows, engine="codex", space="code", *, request=True):
    if request:
        await c._send(ListSessions(engine=engine, space=space))
    c._handle(
        {
            "v": PROTOCOL_VERSION,
            "type": "session_list",
            "engine": engine,
            "space": space,
            "request_id": c.session_catalog_requests.get((engine, space)),
            "sessions": rows,
        }
    )
    await c._flush_history_refreshes()


def row(sid, modified="1", **extra):
    return {"session_id": sid, "last_modified": modified, **extra}


def test_activity_matches_web_seconds_milliseconds_iso_and_invalid():
    expected = 1_700_000_000_000
    for value in ("1700000000", "1700000000000", "2023-11-14T22:13:20Z"):
        assert activity(row("s", value)) == expected
    for value in (None, "invalid", "NaN", "Infinity", ""):
        assert activity(row("s", value)) == -float("inf")


def test_selection_matches_web_order_archives_and_remembered_focus():
    rows = {
        "old": row("old", "1"),
        "new": row("new", "4"),
        "archive": row("archive", "6", tag="archived"),
        "other": row("other", "10", space="work"),
    }
    scoped = scoped_catalog(rows, "codex", "code")
    assert list(scoped) == ["archive", "new", "old"]
    assert select_session(scoped, None) == "new"
    assert select_session(scoped, "old") == "old"
    assert select_session(scoped, "archive") == "archive"
    assert select_session(scoped, "deleted") == "new"
    assert select_session({}, None) is None


@pytest.mark.asyncio
async def test_startup_waits_for_selected_scope_not_background_snapshot():
    c, frames = client()
    c._handle({"v": PROTOCOL_VERSION, "type": "snapshot", "sid": "background"})
    await catalog(c, [row("claude-new", "99")], "claude")
    await catalog(c, [row("work-new", "99")], space="work")
    assert c.attached_sid is None
    await catalog(
        c, [row("old"), row("new", "3"), row("hidden", "9", tag="archived")]
    )
    assert c.attached_sid == "new"
    assert [
        f["session_id"] for f in frames if f["type"] == "switch_session"
    ] == ["new"]
    assert not any(
        f["type"] in {"query", "steer", "new_session"} for f in frames
    )
    await catalog(c, [row("old"), row("new", "3"), row("newer", "10")])
    assert c.attached_sid == "new"  # Refresh never steals focus.


@pytest.mark.asyncio
async def test_explicit_session_wins_over_initial_auto_selection():
    c, _ = client("explicit")
    await catalog(c, [row("newest", "10")])
    assert c.attached_sid == "explicit"


@pytest.mark.asyncio
async def test_all_four_scopes_restore_independent_focus_and_drafts():
    c, frames = client()
    await catalog(c, [row("code-old"), row("code-new", "3")])
    await c._attach("code-old")
    c.workspace.view("code-old").draft = "keep code draft"
    for engine, space in (
        ("codex", "work"),
        ("claude", "work"),
        ("claude", "code"),
    ):
        await c.switch_surface(engine, space)
        assert c.attached_sid is None
        sid = engine + space
        await catalog(c, [row(sid)], engine, space)
        assert c.attached_sid == sid
        assert set(c.visible_catalog()) == {sid}
        switch = [f for f in frames if f["type"] == "switch_session"][-1]
        assert (switch["engine"], switch["space"]) == (engine, space)
    await c.switch_surface("codex", "code")
    await catalog(c, [row("code-old"), row("code-new", "10")])
    assert c.attached_sid == "code-old"
    assert c.workspace.view("code-old").draft == "keep code draft"


@pytest.mark.asyncio
async def test_missing_bookmark_and_empty_surface_are_safe():
    c, frames = client()
    c.last_focus["codex", "code"] = "deleted"
    await catalog(c, [row("valid")])
    assert c.attached_sid == "valid"
    await c.switch_surface("codex", "work")
    count = len([f for f in frames if f["type"] == "switch_session"])
    await catalog(c, [], space="work")
    assert c.attached_sid is None
    assert "No Codex / Work sessions" in c.notice
    assert len([f for f in frames if f["type"] == "switch_session"]) == count
    assert not await c.submit("must not target old Code session")


@pytest.mark.asyncio
async def test_stale_catalog_and_switch_confirmation_cannot_steal_focus():
    c, _ = client()
    await c._send(ListSessions(engine="codex"))
    old_request = c.session_catalog_requests["codex", "code"]
    await c.switch_surface("codex", "work")
    await c.switch_surface("codex", "code")
    c._handle(
        {
            "type": "session_list",
            "v": PROTOCOL_VERSION,
            "engine": "codex",
            "request_id": old_request,
            "sessions": [row("stale")],
        }
    )
    await c._flush_history_refreshes()
    assert c.attached_sid is None
    assert "stale" not in c.workspace.catalog
    await catalog(c, [row("current")], request=False)
    c._handle(
        {"v": PROTOCOL_VERSION, "type": "session_focus", "session_id": "stale"}
    )
    assert c.attached_sid == "current"


@pytest.mark.asyncio
async def test_failed_old_attach_does_not_undo_newer_surface_choice():
    c, _ = client("initial")
    waiting, release = asyncio.Event(), asyncio.Event()

    async def send(message):
        if message.type == "switch_session":
            waiting.set()
            await release.wait()
            return False
        return True

    c._send = send
    task = asyncio.create_task(c._attach("old"))
    await waiting.wait()
    await c.switch_surface("claude", "work")
    release.set()
    await task
    assert c.scope == ("claude", "work") and c.attached_sid is None


@pytest.mark.asyncio
async def test_new_session_command_uses_current_engine_and_space():
    c, frames = client()
    await c.switch_surface("claude", "work")
    await c._command("/new")
    new = [f for f in frames if f["type"] == "new_session"][-1]
    assert (new["engine"], new["space"]) == ("claude", "work")
    assert new["cwd"] is None
    with pytest.raises(ValueError, match="without a path"):
        await c._command("/new /example/project")


@pytest.mark.parametrize(
    "data",
    [
        {"unknown": {}},
        {"keys": {"unknown": []}},
        {"normal": []},
        {"keys": {"sessions": "ctrl+p"}},
        {"keys": {"sessions": ["p"]}},
        {"keys": {"sessions": ["ctrl+q"]}},
        {"normal": {"help": ["space"]}},
        {"normal": {"help": ["made_up_key"]}},
    ],
)
def test_bad_or_conflicting_config_fails_closed(data):
    with pytest.raises(ValueError):
        KeyConfig(data)


def test_config_loading_precedence_and_disabled_bindings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CC_REMOTE_TUI_CONFIG", raising=False)
    assert load_keys().label("sessions") == "Space e"
    default = tmp_path / "cc-remote" / "tui.toml"
    default.parent.mkdir()
    default.write_text('[keys]\nsessions = ["ctrl+o"]\n'
                       '[normal]\njump_back = []\n')
    assert load_keys().label("sessions") == "Ctrl+o"
    override = tmp_path / "override.toml"
    override.write_text("[keys]\nsessions = []\n")
    monkeypatch.setenv("CC_REMOTE_TUI_CONFIG", str(override))
    assert load_keys().label("sessions") == "Space e"
    assert load_keys().global_keys["sessions"] == ()
    assert load_keys(str(default)).label("sessions") == "Ctrl+o"
    with pytest.raises(ValueError, match="not found"):
        load_keys(str(tmp_path / "absent"))


def test_documented_example_loads_and_matches_defaults():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "docs/tui-keys.example.toml"
    assert load_keys(str(path)).help() == KeyConfig().help()


@pytest.mark.asyncio
async def test_demo_scoped_picker_leaders_and_draft_preservation():
    c, frames = client()
    seed_demo(c)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "k", "e", "e", "p", "escape")
        await pilot.press("space", "w")  # Works in draft Normal, too.
        assert c.scope == ("codex", "work")
        assert c.attached_sid == "demo-codex-work"
        assert app.query_one(Composer).text == ""
        await pilot.press("space", "e")
        assert app.query_one(SessionExplorer).display
        assert len(app.query_one(SessionTree).root.children) == 1
        await pilot.press("escape", "space", "c")
        assert c.scope == ("claude", "work")
        await pilot.press("space", "c", "space", "w")
        assert c.attached_sid == "demo-review"
        assert app.query_one(Composer).text == "keep"
        assert not frames and not c._outbox


@pytest.mark.asyncio
async def test_custom_shortcuts_apply_to_help_picker_and_not_insert_text():
    c, _ = client()
    c.keys = KeyConfig(
        {
            "keys": {
                "sessions": ["ctrl+o"],
                "focus_draft": ["ctrl+n"],
                "focus_read": ["ctrl+b"],
            },
            "normal": {"help": ["space z"], "space": ["space m"], "model": [],
                       "jump_back": []},
        }
    )
    seed_demo(c)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        assert len(app.screen_stack) == 1
        await pilot.press("ctrl+o")
        tree = app.query_one(SessionTree)
        start = tree.cursor_line
        await pilot.press("ctrl+n")
        assert tree.cursor_line == start + 1
        await pilot.press("ctrl+b")
        assert tree.cursor_line == start
        await pilot.press("escape", "space", "h")
        assert len(app.screen_stack) == 1  # Rebinding removed the old shortcut.
        await pilot.press("space", "z")
        help_text = app.screen.query_one(TextArea).text
        assert "Ctrl+o" in help_text and "Space z" in help_text
        assert "F1" not in help_text
        await pilot.press("escape", "ctrl+n", "i", "space", "m")
        assert app.query_one(Composer).text == " m"
        assert c.space == "code"
        await pilot.press("escape", "space", "m")
        assert c.space == "work"
    assert all(
        not binding.key.startswith("f") for binding in KeyConfig().bindings()
    )


@pytest.mark.asyncio
async def test_global_navigation_cancels_partial_leader_chord():
    c, _ = client()
    seed_demo(c)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("space", "ctrl+j", "escape", "w")
        assert c.space == "code"
        await pilot.press("space", "ctrl+j", "w")
        assert c.space == "code"
