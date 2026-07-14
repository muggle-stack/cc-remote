from __future__ import annotations

import json

from cc_remote.wrapper import codex_sessions


def test_archived_rollout_still_supports_engine_and_cwd_lookup(tmp_path, monkeypatch):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    session_id = "019f555d-archive-test"
    rollout = archived / f"rollout-2026-07-12-{session_id}.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": "/repo/archived"},
    }) + "\n")
    monkeypatch.setattr(codex_sessions, "_ROOT", str(active))
    monkeypatch.setattr(codex_sessions, "_ARCHIVE_ROOT", str(archived))

    assert codex_sessions.codex_rollout_path(session_id) == str(rollout)
    assert codex_sessions.codex_session_cwd(session_id) == "/repo/archived"


def test_active_rollout_wins_if_both_stores_contain_same_id(tmp_path, monkeypatch):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    session_id = "019f555d-duplicate-test"
    active_rollout = active / f"rollout-active-{session_id}.jsonl"
    archived_rollout = archived / f"rollout-archived-{session_id}.jsonl"
    for path, cwd in ((active_rollout, "/repo/active"),
                      (archived_rollout, "/repo/archived")):
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        }) + "\n")
    monkeypatch.setattr(codex_sessions, "_ROOT", str(active))
    monkeypatch.setattr(codex_sessions, "_ARCHIVE_ROOT", str(archived))

    assert codex_sessions.codex_rollout_path(session_id) == str(active_rollout)
    assert codex_sessions.codex_session_cwd(session_id) == "/repo/active"
