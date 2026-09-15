"""Projection contracts stay testable without mounting the terminal UI."""

import pytest

from cc_remote.tui_actions import build_action
from cc_remote.tui_presentation import merge_rate_window
from cc_remote.tui_state import SessionView, WorkspaceState


def test_projection_folds_completed_tools_without_losing_payload():
    view = SessionView(active_turn="turn")
    view.event(dict(type="tool_use", tool_use_id="tool", tool="command",
                    input={"command": "echo hello"}))
    view.event(dict(type="turn_end", turn_id="turn", result={}))
    text, starts = view.render()
    assert "Turn details" in text and "echo hello" not in text
    next(b for _, b in starts if b.role == "tool_group").expanded = True
    assert "echo hello" in view.render()[0]


def test_rekey_preserves_local_draft_and_read_position():
    state = WorkspaceState()
    view = state.view("temporary")
    view.draft = "unfinished draft"
    view.anchor = ("message", 12)
    state.event(dict(type="session_rekey", old_key="temporary", session_id="real"))
    assert state.view("real") is view
    assert view.draft == "unfinished draft" and view.anchor == ("message", 12)


def test_same_period_usage_does_not_regress_on_sparse_reads():
    current = dict(used_percent=20, resets_at=1000, window_duration_mins=300)
    assert merge_rate_window(current, dict(used_percent=0))["used_percent"] == 20
    assert merge_rate_window(current, dict(resets_at=1020))["used_percent"] == 20
    assert "used_percent" not in merge_rate_window(current, dict(resets_at=2000))


@pytest.mark.parametrize("payload", [
    '{"session_id":"other","title":"name"}',
    '{"session_id":"s","title":"name","sid":"other"}',
])
def test_action_builder_rejects_target_override(payload):
    with pytest.raises(ValueError):
        build_action("rename_session", payload, "s", "client")


def test_action_builder_uses_shared_protocol_validation():
    message = build_action(
        "rename_session", '{"session_id":"s","title":"name"}', "s", "client"
    )
    assert message.type == "rename_session" and message.client_id == "client"
    assert message.sid == message.session_id == "s"
