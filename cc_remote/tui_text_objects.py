"""Shared local Vim text-object ranges for readers and editors.

Ranges are half-open character offsets. This is not a language parser: bracket
objects match nested delimiters, while quote objects stay on the current line.
"""

from __future__ import annotations

import re


def text_object(
    text: str, cursor: int, kind: str, around: bool, count: int = 1
) -> tuple[int, int] | None:
    if not text:
        return None
    cursor = max(0, min(cursor, len(text) - 1))
    count = max(1, min(count, 9999))
    if kind in {"w", "W"}:
        pattern = r"\s+|\S+" if kind == "W" else r"\s+|\w+|[^\w\s]+"
        spans = list(re.finditer(pattern, text))
        for index, match in enumerate(spans):
            start, end = match.span()
            if not start <= cursor < end:
                continue
            # Subsequent objects include the separator and next word, but
            # not trailing whitespace unless the operator requested "around".
            last = index
            for _ in range(count - 1):
                last += 1
                if last < len(spans) and spans[last].group().isspace():
                    last += 1
                if last >= len(spans):
                    last = len(spans) - 1
                    break
            end = spans[last].end()
            if around:
                if match.group().isspace():
                    if index + 1 < len(spans):
                        end = spans[index + 1].end()
                elif (
                    last + 1 < len(spans) and spans[last + 1].group().isspace()
                ):
                    end = spans[last + 1].end()
                elif index and spans[index - 1].group().isspace():
                    start = spans[index - 1].start()
            return start, end
        return None
    if kind == "p":
        paragraphs = list(re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text))
        paragraphs = [p for p in paragraphs if p[0].strip()]
        index = next(
            (i for i, p in enumerate(paragraphs) if p.end() > cursor), None
        )
        if index is None:
            return None
        start = paragraphs[index].start()
        end = paragraphs[min(index + count - 1, len(paragraphs) - 1)].end()
        if around:
            while end < len(text) and text[end].isspace():
                end += 1
        return start, end
    if kind in {'"', "'", "`"}:
        start = text.rfind("\n", 0, cursor) + 1
        end = text.find("\n", cursor)
        end = len(text) if end < 0 else end
        marks = []
        escaped = False
        for position in range(start, end):
            char = text[position]
            if char == kind and not escaped:
                marks.append(position)
            escaped = char == "\\" and not escaped
        pairs = list(zip(marks[::2], marks[1::2]))
        # Vim quote objects can select the next quoted string on this line.
        pair = next((p for p in pairs if p[0] <= cursor <= p[1]), None)
        if pair is None:
            pair = next((p for p in pairs if p[0] > cursor), None)
    else:
        delimiters = next(
            (p for p in ("()", "[]", "{}", "<>") if kind in p), None
        )
        if delimiters is None:
            delimiters = {"b": "()", "B": "{}"}.get(kind)
        if delimiters is None:
            return None
        opening, closing = delimiters
        stack = []
        pairs = []
        for position, char in enumerate(text):
            if char == opening:
                stack.append(position)
            elif char == closing and stack:
                begin = stack.pop()
                if begin <= cursor <= position:
                    pairs.append((begin, position))
        pairs.sort(key=lambda p: p[1] - p[0])
        pair = pairs[count - 1] if len(pairs) >= count else None
    if pair is None:
        return None
    start, end = pair
    return (start, end + 1) if around else (start + 1, end)
