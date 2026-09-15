"""Family-specific text views of Mermaid data, relationships and chronology.

No eval, callbacks, includes, image loads or styling instructions execute here.
Unrecognized records are explicitly marked rather than silently discarded.
"""

import csv
import json
import math
import re

from cc_remote.tui_diagram_text import Canvas, label, statements, web_statement

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
GRAPH_KINDS = {
    "classDiagram",
    "classDiagram-v2",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "requirementDiagram",
    "requirement",
    "architecture",
    "architecture-beta",
    "block",
    "block-beta",
    "swimlane-beta",
    "eventmodeling",
}
TREE_KINDS = {
    "mindmap",
    "kanban",
    "treeView-beta",
    "treemap",
    "treemap-beta",
    "ishikawa",
    "ishikawa-beta",
    "wardley-beta",
    "cynefin-beta",
    "venn-beta",
}
DATA_KINDS = {
    "pie",
    "xychart",
    "xychart-beta",
    "radar-beta",
    "quadrantChart",
    "sankey",
    "sankey-beta",
    "packet",
    "packet-beta",
}


def number(text):
    try:
        value = float(text)
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def bars(canvas, rows, *, percentage=False):
    largest = max((abs(v) for _, v in rows), default=0)
    # Scale before summing: individually finite values may overflow their sum.
    total = sum(v / largest for _, v in rows) if largest else 0
    count = max(1, min(30, canvas.width // 3))
    for name, value in rows:
        ratio = abs(value) / largest if largest else 0
        units = round(ratio * count)
        suffix = f" · {ratio / total:.1%}" if percentage and total else ""
        canvas.line(f"{name} · {value:g}{suffix}")
        canvas.line(("−" if value < 0 else "") + "█" * units, 2, "cyan")


class Projection:
    def __init__(self, document, width):
        self.document = document
        self.canvas = Canvas(width, document.kind + " · terminal text")
        self.browser = document.browser[:]
        self.unknown = False
        if document.title:
            self.canvas.line(document.title, style="bold")

    def source(self, line):
        self.unknown = True
        self.canvas.line("Source · " + line, style="dim")

    def rows(self):
        for row in statements(self.document.body):
            text = row.strip()
            if web_statement(text, self.document.kind):
                self.browser.append(text.split()[0])
                continue
            if re.search(r"\b(?:animate|animation|href|callback)\s*:", text):
                self.browser.append("animation or interaction metadata")
            if text.startswith(("title ", "title:", "accTitle:", "accDescr:")):
                self.canvas.line(
                    text.partition(":" if ":" in text else " ")[2], style="bold"
                )
                continue
            yield row, text

    def sequence(self):
        from cc_remote.tui_diagram_sequence import render_sequence

        render_sequence(self)

    def graph(self):
        depth = 0
        for _, text in self.rows():
            if text in {"}", "end"}:
                depth = max(0, depth - 1)
                self.canvas.line("└", depth * 2, "dim")
                continue
            # Keep UML cardinalities, crow's-foot notation, direction ports,
            # stereotypes and relationship labels verbatim alongside the edge.
            if re.search(
                r"(?:[-.=]{2,}|[|}][|o}][.-]{2}|\s->\s|<-->|-->|<\|)", text
            ):
                self.canvas.line("↳ " + text, depth * 2)
            elif text.endswith("{"):
                self.canvas.line("┌ " + label(text[:-1]), depth * 2, "bold")
                depth = min(16, depth + 1)
            elif depth:
                self.canvas.line("│ " + text, depth * 2)
            elif re.match(
                r"(?:class|state|namespace|requirement|functionalRequirement|performanceRequirement|interfaceRequirement|physicalRequirement|designConstraint|element|group|service|junction|block|columns|space|lane|pool|event|command|view|actor|swimlane)\b",
                text,
            ):
                self.canvas.box(text)
            elif re.match(r"(?:direction|hideEmptyMembers)\b", text):
                self.canvas.line("Layout · " + text, style="dim")
            elif re.match(r"(?:note|[\w]+\s*:|<<|\[\*\])", text):
                self.canvas.line(text)
            elif re.fullmatch(r"\w+", text):
                self.canvas.box(text)
            elif self.document.kind in {"block", "block-beta"}:
                from cc_remote.tui_diagram_flow import (
                    FlowParser,
                    UnsupportedFlowchart,
                )

                parser = FlowParser("graph TB\n")
                rest, nodes = text, []
                try:
                    while rest:
                        identity, rest = parser.node(rest)
                        nodes.append(identity)
                except UnsupportedFlowchart:
                    self.source(text)
                else:
                    self.browser.extend(parser.graph.browser)
                    for identity in nodes:
                        self.canvas.box(
                            identity + " · " + parser.graph.nodes[identity]
                        )
            else:
                self.source(text)

    def hierarchy(self):
        levels = []
        for raw, text in self.rows():
            indent = len(raw) - len(raw.lstrip())
            while levels and indent <= levels[-1]:
                levels.pop()
            depth = min(len(levels), 16)
            levels.append(indent)
            # Preserve attribute values (ticket IDs, assignees, weights, set
            # membership and coordinates); never infer hierarchy from names.
            shape = re.match(
                r"([\w-]*)\s*([\[({]+)(.*?)([\])}]+)(.*)$", text, re.S
            )
            value = text
            if shape:
                identity, _, caption, _, tail = shape.groups()
                value = (
                    (identity + " · " if identity else "")
                    + label(caption)
                    + tail
                )
            self.canvas.line("├─ " + value, depth * 2)

    def chronology(self):
        section = ""
        for _, text in self.rows():
            if text.startswith("section "):
                section = label(text[8:])
                self.canvas.line("── " + section + " ──", style="bold")
            elif self.document.kind == "gantt" and re.match(
                r"(?:dateFormat|axisFormat|tickInterval|excludes|includes|todayMarker|weekday|weekend)\b",
                text,
            ):
                self.canvas.line("Schedule · " + text, style="dim")
                if text.startswith("todayMarker"):
                    self.browser.append("today marker styling")
            elif ":" in text:
                name, _, value = text.partition(":")
                if self.document.kind == "journey":
                    score, _, actors = value.partition(":")
                    self.canvas.box(
                        label(name),
                        [
                            "Score: " + score.strip(),
                            "Actors: " + actors.strip(),
                        ],
                    )
                elif self.document.kind == "gantt":
                    # Dates, dependency expressions and exclusions are kept
                    # exact: do not fabricate a calendar from incomplete data.
                    self.canvas.box(label(name), ["Schedule: " + value.strip()])
                else:
                    self.canvas.line(
                        (label(name) or section) + " → " + label(value)
                    )
            else:
                self.source(text)

    def data(self):
        kind = self.document.kind
        values = []
        bit = 0
        for _, text in self.rows():
            if kind == "pie":
                if text == "showData":
                    continue
                match = re.fullmatch(
                    r'(".*?"|[^:]+)\s*:\s*(' + NUMBER + r")", text
                )
                if (
                    match
                    and (value := number(match[2])) is not None
                    and value >= 0
                ):
                    values.append((label(match[1]), value))
                else:
                    self.source(text)
            elif kind.startswith("sankey"):
                try:
                    row = next(csv.reader([text], strict=True))
                except (csv.Error, StopIteration):
                    row = []
                if len(row) == 3 and number(row[2]) is not None:
                    self.canvas.line(
                        f"{label(row[0])} ── {row[2].strip()} ─▶ {label(row[1])}"
                    )
                else:
                    self.source(text)
            elif kind.startswith("packet"):
                match = re.fullmatch(r"(\+\d+|\d+(?:-\d+)?)\s*:\s*(.+)", text)
                if not match:
                    self.source(text)
                    continue
                size = match[1]
                if len(size) > 32:
                    self.source(text)
                    continue
                if size.startswith("+"):
                    start, end = bit, bit + int(size[1:]) - 1
                else:
                    pieces = size.split("-")
                    start, end = int(pieces[0]), int(pieces[-1])
                if end < start or end - start > 1_000_000:
                    self.source(text)
                    continue
                bit = end + 1
                self.canvas.box(
                    f"Bits {start}–{end} ({end - start + 1})", [label(match[2])]
                )
            elif kind.startswith("xychart"):
                series = re.fullmatch(
                    r'(bar|line)(?:\s+"([^"]*)")?\s*(\[.*\])', text
                )
                if series:
                    try:
                        data = json.loads(series[3])
                    except (ValueError, RecursionError):
                        data = None
                    if (
                        not isinstance(data, list)
                        or len(data) > 500
                        or any(
                            isinstance(v, bool)
                            or not isinstance(v, (int, float))
                            or number(str(v)) is None
                            for v in data
                        )
                    ):
                        self.source(text)
                        continue
                    self.canvas.line(series[2] or series[1], style="bold")
                    bars(
                        self.canvas,
                        [(str(i + 1), float(v)) for i, v in enumerate(data)],
                    )
                elif re.match(r"(?:x-axis|y-axis|horizontal)\b", text):
                    self.canvas.line("Axis · " + text)
                else:
                    self.source(text)
            elif kind == "radar-beta":
                if re.match(
                    r"(?:axis|curve|showLegend|min|max|ticks|graticule)\b", text
                ):
                    self.canvas.line(text)
                else:
                    self.source(text)
            elif kind == "quadrantChart":
                if re.match(r"(?:x-axis|y-axis|quadrant-[1-4])\b", text):
                    self.canvas.line(text, style="bold")
                else:
                    point = re.fullmatch(
                        r"(.+?):\s*\[\s*("
                        + NUMBER
                        + r"),\s*("
                        + NUMBER
                        + r")\s*\]",
                        text,
                    )
                    if point and all(
                        number(point[i]) is not None for i in (2, 3)
                    ):
                        self.canvas.line(
                            f"● {label(point[1])} · x={point[2]}, y={point[3]}"
                        )
                    else:
                        self.source(text)
        if kind == "pie":
            bars(self.canvas, values, percentage=True)

    def c4(self):
        depth = 0
        for _, text in self.rows():
            if text == "}":
                depth = max(0, depth - 1)
                self.canvas.line("└", depth * 2)
                continue
            call = re.fullmatch(r"(\w+)\s*\((.*)\)\s*(\{)?", text, re.S)
            if not call:
                self.source(text)
                continue
            try:
                args = next(csv.reader([call[2]], skipinitialspace=True))
            except (csv.Error, StopIteration):
                self.source(text)
                continue
            if call[1].startswith(("Rel", "BiRel")) and len(args) >= 2:
                arrow = "↔" if call[1].startswith("BiRel") else "→"
                self.canvas.line(
                    f"{args[0]} {arrow} {args[1]} · " + " · ".join(args[2:]),
                    depth * 2,
                )
            elif call[1] in {
                "LAYOUT_TOP_DOWN",
                "LAYOUT_LEFT_RIGHT",
                "SHOW_LEGEND",
            }:
                self.browser.append("graphical layout/legend")
            else:
                self.canvas.box(
                    call[1] + " · " + (args[0] if args else ""),
                    args[1:],
                    depth * 2,
                )
            if call[3]:
                depth = min(16, depth + 1)

    def git(self):
        branch, index = "main", 0
        for _, text in self.rows():
            match = re.match(
                r"(branch|checkout|switch|commit|merge|cherry-pick)\b\s*(.*)",
                text,
            )
            if not match:
                if text in {"LR:", "TB:", "BT:", "LR", "TB", "BT"}:
                    self.canvas.line("Layout · " + text, style="dim")
                else:
                    self.source(text)
                continue
            action, args = match.groups()
            if action in {"checkout", "switch"}:
                branch = label(args)
                self.canvas.line("│ checkout " + branch)
            elif action == "branch":
                self.canvas.line(f"├─ branch {args} from {branch}")
                # Native gitGraph creates and checks out the new branch.
                branch = label(args.split(" order:", 1)[0])
            else:
                index += 1
                self.canvas.line(f"● {index} [{branch}] {action} {args}")


def render_family(document, width, browser_key):
    projection = Projection(document, width)
    kind = document.kind
    if kind == "sequenceDiagram":
        projection.sequence()
    elif kind in GRAPH_KINDS:
        projection.graph()
    elif kind in TREE_KINDS or kind == "zenuml":
        projection.hierarchy()
    elif kind in DATA_KINDS:
        projection.data()
    elif kind in {"gantt", "journey", "timeline"}:
        projection.chronology()
    elif kind.startswith("C4"):
        projection.c4()
    elif kind == "gitGraph":
        projection.git()
    elif kind.startswith("railroad"):
        for _, text in projection.rows():
            projection.canvas.line("Production · " + text.replace("::=", "→"))
    elif kind == "info":
        projection.canvas.line("Mermaid source information · terminal renderer")
        projection.browser.append("engine version information")
        for _, text in projection.rows():
            projection.source(text)
    else:
        for _, text in projection.rows():
            projection.source(text)
    if projection.unknown:
        projection.browser.append("unprojected syntax (source rows retained)")
    projection.canvas.browser_hint(browser_key, projection.browser)
    return projection.canvas.text
