"""Claude Remote control persistence and native handoff parsing."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

from cc_remote.wrapper import claude_controls as controls_module
from cc_remote.wrapper.claude_controls import (
    ClaudeControlStore,
    ClaudeControlStoreError,
    last_completed_assistant_controls,
)


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _message(kind: str, uuid: str, message=None):
    return SimpleNamespace(type=kind, uuid=uuid, message=message)


def test_remote_control_store_is_private_bounded_and_roundtrips(tmp_path):
    store = ClaudeControlStore(tmp_path)
    saved = store.update(
        SESSION_ID,
        model="claude-opus-4-6[1m]",
        effort="max",
        permission_mode="bypassPermissions",
    )

    assert saved.model == "claude-opus-4-6[1m]"
    assert saved.effort == "max"
    assert saved.permission_mode == "bypassPermissions"
    if sys.platform != "win32":
        assert (
            tmp_path / "claude-session-controls.json"
        ).stat().st_mode & 0o777 == 0o600
    assert ClaudeControlStore(tmp_path).get(SESSION_ID) == saved


def test_remote_control_store_drops_untrusted_values(tmp_path):
    store = ClaudeControlStore(tmp_path)
    saved = store.update(
        SESSION_ID,
        model="glm-5.2",
        effort="ultra",
        permission_mode="owner",
    )

    assert saved.as_dict() == {}
    assert store.get(SESSION_ID).as_dict() == {}


def test_remote_control_store_rejects_public_or_symlink_state(tmp_path):
    path = tmp_path / "claude-session-controls.json"
    path.write_text(json.dumps({"version": 1, "sessions": {}}))
    os.chmod(path, 0o644)
    if sys.platform != "win32":
        with pytest.raises(ClaudeControlStoreError, match="private bounded"):
            ClaudeControlStore(tmp_path)

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"version": 1, "sessions": {}}))
    try:
        path.symlink_to(target)
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise
    with pytest.raises(ClaudeControlStoreError, match="private bounded"):
        ClaudeControlStore(tmp_path)


def test_handoff_reads_model_and_top_level_effort_from_completed_turn(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "message": {"content": "hi"}},
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-opus-4-6", "stop_reason": "tool_use",
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "claude-opus-4-6", "stop_reason": "end_turn",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", rows[0]["message"]),
        _message("assistant", "a1", rows[1]["message"]),
        _message("assistant", "a2", rows[2]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model == "claude-opus-4-6"
    assert controls.effort == "max"


def test_handoff_ignores_incomplete_latest_turn_and_synthetic_response(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
        }},
        {"type": "assistant", "uuid": "synthetic", "message": {
            "model": "<synthetic>", "stop_reason": "stop_sequence",
            "content": [{"type": "text", "text": "No response requested."}],
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "claude-opus-4-6", "stop_reason": "tool_use",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", {"content": "first"}),
        _message("assistant", "a1", rows[0]["message"]),
        _message("user", "u2", {"content": "menu-only"}),
        _message("assistant", "synthetic", rows[1]["message"]),
        _message("assistant", "a2", rows[2]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model == "claude-sonnet-5"
    assert controls.effort == "high"


def test_handoff_never_falls_back_to_older_model_for_proxy_upstream(
    tmp_path, monkeypatch,
):
    path = tmp_path / f"{SESSION_ID}.jsonl"
    rows = [
        {"type": "assistant", "uuid": "a1", "effort": "high", "message": {
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
        }},
        {"type": "assistant", "uuid": "a2", "effort": "max", "message": {
            "model": "glm-5.2", "stop_reason": "end_turn",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    messages = [
        _message("user", "u1", {"content": "first"}),
        _message("assistant", "a1", rows[0]["message"]),
        _message("user", "u2", {"content": "second"}),
        _message("assistant", "a2", rows[1]["message"]),
    ]
    monkeypatch.setattr(controls_module, "transcript_path", lambda _sid: str(path))
    monkeypatch.setattr(
        controls_module, "get_session_messages",
        lambda _sid, directory: messages,
    )

    controls = last_completed_assistant_controls(
        SESSION_ID, directory=str(tmp_path), max_bytes=1024 * 1024)

    assert controls.model is None
    assert controls.effort == "max"
