"""Cached, inert terminal Mermaid projections; no browser or JS execution."""

from cc_remote.tui_diagram_flow import (
    Flowchart,
    UnsupportedFlowchart,
    parse_flowchart,
)
from cc_remote.tui_diagram_text import (
    Canvas,
    DiagramLimit,
    HEADER,
    MAX_SOURCE,
    diagram_source,
)

# Compatibility exports for callers inspecting the flowchart projection.
__all__ = [
    "Flowchart",
    "UnsupportedFlowchart",
    "parse_flowchart",
    "HEADER",
    "MAX_SOURCE",
    "render_flowchart",
    "render_diagram",
]


def flow_text(graph, width, browser_key):
    canvas = Canvas(
        width, "Flowchart · text layout; edge annotations preserve direction"
    )
    for annotation in graph.annotations:
        canvas.line(annotation, style="bold")
    for identity, (title, parent) in graph.groups.items():
        canvas.line(
            "Group "
            + identity
            + " · "
            + title
            + (" (inside " + parent + ")" if parent else ""),
            style="bold",
        )
    children = {node: [] for node in graph.nodes}
    incoming = set()
    for left, right, text in graph.edges:
        children[left].append((right, text))
        incoming.add(right)
    visited = set()

    def draw(node, depth=0):
        indent = min(depth * 4, max(0, canvas.width - 8))
        if node in visited or depth > 12:
            canvas.line("↪ " + node + " (see node)", indent)
            return
        visited.add(node)
        group = graph.membership.get(node)
        title = node + " · " + graph.nodes[node]
        if group:
            title += " [group: " + group + "]"
        canvas.box(title, indent=indent)
        for target, text in children[node]:
            canvas.line(
                "└─▶ " + target + (" · " + text if text else ""), indent
            )
            if indent + 12 >= canvas.width:
                canvas.line("→ " + target + " (see node)", indent)
            else:
                draw(target, depth + 1)

    for node in [n for n in graph.nodes if n not in incoming] + list(
        graph.nodes
    ):
        if node not in visited:
            draw(node)
    canvas.browser_hint(browser_key, graph.browser)
    return canvas.text


def render_flowchart(source, width, browser_key="Space B"):
    try:
        return flow_text(parse_flowchart(source), width, browser_key)
    except (UnsupportedFlowchart, DiagramLimit):
        return None


def render_diagram(source, width, browser_key="Space B", *, standalone=False):
    """Semantic text where known, explicit source rows for unknown extensions.

    Recognizing a diagram family is not a claim of validating every version's
    grammar. Unprojected constructs stay visible and carry a Web affordance.
    """
    try:
        document = diagram_source(source)
        if document is None:
            return None
        if standalone:
            # A prose paragraph such as 'info about the server' is not a
            # diagram. Bare headers must occupy their own declaration line.
            option = document.body.split("\n", 1)[0].strip()
            if option and option not in {
                "TB",
                "TD",
                "LR",
                "RL",
                "BT",
                "LR:",
                "TB:",
                "BT:",
                "showData",
                "horizontal",
            }:
                return None
        if document.kind in {"graph", "flowchart", "flowchart-elk"}:
            return render_flowchart(source, width, browser_key)
        from cc_remote.tui_diagram_families import render_family

        return render_family(document, width, browser_key)
    except DiagramLimit:
        return None
