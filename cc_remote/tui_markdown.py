"""Styled chat Markdown with visible URLs and stable source coordinates."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata
from urllib.parse import unquote

from markdown_it import MarkdownIt
from rich import box
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from cc_remote.tui import _safe_remote_text
from cc_remote.tui_mermaid import render_diagram

MAX_RENDER_CHARS = 256 * 1024


class RenderLimit(ValueError):
    """Expanding repeated reference links must not exhaust the UI process."""


def display_url(url):
    """Expose readable Unicode without decoding URL syntax or controls."""
    def decode(match):
        try:
            text = unquote(match[0], errors="strict")
        except UnicodeDecodeError:
            return match[0]
        if any(
            not c.isprintable() or c.isspace()
            or unicodedata.category(c).startswith("C") for c in text
        ):
            return match[0]
        return text

    return _safe_remote_text(
        re.sub(r"(?:%[89a-fA-F][0-9a-fA-F])+", decode, url)
    )


@lru_cache(maxsize=256)
def matching_runs(source, rendered):
    """Bound diff work even for long, repetitive generated Markdown.

    Global SequenceMatcher's autojunk loses repeated prose, while disabling it
    globally makes streamed repetitive text quadratic. Match small windows and
    extend their equal runs linearly; decorations map to the preceding run.
    """
    matches = []
    suffix = 0
    while (
        suffix < min(len(source), len(rendered))
        and source[-1 - suffix] == rendered[-1 - suffix]
    ):
        suffix += 1
    source_end, rendered_end = len(source) - suffix, len(rendered) - suffix
    a = b = 0
    while a < source_end and b < rendered_end:
        size = 0
        while (
            a + size < source_end
            and b + size < rendered_end
            and source[a + size] == rendered[b + size]
        ):
            size += 1
        if size:
            matches.append((a, b, size))
            a += size
            b += size
            continue
        runs = SequenceMatcher(
            None,
            source[a : min(a + 256, source_end)],
            rendered[b : min(b + 256, rendered_end)],
            autojunk=False,
        ).get_matching_blocks()
        x, y, size = runs[0]
        if not size:
            a += 256
            b += 256
        else:
            a += x
            b += y
    if suffix:
        matches.append((source_end, rendered_end, suffix))
    return tuple(matches)


def inline(tokens):
    """Use parsed tokens, never Rich markup or terminal escape sequences."""
    result = Text()
    styles, links = [], []
    for token in tokens:
        kind = token.type
        if kind in {"strong_open", "em_open", "s_open"}:
            styles.append(
                {
                    "strong_open": "bold",
                    "em_open": "italic",
                    "s_open": "strike",
                }[kind]
            )
        elif kind in {"strong_close", "em_close", "s_close"}:
            if styles:
                styles.pop()
        elif kind == "link_open":
            links.append(
                (len(result), _safe_remote_text(token.attrGet("href") or ""))
            )
        elif kind == "link_close" and links:
            start, url = links.pop()
            visible = display_url(url)
            result.stylize("underline", start)
            if result.plain[start:] != visible:
                result.append(" (")
                result.append(visible, "underline cyan")
                result.append(")")
        elif kind == "image":
            label = _safe_remote_text(token.content)
            path = display_url(token.attrGet("src") or "")
            result.append(label + " (" if label else "")
            result.append(path, "underline cyan")
            if label:
                result.append(")")
        elif kind in {"softbreak", "hardbreak"}:
            # Keep streamed prose line boundaries; TextArea owns soft wrapping.
            result.append("\n")
        elif kind in {"text", "code_inline", "html_inline"}:
            style = " ".join(
                styles + (["cyan"] if kind == "code_inline" else [])
            )
            content = token.content
            if kind == "text" and links and content == links[-1][1]:
                content = display_url(content)
            result.append(_safe_remote_text(content), style)
        elif token.children:
            result.append_text(inline(token.children))
        if len(result) > MAX_RENDER_CHARS:
            raise RenderLimit()
    return result


def table_text(tokens, width):
    rows, row, cell, urls = [], [], None, []
    characters = 0
    for token in tokens:
        if token.type == "tr_open":
            row = []
        elif token.type in {"th_open", "td_open"}:
            alignment = token.attrGet("style") or ""
            cell = Text(
                justify="right"
                if "right" in alignment
                else "center"
                if "center" in alignment
                else "left"
            )
        elif token.type == "inline" and cell is not None:
            cell.append_text(inline(token.children or []))
            characters += len(cell)
            if characters > MAX_RENDER_CHARS:
                raise RenderLimit()
            for child in token.children or []:
                if child.type in {"link_open", "image"}:
                    url = display_url(
                        child.attrGet("href") or child.attrGet("src") or ""
                    )
                    if url and url not in urls:
                        urls.append(url)
        elif token.type in {"th_close", "td_close"}:
            row.append(cell)
            cell = None
        elif token.type == "tr_close":
            rows.append(row)
    if not rows:
        return Text()
    result = Text()
    if width < len(rows[0]) * 8:
        # A narrow pane must not hide columns or truncate their values.
        if len(rows) == 1:
            result = Text("\n").join(rows[0])
            result.append("\n")
        for values in rows[1:]:
            for header, value in zip(rows[0], values):
                result.append_text(header)
                result.append(": ")
                result.append_text(value)
                result.append("\n")
            result.append("\n")
    else:
        table = Table(
            box=box.ROUNDED,
            header_style="bold",
            padding=(0, 1),
            border_style="bright_black",
            expand=False,
        )
        for heading in rows[0]:
            table.add_column(heading, overflow="fold")
        for values in rows[1:]:
            table.add_row(*values)
        console = Console(width=width, color_system="truecolor")
        for line in console.render_lines(table, pad=False):
            result.append_text(
                Text.assemble(
                    *(
                        (segment.text, segment.style)
                        for segment in line
                        if not segment.control
                    )
                )
            )
            result.append("\n")
    # Table cells wrap at column boundaries. Also retain contiguous URLs for
    # Kitty's URL hints instead of offering only fragments from those cells.
    for url in urls:
        result.append(url, "underline cyan")
        result.append("\n")
    return result


@lru_cache(maxsize=256)
def render_markdown(source, width, browser_key="Space B"):
    """Cache bounded message bodies, not every tick or the entire transcript."""
    try:
        return _render_markdown(source, width, browser_key)
    except RenderLimit:
        return Text(_safe_remote_text(source)), False, ()


def _render_markdown(source, width, browser_key):
    source = _safe_remote_text(source)
    diagram = render_diagram(source, width, browser_key, standalone=True)
    if diagram is not None:
        return diagram, True, ((len(source), len(diagram)),)
    parser = MarkdownIt("commonmark").enable("table").enable("strikethrough")
    env = {}
    tokens = parser.parse(source, env)
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    edits = []
    index = 0
    has_table = False
    while index < len(tokens):
        token = tokens[index]
        if token.type == "table_open":
            end = index + 1
            while tokens[end].type != "table_close":
                end += 1
            rendered = table_text(tokens[index : end + 1], width)
            has_table = True
            index = end
        elif token.type == "inline" and token.map:
            rendered = inline(token.children or [])
            first = source[offsets[token.map[0]] : offsets[token.map[0] + 1]]
            content = token.content.split("\n", 1)[0]
            prefix = first[: max(0, first.find(content))] if content else ""
            heading = index > 0 and tokens[index - 1].type == "heading_open"
            if heading:
                rendered.stylize("bold")
                prefix = ""
                # Setext headings also consume their underline source row.
                token.map = tokens[index - 1].map
            else:
                prefix = re.sub(r"([-+*]) (?=\S|$)", "• ", prefix)
                prefix = prefix.replace(">", "│")
            if prefix:
                lines = rendered.split("\n", allow_blank=True)
                continuation = re.sub(r"[•\d.)-]", " ", prefix)
                rendered = Text("\n").join(
                    [
                        Text(prefix if n == 0 else continuation) + line
                        for n, line in enumerate(lines)
                    ]
                )
            rendered.append("\n")
        elif token.type in {"fence", "code_block"} and token.map:
            language = (
                token.info.split(None, 1)[0] if token.info.strip() else "text"
            )
            rendered = (render_diagram(token.content, width, browser_key)
                        if language.lower() == "mermaid" else None)
            if rendered is not None:
                has_table = True  # Diagrams reflow with the terminal width.
            else:
                rendered = Syntax(
                    token.content, language, theme="ansi_dark"
                ).highlight(_safe_remote_text(token.content))
                if language.lower() == "mermaid":
                    notice = Text(
                        "[Unsupported diagram; showing source]\n"
                        f"[{browser_key}: open session in browser]\n", "dim"
                    )
                    rendered = notice + rendered
        elif token.type == "hr" and token.map:
            rendered = Text("─" * min(width, 60) + "\n", "bright_black")
            has_table = True  # Like tables, rules need width-aware reflow.
        else:
            index += 1
            continue
        if token.map:
            edits.append(
                (offsets[token.map[0]], offsets[token.map[1]], rendered)
            )
        index += 1
    for reference in env.get("references", {}).values():
        if reference.get("map"):
            start, end = reference["map"]
            edits.append((offsets[start], offsets[end], Text()))
    result, previous, ends = Text(), 0, []
    for start, end, text in sorted(edits, key=lambda edit: edit[0]):
        if start < previous:
            continue
        result.append(source[previous:start])
        result.append_text(text)
        if len(result) > MAX_RENDER_CHARS:
            raise RenderLimit()
        ends.append((end, len(result)))
        previous = end
    result.append(source[previous:])
    # Avoid an added line break on a streaming paragraph's unfinished last line.
    if not source.endswith("\n") and result.plain.endswith("\n"):
        result = result[:-1]
    if len(result) > MAX_RENDER_CHARS:
        raise RenderLimit()
    return (
        result,
        has_table,
        tuple(
            (source_end, min(display_end, len(result)))
            for source_end, display_end in ends
        ),
    )


@dataclass
class Replacement:
    source_start: int
    source_end: int
    start: int
    end: int
    equal: tuple

    def map(self, position, reverse=False):
        """Exact unchanged runs; generated borders map to nearby source text."""
        matches = self.equal
        previous = 0
        for a, b, size in matches:
            x, y = (b, a) if reverse else (a, b)
            if position < x:
                return previous
            if position < x + size:
                return y + position - x
            previous = y + size
        return previous


class MarkdownProjection:
    """Compose source-preserving Markdown with the existing image projection."""

    def __init__(self, source="", browser_key="Space B"):
        self.original = source
        self.browser_key = browser_key
        self.replacements = []
        self.responsive = False
        self.ends = {}

    def paragraph_end(self, position):
        # Images belong below a parsed paragraph, not in a nearest-match gap
        # inside a generated reference URL or a reflowed table cell.
        return self.ends.get(position, self.display(position))

    def source(self, position):
        shift = 0
        for item in self.replacements:
            if position < item.start:
                break
            if position < item.end:
                return item.source_start + item.map(position - item.start, True)
            shift += (item.end - item.start) - (
                item.source_end - item.source_start
            )
        return position - shift

    def display(self, position):
        shift = 0
        for item in self.replacements:
            if position < item.source_start:
                break
            if position < item.source_end:
                return item.start + item.map(position - item.source_start)
            shift += (item.end - item.start) - (
                item.source_end - item.source_start
            )
        return position + shift

    def replace_header(self, position, old, new):
        source = self.source(position)
        self.original = (
            self.original[:source] + new + self.original[source + len(old) :]
        )
        delta = len(new) - len(old)
        self.ends = {
            (key + delta if key > source else key): (
                value + delta if value > position else value
            )
            for key, value in self.ends.items()
        }
        for item in self.replacements:
            if item.start > position:
                item.start += delta
                item.end += delta
                item.source_start += delta
                item.source_end += delta

    def project(self, starts, width):
        edits = []
        source = self.original
        for index, (start, block) in enumerate(starts):
            end = (
                starts[index + 1][0] if index + 1 < len(starts) else len(source)
            )
            body = source.find("\n", start, end) + 1
            if block.role == "assistant" and block.channel != "thinking":
                edits.append((body, end))
            elif block.role == "detail" and block.expanded:
                lines = source[body:end].splitlines(keepends=True)
                positions = [body]
                for line in lines:
                    positions.append(positions[-1] + len(line))
                sections = block.data.get("sections", [])
                for n, section in enumerate(sections):
                    if (
                        section["role"] != "assistant"
                        or section["channel"] == "thinking"
                    ):
                        continue
                    a = min(section["line"] + 1, len(lines))
                    b = (
                        min(sections[n + 1]["line"], len(lines))
                        if n + 1 < len(sections)
                        else len(lines)
                    )
                    edits.append((positions[a], positions[b]))
        result, previous = Text(), 0
        for a, b in edits:
            result.append(source[previous:a])
            rendered, responsive, ends = render_markdown(
                source[a:b], width, self.browser_key,
            )
            self.responsive |= responsive
            self.ends.update({a + x: len(result) + y for x, y in ends})
            if source[a:b] != rendered.plain:
                matches = matching_runs(source[a:b], rendered.plain)
                self.replacements.append(
                    Replacement(
                        a, b, len(result), len(result) + len(rendered), matches
                    )
                )
            result.append_text(rendered)
            previous = b
        result.append(source[previous:])
        return result, [(self.display(start), block) for start, block in starts]
