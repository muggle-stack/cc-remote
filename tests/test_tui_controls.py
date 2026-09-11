"""Real key dispatch, scoped settings and readable transcript regressions."""

import json

import pytest
from textual.widgets import Button, OptionList, Static

from cc_remote import protocol as p
from cc_remote.tui_app import (
    WorkspaceApp,
    WorkspaceClient,
    Transcript,
    Composer,
)
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import ActionForm, DetailPanel, ActionPicker
from cc_remote.tui_presentation import SessionPresentation
from cc_remote.tui_settings import SettingsForm, ValuePicker, TextValue


def setup():
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    c.workspace.catalog["s"] = {
        "session_id": "s",
        "engine": "codex",
        "space": "code",
        "cwd": "/repo",
    }
    view = c.workspace.view("s")
    view.write_state = "writable"
    return WorkspaceApp(c, connect=False), c


def catalogs(form):
    for kind, data in {
        "models": {
            "default_model": "alpha",
            "models": [
                {"id": "alpha", "efforts": ["low", "high"]},
                {"id": "beta", "efforts": []},
            ],
        },
        "permission_profiles": {
            "profiles": [
                {"id": ":workspace", "allowed": True},
                {"id": ":danger-full-access", "allowed": True},
                {"id": "forbidden", "allowed": False},
            ]
        },
    }.items():
        key = (kind, *form.client.capability_key(form.args(kind)))
        form.client.settings_catalogs[key] = data


def sent(c, kind):
    frames = [json.loads(raw) for raw, _ in c._outbox.values()]
    return [frame for frame in frames if frame["type"] == kind]


def test_quota_missing_short_window_hidden_and_percent_semantics():
    presentation = SessionPresentation()
    presentation.context = {"percentage": 12.5}
    presentation.rates = {
        "codex": {
            "secondary": {
                "window_duration_mins": 10080,
                "used_percent": 20,
            }
        }
    }
    label = presentation.usage_label()
    assert "5h" not in label and "?" not in label
    assert "12% used" in label and "80% remaining" in label
    presentation.rates["codex"]["primary"] = {
        "window_duration_mins": 300,
        "used_percent": 0,
    }
    assert "5h [██████████] 100% remaining" in presentation.usage_label()
    presentation.rates["codex"]["primary"]["used_percent"] = None
    assert "5h" not in presentation.usage_label()


@pytest.mark.asyncio
async def test_all_message_directions_skip_progress_and_preserve_draft():
    app, c = setup()
    view = c.workspace.view("s")
    for n in range(3):
        view.event(
            {"type": "user_msg", "msg_id": f"u{n}", "prompt": f"user {n}"}
        )
        view.event(
            {
                "type": "delta",
                "message_id": f"p{n}",
                "text": "progress",
                "channel": "commentary",
            }
        )
        view.event(
            {
                "type": "delta",
                "message_id": f"a{n}",
                "text": f"answer {n}",
                "channel": "final",
            }
        )
        view.event({"type": "turn_end", "turn_id": f"u{n}",
                    "result": {"subtype": "success"}})
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.press("g", "a")
        latest = reader.cursor_location
        await pilot.press("left_square_bracket", "a")
        previous = reader.cursor_location
        assert previous < latest
        await pilot.press("right_square_bracket", "a")
        assert reader.cursor_location == latest
        await pilot.press("g", "u")
        latest_user = reader.cursor_location
        await pilot.press("left_square_bracket", "u")
        assert reader.cursor_location < latest_user
        await pilot.press("right_square_bracket", "u")
        assert reader.cursor_location == latest_user
        assert not view.follow and not app.query_one(Composer).text
        assert "on #202a36" in reader.line_styles.values()
        assert "bright_black" in reader.line_styles.values()
        assert "bold green" in reader.line_styles.values()


@pytest.mark.asyncio
async def test_goal_nested_form_insert_escape_and_no_button_or_main_send():
    app, c = setup()
    async with app.run_test() as pilot:
        app.query_one(Composer).load_text("keep draft")
        await pilot.press("space", "g")
        assert isinstance(app.screen, DetailPanel)
        assert not app.screen.query(Button)
        await pilot.press("i")
        assert isinstance(app.screen, ActionForm)
        editor = app.screen.query_one(ModalEditor)
        assert editor.vim_mode == "INSERT"
        await pilot.press("escape")
        editor.load_text('{"objective":"old", "status":"active"}')
        editor.move_cursor((0, 14))
        await pilot.press("c", "i", "quotation_mark")
        assert editor.vim_mode == "INSERT"
        await pilot.press("n", "e", "w", "escape")
        assert '"objective":"new"' in editor.text
        assert editor.vim_mode == "NORMAL"
        await pilot.press("escape")
        assert isinstance(app.screen, DetailPanel)
        await pilot.press("ctrl+s", "ctrl+e")
        assert not sent(c, "query") and not sent(c, "set_goal")
        await pilot.press("escape")
        assert app.query_one(Composer).text == "keep draft"
        assert app.query_one(Composer).vim_mode == "NORMAL"


@pytest.mark.asyncio
async def test_picker_search_ctrl_j_k_and_normal_mode_return():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "a")
        assert isinstance(app.screen, ActionPicker)
        editor = app.screen.query_one(ModalEditor)
        assert editor.vim_mode == "INSERT"
        await pilot.press(*"goal")
        listing = app.screen.query_one(OptionList)
        assert listing.option_count >= 2
        await pilot.press("ctrl+j")
        assert listing.highlighted == 1
        await pilot.press("ctrl+k", "escape")
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_model_change_uses_catalog_and_one_confirmation():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "m")
        form = app.screen
        assert isinstance(form, SettingsForm)
        catalogs(form)
        await pilot.press("enter")
        assert isinstance(app.screen, ValuePicker)
        await pilot.press("ctrl+j", "enter")
        assert app.screen is form
        assert len(sent(c, "set_model")) == 1
        assert sent(c, "set_model")[-1]["model"] == "beta"
        assert sent(c, "set_model")[-1]["sid"] == "s"


@pytest.mark.asyncio
async def test_new_session_collects_cwd_model_permissions_atomically(
    monkeypatch,
):
    from cc_remote.tui_directories import DirectoryPicker

    app, c = setup()

    async def directories(path, **kwargs):
        return {
            "path": "/home/example" if path == "~" else path,
            "parent": "/",
            "dirs": [{"path": "/new"}],
        }

    async def rank(paths, query):
        return [path for path in paths if query in path]

    monkeypatch.setattr(c, "list_directories", directories)
    monkeypatch.setattr("cc_remote.tui_directories.fuzzy_directories", rank)
    async with app.run_test(size=(80, 28)) as pilot:
        await pilot.press("space", "enter")
        form = app.screen
        assert isinstance(form, SettingsForm) and form.new
        assert form.values["cwd"] == "~"
        await pilot.press("enter")
        assert isinstance(app.screen, DirectoryPicker)
        await pilot.pause(0.1)
        await pilot.press(*"/new")
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is form and form.values["cwd"] == "/new"
        catalogs(form)
        form.query_one(OptionList).highlighted = form.fields().index("model")
        await pilot.press("enter", "ctrl+j", "enter")
        assert form.values["model"] == "alpha"
        form.query_one(OptionList).highlighted = form.fields().index(
            "permission_profile"
        )
        await pilot.press("enter")
        assert all(value != "forbidden" for _, value in app.screen.choices)
        await pilot.press("ctrl+j", "ctrl+j", "enter")
        assert form.values["permission_profile"] == ":danger-full-access"
        form.values["permission_mode"] = "never"
        await pilot.press("end", "enter")
        assert len(sent(c, "new_session")) == 1
        request = sent(c, "new_session")[-1]
        assert (
            request["cwd"],
            request["model"],
            request["permission_profile"],
            request["permission_mode"],
        ) == ("/new", "alpha", ":danger-full-access", "never")
        assert request["space"] == "code" and request["engine"] == "codex"
        assert not sent(c, "query") and not sent(c, "set_perm")


def test_new_work_and_cwd_scoped_catalog_never_reuse_foreign_permissions():
    _, c = setup()
    form = SettingsForm(c, None, new=True)
    catalogs(form)
    assert len(form.choices("permission_profile")) == 3
    form.values["cwd"] = "/other"
    assert form.choices("permission_profile") == [
        ("Engine default (no override)", None)
    ]
    c.space = "work"
    work = SettingsForm(c, None, new=True)
    assert (
        "cwd" not in work.fields() and "permission_profile" not in work.fields()
    )
    c.engine = "claude"
    claude = SettingsForm(c, None, new=True)
    assert "permission_mode" not in claude.fields()
    assert len(claude.choices("model")) > 1


def test_modal_key_configuration_is_scoped_and_rejects_global_conflicts():
    keys = KeyConfig({"panel": {"refresh": ["z"]}, "picker": {"down": ["n"]}})
    assert "z: Refresh" in keys.layer_help("panel")
    assert keys.normal_keys["goal"] == ("space g",)
    with pytest.raises(ValueError, match="global"):
        KeyConfig({"panel": {"refresh": ["ctrl+e"]}})


@pytest.mark.asyncio
async def test_catalog_responses_bound_to_requested_directory_and_profile():
    _, c = setup()
    for directory in ("/old", "/new"):
        await c._send(
            p.GetPermissionProfiles(cwd=directory, codex_profile_id="account")
        )
    request, source = next(iter(c.catalog_reads.items()))
    c._on_event(
        {
            "type": "permission_profiles",
            "request_id": request,
            "cwd": "/old",
            "profiles": [{"id": "old", "allowed": True}],
        }
    )
    key = ("permission_profiles", *c.capability_key(source))
    assert c.settings_catalogs[key]["profiles"][0]["id"] == "old"
    form = SettingsForm(c, None, new=True)
    form.values["cwd"] = "/new"
    form.profiles["codex_profile_id"] = "account"
    assert not form.catalog("permission_profiles")


@pytest.mark.asyncio
async def test_configured_panel_refresh_and_main_keys_do_not_leak():
    app, c = setup()
    c.keys = KeyConfig({"panel": {"refresh": ["z"]}})
    async with app.run_test() as pilot:
        await pilot.press("space", "g")
        before = len(sent(c, "get_goal"))
        await pilot.press("z")
        assert len(sent(c, "get_goal")) == before + 1
        assert "z: Refresh" in str(
            app.screen.query_one(".key-hints", Static).render()
        )
        await pilot.press("space", "enter")
        assert isinstance(app.screen, DetailPanel) and not sent(
            c, "new_session"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,command,attribute,value",
    [
        (
            "permission_profile",
            "set_permission_profile",
            "profile",
            ":workspace",
        ),
        ("permission_mode", "set_perm", "mode", "never"),
        ("web_search", "set_web_search", "mode", "cached"),
        ("collaboration_mode", "set_collaboration_mode", "mode", "default"),
        ("service_tier", "set_service_tier", "service_tier", "default"),
    ],
)
async def test_settings_apply_one_scoped_command_after_selection(
    field,
    command,
    attribute,
    value,
):
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "s")
        form = app.screen
        catalogs(form)
        form.query_one(OptionList).highlighted = form.fields().index(field)
        await pilot.press("enter", "enter")
        assert app.screen is form and len(sent(c, command)) == 1
        assert sent(c, command)[-1][attribute] == value
        assert sent(c, command)[-1]["sid"] == "s"
        assert form.values[field] is None  # Wait for effective server settings.


@pytest.mark.asyncio
async def test_custom_close_key_does_not_eat_insert_text():
    app, c = setup()
    c.keys = KeyConfig({"form": {"close": ["q"]}})
    async with app.run_test() as pilot:
        app.push_screen(TextValue("Directory", ""))
        await pilot.pause()
        await pilot.press("i", "q")
        editor = app.screen.query_one(ModalEditor)
        assert editor.text == "q"
        await pilot.press("escape")
        assert editor.vim_mode == "NORMAL"
        await pilot.press("q")
        assert len(app.screen_stack) == 1


@pytest.mark.asyncio
async def test_previous_user_fetches_older_page_without_losing_anchor():
    from cc_remote.tui_state import Block

    app, c = setup()
    view = c.workspace.view("s")
    view.event({"type": "user_msg", "msg_id": "u", "prompt": "current"})
    view.has_more, view.oldest = True, "u"
    async with app.run_test() as pilot:
        await pilot.press("g", "u", "left_square_bracket", "u")
        assert sent(c, "get_history")[-1]["before"] == "u"
        assert app.pending_jump and view.loading
        view.blocks.insert(
            0, Block(id="older", role="user", text="older", turn="older")
        )
        view.loading = False
        view.version += 1
        app.paint()
        await pilot.pause()
        assert app.query_one(Transcript).cursor_location == (0, 0)
        assert app.pending_jump is None


def test_claude_model_fallback_matches_web_curated_choices():
    from pathlib import Path
    from cc_remote.tui_settings import CLAUDE_MODELS

    web = Path("web/src/data.ts").read_text()
    table = web.split("export const MODELS:", 1)[1].split("];")[0]
    assert all(f'id: "{model}"' in table for model in CLAUDE_MODELS)


@pytest.mark.asyncio
async def test_secret_input_paste_requires_insert_and_remains_masked():
    from textual.events import Paste
    from cc_remote.tui_modal import ModalInput
    from cc_remote.tui_panels import QuestionDialog

    app, c = setup()
    async with app.run_test() as pilot:
        app.push_screen(
            QuestionDialog(
                c,
                {
                    "sid": "s",
                    "ask_id": "private",
                    "question": "Secret?",
                    "secret": True,
                    "allow_text": True,
                },
            )
        )
        await pilot.pause()
        editor = app.screen.query_one(ModalInput)
        editor._on_paste(Paste("secret"))
        assert editor.value == ""
        await pilot.press("i")
        editor._on_paste(Paste("secret"))
        assert editor.value == "secret" and editor.password
        await pilot.press("escape", "escape")
        assert len(app.screen_stack) == 1 and app.clipboard == ""
