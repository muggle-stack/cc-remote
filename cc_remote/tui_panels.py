"""Focusable terminal panels for public Web-equivalent controls/reports."""

from __future__ import annotations

import uuid

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_actions import ACTIONS, build_action, defaults, is_read
from cc_remote.tui_presentation import describe
from cc_remote.tui_details import details
from cc_remote.tui_fields import ParameterFields
from cc_remote.tui_questions import supplemental_answer_prompt
from cc_remote.tui_modal import Overlay, ModalEditor, ModalInput, PickerList, hints

PANELS = (
    "Goal / Plan",
    "Usage / Context",
    "Settings",
    "Queue",
    "Background",
    "Questions",
    "Status",
    "Notices",
    "Reports",
    "Web handoff",
)

# Only the explicit main action palette exposes the complete command inventory.
PANEL_ACTIONS = {
    "Goal / Plan": {"get_goal", "set_goal", "clear_goal", "dismiss_goal"},
    "Usage / Context": {
        "get_context",
        "set_codex_context",
        "get_status",
        "consume_rate_limit_reset_credit",
    },
    "Settings": {
        "get_models",
        "get_permission_profiles",
        "set_model",
        "set_effort",
        "set_perm",
        "set_permission_profile",
        "set_service_tier",
        "set_web_search",
        "set_collaboration_mode",
        "set_auto_compact",
        "set_codex_context",
    },
    "Queue": {"get_queued_query", "update_queued_query", "cancel_queued_query",
              "reorder_queued_queries"},
    "Background": {"get_status"},
    "Status": {"get_status", "get_context"},
    "Notices": {"get_status"},
    "Reports": {
        "get_engine_capabilities",
        "manage_engine_plugin",
        "manage_engine_skill",
        "manage_engine_hook",
        "get_diff",
        "get_turn_file_changes",
        "browse_files",
        "get_file_preview",
        "get_agent_detail",
    },
}


class PanelReader(ModalEditor):
    """Independent detail cursor; copying never jumps back into the composer."""

    def __init__(self, text: str = "", **kwargs):
        kwargs.pop("read_only", None)
        super().__init__(text, locked=True, **kwargs)


class DetailPanel(Overlay):
    """Pinned to the session at open; background focus changes cannot retarget it."""

    local_actions = {"cancel", "refresh", "actions", "edit", "hide"}

    def __init__(self, client, sid: str, name: str):
        super().__init__()
        self.client, self.sid, self.panel_name = client, sid, name
        self.last_text = None
        if name != "Goal / Plan":
            self.local_actions = {"cancel", "refresh", "actions"}
        if name not in PANEL_ACTIONS:
            self.local_actions = {"cancel", "refresh"}
        if name == "Help":
            self.local_actions = {"cancel", "search"}

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label(f"{self.panel_name} · {self.sid}", markup=False)
            yield PanelReader(read_only=True)
            yield hints()

    def on_mount(self) -> None:
        self.paint()
        self.set_interval(0.25, self.paint)
        self.query_one(PanelReader).focus()

    def paint(self) -> None:
        sid = self.client.workspace.rekeys.get(self.sid, self.sid)
        view = self.client.workspace.view(sid)
        if self.panel_name == "Queue":
            text = (
                details(view.queue)
                + "\n\nUse Get/Update/Cancel queued query in Actions."
            )
        elif self.panel_name == "Reports":
            text = details(
                {**self.client.workspace.reports, **view.presentation.reports}
            )
        elif self.panel_name == "Help":
            text = (
                self.client.keys.help() + "\n\n"
                "Read / draft\n"
                "i/Esc Vim Insert/Normal\n"
                "v/V select · y copy; quoting keeps the reading cursor\n"
                "yi(/yaw/viw text objects · 2yaw/y2w counts · f/t then ;/,\n"
                "\n"
                "Startup opens the newest session in the selected engine/space.\n"
                "Switching surfaces restores their independent last focus.\n"
                "The tree only lists the current engine and Code/Work.\n"
                "Closing a local tab leaves server tasks running.\n"
                "For each layer.action ID, configure [layer] action = [keys]\n"
                "in ~/.config/cc-remote/tui.toml\n"
                "or pass --config PATH; restart the TUI to load changes.\n\n"
                "Command editor: file /local/path or image /local/path attaches.\n"
                "detach all removes unsent attachments; web shows the Web address.\n"
                "/goal, /goal resume, /goal pause, /goal clear are local controls.\n"
                "Actions exposes session, settings, Goal, plugin and Work controls.\n"
                "Settings apply on Enter; destructive actions require review.\n"
                "Actions use named fields; structured data has an explicit advanced editor.\n"
                "Closing TUI never stops server tasks.\n\n"
                "File previews use numbered hints and read-only Vim navigation.\n"
                "Mermaid uses terminal text; graphical/interactive details use Web.\n"
                "This is a terminal presentation of the shared session, not another model process."
            )
        elif self.panel_name == "Web handoff":
            text = (
                "PDF, graphical Mermaid details and interactive Viewer use Web.\n"
                "Open this address manually; no browser is launched automatically:\n\n"
                + self.client.web_url()
                + "\n\nSession: "
                + sid
            )
        else:
            text = view.presentation.panel(self.panel_name)
        text = _safe_remote_text(text)
        if text != self.last_text:
            editor = self.query_one(PanelReader)
            selection, scroll = editor.selection, editor.scroll_offset
            editor.load_text(text)
            editor.selection = selection
            editor.scroll_to(scroll.x, scroll.y, animate=False)
            self.last_text = text

    async def action_refresh(self) -> None:
        await self.client.refresh_panel(self.sid, self.panel_name)

    def action_actions(self) -> None:
        self.app.push_screen(
            ActionPicker(self.client, self.sid, scope=self.panel_name)
        )

    def action_search(self):
        self.app.push_screen(ShortcutPicker(self.client))

    def action_edit(self) -> None:
        if self.panel_name == "Goal / Plan":
            self.app.push_screen(
                ActionForm(self.client, self.sid, "set_goal", insert=True)
            )

    async def action_hide(self) -> None:
        if self.panel_name == "Goal / Plan":
            goal_id = self.client.workspace.view(self.sid).presentation.goal_id
            if goal_id:
                from cc_remote.protocol import DismissGoal

                await self.client._send(
                    DismissGoal(sid=self.sid, goal_id=goal_id)
                )


class ActionPicker(Overlay):
    key_layer = "picker"
    local_actions = {"cancel", "search", "edit", "down", "up"}

    def __init__(self, client, sid: str | None, *, scope: str | None = None):
        super().__init__()
        self.client, self.sid = client, sid
        self.scope = scope
        self.allowed_actions = (
            set(ACTIONS) if scope is None else PANEL_ACTIONS.get(scope, set())
        )

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label(
                f"{self.scope or 'All'} actions · search by name · "
                f"{self.app.client.keys.label('focus_draft')}/"
                f"{self.app.client.keys.label('focus_read')} or arrows",
                markup=False,
            )
            yield ModalEditor(
                classes="search-editor",
                id="search",
                placeholder="Search actions (see shortcuts below)",
            )
            yield PickerList()
            yield hints()

    def on_mount(self) -> None:
        self.filter("")
        self.action_search()

    def on_screen_resume(self) -> None:
        self.action_search()

    def action_search(self) -> None:
        editor = self.query_one(ModalEditor)
        editor.focus()
        editor.set_mode("INSERT")

    def action_edit(self) -> None:
        self.action_search()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def filter(self, value: str) -> None:
        listing = self.query_one(OptionList)
        listing.clear_options()
        for name in ACTIONS:
            if name not in self.allowed_actions:
                continue
            label = name.replace("_", " ")
            if all(term in label for term in value.casefold().split()):
                listing.add_option(Option(Text(label), id=name))
        listing.highlighted = 0 if listing.option_count else None

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.filter(event.text_area.text.replace("\n", " "))

    def on_input_submitted(self) -> None:
        listing = self.query_one(OptionList)
        if listing.highlighted is not None:
            self.open_form(listing.get_option_at_index(listing.highlighted).id)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self.open_form(event.option.id)

    def open_form(self, name: str) -> None:
        if name not in self.allowed_actions:
            return
        from cc_remote.tui_settings import SettingsForm

        if name in {
            "new_session",
            "set_model",
            "set_permission_profile",
            "set_perm",
        }:
            field = {
                "set_model": "model",
                "set_perm": "permission_mode",
                "set_permission_profile": "permission_profile",
            }.get(name)
            self.app.push_screen(
                SettingsForm(
                    self.client,
                    None if name == "new_session" else self.sid,
                    new=name == "new_session",
                    initial_field=field,
                )
            )
        else:
            self.app.push_screen(ActionForm(self.client, self.sid, name))


class ShortcutPicker(ActionPicker):
    """Search the effective registry without executing the highlighted action."""

    def __init__(self, client):
        super().__init__(client, None, scope="Shortcut index")

    def filter(self, value):
        listing = self.query_one(OptionList)
        listing.clear_options()
        for row in self.client.keys.index(value):
            listing.add_option(Option(Text(
                f"{row['display']} · {row['description']}\n"
                f"{row['id']} · {row['scope']}"
            ), id=row["id"]))
        listing.highlighted = 0 if listing.option_count else None

    def open_form(self, name):
        # Inspection only: pressing choose never triggers the indexed command.
        self.client.notice = "Shortcut configuration: " + name


class ActionForm(Overlay):
    """Schema validated parameters, with an immutable confirmation step."""

    key_layer = "form"
    local_actions = {"cancel", "field_help", "submit", "advanced"}

    def __init__(self, client, sid: str | None, name: str, *, insert=False):
        super().__init__()
        self.client, self.sid, self.panel_name = client, sid, name
        self.prepared = None
        self.submitted = False
        self.initial_insert = insert
        self.ticket = None

    def compose(self) -> ComposeResult:
        row = self.client.workspace.catalog.get(
            self.sid, {"engine": self.client.engine, "space": self.client.space}
        )
        view = self.client.workspace.view(self.sid or "form")
        values = defaults(
            self.panel_name,
            self.sid,
            row.get("engine", self.client.engine),
            row,
            view.presentation,
        )
        with Vertical(classes="tui-panel"):
            yield Label(
                f"{self.panel_name.replace('_', ' ')} · target {self.sid or 'new session'}",
                markup=False,
            )
            yield Label(
                "Edit named fields. Enter submits; destructive actions require review.",
                markup=False,
            )
            yield ParameterFields(values, ACTIONS[self.panel_name].model_json_schema())
            yield Static("", id="action-result", markup=False)
            yield hints()

    def on_mount(self) -> None:
        editor = self.query_one(ParameterFields).focus_editor()
        if self.initial_insert and editor:
            editor.set_mode("INSERT")
        self.set_interval(0.25, self.check_result)

    def check_result(self):
        result = self.client.action_results.get(self.ticket, {})
        if result.get("error"):
            self.query_one("#action-result", Static).update(result["error"])
            self.submitted = False
            self.ticket = None
            self.prepared = None
            fields = self.query_one(ParameterFields)
            for key, types, _, _, editor in fields.rows:
                editor.locked = key == "session_id" or (
                    bool(types & {"object", "array"}) and key not in fields.advanced
                )

    def action_advanced(self):
        if self.prepared is None:
            self.query_one(ParameterFields).toggle_advanced(self.focused)

    def action_field_help(self) -> None:
        self.app.push_screen(FieldHelp(self.panel_name))

    async def action_submit(self) -> None:
        if self.submitted:
            return
        result = self.query_one("#action-result", Static)
        try:
            if self.prepared is None:
                message = build_action(
                    self.panel_name,
                    self.query_one(ParameterFields).payload(),
                    self.sid,
                    self.client.client_id,
                )
                if not is_read(self.panel_name) and not self.panel_name.startswith(
                    ("set_", "rename_", "pin_", "dismiss_", "acknowledge_")
                ):
                    self.prepared = message
                    for editor in self.query(ModalEditor):
                        editor.set_mode("NORMAL")
                        editor.locked = True
                    result.update(
                        "Parameters locked. "
                        + self.client.keys.layer_label("form", "confirm")
                        + " confirms; "
                        + self.client.keys.layer_label("form", "close")
                        + " cancels."
                    )
                    return
            else:
                message = self.prepared
            self.submitted = True
            self.ticket = message.cmd_id
            if await self.client.execute_action(message):
                result.update(
                    "Submitted. The authoritative result appears in Reports / session state."
                )
            else:
                self.submitted = False
                result.update(self.client.notice)
        except (ValueError, TypeError, OSError) as exc:
            self.submitted = False
            result.update(_safe_remote_text(str(exc)))


class FieldHelp(Overlay):
    """Show native schema types/enums instead of maintaining a second schema."""

    def __init__(self, name: str):
        super().__init__()
        self.action_name = name

    def compose(self) -> ComposeResult:
        from cc_remote.tui_actions import HIDDEN

        schema = ACTIONS[self.action_name].model_json_schema()
        fields = {
            key: value
            for key, value in schema.get("properties", {}).items()
            if key not in HIDDEN
        }
        with Vertical(classes="tui-panel"):
            yield Label("Field help · " + self.action_name, markup=False)
            yield PanelReader(
                details(
                    {"fields": fields, "definitions": schema.get("$defs", {})}
                ),
                read_only=True,
            )
            yield hints()


class SuggestionPicker(ActionPicker):
    def __init__(self, choices: list[str]):
        super().__init__(None, None)
        self.choices = choices

    def filter(self, value: str) -> None:
        listing = self.query_one(OptionList)
        listing.clear_options()
        for choice in self.choices:
            if value.casefold() in choice.casefold():
                listing.add_option(Option(Text(choice), id=choice))
        listing.highlighted = 0 if listing.option_count else None

    def open_form(self, name: str) -> None:
        self.dismiss(name)


class QuestionLayout(Overlay):
    """Scrollable question text with a reserved, independently editable reply."""

    DEFAULT_CSS = """
    QuestionLayout { align: center middle; }
    QuestionLayout .tui-panel { height: 90%; }
    QuestionLayout #question-body { height: 2fr; min-height: 3; }
    QuestionLayout #answer { height: 5; min-height: 3; }
    QuestionLayout OptionList { height: 1fr; min-height: 3; }
    QuestionLayout #reading-position { height: 1; color: $text-muted; }
    QuestionLayout .key-hints { max-height: 2; }
    QuestionLayout #answer-result { height: auto; max-height: 2; }
    """

    def start_reading_status(self):
        self.set_interval(0.2, self.reading_status)

    def reading_status(self):
        reader = self.query_one("#question-body", PanelReader)
        text = (
            f"Question line {reader.cursor_location[0] + 1}"
            f"/{reader.document.line_count} · "
            + self.client.keys.label("focus_read") + ": read · "
            + self.client.keys.label("focus_draft") + ": answer"
        )
        if text != getattr(self, "last_reading_status", None):
            self.last_reading_status = text
            self.query_one("#reading-position", Static).update(text)

    def action_up(self):
        self.query_one(PickerList).action_cursor_up()

    def action_down(self):
        self.query_one(PickerList).action_cursor_down()

    def move_selection(self, down):
        if not down:
            self.query_one("#question-body", PanelReader).focus()
        elif isinstance(self.focused, PickerList):
            self.focused.action_cursor_down()
        else:
            listings = self.query(PickerList)
            target = (listings.first() if listings and listings.first().display
                      else self.query_one("#answer"))
            target.focus()

    def check_action(self, action, parameters):
        if action == "dispatch_shortcut":
            name, _ = parameters
            if self.focused is self.query_one("#question-body"):
                if name in {"confirm", "down", "up", "edit"}:
                    return False
        return super().check_action(action, parameters)


class QuestionDialog(QuestionLayout):
    """A scoped pending question. Secret replies never enter the normal draft."""

    key_layer = "form"
    local_actions = {"cancel", "submit"}

    def __init__(self, client, ask: dict):
        super().__init__()
        self.client, self.ask = client, dict(ask)

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label("Answer question", markup=False)
            labels = [self.ask.get("question", ""), ""]
            for index, option in enumerate(self.ask.get("options", []), 1):
                labels.append(
                    f"{index}. {option['label']}\n   {option.get('ds', '')}"
                )
            yield PanelReader(
                _safe_remote_text("\n".join(labels)), id="question-body"
            )
            yield Static("", id="reading-position", markup=False)
            if self.ask.get("secret"):
                yield ModalInput(
                    placeholder="i: enter secret answer",
                    password=True,
                    id="answer",
                )
            else:
                yield ModalEditor(
                    placeholder="i: option number(s), or text if allowed",
                    id="answer",
                )
            yield Static("", id="answer-result", markup=False)
            yield hints()

    def on_mount(self) -> None:
        self.query_one("#answer").focus()
        self.start_reading_status()

    async def on_input_submitted(self) -> None:
        await self.send()

    async def action_submit(self) -> None:
        if self.focused is self.query_one("#question-body"):
            return
        await self.send()

    async def send(self) -> None:
        editor = self.query_one("#answer")
        value = editor.value if isinstance(editor, Input) else editor.text
        if await self.client.answer_for(self.ask, value):
            if isinstance(editor, Input):
                editor.value = ""
            else:
                editor.load_text("")
            self.dismiss(None)
        else:
            self.query_one("#answer-result", Static).update(self.client.notice)


class AsyncQuestionDialog(QuestionLayout):
    """Collect non-blocking answers separately from the normal draft."""

    key_layer = "question"
    local_actions = {"cancel", "submit", "edit", "down", "up", "question"}

    def __init__(self, client, sid, blocks):
        super().__init__()
        self.client, self.sid = client, sid
        self.identities = tuple(b.id for b in blocks)
        self.questions = [q for b in blocks for q in b.data["questions"]]
        self.answers = [""] * len(self.questions)
        self.drafts = [""] * len(self.questions)
        self.index = 0
        self.submitting = False
        self.read_positions = {}

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label("", id="question-title", markup=False)
            yield PanelReader(id="question-body")
            yield Static("", id="reading-position", markup=False)
            yield PickerList()
            yield ModalEditor(
                placeholder="i: write a free-text answer", id="answer"
            )
            yield Static("", id="answer-result", markup=False)
            yield hints()

    def on_mount(self):
        self.show_question()
        self.start_reading_status()

    def show_question(self):
        q = self.questions[self.index]
        self.query_one("#question-title", Label).update(_safe_remote_text(
            f"Question {self.index + 1}/{len(self.questions)} · non-blocking"
        ))
        reader = self.query_one("#question-body", PanelReader)
        reader.load_text(_safe_remote_text(q["title"]))
        cursor, scroll = self.read_positions.get(self.index, ((0, 0), 0))
        reader.move_cursor(cursor)
        reader.call_after_refresh(reader.scroll_to, y=scroll, animate=False)
        listing = self.query_one(PickerList)
        listing.clear_options()
        for n, label in enumerate(q["options"], 1):
            listing.add_option(Option(Text(_safe_remote_text(f"{n}. {label}"))))
        listing.add_option(Option(Text("Write a different answer…")))
        listing.highlighted = 0
        listing.display = bool(q["options"])
        editor = self.query_one("#answer", ModalEditor)
        editor.load_text(self.drafts[self.index])
        editor.set_mode("NORMAL")
        (listing if q["options"] else editor).focus()
        self.update_hint()

    def check_action(self, action, parameters):
        if action == "dispatch_shortcut":
            name, _ = parameters
            if isinstance(self.focused, ModalEditor) and name in {
                "down", "up", "edit"
            }:
                return False  # Preserve the editor's complete Vim grammar.
        return super().check_action(action, parameters)

    def action_edit(self):
        editor = self.query_one("#answer", ModalEditor)
        editor.focus()
        editor.set_mode("INSERT")

    def action_question(self, direction):
        if self.submitting:
            return
        reader = self.query_one("#question-body", PanelReader)
        self.read_positions[self.index] = (reader.cursor_location, reader.scroll_y)
        draft = self.query_one("#answer", ModalEditor).text
        self.drafts[self.index] = draft
        if draft.strip() != self.answers[self.index]:
            self.answers[self.index] = ""
        self.index = (self.index + direction) % len(self.questions)
        self.show_question()

    async def action_submit(self):
        if self.submitting or self.focused is self.query_one("#question-body"):
            return
        editor = self.query_one("#answer", ModalEditor)
        if isinstance(self.focused, PickerList):
            index = self.query_one(PickerList).highlighted
            options = self.questions[self.index]["options"]
            if index is None or index >= len(options):
                self.action_edit()
                return
            editor.load_text(options[index])
        self.answers[self.index] = editor.text.strip()
        self.drafts[self.index] = editor.text
        result = self.query_one("#answer-result", Static)
        if not self.answers[self.index]:
            result.update("Enter an answer; nothing has been sent.")
            return
        if not all(self.answers):
            reader = self.query_one("#question-body", PanelReader)
            self.read_positions[self.index] = (
                reader.cursor_location, reader.scroll_y
            )
            self.index = self.answers.index("")
            self.show_question()
            return
        text = supplemental_answer_prompt(
            (q["title"], answer)
            for q, answer in zip(self.questions, self.answers)
        )
        self.submitting = True
        try:
            if await self.client.answer_async(self.sid, self.identities, text):
                self.dismiss(None)
            else:
                result.update(self.client.notice)
        except (ValueError, OSError):
            result.update("Could not submit answer. Your answer is retained.")
        finally:
            self.submitting = False


class QueuePanel(Overlay):
    key_layer = "queue"
    local_actions = {"cancel", "down", "up", "edit", "delete", "move"}

    def __init__(self, client, sid: str):
        super().__init__()
        self.client, self.sid = client, sid
        self.signature = None
        self.last_notice = client.notice
        self.requested_order = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label(
                "Server-owned queue · "
                + self.client.keys.layer_label("queue", "choose")
                + ": full prompt / edit", markup=False
            )
            yield PickerList()
            yield Static("", id="queue-status", markup=False)
            yield hints()

    def on_mount(self) -> None:
        self.paint()
        self.set_interval(0.25, self.paint)
        self.query_one(OptionList).focus()

    def paint(self) -> None:
        if self.client.notice != self.last_notice:
            self.last_notice = self.client.notice
            self.query_one("#queue-status", Static).update(
                _safe_remote_text(self.last_notice)
            )
        queue = self.client.workspace.view(self.sid).queue
        signature = describe(queue)
        if signature == self.signature:
            return
        listing = self.query_one(OptionList)
        old = listing.highlighted
        selected = self.selected_id()
        listing.clear_options()
        for q in queue:
            label = f"{q['kind']} · {q['prompt_preview']}\n{q['msg_id']} · {q['image_count']} images / {q['file_count']} files"
            listing.add_option(
                Option(Text(_safe_remote_text(label)), id=q["msg_id"])
            )
        ids = [q["msg_id"] for q in queue]
        if self.requested_order == ids:
            self.query_one("#queue-status", Static).update(
                "Server confirmed queue order."
            )
            self.requested_order = None
        listing.highlighted = (
            ids.index(selected) if selected in ids else
            min(old or 0, len(queue) - 1) if queue else None
        )
        self.signature = signature

    def selected_id(self):
        listing = self.query_one(OptionList)
        if listing.highlighted is not None and listing.option_count:
            return listing.get_option_at_index(listing.highlighted).id
        return None

    def action_edit(self):
        if msg_id := self.selected_id():
            self.app.push_screen(
                QueueEdit(self.client, self.sid, msg_id, edit=True)
            )

    def action_delete(self):
        if msg_id := self.selected_id():
            self.app.push_screen(QueueCancel(self.client, self.sid, msg_id))

    async def action_move(self, offset):
        from cc_remote.protocol import ReorderQueuedQueries

        msg_id = self.selected_id()
        ids = [q["msg_id"] for q in self.client.workspace.view(self.sid).queue]
        if msg_id not in ids:
            return
        index = ids.index(msg_id)
        target = index + offset
        if not 0 <= target < len(ids):
            return
        order = ids.copy()
        order[index], order[target] = order[target], order[index]
        self.requested_order = order
        status = self.query_one("#queue-status", Static)
        status.update("Order requested; waiting for server list.")
        sent = await self.client._send(ReorderQueuedQueries(
            sid=self.sid, expected=ids, order=order,
            cmd_id=uuid.uuid4().hex, client_id=self.client.client_id,
        ))
        if not sent:
            self.requested_order = None
            status.update(_safe_remote_text(self.client.notice))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        event.stop()
        self.app.push_screen(QueueEdit(self.client, self.sid, event.option.id))


class QueueCancel(Overlay):
    key_layer = "confirmation"
    local_actions = {"cancel", "yes", "down", "up"}

    def __init__(self, client, sid, msg_id):
        super().__init__()
        self.client, self.sid, self.msg_id = client, sid, msg_id
        self.submitting = False

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label("Cancel this queued message?", markup=False)
            yield Label(self.msg_id, markup=False)
            yield PickerList(Option("No", id="no"), Option("Yes", id="yes"))
            yield Static("", id="cancel-result", markup=False)
            yield hints()

    def on_mount(self):
        self.query_one(OptionList).focus()

    async def on_option_list_option_selected(self, event):
        event.stop()
        if event.option.id == "yes":
            await self.action_yes()
        else:
            self.dismiss(None)

    async def action_yes(self):
        from cc_remote.protocol import CancelQueuedQuery

        if self.submitting:
            return
        self.submitting = True
        if await self.client._send(CancelQueuedQuery(
            sid=self.sid, msg_id=self.msg_id,
            cmd_id=uuid.uuid4().hex, client_id=self.client.client_id,
        )):
            self.dismiss(True)
        else:
            self.submitting = False
            self.query_one("#cancel-result", Static).update(self.client.notice)


class QueueEdit(Overlay):
    key_layer = "form"
    local_actions = {"cancel", "submit", "field_help"}

    def __init__(self, client, sid: str, msg_id: str, *, edit=False):
        super().__init__()
        self.client, self.sid, self.msg_id = client, sid, msg_id
        self.request_id = uuid.uuid4().hex
        self.loaded = False
        self.update_request_id = None
        self.edit_on_load = edit

    def compose(self) -> ComposeResult:
        with Vertical(classes="tui-panel"):
            yield Label(f"Queued message · {self.msg_id}", markup=False)
            yield ModalEditor(locked=True, id="queued-prompt")
            yield Static(
                "Loading full prompt…", id="queue-result", markup=False
            )
            yield Label(
                f"{self.client.keys.layer_label('form', 'help')}: "
                "review cancellation of this queued message", markup=False
            )
            yield hints()

    async def on_mount(self) -> None:
        from cc_remote.protocol import GetQueuedQuery

        accepted = await self.client._send(
            GetQueuedQuery(
                sid=self.sid,
                msg_id=self.msg_id,
                cmd_id=self.request_id,
                client_id=self.client.client_id,
            )
        )
        if not accepted:
            self.query_one("#queue-result", Static).update(self.client.notice)
        self.set_interval(0.1, self.receive_detail)

    def receive_detail(self) -> None:
        if self.update_request_id:
            result = self.client.queue_update_results.pop(
                self.update_request_id, None
            )
            if result:
                self.update_request_id = None
                message = (
                    "Queue edit saved by server."
                    if result.get("updated")
                    else "Queue edit rejected: " + str(
                        result.get("error") or result.get("message")
                        or "Message already left the queue"
                    )
                )
                self.query_one("#queue-result", Static).update(
                    _safe_remote_text(message)
                )
        detail = self.client.queue_details.pop(self.request_id, None)
        if not detail:
            return
        result = self.query_one("#queue-result", Static)
        if detail.get("prompt") is None:
            result.update(
                detail.get("error") or "Message already left the queue"
            )
            return
        editor = self.query_one(ModalEditor)
        editor.load_text(detail["prompt"])
        editor.locked = False
        editor.focus()
        if self.edit_on_load:
            editor.set_mode("INSERT")
            self.edit_on_load = False
        self.loaded = True
        result.update(_safe_remote_text(
            "Full prompt. i: edit; "
            + self.client.keys.layer_label("form", "confirm") + ": save. "
            "Attachments are preserved. " + (detail.get("error") or "")
        ))

    def on_unmount(self) -> None:
        self.client.queue_reads.pop(self.request_id, None)
        self.client.queue_details.pop(self.request_id, None)
        if self.update_request_id:
            self.client.queue_updates.pop(self.update_request_id, None)
            self.client.queue_update_results.pop(self.update_request_id, None)

    def action_field_help(self) -> None:
        self.app.push_screen(QueueCancel(self.client, self.sid, self.msg_id))

    async def action_submit(self) -> None:
        from cc_remote.protocol import UpdateQueuedQuery

        if self.update_request_id:
            return
        args = dict(
            sid=self.sid,
            msg_id=self.msg_id,
            cmd_id=uuid.uuid4().hex,
            client_id=self.client.client_id,
        )
        if self.loaded:
            command = UpdateQueuedQuery(
                **args, prompt=self.query_one(TextArea).text
            )
        else:
            return
        self.update_request_id = command.cmd_id
        self.client.queue_updates[command.cmd_id] = (self.sid, self.msg_id)
        self.query_one("#queue-result", Static).update(
            "Waiting for server confirmation; edit not yet confirmed."
        )
        if await self.client._send(command):
            self.query_one(ModalEditor).set_mode("NORMAL")
        else:
            self.client.queue_updates.pop(command.cmd_id, None)
            self.update_request_id = None
            self.query_one("#queue-result", Static).update(self.client.notice)
