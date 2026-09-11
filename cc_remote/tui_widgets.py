"""Local editing and session selection widgets; no transport side effects."""

from __future__ import annotations

from textual import events
from textual.strip import Strip
from textual.widgets import TextArea
from textual.widgets.text_area import Selection
from rich.cells import get_character_cell_size
from rich.segment import Segment
from rich.text import Text
from textual.expand_tabs import expand_text_tabs_from_widths

from cc_remote.tui_vim import VimGrammar
from cc_remote.tui_vim_history import VimHistory


class VimArea(VimGrammar, TextArea):
    """Shared Vim text commands; locked readers never mutate their source."""

    COMPONENT_CLASSES = TextArea.COMPONENT_CLASSES | {"vim--yank"}
    DEFAULT_CSS = """
    VimArea > .vim--yank { background: $primary 45%; color: $text; }
    """

    def __init__(self, **kwargs):
        self.yank_range = None
        self.yank_timer = None
        self.yank_revision = 0
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

    def clear_yank(self, revision=None):
        if revision is not None and revision != self.yank_revision:
            return
        self.yank_revision += 1
        if self.yank_timer is not None:
            self.yank_timer.stop()
            self.yank_timer = None
        if self.yank_range is not None:
            self.yank_range = None
            self._line_cache.clear()
            self.refresh()

    def _set_document(self, text, language=None):
        self.clear_yank()
        return super()._set_document(text, language)

    def edit(self, edit):
        self.clear_yank()
        return super().edit(edit)

    def undo(self):
        self.clear_yank()
        return super().undo()

    def redo(self):
        self.clear_yank()
        return super().redo()

    def on_unmount(self):
        self.clear_yank()

    def paint_yank(self, strip, y):
        if self.yank_range is None:
            return strip
        wrapped = self.wrapped_document
        offset = y + int(self.scroll_y)
        if not 0 <= offset < len(wrapped._offset_to_line_info):
            return strip
        row, section = wrapped._offset_to_line_info[offset]
        start, end = self.yank_range
        if not start[0] <= row <= end[0]:
            return strip
        line = self.document.get_line(row)
        breaks = [0, *wrapped.get_offsets(row), len(line)]
        begin, limit = breaks[section:section + 2]
        left = max(begin, start[1] if row == start[0] else 0)
        right = min(limit, end[1] if row == end[0] else len(line))
        # Use Textual's cached tab widths and Rich's terminal-cell widths,
        # not Python character indices, for CJK/emoji and wrapped sections.
        tabs = wrapped.get_tab_widths(row)[line[:begin].count("\t"):]

        def cell(column):
            prefix = Text(line[begin:column])
            return expand_text_tabs_from_widths(prefix, tabs).cell_len

        shift = self.gutter_width - int(self.scroll_x)
        x1, x2 = cell(left) + shift, cell(right) + shift
        if not line and row < end[0]:
            x2 = x1 + 1
        x1, x2 = max(0, x1), min(strip.cell_length, x2)
        if left > right or x1 >= x2:
            return strip
        style = self.get_component_rich_style("vim--yank")
        return Strip.join([
            strip.crop(0, x1),
            Strip(Segment.apply_style(
                strip.crop(x1, x2), post_style=style
            ), cell_length=x2 - x1),
            strip.crop(x2, strip.cell_length),
        ])

    def yank_text(self, start, end):
        return self.document.get_text_range(start, end)

    def finish_yank(self, start, end, original, linewise):
        # Line motions keep the column; text objects/character motions land
        # at the copied range's start. This applies to locked readers too.
        target = start
        if linewise:
            target = (start[0], min(original[1], max(
                0, len(self.document.get_line(start[0])) - 1
            )))
        self.move_cursor(target)
        self.set_mode("NORMAL")
        self.app.client.notice = f"Copied {len(self.register)} characters"
        timeout = self.app.client.keys.yank_highlight_ms
        if timeout and start != end:
            self.yank_range = (start, end)
            self._line_cache.clear()
            self.refresh()
            revision = self.yank_revision
            self.yank_timer = self.set_timer(
                timeout / 1000, lambda: self.clear_yank(revision)
            )

    def set_mode(self, mode: str) -> None:
        if mode != "NORMAL":
            self.clear_yank()
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
        self.clear_yank()
        self.prefix = ""
        self.history.finish()

    @property
    def visual_caret(self):
        point = (self.visual_cursor
                 if getattr(self, "vim_mode", "NORMAL") == "VISUAL" else None)
        # A Visual deletion may shrink the document before leaving the mode.
        return self.clamp_visitable(point) if point is not None else None

    def render_line(self, y):
        # Modal editors own their mode transitions too. Refresh the caret map
        # before TextArea builds its render cache key, including on Visual exit.
        if self.vim_mode != getattr(self, "_painted_vim_mode", None):
            self._painted_vim_mode = self.vim_mode
            self._line_cache.clear()
            self._recompute_cursor_offset()
        return self.paint_yank(super().render_line(y), y)

    @property
    def _draw_cursor(self):
        # TextArea paints selection.end, the exclusive boundary. Visual mode
        # keeps that range for selection/copy but paints its logical caret.
        return self.visual_caret is None and super()._draw_cursor

    def _recompute_cursor_offset(self):
        if self.visual_caret is None:
            super()._recompute_cursor_offset()
        else:
            self._cursor_offset = self.wrapped_document.location_to_offset(
                self.visual_caret
            )

    def _render_line(self, y):
        strip = super()._render_line(y)
        point = self.visual_caret
        if point is None or not super()._draw_cursor or not self._theme:
            return strip
        cursor_x, cursor_y = self._cursor_offset
        if cursor_y != y + self.scroll_y:
            return strip
        style = self._theme.cursor_style
        if not style:
            return strip
        x = cursor_x + self.gutter_width - int(self.scroll_x)
        row, column = point
        line = self.document.get_line(row)
        char = line[column:column + 1] or " "
        width = max(1, get_character_cell_size(char))
        left, right = max(0, x), min(strip.cell_length, x + width)
        if left >= right:
            return strip
        return Strip.join([
            strip.crop(0, left),
            Strip(Segment.apply_style(
                strip.crop(left, right), post_style=style
            ), cell_length=right - left),
            strip.crop(right, strip.cell_length),
        ])

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
        self.clear_yank()
        start, end = sorted((start, end))
        if operator == "y" and start == end:
            self.app.client.notice = "Nothing to copy"
            self.prefix = ""
            return
        self.register = (self.yank_text(start, end) if operator == "y"
                         else self.document.get_text_range(start, end))
        self.line_register = linewise
        self.app.vim_register = self.register, linewise
        if operator == "y":
            self.app.copy_to_clipboard(self.register)
            self.finish_yank(start, end, original, linewise)
            return
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
        self.move_cursor(start)
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
