"""Terminal diagram projections retain data without executing directives."""

import json
from urllib.parse import unquote, urlsplit

import pytest
from rich.cells import cell_len

from cc_remote.tui_diagram_text import KINDS, diagram_source
from cc_remote.tui_mermaid import parse_flowchart, render_diagram
from cc_remote.tui_markdown import render_markdown
from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_diagram_browser import (
    session_browser_url,
    open_session_browser,
)
from cc_remote.tui_app import WorkspaceApp, Transcript
from cc_remote.tui_preview import PreviewRef
from cc_remote.tui_preview_views import FilePreviewScreen, MarkdownReader
from tests.test_tui_workspace import client

GPU = """flowchart TB
    H["主机：框架、backend、CUDA 与驱动"]
    subgraph GPU["GPU 卡端"]
        G["GSP 管理处理器<br/>运行管理固件"]
        Q["计算任务入口"]
        S["多个 SM 计算单元<br/>执行 kernel 机器指令"]
        M["显存<br/>计算程序、权重、中间结果"]
        Q --> S
        S <-->|读取与写入| M
        G -. 初始化与设备管理 .-> Q
    end
    H -->|提交计算任务| Q
    H -. 管理通信 .-> G
"""

EXAMPLES = {
    "sequenceDiagram": "participant A as Alice\nparticipant B as Bob\nloop retry\nA->>B: hello\nB-->>A: reply\nend",
    "classDiagram": "class Animal {\n+name: string\n+eat()\n}\nAnimal <|-- Duck",
    "stateDiagram": "[*] --> Idle\nIdle --> Busy: start\nBusy --> [*]",
    "erDiagram": "USER ||--o{ ORDER : places\nUSER {\nstring name\n}",
    "requirementDiagram": 'requirement test {\nid: 1\ntext: "Safe"\n}\nA - satisfies -> test',
    "gitGraph": 'commit id: "a"\nbranch dev\ncommit id: "b"\ncheckout main\nmerge dev',
    "gantt": "dateFormat YYYY-MM-DD\nsection Build\nFirst :a, 2026-01-01, 2d\nNext :after a, 3d",
    "pie": '"A": 30\n"B": 70',
    "journey": "section Work\nWrite: 5: Me, You",
    "timeline": "section Past\n2025: First\n2026: Next",
    "mindmap": "  root((Root))\n    A[First]\n      B[Child]\n    C[Second]",
    "kanban": "  Todo[Todo]\n    task[Implement]@{ assigned: 'me' }",
    "quadrantChart": "x-axis Low --> High\ny-axis Low --> High\nquadrant-1 Best\nA: [0.2, 0.8]",
    "xychart": 'x-axis [jan, feb]\ny-axis "Count" 0 --> 20\nbar [10, 20]\nline [12, 15]',
    "sankey": '"A, one",B,10\nB,C,8',
    "packet": '0-7: "Header"\n+8: "Body"',
    "radar-beta": 'axis a["A"], b["B"]\ncurve one["One"]{1,2}\nmax 10',
    "block": 'columns 2\nA["One"] B["Two"]\nA --> B',
    "architecture": "group cloud(cloud)[Cloud]\nservice api(server)[API] in cloud\nservice db(database)[DB]\napi:R --> L:db",
    "C4Context": 'Person(user, "User", "Person")\nSystem(app, "App")\nRel(user, app, "Uses")',
    "C4Container": 'Container(api, "API", "Python", "Backend")',
    "C4Component": 'Component(db, "DB", "SQL")',
    "C4Dynamic": 'Rel(user, api, "Request", "HTTPS")',
    "C4Deployment": 'Deployment_Node(host, "Host") {\nContainer(api, "API")\n}',
    "treeView-beta": "Root\n  Child\n    Leaf",
    "treemap": '"Root"\n  "A": 30\n  "B": 70',
    "swimlane-beta": "pool Main {\nlane Work {\ntask A\n}\n}",
    "eventmodeling": "swimlane User\nevent Created\ncommand Create",
    "ishikawa": "Problem\n  People\n    Training",
    "venn-beta": 'set A["First"]\nset B["Second"]\nA & B: 10',
    "wardley-beta": "component App [0.8, 0.3]\ncomponent DB [0.5, 0.8]\nApp -> DB",
    "cynefin-beta": "complex\n  Investigate\nclear\n  Repeat",
    "railroad-beta": 'rule = "a" | "b"',
    "railroad-ebnf-beta": 'rule = "a", { "b" };',
    "railroad-abnf-beta": "rule = 1*DIGIT",
    "railroad-peg-beta": 'rule <- "a" / "b"',
    "zenuml": "A->B: hello",
    "info": "",
}
ALIASES = {
    "classDiagram-v2": "classDiagram",
    "stateDiagram-v2": "stateDiagram",
    "requirement": "requirementDiagram",
}
for alias in KINDS:
    if alias.endswith("-beta") and alias[:-5] in EXAMPLES:
        ALIASES[alias] = alias[:-5]
for alias, target in ALIASES.items():
    EXAMPLES[alias] = EXAMPLES[target]
for kind in ("flowchart", "flowchart-elk", "graph"):
    EXAMPLES[kind] = 'TB\nA["Start"] --> B["End"]'


def test_every_pinned_family_has_a_projection_fixture():
    assert set(KINDS) == set(EXAMPLES)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("width", [12, 40, 100])
def test_family_projection_is_bounded_and_responsive(kind, width):
    source = kind + "\n" + EXAMPLES[kind]
    text, responsive, _ = render_markdown(
        "```mermaid\n" + source + "\n```", width
    )
    assert responsive
    assert "Unsupported diagram" not in text.plain
    assert "Source ·" not in text.plain
    assert all(cell_len(line) <= width for line in text.plain.splitlines())
    assert len(text) < 128 * 1024


@pytest.mark.parametrize("width", [12, 40, 100])
def test_gpu_graph_keeps_groups_bidirectional_edges_and_inline_labels(width):
    graph = parse_flowchart(GPU)
    assert graph.groups["GPU"] == ("GPU 卡端", None)
    assert graph.membership["S"] == "GPU"
    assert len(graph.edges) == 5
    assert any(
        a == "S" and b == "M" and "↔" in text for a, b, text in graph.edges
    )
    assert any(
        a == "G" and b == "Q" and "初始化与设备管理" in text
        for a, b, text in graph.edges
    )
    result = render_diagram(GPU, width)
    assert result is not None and "Source ·" not in result.plain


def test_flowchart_chain_sets_shapes_and_nested_groups():
    source = """graph LR
subgraph outer["Outer"]
subgraph inner["Inner"]
A(("Circle")) & B{"Diamond"} --> C@{shape: rect, label: "Final"}
end
end
C --> D["End"]
"""
    graph = parse_flowchart(source)
    assert graph.groups["inner"] == ("Inner", "outer")
    assert graph.nodes["C"] == "Final"
    assert {(a, b) for a, b, _ in graph.edges} == {
        ("A", "C"),
        ("B", "C"),
        ("C", "D"),
    }


def test_directives_are_inert_and_hint_tracks_configured_key():
    source = """---
title: Diagram
config:
  theme: dark
---
%%{init: {"securityLevel": "loose"}}%%
flowchart TB
A["[bold]literal"] --> B
click A "https://example.com"
classDef red fill:red
class A red
"""
    result = render_diagram(source, 100, "Space z")
    assert (
        result is not None
        and "Space z: open session in browser" in result.plain
    )
    assert "Web-only" in result.plain and "[bold]literal" in result.plain
    assert "https://example.com" not in result.plain


def test_unknown_extension_is_not_silently_accepted_or_lost():
    result = render_diagram("sequenceDiagram\nfuture syntax", 100)
    assert "Source · future syntax" in result.plain
    assert "unprojected syntax" in result.plain
    assert render_diagram("futureDiagram\nA", 80) is None


def test_pie_values_and_packet_offsets_are_not_just_source():
    text = render_diagram('pie\n"A": 30\n"B": 70', 80).plain
    assert "30.0%" in text and "70.0%" in text and "█" in text
    text = render_diagram('packet\n0-7: "Head"\n+8: "Body"', 80).plain
    assert "Bits 8–15 (8)" in text and "Body" in text
    text = render_diagram('pie\n"A": 1e308\n"B": 1e308', 80).plain
    assert text.count("50.0%") == 2


def test_browser_route_has_no_source_or_secret_and_preserves_scope():
    c = client()
    c.workspace.catalog["s"] = {"engine": "claude", "space": "work"}
    target = session_browser_url(c, "s")
    route = json.loads(unquote(urlsplit(target).fragment.split("=", 1)[1]))
    assert route == {
        "machine_id": c.machine_id,
        "session_id": "s",
        "engine": "claude",
        "space": "work",
    }
    assert not urlsplit(target).query
    assert KeyConfig().lookup("space B")[0]["id"] == "normal.diagram_browser"


@pytest.mark.asyncio
async def test_browser_opens_only_when_explicitly_requested(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cc_remote.tui_diagram_browser.webbrowser.open",
        lambda url, **kw: calls.append(url) or True,
    )
    render_diagram('graph TB\nA-->B\nclick A "javascript:evil()"', 80)
    assert not calls
    assert (
        await open_session_browser(client(), "s") == "Opened session in browser"
    )
    assert len(calls) == 1 and "evil" not in calls[0]


@pytest.mark.parametrize(
    "source",
    [
        "graph TB\nA" + "x" * 33000,
        "sequenceDiagram\n" + "A->>B: hello\n" * 501,
    ],
)
def test_limits_fail_closed(source):
    assert render_diagram(source, 80) is None


def test_comment_frontmatter_and_non_mermaid_code():
    assert diagram_source('%% note\npie\n"A": 1').kind == "pie"
    text, responsive, _ = render_markdown('```text\npie\n"A": 1\n```', 80)
    assert text.plain == 'pie\n"A": 1' and not responsive
    for prose in ("info about the server", "graph of our work", "pie is food"):
        assert render_markdown(prose, 80)[0].plain == prose


@pytest.mark.asyncio
async def test_configured_browser_key_is_live_in_chat_and_preview(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "cc_remote.tui_diagram_browser.webbrowser.open",
        lambda url, **kw: calls.append(url) or True,
    )

    async def request(*args, **kwargs):
        return {
            "type": "file_preview",
            "format": "markdown",
            "content": '```mermaid\ngraph TB\nA-->B\nclick A "https://example.com"\n```',
        }

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    c = client()
    c.keys = KeyConfig(
        {
            "normal": {"diagram_browser": ["space z"]},
            "preview": {"browser": ["alt+m"]},
        }
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(Transcript).focus()
        assert not calls
        await pilot.press("space", "z")
        assert len(calls) == 1
        await app.push_screen(
            FilePreviewScreen(
                c,
                "s",
                PreviewRef(path="/diagram.md", kind="markdown"),
            )
        )
        await pilot.pause()
        reader = app.screen.query_one(MarkdownReader)
        assert "alt+m" in reader.text.lower()
        await pilot.press("alt+m")
        assert len(calls) == 2


def test_large_numeric_literals_and_terminal_controls_stay_inert():
    result = render_diagram("packet\n" + "1" * 5000 + ': "Bad"', 40)
    assert "Source ·" in result.plain
    for kind in ("pie", "sequenceDiagram", "mindmap"):
        result = render_diagram(kind + '\n"&#27;[31m": 1', 80)
        assert "\x1b" not in result.plain
