"""Configuration, dispatch, help and inspection share one shortcut registry."""

import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest
from textual.widgets import OptionList, TextArea

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp, WorkspaceClient
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import ShortcutPicker
from cc_remote.tui_preview import PreviewRef
from cc_remote.tui_preview_views import FileHints
from cc_remote.tui_settings import TextValue, ValuePicker
from cc_remote.tui_state import Block
from cc_remote.tui_tree import SessionExplorer, SessionTree, TreeSearch


def make_app(config=None):
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    c.keys = KeyConfig(config)
    c.workspace.catalog["s"] = dict(
        session_id="s", engine="codex", space="code", cwd="/project",
        summary="Current session",
    )
    v = c.workspace.view("s")
    v.write_state = "writable"
    v.put(Block("u", "user", "question\n" * 20, "t"))
    v.put(Block("a", "assistant", "answer\n" * 20, "t", "final"))
    return WorkspaceApp(c, connect=False)


def test_index_preserves_layers_and_disabled_aliases_without_misleading_help():
    keys = KeyConfig()
    rows = {r["id"]: r for r in keys.index()}
    assert rows["keys.sessions"]["keys"] == []
    assert rows["normal.tree"]["keys"] == ["space e"]
    assert keys.label("sessions") == keys.label("tree") == "Space e"
    assert "[keys.sessions]" not in keys.help()
    assert "[normal.tree]" in keys.help()
    assert "unbound" not in keys.help()
    assert {r["id"] for r in keys.index("session tree")} >= {
        "keys.sessions", "normal.tree",
    }
    for layer in ("reader", "draft", "tree_search", "picker", "file_hints"):
        assert any(r["layer"] == layer for r in rows.values())
    assert json.loads(json.dumps(keys.index())) == keys.index()
    assert [r["id"] for r in keys.lookup("space e")] == ["normal.tree"]
    assert [r["id"] for r in keys.lookup("enter", layer="draft")] == ["draft.send"]


@pytest.mark.parametrize("config", [
    {"normal": {"help": ["g"]}},
    {"normal": {"help": ["enter"]}},
    {"tree": {"rename": ["space"]}},
])
def test_cross_layer_prefixes_cannot_hide_registered_actions(config):
    with pytest.raises(ValueError, match="Conflicting"):
        KeyConfig(config)


def test_index_cli_reads_config_without_login_or_connection(tmp_path):
    config = tmp_path / "keys.toml"
    config.write_text('[normal]\ntree = ["space z"]\n')
    result = subprocess.run(
        [sys.executable, "-m", "cc_remote.tui", "--config", str(config),
         "--list-keys", "normal.tree", "--json"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert json.loads(result.stdout)[0]["keys"] == ["space z"]
    assert "password" not in result.stdout.lower()


def test_example_covers_every_registered_action_with_current_defaults():
    path = Path(__file__).resolve().parents[1] / "docs/tui-keys.example.toml"
    data = tomllib.loads(path.read_text())
    defaults = KeyConfig().index()
    assert {f"{layer}.{name}" for layer, section in data.items()
            for name in section if layer != "vim"} == {
                row["id"] for row in defaults
            }
    assert KeyConfig(data).index() == defaults
    assert KeyConfig(data).yank_highlight_ms == KeyConfig().yank_highlight_ms


@pytest.mark.parametrize("row", KeyConfig().index(), ids=lambda row: row["id"])
def test_every_function_shortcut_can_be_remapped_disabled_and_indexed(row):
    layer, name = row["id"].split(".")
    remapped = KeyConfig({layer: {name: ["f20"]}})
    match = next(r for r in remapped.index() if r["id"] == row["id"])
    assert match["keys"] == ["f20"] and match["enabled"]
    assert row["id"] in {r["id"] for r in remapped.lookup("f20", layer=layer)}
    for old in row["keys"]:
        assert row["id"] not in {
            r["id"] for r in remapped.lookup(old, layer=layer)
        }
    disabled = KeyConfig({layer: {name: []}})
    match = next(r for r in disabled.index() if r["id"] == row["id"])
    assert match["keys"] == [] and not match["enabled"]


@pytest.mark.asyncio
async def test_picker_boundary_navigation_uses_only_configured_keys():
    app = make_app({"picker": {"first": ["ctrl+b"], "last": ["ctrl+f"]}})
    async with app.run_test() as pilot:
        app.push_screen(ValuePicker("Choose", [("a", "a"), ("b", "b")]))
        await pilot.pause()
        listing = app.screen.query_one(OptionList)
        assert listing.highlighted == 0
        await pilot.press("end")
        assert listing.highlighted == 0
        await pilot.press("ctrl+f")
        assert listing.highlighted == 1
        await pilot.press("home")
        assert listing.highlighted == 1
        await pilot.press("ctrl+b")
        assert listing.highlighted == 0


@pytest.mark.asyncio
async def test_draft_send_rebind_removes_enter_and_preserves_insert_newlines():
    app = make_app({"draft": {"send": ["ctrl+y"]}})
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j", "i", "x", "enter", "y", "escape", "enter")
        editor = app.query_one(Composer)
        assert editor.text == "x\ny" and not app.client._outbox
        await pilot.press("ctrl+y")
        assert editor.text == "" and len(app.client._outbox) == 1
        await pilot.press("space", "enter")
        assert len(app.screen_stack) == 2  # Leader is not draft confirmation.


@pytest.mark.asyncio
async def test_reader_custom_jumps_details_and_older_have_no_old_key_fallback():
    app = make_app({"reader": {
        "latest_user": ["g U"], "latest_assistant": ["g A"],
        "details": ["x"], "older": ["z"], "command": ["semicolon"],
    }})
    v = app.client.workspace.view("s")
    v.put(Block("d", "tool", "command\nprivate details", "t"))
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("g", "g", "g", "a")
        assert reader.cursor_location == (0, 0)
        await pilot.press("g", "A")
        assert app.current_block().id == "a"
        await pilot.press("colon")
        assert not app.command_mode
        await pilot.press("semicolon")
        assert app.command_mode
        await pilot.press("escape", "ctrl+k", "G", "enter")
        assert not v.blocks[-1].expanded
        await pilot.press("x")
        app.paint()
        assert "private details" in reader.text
        assert "[x: details]" not in reader.text


@pytest.mark.asyncio
async def test_tree_and_search_choose_can_be_rebound_independently():
    app = make_app({"tree": {"choose": ["x"]},
                    "tree_search": {"choose": ["ctrl+y"], "close": ["ctrl+z"]}})
    async with app.run_test() as pilot:
        await pilot.press("space", "e", "H", "enter")
        tree = app.query_one(SessionTree)
        assert not tree.cursor_node.is_expanded
        await pilot.press("x")
        assert tree.cursor_node.is_expanded
        await pilot.press("slash", *"Current", "enter")
        assert isinstance(app.focused, TreeSearch)
        await pilot.press("ctrl+y")
        assert isinstance(app.focused, Transcript)
        await pilot.press("space", "e", "space", "e", "slash", "ctrl+z")
        assert isinstance(app.focused, SessionTree)
        assert not app.query_one(SessionExplorer).query_one(TreeSearch).display


@pytest.mark.asyncio
async def test_picker_rebinding_works_in_search_without_inherited_enter():
    app = make_app({"picker": {"choose": ["ctrl+y"], "down": ["n", "down"]}})
    picked = []
    async with app.run_test() as pilot:
        app.push_screen(ValuePicker("Pick", [("one", 1), ("two", 2)]), picked.append)
        await pilot.pause()
        await pilot.press("n")
        assert app.screen.query_one(ModalEditor).text == "n"
        await pilot.press("enter")
        assert not picked
        await pilot.press("ctrl+y")
        assert picked == [(1,)]


@pytest.mark.asyncio
async def test_forms_and_file_numbers_use_the_same_indexed_configuration():
    app = make_app({"form": {"confirm": ["ctrl+y"]},
                    "file_hints": {"select_1": ["x"]}})
    picked = []
    async with app.run_test() as pilot:
        app.push_screen(TextValue("Name", "value"), picked.append)
        await pilot.pause()
        await pilot.press("enter")
        assert not picked
        await pilot.press("ctrl+y")
        assert picked == [("value",)]
        app.push_screen(FileHints([PreviewRef("/a.md", "markdown")]), picked.append)
        await pilot.pause()
        assert "x  markdown" in app.screen.query_one(OptionList).get_option_at_index(0).prompt.plain
        await pilot.press("1")
        assert len(picked) == 1
        await pilot.press("x")
        assert picked[-1].path == "/a.md"


@pytest.mark.asyncio
async def test_help_search_is_inspection_only_and_uses_remapped_keys():
    app = make_app({"normal": {"tree": ["space z"]}})
    async with app.run_test() as pilot:
        await pilot.press("space", "h")
        help_text = app.screen.query_one(TextArea).text
        assert "Space z" in help_text and "Space e" not in help_text
        await pilot.press("slash", *"normal.tree")
        assert isinstance(app.screen, ShortcutPicker)
        assert app.screen.query_one(ModalEditor).text == "normal.tree"
        listing = app.screen.query_one(OptionList)
        assert listing.option_count == 1
        assert "Space z" in listing.get_option_at_index(0).prompt.plain
        await pilot.press("enter")
        assert not app.client._outbox  # Search never executes indexed actions.
