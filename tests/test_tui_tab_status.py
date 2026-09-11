"""Terminal tab badges are unread results, not inferred idle transitions."""

import pytest
from rich.console import Console

from cc_remote.tui_app import WorkspaceApp
from cc_remote.tui_buffers import tab_line
from cc_remote.tui_state import SessionView
from tests.test_tui_buffers import client


def finish(view, identity="turn", subtype="success", is_error=False):
    view.event(dict(type="user_msg", msg_id=identity, prompt="question"))
    view.event(dict(type="turn_end", turn_id=identity,
                    result=dict(subtype=subtype, is_error=is_error)))
    view.event(dict(type="state", state="idle"))


@pytest.mark.parametrize(("subtype", "error", "status", "color"), [
    ("success", False, "completed", "green"),
    ("interrupted", False, "interrupted", "red"),
    ("error_during_execution", True, "interrupted", "red"),
    ("error_max_turns", True, "failed", "red"),
])
def test_terminal_badge_color_read_and_replay(subtype, error, status, color):
    c, _ = client()
    c.buffers.open("s")
    view = c.workspace.view("s")
    finish(view, subtype=subtype, is_error=error)
    assert view.tab_badge() == status
    text = tab_line(c, 80)
    style = text.get_style_at_offset(Console(), text.plain.index("●"))
    assert style.color.name == color
    view.read_tab()
    assert "●" not in tab_line(c, 80).plain
    finish(view, subtype=subtype, is_error=error)
    assert view.tab_badge() is None  # Replayed terminal stays read.
    finish(view, "next", subtype, error)
    assert view.tab_badge() == status


def test_running_wins_and_idle_alone_does_not_invent_completion():
    view = SessionView()
    view.event(dict(type="state", state="running"))
    assert view.tab_badge() == "running"
    view.event(dict(type="state", state="idle"))
    assert view.tab_badge() is None
    finish(view)
    view.event(dict(type="state", state="running"))
    assert view.tab_badge() == "running"


def test_server_receipt_read_sync_and_late_receipt_do_not_revive_badge():
    view = SessionView()
    finish(view)
    view.read_tab()
    view.event(dict(type="completion_state", completion_id="turn",
                    unread=True, revision=1))
    assert view.tab_badge() is None
    finish(view, "next")
    view.event(dict(type="completion_state", completion_id="next",
                    unread=False, revision=3))
    assert view.tab_badge() is None
    view.event(dict(type="completion_state", completion_id="next",
                    unread=True, revision=2))
    assert view.tab_badge() is None
    cold = SessionView()
    cold.event(dict(type="completion_state", completion_id="cold",
                    unread=True, revision=1))
    assert cold.tab_badge() == "completed"


def test_late_binding_and_session_rekey_preserve_read_identity():
    c, _ = client()
    view = c.workspace.view("tmp-s")
    finish(view, "local")
    view.read_tab()
    view.event(dict(type="turn_binding", msg_id="local", turn_id="native"))
    view.event(dict(type="completion_state", completion_id="native",
                    unread=True, revision=1))
    c.workspace.event(dict(type="session_rekey", old_key="tmp-s",
                           session_id="real"))
    assert c.workspace.view("real").tab_badge() is None


def test_neutral_boundary_and_background_error_do_not_signal_completion():
    view = SessionView()
    finish(view, subtype="steered")
    assert view.tab_badge() is None
    view.event(dict(type="error", code="lookup_failed", message="No details"))
    assert view.tab_badge() is None


@pytest.mark.asyncio
async def test_only_visible_tab_is_read_without_needing_follow():
    c, _ = client()
    for sid in ("a", "b"):
        c.buffers.open(sid)
        finish(c.workspace.view(sid), sid, "interrupted")
    c.attached_sid = "a"
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert c.workspace.view("a").tab_badge() is None
        assert c.workspace.view("b").tab_badge() == "interrupted"
        app.app_focus = False
        c.attached_sid = "b"
        c.workspace.view("b").follow = False
        app.paint()
        await pilot.pause()
        assert c.workspace.view("b").tab_badge() == "interrupted"
        app.app_focus = True
        app.paint()
        assert c.workspace.view("b").tab_badge() is None
