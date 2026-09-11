"""Shared operator/count/motion grammar for TextArea readers and editors.

No transport, filesystem access, key replay or shell commands live here.
All ranges use TextArea's half-open character offsets (not terminal columns).
"""

import re

from textual.keys import key_to_character
from textual.widgets.text_area import Selection

from cc_remote.tui_text_objects import text_object


KEYS = {
    "left_parenthesis": "(",
    "right_parenthesis": ")",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "left_curly_bracket": "{",
    "right_curly_bracket": "}",
    "less_than_sign": "<",
    "greater_than_sign": ">",
    "quotation_mark": '"',
    "apostrophe": "'",
    "grave_accent": "`",
    "dollar_sign": "$",
    "circumflex_accent": "^",
    "percent_sign": "%",
    "semicolon": ";",
    "comma": ",",
    "space": " ",
}
MOTIONS = {
    "h",
    "j",
    "k",
    "l",
    "w",
    "W",
    "b",
    "B",
    "e",
    "E",
    "0",
    "^",
    "$",
    "G",
    "gg",
    "ge",
    "gE",
    "%",
    "{",
    "}",
    ";",
    ",",
}


class VimGrammar:
    """Mixin: host provides prefix, vim_mode, operate(), set_mode()."""

    last_find = None
    visual_anchor = None
    visual_cursor = None

    def vim_cursor_location(self):
        if self.vim_mode == "VISUAL" and self.visual_cursor is not None:
            return self.visual_cursor
        return self.cursor_location

    def begin_visual(self):
        self.visual_anchor = self.cursor_location
        self.move_visual(self.cursor_location)

    def move_visual(self, point):
        # Vim includes the character under BOTH endpoints. TextArea uses an
        # exclusive end, so keep the logical cursor separate from its boundary.
        row, col = point
        col = min(col, max(0, len(self.document.get_line(row)) - 1))
        point = (row, col)
        anchor = self.visual_anchor or point
        self.visual_anchor, self.visual_cursor = anchor, point

        def after(location):
            r, c = location
            return r, min(c + 1, len(self.document.get_line(r)))

        self.selection = (Selection(anchor, after(point)) if point >= anchor
                          else Selection(after(anchor), point))

    def vim_command(self, key: str) -> bool:
        char = key_to_character(key) or KEYS.get(key, key)
        pending = self.prefix
        visual = self.vim_mode.startswith("VISUAL")
        if not pending and visual and char in {"i", "a"}:
            self.prefix = "v" + char
            return True
        if not pending and visual and char in {"d", "c", "y"}:
            self.operate(char, *sorted(self.selection))
            return True
        if not pending and not (
            char in MOTIONS
            or char in {"g", "d", "c", "y", "f", "F", "t", "T"}
            or char in "123456789"
        ):
            return False
        self.prefix = ""
        command = pending + char
        match = re.fullmatch(
            r"([1-9][0-9]*)?([dcyv]?)([1-9][0-9]*)?(.*)", command
        )
        if not match:
            return True
        before, operator, after, tail = match.groups()
        count = min(int(before or 1) * int(after or 1), 9999)
        # Keep one bounded pending command; zero is a motion unless it follows
        # an existing count. Find targets and object keys are consumed below.
        if (
            not tail
            or tail in {"g", "f", "F", "t", "T"}
            or (operator and tail in {"i", "a"})
        ):
            self.prefix = command[:24]
            return True
        cursor = self.document.get_index_from_location(self.vim_cursor_location())
        span = None
        linewise = False
        if operator and len(tail) == 2 and tail[0] in "ia":
            span = text_object(
                self.text, cursor, tail[1], tail[0] == "a", count
            )
        elif operator and tail == operator:
            row = self.cursor_location[0]
            end_row = min(row + count, self.document.line_count)
            start = self.document.get_index_from_location((row, 0))
            end = (
                self.document.get_index_from_location((end_row, 0))
                if end_row < self.document.line_count
                else len(self.text)
            )
            span, linewise = (start, end), True
        elif tail in MOTIONS or (len(tail) == 2 and tail[0] in "fFtT"):
            target = self.vim_motion(tail, count, cursor, bool(before or after))
            if target is not None:
                if not operator:
                    point = self.document.get_location_from_index(target)
                    if self.vim_mode == "VISUAL LINE":
                        anchor = self.selection.start[0]
                        last = point[0]
                        if last >= anchor:
                            self.selection = Selection(
                                (anchor, 0),
                                (last, len(self.document.get_line(last))),
                            )
                        else:
                            self.selection = Selection(
                                (anchor, len(self.document.get_line(anchor))),
                                (last, 0),
                            )
                    elif self.vim_mode == "VISUAL":
                        self.move_visual(point)
                    else:
                        self.move_cursor(point, select=visual)
                    return True
                start, end = sorted((cursor, target))
                if tail in {"j", "k", "G", "gg", "{", "}"}:
                    start = self.text.rfind("\n", 0, start) + 1
                    next_line = self.text.find("\n", end)
                    end = len(self.text) if next_line < 0 else next_line + 1
                    linewise = True
                elif (
                    tail in {"e", "E", "ge", "gE", "%"}
                    or (len(tail) == 2 and tail[0] in "fFtT")
                    or (tail in {";", ","} and self.last_find)
                ):
                    end = min(end + 1, len(self.text))
                    if target < cursor and tail not in {"ge", "gE", "%"}:
                        end = cursor
                # Vim cw/cW changes through the end of the last word, not its
                # following separator. Whitespace at the cursor still uses w.
                if (
                    operator == "c"
                    and tail in {"w", "W"}
                    and (
                        cursor < len(self.text)
                        and not self.text[cursor].isspace()
                    )
                ):
                    target = self.vim_motion(
                        "e" if tail == "w" else "E",
                        count,
                        cursor,
                        False,
                        change=True,
                    )
                    end = min(target + 1, len(self.text))
                span = start, end
        if span is None:
            self.app.client.notice = "No matching Vim text object or motion"
            return True
        start, end = (self.document.get_location_from_index(p) for p in span)
        if operator == "v":
            self.visual_anchor = start
            last = self.document.get_location_from_index(max(span[0], span[1] - 1))
            self.move_visual(last)
            self.selection = Selection(start, end)
        else:
            self.operate(operator, start, end, linewise=linewise)
        return True

    def vim_motion(self, key, count, cursor, explicit=False, *, change=False):
        text = self.text
        row, col = self.document.get_location_from_index(cursor)
        if key in {"w", "W", "b", "B", "e", "E", "ge", "gE"}:
            pattern = (
                r"\S+" if key.endswith(("W", "B", "E")) else r"\w+|[^\w\s]+"
            )
            words = list(re.finditer(pattern, text))
            backward = key in {"b", "B", "ge", "gE"}
            ends = key in {"e", "E", "ge", "gE"}
            points = [m.end() - 1 if ends else m.start() for m in words]
            points = [
                p
                for p in points
                if (
                    p < cursor
                    if backward
                    else p >= cursor
                    if change
                    else p > cursor
                )
            ]
            if backward:
                points.reverse()
            return (
                points[min(count, len(points)) - 1]
                if len(points) >= count
                else (
                    0
                    if backward
                    else max(0, len(text) - 1)
                    if ends
                    else len(text)
                )
            )
        if key in {";", ","}:
            if self.last_find is None:
                return None
            kind, target = self.last_find
            if key == ",":
                kind = kind.swapcase()
            return self.find_character(kind, target, count, cursor, repeat=True)
        if len(key) == 2 and key[0] in "fFtT":
            self.last_find = key[0], key[1]
            return self.find_character(key[0], key[1], count, cursor)
        if key == "%":
            line_end = text.find("\n", cursor)
            line_end = len(text) if line_end < 0 else line_end
            position = next(
                (p for p in range(cursor, line_end) if text[p] in "()[]{}"),
                None,
            )
            if position is None:
                return None
            pairs = {"(": ")", "[": "]", "{": "}", ")": "(", "]": "[", "}": "{"}
            char = text[position]
            step = 1 if char in "([{" else -1
            depth = 1
            for p in range(
                position + step, len(text) if step > 0 else -1, step
            ):
                depth += (text[p] == char) - (text[p] == pairs[char])
                if not depth:
                    return p
            return None
        if key in {"{", "}"}:
            points = [m.start() for m in re.finditer(r"\n\s*\n", text)]
            points = (
                [p for p in points if p < cursor]
                if key == "{"
                else [p for p in points if p > cursor]
            )
            if key == "{":
                points.reverse()
            return (
                points[min(count, len(points)) - 1]
                if points
                else (0 if key == "{" else len(text))
            )
        if key in {"gg", "G"}:
            row = (
                count - 1
                if explicit
                else (0 if key == "gg" else self.document.line_count - 1)
            )
            col = 0
        else:
            row += {"j": count, "k": -count}.get(key, 0)
        row = max(0, min(row, self.document.line_count - 1))
        line = self.document.get_line(row)
        col += {"l": count, "h": -count}.get(key, 0)
        if key == "0":
            col = 0
        elif key == "^":
            col = len(line) - len(line.lstrip())
        elif key == "$":
            row = min(row + count - 1, self.document.line_count - 1)
            col = len(self.document.get_line(row))
        return self.document.get_index_from_location(
            (row, max(0, min(col, len(self.document.get_line(row)))))
        )

    def find_character(self, kind, target, count, cursor, *, repeat=False):
        row, col = self.document.get_location_from_index(cursor)
        line = self.document.get_line(row)
        forward = kind in "ft"
        offset = 1 if forward else -1
        if repeat and kind in "tT":
            col += offset
        for _ in range(count):
            col = (
                line.find(target, col + 1)
                if forward
                else line.rfind(target, 0, max(0, col))
            )
            if col < 0:
                return None
        if kind in "tT":
            col -= offset
        return self.document.get_index_from_location((row, col))
