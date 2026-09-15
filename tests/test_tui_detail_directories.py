"""Scoped menus, fold ownership, fzf directory picking and folder labels."""

import asyncio
import json
import shutil

import pytest
from textual.widgets import OptionList

from cc_remote.tui_app import (
    WorkspaceApp,
    WorkspaceClient,
    Transcript,
    location,
)
from cc_remote.tui_panels import ActionPicker, PANEL_ACTIONS, ActionForm
from cc_remote.tui_directories import DirectoryPicker, fuzzy_directories
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_state import Block
from cc_remote.tui_settings import SettingsForm
from cc_remote.tui_tree import SessionTree, SessionExplorer, TreeSearch


def setup():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    client.workspace.catalog["s"] = {
        "session_id": "s",
        "cwd": "/a/project",
        "engine": "codex",
        "space": "code",
    }
    return WorkspaceApp(client, connect=False), client


def detail_event():
    return {
        "type": "turn_detail",
        "session_id": "s",
        "turn_id": "u",
        "revision": "r",
        "has_more": True,
        "oldest_cursor": "cursor",
        "events": [
            {
                "type": "delta",
                "message_id": "a",
                "channel": "commentary",
                "text": "\n".join(f"detail line {n}" for n in range(40)),
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["enter", "escape"])
@pytest.mark.parametrize("role", ["detail", "process", "tool", "thinking"])
async def test_fold_from_any_body_line_keeps_owner_header(key, role):
    app, c = setup()
    view = c.workspace.view("s")
    view.revision = "r"
    if role == "detail":
        c.workspace.event(detail_event())
        view.render()
        block = view.local_details["u"]
    else:
        block = Block(
            "b",
            "assistant" if role == "thinking" else role,
            "\n".join(f"detail line {n}" for n in range(40)),
            "u",
            channel="thinking" if role == "thinking" else "unknown",
            expanded=True,
        )
        view.put(block)
        view.render()
        block = view.tool_groups["tools:b"]
        block.expanded = True
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        reader.move_cursor(
            location(reader.text, reader.text.index("detail line 25"))
        )
        await pilot.press(key)
        app.paint()
        assert not block.expanded
        owner = next(i for i, b in app.starts if b.id == block.id)
        assert reader.cursor_location == location(reader.text, owner)
        assert "detail line 25" not in reader.text
        assert not c._outbox
        if role == "detail":
            # A page already in flight cannot reopen a deliberately folded block.
            c.workspace.event(detail_event())
            assert not block.expanded
        await pilot.press("enter")
        app.paint()
        assert block.expanded and "detail line 25" in reader.text


@pytest.mark.asyncio
async def test_detail_older_page_is_separate_from_enter_and_escape():
    app, c = setup()
    view = c.workspace.view("s")
    view.revision = "r"
    c.workspace.event(detail_event())
    async with app.run_test() as pilot:
        await pilot.press("o")
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert frames[-1]["type"] == "get_turn_detail"
        assert frames[-1]["before"] == "cursor"
        await pilot.press("escape")
        assert not view.local_details["u"].expanded


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_key", ["O", "ctrl+y"])
async def test_detail_paging_returns_to_newest_without_reopening_fold(newer_key):
    app, c = setup()
    c.keys = KeyConfig({"reader": {"newer": [newer_key]}})
    view = c.workspace.view("s")
    view.revision = "r"
    c.workspace.event(detail_event())
    async with app.run_test() as pilot:
        await pilot.press("o")
        older = detail_event() | {
            "has_more": False, "oldest_cursor": None,
            "has_newer": True, "newer_cursor": "toward-newest",
            "events": [{"type": "delta", "message_id": "old",
                        "channel": "commentary", "text": "older body"}],
        }
        c.workspace.event(older)
        app.paint()
        reader = app.query_one(Transcript)
        reader.move_cursor(location(reader.text, reader.text.index("older body")))
        assert c.keys.layer_label("reader", "newer") + ": load newer" in reader.text
        await pilot.press(newer_key)
        frames = [json.loads(raw) for raw, _ in c._outbox.values()]
        assert frames[-1]["type"] == "get_turn_detail"
        assert frames[-1]["before"] == "toward-newest"
        c.workspace.event(detail_event())
        app.paint()
        assert "detail line 39" in reader.text
        assert "older body" not in reader.text
        assert not view.details_newer["u"]


@pytest.mark.parametrize("invalidate", ["turn_detail", "history_invalidated"])
def test_detail_newer_cursor_is_invalidated_with_its_page(invalidate):
    _, c = setup()
    view = c.workspace.view("s")
    view.revision = "r"
    c.workspace.event(detail_event() | {
        "has_newer": True, "newer_cursor": "newer",
    })
    c.workspace.event({
        "type": invalidate, "sid": "s", "session_id": "s",
        "turn_id": "u", "revision": "r", "reset_required": True,
    })
    assert not view.details_newer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "panel",
    ["Goal / Plan", "Usage / Context", "Reports", "Background", "Status"],
)
async def test_panel_actions_stay_scoped_even_when_searching(panel):
    app, c = setup()
    async with app.run_test() as pilot:
        await app.action_panel(panel)
        await pilot.press("a")
        picker = app.screen
        assert isinstance(picker, ActionPicker)
        assert picker.allowed_actions == PANEL_ACTIONS[panel]
        names = {option.id for option in picker.query_one(OptionList)._options}
        assert names == PANEL_ACTIONS[panel]
        await pilot.press(*"new session")
        assert not picker.query_one(OptionList).option_count
        picker.open_form("new_session")
        assert app.screen is picker


@pytest.mark.asyncio
async def test_goal_edit_i_enters_insert_but_old_e_is_not_an_action():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "g", "e")
        assert not isinstance(app.screen, ActionForm)
        await pilot.press("i")
        assert isinstance(app.screen, ActionForm)
        assert app.screen.query_one(ModalEditor).vim_mode == "INSERT"
        await pilot.press("escape")
        assert app.screen.query_one(ModalEditor).vim_mode == "NORMAL"


@pytest.mark.asyncio
async def test_tree_basename_labels_keep_distinct_paths_and_search():
    app, c = setup()
    c.workspace.catalog["t"] = {
        "session_id": "t",
        "cwd": "/b/project",
        "engine": "codex",
        "space": "code",
    }
    async with app.run_test() as pilot:
        await pilot.press("space", "e")
        tree = app.query_one(SessionTree)
        assert [node.label.plain for node in tree.root.children] == [
            "project",
            "project",
        ]
        assert {node.data[1] for node in tree.root.children} == {
            "/a/project",
            "/b/project",
        }
        await pilot.press("slash")
        app.query_one(TreeSearch).value = "/b/"
        app.query_one(SessionExplorer).refresh_catalog()
        assert len(tree.root.children) == 1
        assert tree.root.children[0].data == ("folder", "/b/project")


@pytest.mark.asyncio
@pytest.mark.skipif(
    shutil.which("fzf") is None, reason="optional fzf CLI not installed"
)
async def test_real_fzf_unicode_spaces_and_shell_options_are_not_executed(
    monkeypatch, tmp_path
):
    marker = tmp_path / "unexpected"
    monkeypatch.setenv(
        "FZF_DEFAULT_OPTS", f"--bind=start:execute(touch {marker})"
    )
    monkeypatch.setenv("FZF_DEFAULT_COMMAND", f"touch {marker}")
    paths = ["/projects/cc-remote", "/projects/kernel", "/目录/hello world"]
    assert await fuzzy_directories(paths, "ccrmt") == [paths[0]]
    assert await fuzzy_directories(paths, "目录 world") == [paths[2]]
    assert (
        await fuzzy_directories(paths, "--bind=start:execute(echo bad)") == []
    )
    assert not marker.exists()


@pytest.mark.asyncio
async def test_directory_rpc_correlation_cache_and_cancellation():
    _, c = setup()
    task = asyncio.create_task(c.list_directories("~"))
    await asyncio.sleep(0)
    request = next(iter(c.directory_waiters))
    c._on_event(
        {
            "type": "dir_list",
            "request_id": "foreign",
            "path": "/wrong",
            "dirs": [],
        }
    )
    assert not task.done()
    c._on_event(
        {
            "type": "dir_list",
            "request_id": request,
            "path": "/home/example",
            "dirs": [],
        }
    )
    assert (await task)["path"] == "/home/example"
    count = len(c._outbox)
    assert (await c.list_directories("~"))["path"] == "/home/example"
    assert len(c._outbox) == count
    task = asyncio.create_task(c.list_directories("/other"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not c.directory_waiters


@pytest.mark.asyncio
async def test_directory_picker_search_browse_select_and_no_model_turn(
    monkeypatch,
):
    app, c = setup()
    reads = []

    async def directories(path, **kwargs):
        reads.append(path)
        base = "/home/test" if path == "~" else path
        return {
            "path": base,
            "parent": "/home/test" if path != "~" else "/home",
            "dirs": ([{"path": base + "/repo"}, {"path": base + "/code"}]
                     if base == "/home/test" else []),
        }

    async def rank(paths, query):
        return [path for path in paths if query in path]

    monkeypatch.setattr(c, "list_directories", directories)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test() as pilot:
        await pilot.press("space", "enter")
        form = app.screen
        assert isinstance(form, SettingsForm) and form.values["cwd"] == "~"
        await pilot.press("i")
        assert isinstance(app.screen, DirectoryPicker)
        await pilot.pause(0.12)
        assert app.screen.query_one(ModalEditor).vim_mode == "INSERT"
        assert app.focused is app.screen.query_one(ModalEditor)
        await pilot.press(*"repo")
        await pilot.pause(0.12)
        assert app.screen.query_one(ModalEditor).text == "repo"
        await pilot.press("ctrl+right")
        await pilot.pause(0.12)
        assert app.screen.path == "/home/test/repo"
        await pilot.press("ctrl+left")
        await pilot.pause(0.12)
        assert app.screen.path == "/home/test"
        await pilot.press(*"code")
        await pilot.pause(0.12)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is form and form.values["cwd"] == "/home/test/code"
        assert (
            reads[-1] == "/home/test/code"
        )  # Verify even a known-directory shortcut.
        assert all(
            json.loads(raw)["type"] in {"get_models", "get_permission_profiles"}
            for raw, _ in c._outbox.values()
        )


@pytest.mark.asyncio
async def test_directory_filter_never_selects_stale_match_or_starts_command(
    monkeypatch,
):
    app, c = setup()

    async def directories(path, **kwargs):
        return {"path": "/home/test", "dirs": []}

    async def rank(paths, query):
        if query:
            await asyncio.sleep(0.5)
        return paths

    monkeypatch.setattr(c, "list_directories", directories)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test() as pilot:
        app.push_screen(DirectoryPicker(c, "~"))
        await pilot.pause(0.12)
        screen = app.screen
        screen.query_one(ModalEditor).load_text("changed")
        assert screen.selected_path() is None
        await pilot.press("enter")
        assert app.screen is screen
        await pilot.press("escape")
        assert len(app.screen_stack) == 1
        assert not c._outbox


@pytest.mark.asyncio
async def test_missing_fzf_is_an_explicit_non_mutating_error(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="install fzf"):
        await fuzzy_directories(["/tmp"], "tmp")
