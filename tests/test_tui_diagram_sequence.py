"""Sequence layout assertions inspect cells, not just presence of source text."""

import pytest
from rich.cells import cell_len

from cc_remote.tui_mermaid import render_diagram
from cc_remote.tui_markdown import render_markdown


SOURCE = """sequenceDiagram
participant H as 主机 llama / backend
participant C as 卡端 runtime / 执行器
participant K as 卡端 kernel 库
Note over H,K: 加载与准备阶段
H->>C: 上传权重，分配卡端 buffer
H->>C: 提交可复用的计算图／执行计划
C-->>H: 返回计划句柄
Note over H,K: 执行阶段
H->>C: 更新输入，执行指定计划
C->>K: 矩阵乘
K-->>C: 完成，中间数据留在卡上
C->>K: 加法、激活及后续计算
K-->>C: 完成
C-->>H: 报告计划完成
H->>C: 读取需要的输出
"""


def render(body, width=80):
    result = render_diagram("sequenceDiagram\n" + body, width)
    assert result is not None
    return result.plain


def test_user_diagram_has_parallel_lifelines_and_ten_directional_arrows():
    text = render_diagram(SOURCE, 100).plain
    lines = text.splitlines()
    header = next(line for line in lines if "主机" in line)
    assert "执行器" in header and "kernel" in header
    lifelines = next(line for line in lines if line.count("│") == 3)
    positions = [i for i, char in enumerate(lifelines) if char == "│"]
    h, c, k = positions
    arrows = [line for line in lines if "▶" in line or "◀" in line]
    assert len(arrows) == 10
    for line, sender, receiver, dashed in zip(
        arrows,
        [h, h, c, h, c, k, c, k, c, h],
        [c, c, h, c, k, c, k, c, h, c],
        [False, False, True, False, False, True, False, True, True, False],
    ):
        assert line[receiver] == ("▶" if receiver > sender else "◀")
        assert line[sender] in "├┤"
        assert ("┄" if dashed else "─") in line
        other = ({h, c, k} - {sender, receiver}).pop()
        assert line[other] == "│"
    assert "[->>" not in text
    assert "1." not in text  # Mermaid does not number without autonumber.
    assert "Note: 加载与准备阶段" in text


@pytest.mark.parametrize("width", [8, 12, 16, 24, 40, 80, 100, 240])
def test_chinese_reflow_is_bounded_and_preserves_every_message(width):
    text, responsive, _ = render_markdown(
        "```mermaid\n" + SOURCE + "```",
        width,
    )
    assert responsive
    assert all(cell_len(line) <= width for line in text.plain.splitlines())
    if width >= 16:
        assert text.plain.count("▶") + text.plain.count("◀") == 10
        assert "Narrow terminal" not in text.plain
    # Wrapping can insert whitespace and lifelines, but cannot drop content.
    flattened = "".join(c for c in text.plain if not c.isspace() and c != "│")
    assert "提交可复用的计算图／执行计划" in flattened


@pytest.mark.parametrize(
    "arrow,head",
    [
        ("->>", "▶"),
        ("-->>", "▶"),
        ("->", "┤"),
        ("-->", "┤"),
        ("-)", "▷"),
        ("--)", "▷"),
        ("-x", "×"),
        ("--x", "×"),
        ("<<->>", "▶"),
        ("<<-->>", "▶"),
    ],
)
def test_arrow_styles_and_heads(arrow, head):
    lines = render(f"A{arrow}B: Request").splitlines()
    edge = next(line for line in lines if "─" in line or "┄" in line)
    assert head in edge
    assert ("┄" if "--" in arrow else "─") in edge
    assert ("◀" in edge) == arrow.startswith("<<")


def test_self_message_has_return_loop_not_a_one_character_arrow():
    text = render("A->>A: first\nA-->>A: return\nA-)A: async")
    assert text.count("╮") == text.count("╯") == 3
    assert "◀────╯" in text and "◀┄┄┄┄╯" in text
    assert "◁────╯" in text


def test_blocks_notes_and_activation_are_drawn_in_order():
    text = render("""actor A as User
participant B as Server
alt success
A->>+B: request
Note right of B: working
loop retry
B->>B: step
end
B-->>-A: result
else error
Note over A,B: failure
B-xA: failed
end""")
    assert "actor · User" in text
    assert "┃" in text
    assert "┌" in text and "└" in text
    assert text.index("alt success") < text.index("request")
    assert text.index("loop retry") < text.index("step")
    assert text.index("else error") < text.index("failure")
    assert "Note: working" in text
    last_line = text.splitlines()[-1]
    assert "┃" not in last_line


def test_autonumber_off_and_resume_do_not_reset_counter():
    text = render("""autonumber 10 5
A->>B: one
autonumber off
A->>B: two
autonumber resume
A->>B: three""")
    assert "10. one" in text and "15. three" in text
    assert ". two" not in text


def test_creation_and_destruction_bound_lifetimes():
    text = render("""participant A
A->>A: prepare
create participant B as Worker
A->>B: create
destroy B
B-->>A: finish
A->>A: alone""")
    before, after = text.split("create B")
    live = next(line for line in after.splitlines() if line.count("│") == 2)
    worker_column = live.rindex("│")
    assert all(
        line.ljust(80)[worker_column] != "│" for line in before.splitlines()
    )
    assert "×" in after
    assert after.splitlines()[-1].count("│") == 1


def test_excess_participants_and_numeric_extensions_do_not_crash():
    assert (
        render_diagram(
            "sequenceDiagram\n"
            + "\n".join(f"participant P{i}" for i in range(65)),
            100,
        )
        is None
    )
    assert "Source ·" in render("autonumber " + "9" * 5000)


def test_unknown_syntax_and_browser_hint_survive_narrow_fallback():
    text = render(
        "participant A\nparticipant B\nparticipant C\nfuture syntax", 8
    )
    assert "Source" in text
    assert "browser" in text


def test_deep_blocks_keep_outer_frame_after_inner_frames_end():
    text = render("""loop outer
loop middle
loop inner
A->>B: inner
end
end
A->>B: outer
end""")
    arrow = [line for line in text.splitlines() if "▶" in line][-1]
    assert arrow[0] == arrow[-1] == "│"


def test_alias_entities_are_decoded_once():
    assert "&lt;" in render('participant A as "&amp;lt;"\nA->>A: hello')


def test_message_entities_do_not_split_into_separate_statements():
    text = render("A->>B: A#59;B &amp; C;B-->>A: done")
    assert "A;B & C" in text and "done" in text
    assert "Source ·" not in text


@pytest.mark.parametrize("width", range(8, 85))
def test_notes_self_calls_and_nested_frames_fit_all_terminal_widths(width):
    text = render(
        """participant A as 中文<br/>é 👩‍💻
participant B as Server
alt first
loop retry
Note left of A: 左边<br/>第二行
A->>A: 本地处理
Note right of B: 右边
B-->>B: 本地返回
Note over A: 自己
Note over B,A: 两者
end
else second
A->>B: remote
end""",
        width,
    )
    assert all(cell_len(line) <= width for line in text.splitlines())
    assert "Source ·" not in text
