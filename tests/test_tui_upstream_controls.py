"""Upstream controls retain the TUI's scoped, protocol-backed action path."""

import json

import pytest

from cc_remote import protocol as p
from cc_remote.tui_actions import build_action, defaults, is_read
from cc_remote.tui_app import WorkspaceClient
from cc_remote.tui_panels import PANEL_ACTIONS
from cc_remote.tui_presentation import SessionPresentation


@pytest.mark.parametrize("name,payload,readonly", [
    ("browse_files", {"path": ".", "offset": 100}, True),
    ("get_turn_file_changes", {
        "engine": "codex", "turn_id": "turn", "revision": "r",
    }, True),
    ("set_codex_context", {"max_context_tokens": 900000}, False),
])
def test_new_actions_use_native_schema_and_pinned_session(
    name, payload, readonly,
):
    action = build_action(name, json.dumps(payload), "s", "client")
    assert p.deserialize(p.serialize(action)) == action
    assert action.sid == "s" and action.client_id == "client"
    assert is_read(name) == readonly
    if name == "browse_files":
        assert action.request_id == action.cmd_id
    with pytest.raises(ValueError, match="Select a target session"):
        build_action(name, json.dumps(payload), None, "client")
    with pytest.raises(ValueError, match="routing fields"):
        build_action(name, json.dumps({**payload, "sid": "other"}),
                     "s", "client")


def test_context_settings_preserve_pending_and_applied_values():
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    client._handle(json.loads(p.serialize(p.CodexContext(
        sid="s", max_context_tokens=900000, applied_max_context_tokens=200000,
        pending=True, error="Reload pending",
    ))))
    presentation = client.workspace.view("s").presentation
    current = presentation.settings["codex_context"]
    assert current["pending"] and current["error"] == "Reload pending"
    assert current["applied_max_context_tokens"] == 200000
    values = defaults("set_codex_context", "s", "codex", {}, presentation)
    assert values == {"max_context_tokens": 900000}
    assert "900000" in presentation.panel("Usage / Context")
    assert "Reload pending" in presentation.panel("Settings")
    assert "set_codex_context" in PANEL_ACTIONS["Usage / Context"]
    assert "set_codex_context" in PANEL_ACTIONS["Settings"]
    assert not client._outbox


def test_live_diff_defaults_do_not_select_incomplete_archive_identity():
    values = defaults("get_diff", "s", "codex", {}, SessionPresentation())
    values["file"] = "file.py"
    live = build_action("get_diff", json.dumps(values), "s", "client")
    assert live.engine is live.turn_id is live.revision is None
    values.update(engine="codex", turn_id="turn", revision="r")
    archive = build_action("get_diff", json.dumps(values), "s", "client")
    assert archive.turn_id == "turn" and archive.revision == "r"


@pytest.mark.parametrize("frame", [
    p.FilesListed(sid="s", request_id="request", path="/project", entries=[]),
    p.TurnFileChanges(sid="s", turn_id="turn", changes=p.TurnChangeSummary(
        revision="r", files=[], total_files=0,
    )),
    p.TurnFileChangesPage(sid="s", engine="codex", turn_id="turn",
                          revision="r", offset=0, files=[], total_files=0),
])
def test_file_responses_are_visible_only_in_the_target_session(frame):
    client = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "s")
    event = json.loads(p.serialize(frame))
    client._handle({**event, "to": "another-client"})
    assert not client.workspace.view("s").presentation.reports
    client._handle({**event, "to": client.client_id})
    reports = client.workspace.view("s").presentation.reports
    assert reports[frame.type]["sid"] == "s"
    assert not client.workspace.view("other").presentation.reports
    assert not client.workspace.reports
    assert not client._outbox
    assert {"browse_files", "get_turn_file_changes"} <= PANEL_ACTIONS["Reports"]
