"""Local editing and session selection widgets; no transport side effects."""

from __future__ import annotations

from textual import events
from textual.widgets import TextArea
from textual.widgets.text_area import Selection

from cc_remote.tui_vim import VimGrammar
from cc_remote.tui_vim_history import VimHistory


class VimArea(VimGrammar, TextArea):
    """Shared Vim text commands; locked readers never mutate their source."""

    def __init__(self, **kwargs):
        self.locked = kwargs.pop("read_only", False)
        super().__init__(read_only=True, **kwargs)
        self.vim_mode = "NORMAL"
        self.prefix = ""
        self.register = ""
        self.line_register = False
        self.history = VimHistory(
            self.history.max_checkpoints,
            self.history.checkpoint_timer,
            self.history.checkpoint_max_characters,
        )

    def set_mode(self, mode: str) -> None:
        if self.locked and mode == "INSERT":
            return
        self.vim_mode = mode
        self.read_only = self.locked or mode != "INSERT"
        self.prefix = ""
        self.app.mode = mode
        self.history.begin() if mode == "INSERT" else self.history.finish()
        if mode == "VISUAL":
            self.begin_visual()
        else:
            self.visual_anchor = self.visual_cursor = None

    def on_focus(self) -> None:
        self.app.mode = self.vim_mode

    def on_blur(self) -> None:
        self.prefix = ""
        self.history.finish()

    def paste_text(self, text: str) -> None:
        # Normal mode blocks typing, not an explicit paste. Only locked
        # readers are truly read-only. Keep pasted newlines out of key dispatch.
        if self.locked or not text:
            return
        self.prefix = ""
        normal = self.vim_mode != "INSERT"
        if normal:
            self.history.finish()
        result = self.replace(
            text, *self.selection, maintain_selection_offset=False
        )
        self.move_cursor(result.end_location)
        if normal:
            self.set_mode("NORMAL")

    async def _on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        self.paste_text(event.text)

    def action_paste(self) -> None:
        self.paste_text(self.app.clipboard)

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if (
            self.vim_mode != "INSERT"
            and not self.prefix
            and (await self.app.normal_shortcut(key))
        ):
            event.stop()
            event.prevent_default()
            return
        if key == "escape":
            event.stop()
            event.prevent_default()
            if self.vim_mode == "INSERT":
                row, col = self.cursor_location
                self.move_cursor((row, max(0, col - 1)))
            self.selection = Selection.cursor(self.cursor_location)
            self.set_mode("NORMAL")
            return
        if self.vim_mode == "INSERT":
            await super()._on_key(event)
            return
        if self.edit_key(key):
            event.stop()
            event.prevent_default()

    def edit_key(self, key: str) -> bool:
        if self.vim_command(key):
            return True
        if self.locked and key not in {"v", "V"}:
            return len(key) == 1 or key in {
                "space",
                "enter",
                "backspace",
                "delete",
                "ctrl+r",
            }
        row, col = self.cursor_location
        line = self.document.get_line(row)
        if key in {"i", "a", "I", "A"}:
            if key == "a":
                self.move_cursor((row, min(col + 1, len(line))))
            elif key == "I":
                self.move_cursor((row, len(line) - len(line.lstrip())))
            elif key == "A":
                self.move_cursor((row, len(line)))
            self.set_mode("INSERT")
        elif key in {"o", "O"}:
            target = (row, len(line)) if key == "o" else (row, 0)
            self.history.begin()
            self.insert("\n", target, maintain_selection_offset=False)
            self.move_cursor((row + 1 if key == "o" else row, 0))
            self.set_mode("INSERT")
        elif key == "v":
            self.set_mode("VISUAL")
        elif key == "V":
            self.selection = Selection((row, 0), (row, len(line)))
            self.set_mode("VISUAL LINE")
        elif key == "x":
            if col < len(line):
                self.operate("d", (row, col), (row, col + 1))
        elif key == "p":
            text, linewise = getattr(
                self.app, "vim_register", (self.app.clipboard, False)
            )
            if text:
                if linewise:
                    self.insert(
                        "\n" + text.removesuffix("\n"),
                        (row, len(line)),
                        maintain_selection_offset=False,
                    )
                    self.move_cursor((row + 1, 0))
                else:
                    self.insert(
                        text,
                        (row, min(col + 1, len(line))),
                        maintain_selection_offset=False,
                    )
                self.history.checkpoint()
        elif key == "u":
            self.undo()
        elif key == "ctrl+r":
            self.redo()
        else:
            # Normal mode must never turn an unimplemented printable binding
            # into literal input. Navigation keys may use TextArea defaults.
            return len(key) == 1 or key in {
                "space",
                "enter",
                "backspace",
                "delete",
            }
        return True

    def operate(
        self,
        operator: str,
        start: tuple[int, int],
        end: tuple[int, int],
        *,
        linewise: bool = False,
    ) -> None:
        if self.locked and operator != "y":
            self.prefix = ""
            self.app.client.notice = (
                "Read-only text: use y to copy or "
                + self.app.client.keys.label("quote") + " to quote"
            )
            return
        original = self.vim_cursor_location()
        start, end = sorted((start, end))
        self.register = self.document.get_text_range(start, end)
        self.line_register = linewise
        self.app.vim_register = self.register, linewise
        if operator == "y":
            self.app.copy_to_clipboard(self.register)
        else:
            self.history.checkpoint()
            if operator == "c":
                self.history.begin()
            delete_start, delete_end = start, end
            if linewise and operator == "c":
                # cc changes the line's contents, retaining its line boundary.
                last_row = (
                    end[0] - 1 if end[1] == 0 and end[0] > start[0] else end[0]
                )
                delete_end = (last_row, len(self.document.get_line(last_row)))
            elif (
                linewise
                and start[0] > 0
                and end
                == (
                    self.document.line_count - 1,
                    len(self.document.get_line(self.document.line_count - 1)),
                )
            ):
                # The final line has no following newline; remove its preceding
                # separator without adding that separator to the yank register.
                delete_start = (
                    start[0] - 1,
                    len(self.document.get_line(start[0] - 1)),
                )
            self.delete(
                delete_start, delete_end, maintain_selection_offset=False
            )
        self.move_cursor(original if self.locked and operator == "y" else start)
        self.set_mode("INSERT" if operator == "c" else "NORMAL")


class Composer(VimArea):
    """Editable draft (separate from readers for focus and widget queries)."""

    async def _on_key(self, event: events.Key) -> None:
        if (self.vim_mode == "NORMAL" and not self.prefix
                and not self.app.shortcut_prefix
                and len(self.app.screen_stack) == 1):
            name = self.app.client.keys.match("draft", event.key)
            if name:
                from cc_remote.tui_keys import LAYERS

                event.stop()
                event.prevent_default()
                await self.app.run_action(LAYERS["draft"][name].action)
                return
        await super()._on_key(event)
