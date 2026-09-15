"""Keyboard-native session setup using the same catalogs/commands as Web."""

from __future__ import annotations

import uuid

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from cc_remote import protocol as p
from cc_remote.tui_actions import build_action
from cc_remote.tui_modal import ModalEditor, Overlay, PickerList, hints
from cc_remote.tui_panels import ActionPicker


class ValuePicker(ActionPicker):
    def __init__(self, title: str, choices: list[tuple[str, object]], *, selected=...):
        super().__init__(None, None)
        self.title_text, self.choices = title, choices
        self.selected = selected

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label(self.title_text, markup=False)
            yield ModalEditor(
                classes="search-editor",
                id="search",
                placeholder="Search choices (see shortcuts below)",
            )
            yield PickerList()
            yield hints()

    def filter(self, text: str) -> None:
        listing = self.query_one(OptionList)
        listing.clear_options()
        for index, (label, _) in enumerate(self.choices):
            if all(
                word in label.casefold() for word in text.casefold().split()
            ):
                listing.add_option(Option(Text(label), id=str(index)))
        listing.highlighted = 0 if listing.option_count else None
        if not text and self.selected is not ...:
            for index, (_, value) in enumerate(self.choices):
                if value == self.selected:
                    listing.highlighted = index
                    break

    def open_form(self, name: str) -> None:
        # A wrapper distinguishes choosing the engine default (None) from Esc.
        self.dismiss((self.choices[int(name)][1],))


class TextValue(Overlay):
    key_layer = "form"
    local_actions = {"cancel", "submit"}

    def __init__(self, title: str, value: str):
        super().__init__()
        self.title_text, self.value = title, value

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label(self.title_text, markup=False)
            yield ModalEditor(self.value)
            yield hints()

    def on_mount(self):
        self.query_one(ModalEditor).focus()

    async def action_submit(self):
        self.dismiss((self.query_one(ModalEditor).text.strip(),))


class SettingsForm(Overlay):
    key_layer = "picker"
    local_actions = {"cancel", "edit", "down", "up"}

    def __init__(self, client, sid, *, new=False, initial_field=None):
        super().__init__()
        self.client, self.sid, self.new = client, sid, new
        self.initial_field = initial_field
        row = client.workspace.catalog.get(sid or client.attached_sid, {})
        self.engine = client.engine if new else row.get("engine", client.engine)
        self.space = client.space if new else row.get("space", client.space)
        self.values = {"cwd": "~" if new else row.get("cwd") or "~"}
        self.profiles = {
            key: row.get(key)
            for key in ("claude_profile_id", "codex_profile_id")
        }
        self.profiles[
            "claude_profile_id"
            if self.engine == "codex"
            else "codex_profile_id"
        ] = None
        profile_key = self.engine + "_profile_id"
        if not self.profiles.get(profile_key):
            self.profiles[profile_key] = client.workspace.reports.get(
                self.engine + "_profiles", {}
            ).get("default_" + profile_key)
        if new:
            self.values.update(
                {name: None for name in self.fields() if name != "cwd"}
            )
        else:
            settings = client.workspace.view(sid).presentation.settings
            for field, (event, attr, _) in SETTING_FIELDS.items():
                self.values[field] = settings.get(event, {}).get(attr)
        self.signature = None
        self.submitting = False
        self.pending_change = None
        self.last_choices = {}

    def fields(self):
        fields = ["cwd"] if self.new and self.space == "code" else []
        fields += ["model", "effort"]
        if self.engine == "codex":
            if self.space == "code":
                fields += [
                    "permission_profile",
                    "permission_mode",
                    "web_search",
                ]
            fields += ["collaboration_mode", "service_tier"]
        elif not self.new and self.space == "code":
            fields += ["permission_mode"]
        return fields

    def args(self, kind):
        values = {"cwd": self.values["cwd"], **self.profiles}
        if kind == "models":
            values["engine"] = self.engine
        else:
            values.pop("claude_profile_id", None)
        return values

    def catalog(self, kind):
        key = (kind, *self.client.capability_key(self.args(kind)))
        return self.client.settings_catalogs.get(key, {})

    def compose(self):
        with Vertical(classes="tui-panel"):
            yield Label(
                f"{'New session' if self.new else 'Settings'} · "
                f"{self.engine.title()} / {self.space.title()} · {self.sid or 'new'}",
                markup=False,
            )
            yield Label(
                self.client.keys.layer_label("picker", "choose")
                + ": choose/edit field · "
                + (
                    "Select the create row to submit"
                    if self.new
                    else "Choose a value and press Enter to apply"
                ),
                markup=False,
            )
            if self.space == "work":
                yield Label(
                    "Work assigns its directory and safe permissions.",
                    markup=False,
                )
            yield PickerList()
            yield Static("", id="form-result", markup=False)
            yield hints()

    async def on_mount(self):
        self.paint()
        self.query_one(OptionList).focus()
        self.set_interval(0.25, self.paint)
        await self.fetch()
        if self.initial_field in self.fields():
            self.query_one(OptionList).highlighted = self.fields().index(
                self.initial_field
            )

    async def fetch(self):
        await self.client._send(
            p.GetModels(sid=self.sid, **self.args("models"))
        )
        if self.engine == "codex" and self.space == "code":
            await self.client._send(
                p.GetPermissionProfiles(
                    sid=self.sid, **self.args("permission_profiles")
                )
            )

    def paint(self):
        if not self.new:
            sid = self.client.workspace.rekeys.get(self.sid, self.sid)
            settings = self.client.workspace.view(sid).presentation.settings
            for field, (event, attr, _) in SETTING_FIELDS.items():
                if event in settings:
                    self.values[field] = settings[event].get(attr)
            if "fast" in settings:
                self.values["service_tier"] = (
                    "fast" if settings["fast"].get("on") else "default"
                )
            if self.pending_change:
                field, value, ticket, previous = self.pending_change
                result = self.client.action_results.get(ticket, {})
                if result.get("error"):
                    self.query_one("#form-result", Static).update(result["error"])
                    self.pending_change = None
                event = "fast" if field == "service_tier" else SETTING_FIELDS[field][0]
                if self.pending_change and event in settings and settings[event] is not previous:
                    self.query_one("#form-result", Static).update(
                        "Effective setting confirmed by the server."
                    )
                    self.pending_change = None
        signature = (
            repr(self.values),
            repr(self.catalog("models")),
            repr(self.catalog("permission_profiles")),
        )
        if signature == self.signature:
            return
        self.signature = signature
        listing = self.query_one(OptionList)
        selected = listing.highlighted
        listing.clear_options()
        for field in self.fields():
            value = self.values.get(field)
            label = str(value) if value is not None else "Engine default"
            listing.add_option(
                Option(
                    Text(f"{field.replace('_', ' ').title()}: {label}"),
                    id=field,
                )
            )
        if self.new:
            listing.add_option(Option("Create session", id="review"))
        listing.highlighted = min(selected or 0, listing.option_count - 1)

    def choices(self, field):
        choices = [("Engine default (no override)", None)] if self.new else []
        if field == "model":
            models = self.catalog("models").get("models", [])
            if self.engine == "claude":
                # Claude has no model/list RPC; same curated choices as Web.
                models = [{"id": value} for value in CLAUDE_MODELS]
            for model in models:
                identity = model.get("id")
                if identity:
                    choices.append(
                        (str(model.get("display_name") or identity), identity)
                    )
        elif field == "effort":
            catalog = self.catalog("models")
            selected = self.values.get("model") or catalog.get("default_model")
            model = next(
                (
                    m
                    for m in catalog.get("models", [])
                    if m.get("id") == selected
                ),
                {},
            )
            efforts = model.get("efforts", [])
            if self.engine == "claude":
                from cc_remote.wrapper.claude_controls import CLAUDE_EFFORTS

                efforts = sorted(CLAUDE_EFFORTS)
            for effort in efforts:
                value = (
                    effort.get("id") or effort.get("effort")
                    if isinstance(effort, dict)
                    else effort
                )
                if value:
                    choices.append((str(value), value))
        elif field == "permission_profile":
            for profile in self.catalog("permission_profiles").get(
                "profiles", []
            ):
                if profile.get("allowed"):
                    choices.append(
                        (
                            profile["id"]
                            + " · "
                            + (profile.get("description") or ""),
                            profile["id"],
                        )
                    )
        else:
            values = {
                "permission_mode": ["never", "on-request", "untrusted"]
                if self.engine == "codex"
                else [
                    "default",
                    "acceptEdits",
                    "auto",
                    "bypassPermissions",
                    "plan",
                ],
                "web_search": ["cached", "live"],
                "collaboration_mode": ["default", "plan"],
                "service_tier": ["default", "fast"],
            }.get(field, [])
            choices += [(value, value) for value in values]
        return choices

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        event.stop()
        self.open_field(event.option.id)

    def action_edit(self):
        listing = self.query_one(OptionList)
        if listing.highlighted is not None:
            self.open_field(listing.get_option_at_index(listing.highlighted).id)

    def open_field(self, field):
        if self.submitting:
            return
        if self.pending_change:
            self.query_one("#form-result", Static).update(
                "Still waiting for the server; no duplicate command sent."
            )
            return
        if field == "review" and self.new:
            self.run_worker(self.action_submit())
            return
        if field == "cwd":
            from cc_remote.tui_directories import DirectoryPicker

            dialog = DirectoryPicker(self.client, self.values["cwd"])
        else:
            choices = self.choices(field)
            if not choices or (self.new and len(choices) == 1):
                self.query_one("#form-result", Static).update(
                    "Catalog not available yet; reopen or wait for the server. "
                    "No permissions were changed."
                )
                # No catalog must not offer a guessed Full Access option.
                return
            dialog = ValuePicker(
                field.replace("_", " ").title(), choices,
                selected=self.last_choices.get(field, ...),
            )

        async def chosen(result):
            if result is None:
                return
            value = result[0]
            if field != "cwd" and value not in [
                v for _, v in self.choices(field)
            ]:
                self.query_one("#form-result", Static).update(
                    "Available choices changed; reopen this field before applying."
                )
                return
            if field == "cwd" and (not value or "\n" in value):
                self.query_one("#form-result", Static).update(
                    "Enter one nonempty directory."
                )
                return
            if self.new:
                self.values[field] = value
                if field == "cwd":
                    self.values["permission_profile"] = None
                    self.values["model"] = self.values["effort"] = None
                    await self.fetch()
                elif field == "model":
                    self.values["effort"] = None
                self.paint()
            else:
                import json

                _, attr, command = SETTING_FIELDS[field]
                try:
                    message = build_action(
                        command,
                        json.dumps({attr: value}),
                        self.sid,
                        self.client.client_id,
                    )
                except ValueError as exc:
                    self.query_one("#form-result", Static).update(str(exc))
                    return

                self.submitting = True
                self.last_choices[field] = value
                event = "fast" if field == "service_tier" else SETTING_FIELDS[field][0]
                previous = self.client.workspace.view(self.sid).presentation.settings.get(event)
                self.pending_change = (field, value, message.cmd_id, previous)
                try:
                    if await self.client.execute_action(message):
                        self.query_one("#form-result", Static).update(
                            "Submitted; displayed settings follow the server confirmation."
                        )
                        self.paint()
                    else:
                        self.pending_change = None
                        self.query_one("#form-result", Static).update(
                            self.client.notice
                        )
                except (OSError, ValueError) as exc:
                    self.pending_change = None
                    self.query_one("#form-result", Static).update(str(exc))
                finally:
                    self.submitting = False

        self.app.push_screen(dialog, chosen)

    async def action_submit(self):
        if not self.new or self.submitting:
            return
        try:
            args = {name: self.values.get(name) for name in self.fields()}
            profile = args.get("permission_profile")
            if profile and profile not in [
                value for _, value in self.choices("permission_profile")
            ]:
                raise ValueError(
                    "Selected permission is not allowed in this directory."
                )
            for field in ("model", "effort"):
                if args.get(field) is not None and args[field] not in [
                    value for _, value in self.choices(field)
                ]:
                    raise ValueError(
                        f"Selected {field} is no longer available."
                    )
            message = p.NewSession(
                **args,
                **self.profiles,
                engine=self.engine,
                space=self.space,
                request_id=uuid.uuid4().hex,
                cmd_id=uuid.uuid4().hex,
                client_id=self.client.client_id,
            )
        except ValueError as exc:
            self.query_one("#form-result", Static).update(str(exc))
            return
        self.submitting = True
        try:
            if await self.client.execute_action(message):
                self.created(True)
            else:
                self.query_one("#form-result", Static).update(self.client.notice)
        finally:
            self.submitting = False

    def created(self, success):
        if success:
            self.dismiss(None)


SETTING_FIELDS = {
    "model": ("model", "model", "set_model"),
    "effort": ("effort", "effort", "set_effort"),
    "permission_profile": (
        "permission_profile",
        "profile",
        "set_permission_profile",
    ),
    "permission_mode": ("perm", "mode", "set_perm"),
    "web_search": ("web_search", "mode", "set_web_search"),
    "collaboration_mode": (
        "collaboration_mode",
        "mode",
        "set_collaboration_mode",
    ),
    "service_tier": ("fast", "service_tier", "set_service_tier"),
}

CLAUDE_MODELS = (
    "claude-opus-5[1m]",
    "claude-mythos-5-1",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-fable-5-1",
)
