"""Zero-model regressions for provider-independent Claude session discovery."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from claude_agent_sdk import (
    get_session_info,
    get_session_messages,
    list_sessions,
)

from cc_remote.claude_paths import claude_config_dir, claude_projects_dir
from cc_remote.wrapper.stream import transcript_path


SESSION_ID = "11111111-1111-4111-8111-111111111111"
CWD = "/tmp/cc-remote-provider-root"


def _write_transcript(root: Path, prompt: str) -> Path:
    path = root / "projects" / "-tmp-cc-remote-provider-root" / (
        f"{SESSION_ID}.jsonl"
    )
    path.parent.mkdir(parents=True)
    rows = [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "sessionId": SESSION_ID,
            "cwd": CWD,
            "timestamp": "2026-07-25T00:00:00.000Z",
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "parentUuid": "user-1",
            "sessionId": SESSION_ID,
            "cwd": CWD,
            "timestamp": "2026-07-25T00:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "world"}],
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
            },
        },
    ]
    path.write_text(
        "".join(
            json.dumps(row, separators=(",", ":")) + "\n"
            for row in rows
        )
    )
    return path


def _replace_provider_settings(root: Path, provider: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".settings-{provider}.json"
    temporary.write_text(json.dumps({
        "model": "claude-opus-5[1m]",
        "env": {
            "ANTHROPIC_BASE_URL": f"https://{provider}.invalid",
            "ANTHROPIC_AUTH_TOKEN": f"test-{provider}",
        },
    }))
    os.replace(temporary, root / "settings.json")


def test_settings_only_provider_switch_keeps_one_claude_catalog(
    monkeypatch,
    tmp_path,
):
    home = tmp_path / "decoy-home"
    active = tmp_path / "shared-claude-home"
    decoy = _write_transcript(home / ".claude", "wrong root")
    source = _write_transcript(active, "shared session")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(active))

    assert claude_config_dir() == active.resolve()
    assert claude_projects_dir() == (active / "projects").resolve()

    for provider in ("provider-a", "provider-b"):
        _replace_provider_settings(active, provider)
        monkeypatch.setenv(
            "ANTHROPIC_BASE_URL", f"https://runtime-{provider}.invalid")

        sessions = list_sessions(limit=20)
        assert [item.session_id for item in sessions] == [SESSION_ID]
        assert sessions[0].first_prompt == "shared session"
        assert get_session_info(SESSION_ID).cwd == CWD
        assert [message.type for message in get_session_messages(
            SESSION_ID,
        )] == ["user", "assistant"]
        assert transcript_path(SESSION_ID) == str(source.resolve())

    assert transcript_path(SESSION_ID) != str(decoy.resolve())


def test_default_claude_root_remains_home_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    if sys.platform == "win32":
        # Path.home()/os.path.expanduser resolve "~" from USERPROFILE (or
        # HOMEDRIVE+HOMEPATH) on Windows, not from HOME.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    assert claude_config_dir() == (tmp_path / ".claude").resolve()
    assert claude_projects_dir() == (tmp_path / ".claude/projects").resolve()


def test_configured_claude_root_matches_sdk_path_semantics(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-profile")

    assert claude_config_dir() == Path("~/.claude-profile")
    assert claude_projects_dir() == Path("~/.claude-profile/projects")


def test_relative_claude_root_keeps_transcript_watcher_aligned(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/.claude-profile")
    source = _write_transcript(
        Path("~/.claude-profile"),
        "relative root session",
    )

    sessions = list_sessions(limit=20)
    assert [item.session_id for item in sessions] == [SESSION_ID]
    assert transcript_path(SESSION_ID) == str(source.resolve())
