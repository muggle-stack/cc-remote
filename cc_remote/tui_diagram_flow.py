"""Flowchart structure and relations, independent of graphical layout/CSS."""

from dataclasses import dataclass, field
import re

from cc_remote.tui_diagram_text import (
    DiagramLimit,
    bounded,
    diagram_source,
    label,
    statements,
    web_statement,
)

MAX_NODES = 64
MAX_EDGES = 128
IDENTIFIER = re.compile(r"[\w](?:[\w./]|:(?!::)|-(?![-.=>ox]))*", re.UNICODE)
# Longer delimiters win: shapes only change presentation, never node identity.
SHAPES = (
    ("(((", ")))"),
    ("((", "))"),
    ("([", "])"),
    ("[[", "]]"),
    ("[(", ")]"),
    ("{{", "}}"),
    ("[/", "/]"),
    ("[\\", "\\]"),
    ("[", "]"),
    ("(", ")"),
    ("{", "}"),
    (">", "]"),
)
ARROW = re.compile(r"(?:<|o|x)?(?:-\.+-+|-{2,}|={2,}|~{3,})(?:>|o|x)?")


class UnsupportedFlowchart(ValueError):
    pass


@dataclass
class Flowchart:
    direction: str
    nodes: dict[str, str] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    groups: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    membership: dict[str, str] = field(default_factory=dict)
    browser: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)


class FlowParser:
    def __init__(self, source):
        bounded(source)
        document = diagram_source(source)
        if not document or document.kind not in {
            "graph",
            "flowchart",
            "flowchart-elk",
        }:
            raise UnsupportedFlowchart()
        match = re.match(r"\s*(TB|TD|BT|LR|RL)\b", document.body)
        if not match:
            raise UnsupportedFlowchart()
        self.graph = Flowchart(match[1], browser=document.browser[:])
        if document.title:
            self.graph.annotations.append(document.title)
        self.body, self.groups = document.body[match.end() :], []

    def node(self, text):
        text = text.lstrip()
        match = IDENTIFIER.match(text)
        if not match:
            raise UnsupportedFlowchart()
        identity, rest = match[0], text[match.end() :].lstrip()
        value = None
        if rest.startswith("@{"):
            # Shape/icon/image metadata is inert. Images/icons remain labelled
            # text; fetching an image or executing a callback is never allowed.
            end = self.metadata_end(rest)
            metadata, rest = rest[2:end], rest[end + 1 :].lstrip()
            match = re.search(
                r'\blabel\s*:\s*("(?:\\.|[^"\\])*"|[^,}]+)', metadata
            )
            value = label(match[1]) if match else identity
            if re.search(r"\b(?:img|icon|animate|animation)\s*:", metadata):
                self.graph.browser.append("node/edge graphics or animation")
        else:
            for opening, closing in SHAPES:
                if not rest.startswith(opening):
                    continue
                start = len(opening)
                if opening in {"[/", "[\\"}:
                    # Parallelograms and trapezoids have either slanted end.
                    closings = [c for c in ("/]", "\\]") if c in rest[start:]]
                    if not closings:
                        raise UnsupportedFlowchart()
                    closing = min(closings, key=lambda c: rest.find(c, start))
                if rest[start : start + 1] == '"':
                    end = start + 1
                    while end < len(rest):
                        if rest[end] == "\\":
                            end += 2
                        elif rest[end] == '"':
                            break
                        else:
                            end += 1
                    if not rest.startswith(closing, end + 1):
                        raise UnsupportedFlowchart()
                    value, rest = (
                        label(rest[start : end + 1]),
                        rest[end + 1 + len(closing) :],
                    )
                else:
                    end = rest.find(closing, start)
                    if end < 0:
                        raise UnsupportedFlowchart()
                    value, rest = (
                        label(rest[start:end]),
                        rest[end + len(closing) :],
                    )
                break
        if rest.lstrip().startswith(":::"):
            match = re.match(r"\s*:::[\w,-]+", rest)
            if not match:
                raise UnsupportedFlowchart()
            rest = rest[match.end() :]
            self.graph.browser.append("node styles")
        self.graph.nodes.setdefault(identity, identity)
        if value is not None:
            self.graph.nodes[identity] = value
        if len(self.graph.nodes) > MAX_NODES:
            raise DiagramLimit("Too many flowchart nodes")
        if self.groups:
            self.graph.membership[identity] = self.groups[-1]
        return identity, rest.lstrip()

    @staticmethod
    def metadata_end(text):
        quote, depth, index = None, 0, 1
        while index < len(text):
            char = text[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
            elif char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        raise UnsupportedFlowchart()

    def node_set(self, text):
        node, text = self.node(text)
        nodes = [node]
        while text.startswith("&"):
            node, text = self.node(text[1:])
            nodes.append(node)
        return nodes, text

    def parse(self):
        graph = self.graph
        for statement in statements(self.body, brackets=True):
            text = statement.strip()
            if web_statement(text, "flowchart"):
                graph.browser.append(text.split()[0])
                continue
            if re.match(r"(?:direction|accTitle|accDescr)\b", text):
                # Accessibility metadata is preserved as an explicit annotation.
                graph.annotations.append(text)
                continue
            if text == "end":
                if not self.groups:
                    raise UnsupportedFlowchart()
                self.groups.pop()
                continue
            if text.startswith("subgraph "):
                definition = text[9:].strip()
                parent = self.groups[-1] if self.groups else None
                # A bare multi-word title is also a legal anonymous subgraph.
                if not any(c in definition for c in "[({"):
                    identity, title = definition, label(definition)
                else:
                    identity, rest = self.node(definition)
                    if rest:
                        raise UnsupportedFlowchart()
                    title = graph.nodes.pop(identity)
                    graph.membership.pop(identity, None)
                graph.groups[identity] = (title, parent)
                self.groups.append(identity)
                if len(self.groups) > 16 or len(graph.groups) > MAX_NODES:
                    raise DiagramLimit("Too many nested subgraphs")
                continue
            left, rest = self.node_set(text)
            while rest:
                edge_id = re.match(r"[\w]+@(?=[<.=-])", rest)
                if edge_id:
                    rest = rest[edge_id.end() :]
                # Mermaid accepts both -->|label| and -- label --> (also
                # -. label .-> and == label ==>).
                inline = re.match(
                    r"(--|==|-\.)\s+(.+?)\s+(--+>|==+>|\.->)", rest, re.S
                )
                edge_label = ""
                if inline:
                    arrow = {"--": "-->", "==": "==>", "-.": "-.->"}[inline[1]]
                    edge_label, rest = (
                        label(inline[2]),
                        rest[inline.end() :].lstrip(),
                    )
                else:
                    match = ARROW.match(rest)
                    if not match:
                        raise UnsupportedFlowchart()
                    arrow, rest = match[0], rest[match.end() :].lstrip()
                    if rest.startswith("|"):
                        end = rest.find("|", 1)
                        if end < 0:
                            raise UnsupportedFlowchart()
                        edge_label, rest = (
                            label(rest[1:end]),
                            rest[end + 1 :].lstrip(),
                        )
                right, rest = self.node_set(rest)
                notation = "↔" if arrow.startswith("<") else "→"
                if not arrow.endswith((">", "o", "x")):
                    notation = "←" if arrow.startswith("<") else "─"
                notes = [edge_label] if edge_label else []
                if "." in arrow:
                    notes.append("dotted")
                if "=" in arrow:
                    notes.append("thick")
                if "~" in arrow:
                    notes.append("invisible layout link")
                if "o" in arrow or "x" in arrow:
                    notes.append("endpoints " + arrow)
                if notation != "→":
                    notes.insert(0, notation)
                for a in left:
                    for b in right:
                        if len(graph.edges) >= MAX_EDGES:
                            raise DiagramLimit("Too many flowchart edges")
                        if notation == "←":
                            a_edge, b_edge = b, a
                        else:
                            a_edge, b_edge = a, b
                        graph.edges.append((a_edge, b_edge, " · ".join(notes)))
                left = right
        if self.groups or not (graph.nodes or graph.groups):
            raise UnsupportedFlowchart()
        # Subgraph IDs are endpoints too, not duplicate anonymous nodes.
        for identity, (title, _) in graph.groups.items():
            if identity in graph.nodes:
                graph.nodes[identity] = "Group: " + title
        return graph


def parse_flowchart(source):
    try:
        return FlowParser(source).parse()
    except DiagramLimit as error:
        raise UnsupportedFlowchart(str(error)) from error
