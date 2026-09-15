"""Generic preview authorization preserves its original challenge identity."""

import json

import pytest

from cc_remote.tui_actions import build_action, defaults
from cc_remote.tui_presentation import SessionPresentation


def test_generic_authorization_uses_reported_request_identity():
    presentation = SessionPresentation()
    presentation.reports["preview_authorization_required"] = {
        "authorization_id": "challenge", "request_id": "original-read",
    }
    payload = defaults("authorize_preview", "s", "codex", {}, presentation)
    payload["decision"] = "allow"
    command = build_action("authorize_preview", json.dumps(payload), "s", "c")
    assert command.request_id == "original-read"
    assert command.authorization_id == "challenge"
    assert command.cmd_id != command.request_id


def test_other_actions_cannot_override_request_identity():
    with pytest.raises(ValueError, match="cannot be edited"):
        build_action("get_file_preview", json.dumps({
            "path": "a.md", "request_id": "injected",
        }), "s", "c")
