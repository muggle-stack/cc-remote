"""Independent modal key scope and shared Vim editing for terminal forms."""

import inspect

from textual import actions, events
from textual.binding import BindingsMap
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.keys import key_to_character

from cc_remote.tui_widgets import Composer
from cc_remote.tui_keys import LAYERS


class PickerList(OptionList, inherit_bindings=False):
    """Only the owning dialog's registry may activate or navigate choices."""

    BINDINGS = []


class Overlay(ModalScreen, inherit_bindings=False):
    DEFAULT_CSS = """
    Overlay { align: center middle; background: $background 65%; }
    .tui-panel { width: 90%; max-width: 110; height: 85%; border: solid $primary; }
    .tui-panel TextArea, .tui-panel OptionList { height: 1fr; }
    .tui-panel .search-editor { height: 3; }
    .tui-panel Input { height: 3; }
    .tui-panel Label { height: auto; max-height: 6; }
    .tui-panel .key-hints { height: auto; color: $text-muted; }
    """
    BINDINGS = []
    key_layer = "panel"
    local_actions = {"cancel"}

    def on_mount(self) -> None:
        self.install_keys()

    def install_keys(self) -> None:
        self._bindings = BindingsMap(
            self.app.client.keys.layer_bindings(self.key_layer)
        )
        self.refresh_bindings()
        self.update_hint()

    def update_hint(self) -> None:
        mode = getattr(self.focused, "vim_mode", "NORMAL")
        text = self.app.client.keys.layer_help(
            self.key_layer, self.available_actions()
        )
        text = f"{self.key_layer.upper()} {mode} · " + text
        if self.key_layer == "form":
            text += " · i: edit"
        for hint in self.query(".key-hints"):
            hint.update(text)

    def check_action(self, action: str, parameters: tuple) -> bool:
        if action != "dispatch_shortcut":
            return action in self.available_actions()
        name, key = parameters
        _, target, _ = actions.parse(LAYERS[self.key_layer][name].action)
        if target not in self.available_actions():
            return False
        editor = self.focused
        if name in {"next_field", "previous_field"}:
            return True
        # Arrow/control navigation and confirmation also work while searching;
        # letters remain literal Insert input and never trigger panel actions.
        if (getattr(editor, "has_class", lambda _: False)("search-editor")
                and name in {"down", "up", "choose", "close", "first",
                             "last", "page_up", "page_down", "parent",
                             "open", "refresh"}
                and (key_to_character(key) is None
                     or not key_to_character(key).isprintable())):
            return True
        if getattr(editor, "vim_mode", "NORMAL") != "NORMAL" or getattr(
            editor, "prefix", ""
        ):
            return False
        # Normal Enter reviews first; mutating forms still require a second
        # Enter on their immutable prepared action. Insert never submits.
        return True

    def available_actions(self):
        return self.local_actions | {"next_field", "previous_field"} | (
            {"choose", "first", "last", "page_up", "page_down"}
            if self.query(OptionList) else set()
        )

    async def action_dispatch_shortcut(self, name, key):
        if not self.check_action("dispatch_shortcut", (name, key)):
            return
        _, action, args = actions.parse(LAYERS[self.key_layer][name].action)
        result = getattr(self, "action_" + action)(*args)
        if inspect.isawaitable(result):
            await result

    def action_next_field(self):
        self.focus_next()

    def action_previous_field(self):
        self.focus_previous()

    def action_choose(self):
        listings = self.query(OptionList)
        if listings:
            listings.first().action_select()

    def action_first(self):
        self.query_one(OptionList).action_first()

    def action_last(self):
        self.query_one(OptionList).action_last()

    def action_page_up(self):
        self.query_one(OptionList).action_page_up()

    def action_page_down(self):
        self.query_one(OptionList).action_page_down()

    def action_cancel(self) -> None:
        editor = self.focused
        if getattr(editor, "vim_mode", "NORMAL") != "NORMAL" or getattr(
            editor, "prefix", ""
        ):
            editor.set_mode("NORMAL")
            return
        self.dismiss(None)

    def on_screen_resume(self) -> None:
        for editor in self.query(ModalEditor):
            editor.set_mode(
                "INSERT" if editor.has_class("search-editor")
                or getattr(self, "initial_insert", False) else "NORMAL"
            )
        if hasattr(self, "initial_insert"):
            self.initial_insert = False
        self.update_hint()

    def on_unmount(self) -> None:
        # Never return from a nested dialog into an unnoticed Insert mode.
        self.app.mode = "NORMAL"
        self.app.shortcut_prefix = ()
        self.app.prefix = ""
        for editor in self.app.screen_stack[0].query(Composer):
            editor.set_mode("NORMAL")

    def move_selection(self, down: bool) -> None:
        listings = self.query(OptionList)
        if listings:
            listing = listings.first()
            (listing.action_cursor_down if down else listing.action_cursor_up)()
        else:
            (self.focus_next if down else self.focus_previous)()

    def action_down(self) -> None:
        self.move_selection(True)

    def action_up(self) -> None:
        self.move_selection(False)


class ModalEditor(Composer):
    """The same text objects/operators as the draft, scoped to this form."""

    def __init__(self, text: str = "", *, locked: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.load_text(text)
        self.locked = locked

    def set_mode(self, mode: str) -> None:
        if mode != "NORMAL":
            self.clear_yank()
        if self.locked and mode == "INSERT":
            return
        self.vim_mode = mode
        self.read_only = mode != "INSERT"
        self.prefix = ""
        self.history.begin() if mode == "INSERT" else self.history.finish()
        # This override keeps modal state local, but must retain VimArea's
        # inclusive Visual selection lifecycle on every form/reader surface.
        if mode == "VISUAL":
            self.begin_visual()
        else:
            self.visual_anchor = self.visual_cursor = None
        if self.is_mounted:
            self.screen.update_hint()

    def on_focus(self) -> None:
        if self.is_mounted:
            if self.has_class("search-editor"):
                self.set_mode("INSERT")
            self.screen.update_hint()

    def on_blur(self) -> None:
        self.clear_yank()
        self.set_mode("NORMAL")

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if (
                self.has_class("search-editor")
                and "escape" in self.app.client.keys.layers[
                    self.screen.key_layer
                ]["close"]
            ):
                self.screen.action_cancel()
                return
            if self.vim_mode != "NORMAL" or self.prefix:
                self.set_mode("NORMAL")
            elif (
                "escape"
                in self.app.client.keys.layers[self.screen.key_layer]["close"]
            ):
                self.screen.action_cancel()
            return
        # The shared Vim grammar consumes whole commands (yi(, 2yaw, …).
        # Locking is enforced at the mutation boundary, not per-character:
        # filtering i/a here would also reject read-only text objects.
        await super()._on_key(event)


class ModalInput(Input):
    """Masked answer input: never copy secret replies to a shared register."""

    vim_mode = "NORMAL"

    def action_submit(self):
        # Form confirmation is registry-owned, never Input's built-in Enter.
        pass

    def set_mode(self, mode: str) -> None:
        self.vim_mode = mode
        self.screen.update_hint()

    def _on_paste(self, event: events.Paste) -> None:
        if self.vim_mode != "INSERT":
            event.stop()
            event.prevent_default()
            return
        super()._on_paste(event)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if self.vim_mode != "NORMAL":
                self.set_mode("NORMAL")
            elif (
                "escape"
                in self.app.client.keys.layers[self.screen.key_layer]["close"]
            ):
                self.screen.action_cancel()
        elif self.vim_mode == "NORMAL":
            event.stop()
            event.prevent_default()
            if event.key == "i":
                self.set_mode("INSERT")
            elif event.key in {"h", "left"}:
                self.action_cursor_left()
            elif event.key in {"l", "right"}:
                self.action_cursor_right()
        else:
            await super()._on_key(event)


def hints() -> Static:
    return Static("", classes="key-hints", markup=False)
