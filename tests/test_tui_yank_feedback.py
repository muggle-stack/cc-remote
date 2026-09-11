"""Yank feedback is shared, transient and independent from selection."""

import pytest

from cc_remote.tui_keys import KeyConfig
from cc_remote.tui_preview_views import MarkdownReader
from cc_remote.tui_panels import PanelReader, QuestionDialog
from tests.test_tui_vim_shared import make_app, open_surface


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["chat", "draft", "report", "form", "search"]
)
async def test_yank_flash_lands_at_start_without_visual_selection(surface):
    app = make_app()
    app.client.keys = KeyConfig({"vim": {"yank_highlight_ms": 5000}})
    async with app.run_test() as pilot:
        editor = await open_surface(app, surface)
        editor.load_text("call(中文 word)")
        editor.set_mode("NORMAL")
        editor.move_cursor((0, 9))
        await pilot.press("y", "i", "left_parenthesis")
        assert app.clipboard == "中文 word"
        assert editor.cursor_location == (0, 5)
        assert editor.selection.start == editor.selection.end
        assert editor.yank_range == ((0, 5), (0, 12))
        assert editor.vim_mode == "NORMAL"
        style = editor.get_component_rich_style("vim--yank")
        assert any(
            segment.style and segment.style.bgcolor == style.bgcolor
            for segment in editor.render_line(0)
        )
        assert "Copied 7 characters" in app.client.notice
        revision = editor.yank_revision
        await pilot.press("y", "i", "w")
        latest = editor.yank_range
        editor.clear_yank(revision)
        assert editor.yank_range == latest
        editor.clear_yank()
        assert editor.yank_range is None
        assert app.clipboard and not app.client._outbox


@pytest.mark.asyncio
async def test_yank_timer_clears_only_paint_not_cursor_or_scroll():
    app = make_app()
    async with app.run_test() as pilot:
        editor = await open_surface(app, "draft")
        editor.load_text("line (copy)")
        editor.move_cursor((0, 8))
        editor.operate("y", (0, 6), (0, 10))
        assert editor.yank_range
        cursor, selection, scroll = (
            editor.cursor_location,
            editor.selection,
            editor.scroll_offset,
        )
        await pilot.pause(0.3)
        assert editor.yank_range is None
        assert (
            editor.cursor_location,
            editor.selection,
            editor.scroll_offset,
        ) == (cursor, selection, scroll)


@pytest.mark.asyncio
async def test_line_yank_keeps_column_and_document_changes_clear_highlight():
    app = make_app()
    app.client.keys = KeyConfig({"vim": {"yank_highlight_ms": 5000}})
    async with app.run_test() as pilot:
        editor = await open_surface(app, "draft")
        editor.load_text("first line\nsecond line\nthird")
        editor.move_cursor((1, 5))
        await pilot.press("y", "k")
        assert editor.cursor_location == (0, 5)
        assert editor.yank_range == ((0, 0), (2, 0))
        assert app.vim_register[1]
        editor.load_text("replacement")
        assert editor.yank_range is None
        editor.operate("y", (0, 0), (0, 3))
        editor.insert("edit")
        assert editor.yank_range is None


@pytest.mark.asyncio
async def test_preview_and_question_readers_share_feedback():
    app = make_app()
    app.client.keys = KeyConfig({"vim": {"yank_highlight_ms": 5000}})
    async with app.run_test() as pilot:
        await app.push_screen(
            QuestionDialog(
                app.client,
                {
                    "sid": "s",
                    "ask_id": "ask",
                    "question": "choose (one two)",
                },
            )
        )
        reader = app.screen.query_one(PanelReader)
        await pilot.press("ctrl+k")
        reader.move_cursor((0, 11))
        await pilot.press("y", "i", "left_parenthesis")
        assert app.clipboard == "one two" and reader.yank_range
        # Preview's styled get_line override must retain the shared overlay.
        preview = MarkdownReader()
        await app.screen.query_one(".tui-panel").mount(preview)
        preview.focus()
        preview.show_markdown("**hello** (world)")
        begin = preview.text.index("world")
        start = preview.document.get_location_from_index(begin)
        end = preview.document.get_location_from_index(begin + 5)
        preview.operate("y", start, end)
        style = preview.get_component_rich_style("vim--yank")
        assert any(segment.style and segment.style.bgcolor == style.bgcolor
                   for segment in preview.render_line(0))


def test_yank_timeout_config_is_bounded_and_can_disable():
    assert KeyConfig().yank_highlight_ms == 200
    assert KeyConfig({"vim": {"yank_highlight_ms": 0}}).yank_highlight_ms == 0
    for value in (-1, 5001, True, "200"):
        with pytest.raises(ValueError):
            KeyConfig({"vim": {"yank_highlight_ms": value}})


@pytest.mark.asyncio
async def test_flash_disabled_and_empty_yank_preserves_register():
    app = make_app()
    app.client.keys = KeyConfig({"vim": {"yank_highlight_ms": 0}})
    async with app.run_test():
        editor = await open_surface(app, "draft")
        editor.load_text("copy")
        editor.operate("y", (0, 0), (0, 4))
        assert app.clipboard == "copy" and editor.yank_range is None
        editor.operate("y", (0, 2), (0, 2))
        assert app.clipboard == "copy" and editor.yank_range is None
        assert app.client.notice == "Nothing to copy"
