"""Visual's painted caret must be inside the exact inclusive yank range."""

import pytest

from tests.test_tui_vim_shared import make_app, open_surface


def painted_cursor(widget):
    style = widget._theme.cursor_style
    return "".join(
        segment.text
        for y in range(widget.scrollable_content_region.height)
        for segment in widget.render_line(y)
        if segment.style
        and segment.style.color == style.color
        and segment.style.bgcolor == style.bgcolor
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["chat", "draft", "report", "form", "search"]
)
@pytest.mark.parametrize(
    "text,start,moves,caret,copied",
    [
        ("abcd", (0, 0), (), "a", "a"),
        ("abcd", (0, 0), ("l",), "b", "ab"),
        ("abc", (0, 0), ("l", "l"), "c", "abc"),
        ("abcd", (0, 2), ("h", "h"), "a", "abc"),
        ("中文字符", (0, 0), ("l", "l"), "字", "中文字"),
        ("abc\ndef", (0, 0), ("j", "l"), "e", "abc\nde"),
    ],
)
async def test_visible_character_is_also_copied(
    surface, text, start, moves, caret, copied
):
    app = make_app()
    async with app.run_test(size=(80, 30)) as pilot:
        widget = await open_surface(app, surface)
        widget.load_text(text)
        widget.set_mode("NORMAL")
        widget.move_cursor(start)
        await pilot.press("v", *moves)
        await pilot.pause()
        assert painted_cursor(widget) == caret
        assert widget._cursor_offset == (
            widget.wrapped_document.location_to_offset(widget.visual_cursor)
        )
        await pilot.press("y")
        assert app.clipboard == copied


@pytest.mark.asyncio
async def test_wrapped_visual_endpoint_does_not_draw_on_next_line():
    app = make_app()
    async with app.run_test(size=(35, 20)) as pilot:
        widget = await open_surface(app, "draft")
        widget.load_text("a" * 200)
        width = widget.wrap_width
        widget.move_cursor((0, width - 2))
        await pilot.press("v", "l")
        await pilot.pause()
        point = widget.wrapped_document.location_to_offset(widget.visual_cursor)
        assert widget._cursor_offset == point
        assert point.y == 0
        assert painted_cursor(widget) == "a"
        await pilot.press("y")
        assert app.clipboard == "aa"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["draft", "form"])
async def test_visual_delete_clamps_caret_before_document_shrinks(surface):
    app = make_app()
    async with app.run_test() as pilot:
        widget = await open_surface(app, surface)
        widget.load_text("abc\ndef\nghi")
        widget.set_mode("NORMAL")
        widget.move_cursor((0, 0))
        await pilot.press("v", "j", "j", "l", "l", "d")
        await pilot.pause()
        assert widget.text == ""
        assert widget.vim_mode == "NORMAL"
        assert widget._cursor_offset == (0, 0)
