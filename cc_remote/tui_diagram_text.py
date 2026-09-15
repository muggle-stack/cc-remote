"""Bounded text primitives for inert Mermaid projections, not a JS runtime."""

from dataclasses import dataclass, field
from html import unescape
import re

from rich.console import Console
from rich.text import Text

from cc_remote.tui import _safe_remote_text

MAX_SOURCE = 32 * 1024
MAX_STATEMENTS = 500
MAX_OUTPUT = 128 * 1024
ENTITY = re.compile(
    r"(?:&(?:#[xX][0-9a-fA-F]{1,8}|#\d{1,10}|[A-Za-z][A-Za-z0-9]{0,31})"
    r"|#\d{1,10});"
)


class DiagramLimit(ValueError):
    pass


def label(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    # Mermaid's numeric entities also allow #NN; without the HTML ampersand.
    value = re.sub(r"(?<!&)#(\d+);", r"&#\1;", value)
    return _safe_remote_text(unescape(value))


def bounded(source):
    if (
        len(source) > MAX_SOURCE
        or len(source.encode("utf-8", errors="surrogatepass")) > MAX_SOURCE
        or len(source.splitlines()) > MAX_STATEMENTS
    ):
        raise DiagramLimit("Diagram exceeds terminal source limits")


def statements(source, *, brackets=False):
    """Split outside quotes/labels; comments and directives are separate rows.

    Braces are deliberately not structural here: class/state blocks and
    flowchart shape metadata assign different meanings to the same character.
    """
    result, start, index, quote, stack = [], 0, 0, None, []
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\" and index + 1 < len(source):
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "`"}:
            quote = char
        elif char in "&#" and (entity := ENTITY.match(source, index)):
            # An encoded semicolon in message text is not a statement break.
            index = entity.end()
            continue
        elif source.startswith("%%", index) and not stack:
            if source[start:index].strip():
                result.append(source[start:index])
            if source.startswith("%%{", index):
                end = source.find("}%%", index + 3)
                end = len(source) if end < 0 else end + 3
                result.append(source[index:end])
            else:
                end = source.find("\n", index)
                end = len(source) if end < 0 else end
            start = index = end
            continue
        elif brackets and char in "[({":
            stack.append({"[": "]", "(": ")", "{": "}"}[char])
        elif brackets and stack and char == stack[-1]:
            stack.pop()
        elif char in "\n;" and not stack:
            if source[start:index].strip():
                result.append(source[start:index])
            start = index + 1
        index += 1
        if len(result) > MAX_STATEMENTS:
            raise DiagramLimit("Too many diagram statements")
    if source[start:].strip():
        result.append(source[start:])
    return result


@dataclass
class DiagramSource:
    kind: str
    body: str
    title: str = ""
    browser: list[str] = field(default_factory=list)


# Match the diagram families registered by the pinned Web dependency. Aliases
# are normalized by the projection dispatcher, not by executing Mermaid.
KINDS = (
    "flowchart-elk",
    "flowchart",
    "graph",
    "sequenceDiagram",
    "classDiagram-v2",
    "classDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "erDiagram",
    "requirementDiagram",
    "requirement",
    "gitGraph",
    "gantt",
    "pie",
    "journey",
    "timeline",
    "mindmap",
    "kanban",
    "quadrantChart",
    "xychart-beta",
    "xychart",
    "sankey-beta",
    "sankey",
    "packet-beta",
    "packet",
    "radar-beta",
    "block-beta",
    "block",
    "architecture-beta",
    "architecture",
    "C4Context",
    "C4Container",
    "C4Component",
    "C4Dynamic",
    "C4Deployment",
    "treeView-beta",
    "treemap-beta",
    "treemap",
    "swimlane-beta",
    "eventmodeling",
    "ishikawa-beta",
    "ishikawa",
    "venn-beta",
    "wardley-beta",
    "cynefin-beta",
    "railroad-ebnf-beta",
    "railroad-abnf-beta",
    "railroad-peg-beta",
    "railroad-beta",
    "zenuml",
    "info",
)
HEADER = re.compile(r"\s*(" + "|".join(map(re.escape, KINDS)) + r")\b")
WEB_STATEMENT = re.compile(
    r"^(?:click|style|classDef|linkStyle|cssClass|callback|link|links|"
    r"Update\w*Style|update\w*Style)\b|^%%\{"
)


def diagram_source(source):
    bounded(source)
    source = source.lstrip()
    browser, title = [], ""
    if source.startswith("---\n") or source.startswith("---\r\n"):
        lines = source.splitlines(keepends=True)
        end = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if end is None:
            return None
        frontmatter = "".join(lines[1:end])
        match = re.search(r"(?m)^title:\s*(.+)$", frontmatter)
        title = label(match[1]) if match else ""
        if re.search(r"(?m)^config\s*:", frontmatter):
            browser.append("frontmatter configuration")
        source = "".join(lines[end + 1 :]).lstrip()
    while source.startswith("%%"):
        if source.startswith("%%{"):
            end = source.find("}%%")
            if end < 0:
                return None
            browser.append("initialization directive")
            source = source[end + 3 :].lstrip()
        else:
            source = source.partition("\n")[2].lstrip()
    match = HEADER.match(source)
    if not match:
        return None
    return DiagramSource(match[1], source[match.end() :], title, browser)


class Canvas:
    def __init__(self, width, title):
        self.width = max(8, min(int(width), 240))
        self.console = Console(width=self.width)
        self.text = Text()
        self.line(title, style="bold")

    def line(self, value="", indent=0, style=""):
        prefix = " " * min(indent, max(0, self.width - 8))
        for row in Text(label(str(value)), style=style).wrap(
            self.console,
            self.width - len(prefix),
        ):
            self.text.append(prefix)
            self.text.append_text(row)
            self.text.append("\n")
        if len(self.text) > MAX_OUTPUT:
            raise DiagramLimit("Diagram exceeds terminal output limit")

    def box(self, title, lines=(), indent=0):
        indent = min(indent, max(0, self.width - 8))
        width = self.width - indent - 4
        rows = []
        for value in (title, *lines):
            for part in label(value).splitlines() or [""]:
                rows.extend(Text(part).wrap(self.console, width))
        size = max((row.cell_len for row in rows), default=1)
        self.line("╭" + "─" * (size + 2) + "╮", indent, "dim")
        for row in rows:
            self.line(
                "│ " + row.plain + " " * (size - row.cell_len) + " │", indent
            )
        self.line("╰" + "─" * (size + 2) + "╯", indent, "dim")

    def browser_hint(self, shortcut, reasons):
        if reasons:
            self.line(
                "Web-only: " + ", ".join(dict.fromkeys(reasons)), style="dim"
            )
            self.line(f"[{shortcut}: open session in browser]", style="cyan")


def web_statement(statement, kind):
    if WEB_STATEMENT.match(statement):
        return True
    # 'class' is a semantic declaration in UML, but a CSS assignment in graphs.
    return kind.startswith(("flowchart", "graph")) and bool(
        re.match(r"class\s", statement)
    )
