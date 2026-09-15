"""Markdown is presentation, never a rewrite of session content or URLs."""

import pytest
from rich.cells import cell_len
from rich.console import Console
from textual.widgets.text_area import Selection

from cc_remote.tui_app import Composer, Transcript, WorkspaceApp, location
from cc_remote.tui_markdown import (
    display_url,
    MarkdownProjection,
    matching_runs,
    render_markdown,
)
from cc_remote.tui_state import Block
from tests.test_tui_send_jumps import client
from tests.test_tui_scroll_frames import watch_frames


def style(text, word):
    return text.get_style_at_offset(Console(), text.plain.index(word))


def test_inline_markdown_styles_without_delimiters():
    text, _, _ = render_markdown("**bold** *italic* ~~gone~~ `call(x)`", 80)
    assert text.plain == "bold italic gone call(x)"
    assert style(text, "bold").bold
    assert style(text, "italic").italic
    assert style(text, "gone").strike
    assert style(text, "call").color


@pytest.mark.parametrize(
    "source",
    [
        "[title](https://example.com/a?q=1#anchor)",
        "[title][ref]\n\n[ref]: https://example.com/a?q=1#anchor",
        "<https://example.com/a?q=1#anchor>",
    ],
)
def test_links_keep_a_contiguous_complete_url(source):
    text, _, _ = render_markdown(source, 20)
    assert "https://example.com/a?q=1#anchor" in text.plain
    assert "[title]" not in text.plain and "[ref]" not in text.plain
    assert text.plain.count("https://example.com/a?q=1#anchor") == 1


@pytest.mark.parametrize("source", [
    "[讲稿](/docs/%E6%BC%94%E8%AE%B2.md)",
    "[讲稿](/docs/演讲.md)",
    "![讲稿](/docs/%E6%BC%94%E8%AE%B2.md)",
    "| File |\n| --- |\n| [讲稿](/docs/演讲.md) |",
])
def test_unicode_destinations_are_readable(source):
    text, _, _ = render_markdown(source, 80)
    assert "/docs/演讲.md" in text.plain
    assert "%E6%BC%94" not in text.plain


def test_unicode_autolink_is_not_duplicated():
    text, _, _ = render_markdown(
        "<https://example.com/%E6%BC%94%E8%AE%B2>", 80
    )
    assert text.plain == "https://example.com/演讲"


def test_unicode_link_preserves_surrounding_emphasis():
    text, _, _ = render_markdown(
        "**<https://example.com/%E6%BC%94%E8%AE%B2>**", 80
    )
    assert text.plain == "https://example.com/演讲"
    assert style(text, "https://").bold


@pytest.mark.parametrize("encoded", [
    "%20", "%2F", "%23", "%3F", "%25", "%1B", "%0A",
    "%FF", "%E6%BC", "%C2%85", "%E2%80%AE", "%E3%80%80",
])
def test_url_escapes_cannot_change_structure_or_inject_controls(encoded):
    assert display_url("/docs/" + encoded) == "/docs/" + encoded


def test_literal_code_keeps_percent_encoding():
    text, _, _ = render_markdown("`/docs/%E6%BC%94.md`", 80)
    assert text.plain == "/docs/%E6%BC%94.md"


def test_headings_lists_quotes_and_literal_markup():
    text, _, _ = render_markdown(
        "# Heading\n\nSetext\n======\n\n- **first**\n  - nested\n"
        "\n> quoted\n> continuation\n\n\\*literal\\* and &amp;",
        60,
    )
    assert "# Heading" not in text.plain and "======" not in text.plain
    assert style(text, "Heading").bold and style(text, "Setext").bold
    assert "• first" in text.plain and "• nested" in text.plain
    assert "│ quoted\n│ continuation" in text.plain
    assert "*literal* and &" in text.plain
    assert not style(text, "literal").italic


@pytest.mark.parametrize("width", [18, 70])
def test_tables_keep_all_columns_values_and_link_targets(width):
    url = "https://example.com/" + "long-path/" * 8
    text, responsive, _ = render_markdown(
        "| Name | Value |\n| --- | ---: |\n| **鱼缸** | 123 |\n"
        f"| [docs]({url}) | 456 |\n",
        width,
    )
    assert responsive
    assert "鱼缸" in text.plain and "123" in text.plain and "456" in text.plain
    assert "---" not in text.plain and "**" not in text.plain
    assert url in text.plain  # Not only wrapped fragments in a cell.
    if width == 70:
        assert "╭" in text.plain and "╰" in text.plain
        assert all(
            cell_len(line) <= width
            for line in text.plain.splitlines()
            if not line.startswith("https://")
        )


def test_code_and_control_sequences_are_inert():
    text, _, _ = render_markdown(
        '```python\nprint("**literal**")\n```\n\n'
        "\x1b]52;c;secret\x07[bold red]not Rich markup[/]",
        80,
    )
    assert "```" not in text.plain and 'print("**literal**")' in text.plain
    assert "\x1b" not in text.plain and "\x07" not in text.plain
    assert "[bold red]not Rich markup[/]" in text.plain
    assert not style(text, "literal").bold


def test_reference_expansion_is_bounded_and_empty_table_keeps_headers():
    source = (
        "[link][r] " * 800
        + "\n\n[r]: https://example.com/"
        + "long-path/" * 100
    )
    text, _, _ = render_markdown(source, 80)
    assert text.plain == source  # Safe raw fallback, not an unbounded layout.
    text, _, _ = render_markdown("| One | Two |\n| --- | --- |\n", 10)
    assert "One" in text.plain and "Two" in text.plain


def test_repetitive_prose_has_exact_coordinates_and_bounded_diff(monkeypatch):
    from difflib import SequenceMatcher

    matching_runs.cache_clear()
    lengths = []

    def matcher(junk, a, b, **kwargs):
        lengths.append(max(len(a), len(b)))
        return SequenceMatcher(junk, a, b, **kwargs)

    monkeypatch.setattr("cc_remote.tui_markdown.SequenceMatcher", matcher)
    c = client()
    view = c.workspace.view("s")
    view.blocks = [
        Block("a", "assistant", "**" + "same word " * 4000 + "end**")
    ]
    source, starts = view.render()
    projection = MarkdownProjection(source)
    rendered, _ = projection.project(starts, 50)
    for word in ("same", "end"):
        position = source.index(word)
        assert projection.source(projection.display(position)) == position
        assert rendered.plain[projection.display(position) :].startswith(word)
    assert lengths and max(lengths) <= 256


@pytest.mark.asyncio
async def test_chat_renders_prose_and_copies_visible_text():
    c = client()
    view = c.workspace.view("s")
    view.blocks = [
        Block("u", "user", "**literal prompt**", "turn"),
        Block(
            "a",
            "assistant",
            "**bold answer** and [docs](https://example.com)",
            "turn",
            "final",
        ),
        Block("t", "tool", "**literal tool**", expanded=True),
    ]
    view.render()
    view.tool_groups["tools:t"].expanded = True
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        assert "**bold answer**" not in reader.text
        assert (
            "**literal prompt**" in reader.text
            and "**literal tool**" in reader.text
        )
        assert "docs (https://example.com)" in reader.text
        row = reader.text[: reader.text.index("bold answer")].count("\n")
        assert style(reader.get_line(row), "bold answer").bold
        reader.selection = Selection((0, 0), reader.document.end)
        assert reader.selected_text == reader.text
        assert "**bold answer**" in view.render()[0]
        start = reader.text.index("bold answer")
        reader.selection = Selection(
            location(reader.text, start),
            location(reader.text, start + len("bold answer")),
        )
        assert reader.selected_text == "bold answer"
        app.quote()
        assert "> bold answer" in app.query_one(Composer).text
        assert not c._outbox


@pytest.mark.asyncio
async def test_rendered_table_resizes_without_losing_source_cursor():
    c = client()
    view = c.workspace.view("s")
    view.put(
        Block(
            "table",
            "assistant",
            "| Name | Value |\n| --- | --- |\n| fish | 123 |\n\n"
            "after-table marker",
        )
    )
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(90, 32)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        app.stop_following()
        reader.move_cursor(
            location(reader.text, reader.text.index("after-table"))
        )
        await pilot.pause()
        app.remember()
        anchor = view.anchor
        for width in (20, 100, 35):
            await pilot.resize_terminal(width, 30)
            await pilot.pause()
            assert view.anchor == anchor
            assert reader.document.get_line(
                reader.cursor_location[0]
            ).startswith("after-table")
            assert "fish" in reader.text and "123" in reader.text


@pytest.mark.asyncio
async def test_markdown_streaming_and_table_reflow_keep_bottom_frames(
    monkeypatch,
):
    c = client()
    view = c.workspace.view("s")
    app = WorkspaceApp(c, connect=False)
    async with app.run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        reader = app.query_one(Transcript)
        reader.move_cursor((20, 0))
        await pilot.press("ctrl+j")
        reader.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        frames = watch_frames(monkeypatch, app)
        for text in (
            "**stream",
            "ed**\n\n",
            "| key | value |\n",
            "| --- | --- |\n",
            "| bold | **yes** |\n",
        ):
            view.put(Block("stream", "assistant", text), append=True)
            app.paint()
            await pilot.pause()
        for size in ((45, 22), (95, 35)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
        assert frames and all(y == bottom for y, bottom in frames), frames


@pytest.mark.asyncio
async def test_unchanged_bodies_use_render_cache():
    render_markdown.cache_clear()
    c = client()
    c.workspace.view("s").put(Block("bold", "assistant", "**cached**"))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.rendered_version = -1
        app.paint()  # Initial mount now has the actual scrollbar/content width.
        await pilot.pause()
        before = render_markdown.cache_info()
        for _ in range(3):
            c.workspace.view("s").version += 1
            app.paint()
            await pilot.pause()
        after = render_markdown.cache_info()
        assert after.misses == before.misses
        assert after.hits > before.hits


@pytest.mark.asyncio
async def test_folded_markdown_progress_keeps_tool_styles_and_navigation():
    from tests.test_tui_live_layout import turn
    from tests.test_tui_progress_details import body_style

    c = client()
    view = c.workspace.view("s")
    view.blocks = []
    turn(view)
    progress = next(b for b in view.blocks if b.channel == "commentary")
    progress.text = (
        "**progress explanation**\n\n| Name | Value |\n"
        "| --- | --- |\n| test | 12 |\n"
    )
    view.event(
        dict(
            type="delta",
            message_id="answer",
            channel="final",
            text="**final answer**",
        )
    )
    view.event(dict(type="turn_end", turn_id="t", result={}))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        await pilot.pause()
        assert "progress explanation" in reader.text
        reader.move_cursor(
            location(reader.text, reader.text.index("final answer"))
        )
        await pilot.press("enter")
        assert not view.local_details["t"].expanded
        app.paint()
        await pilot.press("enter")
        assert view.local_details["t"].expanded
        app.paint()  # Do not race the workspace's 100 ms presentation timer.
        await pilot.pause()
        assert "**progress explanation**" not in reader.text
        assert "progress explanation" in reader.text and "╭" in reader.text
        assert body_style(reader, "progress explanation") == ""
        assert "echo test" not in reader.text
        start = next(i for i, b in app.starts if b.role == "tool_group")
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        app.paint()
        assert body_style(reader, "echo test") == "bright_black"
        assert body_style(reader, "final answer") == ""
        reader.move_cursor(
            location(reader.text, reader.text.index("echo test"))
        )
        await pilot.press("enter")
        assert view.local_details["t"].expanded
        app.paint()
        await pilot.pause()
        assert "echo test" not in reader.text


def test_source_position_after_a_wrapped_link_table_remains_resolvable():
    c = client()
    view = c.workspace.view("s")
    url = "https://example.com/" + "very-long-path/" * 100
    view.blocks = [
        Block(
            "a",
            "assistant",
            "| Link | Value |\n| --- | --- |\n"
            f"| [link]({url}) | 123 |\n\nafter-table marker",
        )
    ]
    source, starts = view.render()
    for width in (18, 80):
        projection = MarkdownProjection(source)
        text, _ = projection.project(starts, width)
        original = source.index("after-table")
        displayed = text.plain.index("after-table")
        assert projection.display(original) == displayed
        assert projection.source(displayed) == original


@pytest.mark.asyncio
async def test_style_only_change_invalidates_rendered_line_cache():
    c = client()
    view = c.workspace.view("s")
    view.blocks = [Block("a", "assistant", "marker")]
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+j")
        await pilot.pause()
        reader = app.query_one(Transcript)
        row = reader.text[: reader.text.index("marker")].count("\n")

        def rendered_bold():
            y = reader.wrapped_document.location_to_offset((row, 0)).y
            line = reader.render_line(y - int(reader.scroll_y))
            return any(
                "marker" in segment.text and segment.style.bold
                for segment in line
                if segment.style
            )

        assert not rendered_bold()
        view.blocks[0].text = "**marker**"
        view.version += 1
        app.paint()
        await pilot.pause()
        assert rendered_bold()
