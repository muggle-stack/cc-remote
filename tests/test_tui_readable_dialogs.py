"""Human-readable controls and long questions retain full content and scope."""

import json

import pytest
from textual.widgets import Static

from cc_remote.tui_fields import ParameterFields
from cc_remote.tui_modal import ModalEditor
from cc_remote.tui_panels import ActionForm, PanelReader, QuestionDialog
from tests.test_tui_controls import setup, sent, catalogs
from tests.test_tui_questions import ask, pending_async, setup as question_setup
from cc_remote.tui_app import WorkspaceApp


@pytest.mark.asyncio
async def test_named_goal_fields_send_without_json_confirmation():
    app, c = setup()
    async with app.run_test() as pilot:
        await app.push_screen(ActionForm(c, "s", "set_goal"))
        form = app.screen.query_one(ParameterFields)
        fields = {row[0]: row[-1] for row in form.rows}
        fields["objective"].load_text("A multi-line\n目标")
        fields["token_budget"].load_text("12345")
        await pilot.press("enter", "enter")
        commands = sent(c, "set_goal")
        assert len(commands) == 1
        assert commands[0]["objective"] == "A multi-line\n目标"
        assert commands[0]["token_budget"] == 12345


@pytest.mark.asyncio
async def test_destructive_named_form_still_requires_frozen_review():
    app, c = setup()
    async with app.run_test() as pilot:
        await app.push_screen(ActionForm(c, "s", "clear_goal"))
        await pilot.press("enter")
        assert not sent(c, "clear_goal")
        assert app.screen.prepared is not None
        await pilot.press("enter", "enter")
        assert len(sent(c, "clear_goal")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(48, 24), (100, 40)])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_long_questions_scroll_without_submitting_and_resize(
    size, asynchronous
):
    c, view = question_setup()
    question = "\n".join(
        f"问题 {n}：请完整阅读这一行的详细内容" for n in range(100)
    )
    if asynchronous:
        ask(view, options=["First", "Second"])
        pending_async(view)[0].data["questions"][0]["title"] = question
        pending_async(view)[0].data["questions"].append(
            {"title": "Second question", "options": []}
        )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=size) as pilot:
        if asynchronous:
            await pilot.press("ctrl+t")
        else:
            await app.push_screen(
                QuestionDialog(
                    c,
                    {
                        "question": question,
                        "sid": "s",
                        "ask_id": "ask",
                    },
                )
            )
        dialog = app.screen
        reader = dialog.query_one("#question-body", PanelReader)
        assert reader.text == question or reader.text.startswith(question)
        await pilot.press("ctrl+k", "G", "enter")
        await pilot.pause()
        assert dialog.focused is reader and not c._outbox
        assert reader.cursor_location[0] >= 99 and reader.scroll_y > 0
        assert dialog.query_one("#answer").region.bottom <= size[1]
        cursor = reader.cursor_location
        await pilot.resize_terminal(size[0] - 8, size[1])
        assert reader.cursor_location == cursor
        if asynchronous:
            await pilot.press("ctrl+right", "ctrl+left", "ctrl+k")
            assert reader.cursor_location == cursor
        await pilot.press("ctrl+j")
        if asynchronous:
            from cc_remote.tui_modal import PickerList

            listing = dialog.query_one(PickerList)
            await pilot.press("j", "k")
            assert listing.highlighted == 0 and dialog.focused is listing
        await pilot.press("i")
        editor = dialog.query_one("#answer", ModalEditor)
        if not asynchronous:
            assert dialog.focused is editor
        assert not c._outbox
        assert "Question line" in str(
            dialog.query_one("#reading-position", Static).render()
        )


@pytest.mark.asyncio
async def test_structured_parameters_only_show_json_after_explicit_request():
    app, c = setup()
    async with app.run_test() as pilot:
        await app.push_screen(ActionForm(c, "s", "reorder_queued_queries"))
        fields = app.screen.query_one(ParameterFields)
        row = next(row for row in fields.rows if row[0] == "order")
        row[-1].focus()
        assert row[-1].locked and row[-1].text != "[]"
        await pilot.press("ctrl+r")
        assert row[-1].vim_mode == "INSERT"
        assert json.loads(row[-1].text) == []


@pytest.mark.asyncio
async def test_rejected_setting_preserves_choice_and_effective_value():
    app, c = setup()
    async with app.run_test() as pilot:
        await pilot.press("space", "m")
        form = app.screen
        catalogs(form)
        await pilot.press("enter", "ctrl+j", "enter")
        command = sent(c, "set_model")[-1]
        # While the request is pending, Enter must not duplicate it.
        await pilot.press("enter", "enter")
        assert len(sent(c, "set_model")) == 1
        assert form.values["model"] is None
        c._handle(
            dict(
                type="error",
                sid="s",
                to=c.client_id,
                request_id=command["cmd_id"],
                message="Cannot change model",
            )
        )
        form.paint()
        assert "Cannot change model" in str(
            form.query_one("#form-result", Static).render()
        )
        await pilot.press("enter")
        assert app.screen.selected == "beta"
        await pilot.press("enter")
        assert len(sent(c, "set_model")) == 2
        assert sent(c, "set_model")[-1]["cmd_id"] != command["cmd_id"]
        c._handle(dict(type="model", sid="s", model="beta"))
        form.paint()
        assert form.values["model"] == "beta" and form.pending_change is None


@pytest.mark.asyncio
async def test_rejected_named_action_is_editable_and_retry_has_new_id():
    app, c = setup()
    async with app.run_test() as pilot:
        await app.push_screen(ActionForm(c, "s", "set_effort"))
        form = app.screen
        form.query_one(ModalEditor).load_text("high")
        await pilot.press("enter")
        command = sent(c, "set_effort")[-1]
        c._handle(
            dict(
                type="error",
                sid="s",
                request_id=command["cmd_id"],
                to=c.client_id,
                message="Rejected",
            )
        )
        form.check_result()
        assert not form.submitted
        assert form.query_one(ModalEditor).text == "high"
        await pilot.press("enter")
        assert sent(c, "set_effort")[-1]["cmd_id"] != command["cmd_id"]
