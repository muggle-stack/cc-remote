"""Local Markdown limits are visible independently of wrapper truncation."""

import pytest
from rich.segment import Segment
from types import SimpleNamespace
from textual.widgets import Static

from cc_remote.tui_preview import PreviewRef
from cc_remote.tui_preview_views import FilePreviewScreen, MarkdownReader
from tests.test_tui_preview import make_app


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["source", "lines"])
async def test_preview_marks_local_limits_and_clears_marker(monkeypatch, limit):
    async def request(*args, **kwargs):
        return dict(type="file_preview", format="markdown", truncated=False,
                    content="long" * (70000 if limit == "source" else 1))

    line_count = 5001 if limit == "lines" else 2

    def render(*args, **kwargs):
        return [[Segment("line")] for _ in range(line_count)]

    monkeypatch.setattr("cc_remote.tui_preview_views.preview_request", request)
    monkeypatch.setattr("cc_remote.tui_preview_views.Console", lambda **kw:
                        SimpleNamespace(render_lines=render, options=None))
    app = make_app()
    async with app.run_test() as pilot:
        await app.push_screen(FilePreviewScreen(
            app.client, "s", PreviewRef(path="/file.md", kind="markdown"),
        ))
        await pilot.pause()
        reader = app.screen.query_one(MarkdownReader)
        assert reader.local_truncated
        assert "truncated locally" in reader.text
        assert "truncated locally" in str(
            app.screen.query_one("#preview-status", Static).render()
        )
        line_count = 2
        reader.show_markdown("short")
        assert not reader.local_truncated
        assert "truncated locally" not in reader.text
