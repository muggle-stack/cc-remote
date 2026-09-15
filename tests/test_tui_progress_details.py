"""Prose stays readable and a final answer opens its own folded activity."""

import pytest

from cc_remote.tui_app import Transcript, WorkspaceApp, location
from tests.test_tui_live_layout import client, turn


def body_style(reader, text):
    row = reader.text[:reader.text.rindex(text)].count("\n")
    return reader.line_styles[row]


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", [False, True])
async def test_final_enter_opens_progress_and_tools_with_typed_colors(canonical):
    c = client()
    view = c.workspace.view("s")
    turn(view)
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        assert body_style(reader, "progress explanation") == ""
        assert body_style(reader, "command") == "bright_black"
        view.event(dict(type="delta", message_id="answer", channel="final",
                        text="final answer"))
        view.event(dict(type="turn_end", turn_id="t", result={}))
        if canonical:
            view.revision = "r"
            c.workspace.event(dict(
                type="turn_detail", session_id="s", revision="r", turn_id="t",
                events=[dict(type="delta", message_id="p", channel="commentary",
                             text="progress explanation"),
                        dict(type="tool_use", tool_use_id="tool", tool="command",
                             input={"command": "echo test"}),
                        dict(type="tool_result", tool_use_id="tool",
                             content="result")],
            ))
        app.paint()
        await pilot.pause()
        assert "progress explanation" in reader.text
        assert "echo test" not in reader.text
        reader.move_cursor(location(reader.text, reader.text.index("final answer")))
        app.stop_following()
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "progress explanation" not in reader.text
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "progress explanation" in reader.text
        assert "echo test" not in reader.text
        start = next(i for i, b in app.starts if b.role == "tool_group")
        reader.move_cursor(location(reader.text, start))
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "echo test" in reader.text
        assert body_style(reader, "progress explanation") == ""
        assert body_style(reader, "echo test") == "bright_black"
        assert body_style(reader, "final answer") == ""
        assert app.current_block().role == "tool_group"
        reader.move_cursor(location(reader.text, reader.text.index("echo test")))
        await pilot.press("enter")
        app.paint()
        await pilot.pause()
        assert "echo test" not in reader.text
        assert "progress explanation" in reader.text
        assert not c._outbox  # Local/cached detail needs no history request.


@pytest.mark.asyncio
async def test_final_enter_never_opens_another_turns_detail():
    c = client()
    view = c.workspace.view("s")
    turn(view)
    view.event(dict(type="turn_end", turn_id="t", result={}))
    view.event(dict(type="user_msg", msg_id="next", prompt="next question"))
    view.event(dict(type="delta", message_id="next-answer", channel="final",
                    text="next answer"))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        reader = app.query_one(Transcript)
        app.stop_following()
        reader.move_cursor(location(reader.text, reader.text.index("next answer")))
        await pilot.press("enter")
        assert view.local_details["t"].expanded  # Unrelated turn is untouched.
