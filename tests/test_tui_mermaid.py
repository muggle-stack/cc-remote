"""Flowcharts are readable terminal text, never executable Mermaid scripts."""

import pytest
from rich.cells import cell_len

from cc_remote.tui_markdown import render_markdown
from cc_remote.tui_mermaid import parse_flowchart, render_flowchart

EXAMPLE = '''flowchart TB
    A["ggml 计算节点<br/>矩阵乘：输入、输出、形状、类型"]
    A --> B["K3：CPU backend 的 SpacemiT 优化路径"]
    B --> C["选择并调用已编译的矩阵乘函数"]
    C --> D["执行 RISC-V / RVV / IME 指令"]
    A --> E["NVIDIA：CUDA backend"]
    E --> F["选择自有 CUDA kernel 或 cuBLAS 实现"]
    F --> G["通过 CUDA 提交到 GPU<br/>由 SM 执行计算程序"]
'''


@pytest.mark.parametrize("width", [20, 48, 100])
@pytest.mark.parametrize("fenced", [False, True])
def test_requested_flowchart_is_rendered_with_all_nodes(width, fenced):
    source = "```mermaid\n" + EXAMPLE + "```" if fenced else EXAMPLE
    text, responsive, _ = render_markdown(source, width)
    assert responsive and "╭" in text.plain and "─▶" in text.plain
    assert '<br/>' not in text.plain and 'A["' not in text.plain
    assert all(cell_len(line) <= width for line in text.plain.splitlines())
    graph = parse_flowchart(EXAMPLE)
    compact = "".join(text.plain.split())
    # Labels may wrap; every node ID is retained and all six edges are parsed.
    assert all(node + "·" in compact for node in graph.nodes)
    assert len(graph.edges) == 6


def test_shared_nodes_cycles_and_edge_labels_are_bounded():
    source = 'graph LR;A["start"]-->B;A-->|other|C;B-->D;C-->D;D-->A'
    graph = parse_flowchart(source)
    assert len(graph.edges) == 5
    text = render_flowchart(source, 80).plain
    assert text.count("D · D") == 1 and "↪ D" in text and "↪ A" in text
    assert "other" in text and len(text) < 2000


@pytest.mark.parametrize("source", [
    'flowchart TB\nA["unfinished',
    "graph TB\n" + ";".join(f"N{i}" for i in range(65)),
    'graph TB\nA["' + "x" * 33000 + '"]',
    'graph TB\nA["' + "中" * 12000 + '"]',
])
def test_unsupported_or_oversized_diagrams_keep_source(source):
    assert render_flowchart(source, 80) is None
    text, _, _ = render_markdown("```mermaid\n" + source + "\n```", 80)
    assert "Unsupported diagram" in text.plain and source in text.plain


@pytest.mark.parametrize("ending", ["", "\n"])
def test_flowcharts_do_not_change_other_code_blocks_or_prose(ending):
    text, responsive, _ = render_markdown(
        "```text\n" + EXAMPLE + "```" + ending, 80,
    )
    # Streaming Markdown drops the final display newline when its source has
    # none. Ordinary code must retain all content under that existing policy.
    expected = EXAMPLE if ending else EXAMPLE.removesuffix("\n")
    assert text.plain == expected and not responsive
    source = "Mention flowchart TB in a sentence."
    assert render_markdown(source, 80)[0].plain == source


def test_labels_cannot_inject_terminal_controls_or_rich_markup():
    text = render_flowchart('graph TB;A["[bold]literal &amp; &#27;[31m"]', 80)
    assert "\x1b" not in text.plain and "[bold]literal &" in text.plain
