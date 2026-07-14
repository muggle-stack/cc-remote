from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.wrapper import claude_forks as forks_module
from cc_remote.wrapper.claude_forks import (
    ClaudeForkJournal,
    ClaudeForkJournalError,
    claude_fork_marker,
    find_claude_fork,
)


def _begin(journal: ClaudeForkJournal, request: str = "request-1"):
    return journal.begin(request, "parent", "message-1", "/repo")


def test_journal_persists_claim_uncertain_and_complete(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    intent = _begin(journal)

    assert intent["status"] == "intent"
    assert intent["marker"] == "cc-remote-fork:request-1"
    assert stat.S_IMODE((tmp_path / "claude-forks.json").stat().st_mode) == 0o600
    assert journal.claim_submission("request-1") is True
    assert journal.claim_submission("request-1") is False
    assert journal.mark_uncertain("request-1")["status"] == "uncertain"
    assert journal.complete("request-1", "child-1")["session_id"] == "child-1"

    reloaded = ClaudeForkJournal(tmp_path)
    result = _begin(reloaded)
    assert result["status"] == "complete"
    assert result["session_id"] == "child-1"
    assert reloaded.claim_submission("request-1") is False

    with pytest.raises(ClaudeForkJournalError, match="another source message"):
        reloaded.begin("request-1", "parent", "message-2", "/repo")
    with pytest.raises(ClaudeForkJournalError, match="not claimed"):
        fresh = ClaudeForkJournal(tmp_path / "fresh")
        _begin(fresh)
        fresh.complete("request-1", "child-1")


def test_same_unresolved_identity_aliases_to_canonical_marker(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    original = _begin(journal, "request-old")
    assert journal.claim_submission("request-old") is True

    alias = _begin(journal, "request-refresh")
    assert alias["status"] == "alias"
    assert alias["canonical_request_id"] == "request-old"
    assert alias["marker"] == original["marker"]
    assert alias["marker"] == claude_fork_marker("request-old")
    assert journal.claim_submission("request-refresh") is False

    reloaded = ClaudeForkJournal(tmp_path)
    assert reloaded.claim_submission("request-refresh") is False
    reloaded.mark_uncertain("request-refresh")
    reloaded.complete("request-refresh", "shared-child")
    resolved = ClaudeForkJournal(tmp_path).entries
    assert {resolved[key]["status"] for key in resolved} == {"complete"}
    assert {resolved[key]["session_id"] for key in resolved} == {"shared-child"}

    # Once terminal, the same source message may deliberately be forked again.
    later = _begin(ClaudeForkJournal(tmp_path), "request-later")
    assert later["status"] == "intent"
    assert later["marker"] == claude_fork_marker("request-later")


def test_get_canonical_follows_alias_and_returns_an_independent_copy(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    assert journal.get_canonical("missing-request") is None

    root = _begin(journal, "request-old")
    assert journal.claim_submission("request-old") is True
    alias = _begin(journal, "request-refresh")

    assert alias["status"] == "alias"
    canonical = journal.get_canonical("request-refresh")
    assert canonical == journal.get_canonical("request-old")
    assert canonical["status"] == "submitted"
    assert canonical["marker"] == root["marker"]

    # Callers cannot mutate the journal by changing the returned dictionary.
    canonical["status"] = "corrupt"
    assert journal.get_canonical("request-refresh")["status"] == "submitted"

    journal.complete("request-refresh", "shared-child")
    resolved = journal.get_canonical("request-refresh")
    assert resolved["status"] == "complete"
    assert resolved["session_id"] == "shared-child"


def test_reject_resolves_whole_alias_group_and_is_durable(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    _begin(journal, "request-old")
    journal.claim_submission("request-old")
    _begin(journal, "request-refresh")

    result = journal.reject("request-refresh", "cutoff not found")

    assert result["status"] == "rejected"
    reloaded = ClaudeForkJournal(tmp_path)
    assert {entry["status"] for entry in reloaded.entries.values()} == {"rejected"}
    assert {entry["error_message"] for entry in reloaded.entries.values()} == {
        "cutoff not found"
    }
    assert reloaded.claim_submission("request-old") is False
    with pytest.raises(ClaudeForkJournalError, match="cannot complete"):
        reloaded.complete("request-old", "child")


def test_concurrent_refreshes_have_one_submission_owner(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    request_ids = [f"request-{number}" for number in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(lambda rid: _begin(journal, rid), request_ids))
    assert len({entry["marker"] for entry in entries}) == 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(journal.claim_submission, request_ids))
    assert claims.count(True) == 1
    assert claims.count(False) == 7
    reloaded = ClaudeForkJournal(tmp_path)
    roots = [e for e in reloaded.entries.values() if "canonical_request_id" not in e]
    assert len(roots) == 1 and roots[0]["status"] == "submitted"


@pytest.mark.parametrize("payload", [
    b"not-json",
    json.dumps({"bad id!": {}}).encode(),
    json.dumps({
        "request-1": {
            "parent_session_id": "parent",
            "cutoff_message_id": "message-1",
            "cwd": "/repo",
            "marker": "cc-remote-fork:request-1",
            "status": "intent",
            "created_at": 1,
            "unknown": True,
        },
    }).encode(),
])
def test_journal_load_fails_closed_on_malformed_state(tmp_path, payload):
    (tmp_path / "claude-forks.json").write_bytes(payload)
    with pytest.raises(ClaudeForkJournalError, match="unreadable"):
        ClaudeForkJournal(tmp_path)


def test_journal_load_rejects_orphan_alias_and_oversized_file(tmp_path):
    journal = ClaudeForkJournal(tmp_path)
    _begin(journal, "request-old")
    journal.claim_submission("request-old")
    _begin(journal, "request-refresh")
    path = tmp_path / "claude-forks.json"
    raw = json.loads(path.read_text())
    raw.pop("request-old")
    path.write_text(json.dumps(raw))
    with pytest.raises(ClaudeForkJournalError, match="unreadable"):
        ClaudeForkJournal(tmp_path)

    other = tmp_path / "oversized"
    other.mkdir()
    (other / "claude-forks.json").write_bytes(
        b"x" * (forks_module._MAX_FILE_BYTES + 1))
    with pytest.raises(ClaudeForkJournalError, match="unreadable"):
        ClaudeForkJournal(other)


def test_capacity_compacts_terminal_alias_group_but_keeps_unresolved(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(forks_module, "_MAX_ENTRIES", 3)
    journal = ClaudeForkJournal(tmp_path)
    _begin(journal, "request-old")
    journal.claim_submission("request-old")
    _begin(journal, "request-alias")
    journal.complete("request-alias", "old-child")
    journal.begin("request-keep", "parent", "message-keep", "/repo")

    journal.begin("request-new", "parent", "message-new", "/repo")

    assert set(journal.entries) == {"request-keep", "request-new"}
    assert set(ClaudeForkJournal(tmp_path).entries) == {
        "request-keep", "request-new"
    }

    blocked_dir = tmp_path / "blocked"
    blocked = ClaudeForkJournal(blocked_dir)
    blocked.begin("request-1", "parent", "message-1", "/repo")
    blocked.begin("request-2", "parent", "message-2", "/repo")
    blocked.begin("request-3", "parent", "message-3", "/repo")
    with pytest.raises(ClaudeForkJournalError, match="capacity exhausted"):
        blocked.begin("request-4", "parent", "message-4", "/repo")


def _write_fork(
    path: Path,
    *,
    child: str,
    marker: str,
    parent: str = "parent",
    cutoff: str = "message-cutoff",
) -> None:
    records = [
        {
            "type": "user",
            "uuid": "new-first",
            "sessionId": child,
            "forkedFrom": {"sessionId": parent, "messageUuid": "source-first"},
        },
        {
            "type": "assistant",
            "uuid": "new-last",
            "sessionId": child,
            "forkedFrom": {"sessionId": parent, "messageUuid": cutoff},
        },
        {
            "type": "custom-title",
            "sessionId": child,
            "customTitle": marker,
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def _install_recovery_fakes(monkeypatch, infos, paths):
    monkeypatch.setattr(forks_module, "list_sessions", lambda **_kwargs: infos)
    monkeypatch.setattr(
        forks_module,
        "_find_session_file_with_dir",
        lambda sid, _cwd: ((paths[sid], paths[sid].parent) if sid in paths else None),
    )


def test_find_claude_fork_requires_exact_marker_and_raw_boundary(
    tmp_path, monkeypatch,
):
    marker = claude_fork_marker("request-1")
    child_path = tmp_path / "child.jsonl"
    _write_fork(child_path, child="child", marker=marker)
    infos = [
        SimpleNamespace(
            session_id="substring", custom_title=marker + "-extra", cwd="/repo"),
        SimpleNamespace(session_id="child", custom_title=marker, cwd="/repo"),
    ]
    _install_recovery_fakes(
        monkeypatch, infos, {"child": child_path})

    found = find_claude_fork(
        marker, "parent", "message-cutoff", "/repo")

    assert found == {
        "session_id": "child", "cwd": "/repo", "marker": marker,
    }
    assert find_claude_fork(marker, "parent", "wrong-cutoff", "/repo") is None
    assert find_claude_fork(marker, "wrong-parent", "message-cutoff", "/repo") is None


def test_find_claude_fork_fails_closed_on_ambiguous_marker(
    tmp_path, monkeypatch,
):
    marker = claude_fork_marker("request-1")
    infos = [
        SimpleNamespace(session_id="child-1", custom_title=marker, cwd="/repo"),
        SimpleNamespace(session_id="child-2", custom_title=marker, cwd="/repo"),
    ]
    _install_recovery_fakes(monkeypatch, infos, {})

    with pytest.raises(ClaudeForkJournalError, match="multiple sessions"):
        find_claude_fork(marker, "parent", "message-cutoff", "/repo")


def test_find_claude_fork_rejects_wrong_cwd_corrupt_tail_and_list_error(
    tmp_path, monkeypatch,
):
    marker = claude_fork_marker("request-1")
    child_path = tmp_path / "child.jsonl"
    _write_fork(child_path, child="child", marker=marker)
    info = SimpleNamespace(session_id="child", custom_title=marker, cwd="/other")
    _install_recovery_fakes(monkeypatch, [info], {"child": child_path})
    assert find_claude_fork(marker, "parent", "message-cutoff", "/repo") is None

    info.cwd = "/repo"
    child_path.write_text("{}\n")
    assert find_claude_fork(marker, "parent", "message-cutoff", "/repo") is None

    def unavailable(**_kwargs):
        raise OSError("session store unavailable")

    monkeypatch.setattr(forks_module, "list_sessions", unavailable)
    with pytest.raises(ClaudeForkJournalError, match="list is unavailable"):
        find_claude_fork(marker, "parent", "message-cutoff", "/repo")


@pytest.mark.parametrize("args", [
    ("bad-marker", "parent", "message", "/repo", 1000),
    ("cc-remote-fork:request-1", "bad id!", "message", "/repo", 1000),
    ("cc-remote-fork:request-1", "parent", "bad id!", "/repo", 1000),
    ("cc-remote-fork:request-1", "parent", "message", "relative", 1000),
    ("cc-remote-fork:request-1", "parent", "message", "/repo", 0),
    ("cc-remote-fork:request-1", "parent", "message", "/repo", 5000),
])
def test_find_claude_fork_bounds_inputs(monkeypatch, args):
    monkeypatch.setattr(forks_module, "list_sessions", lambda **_kwargs: [])
    with pytest.raises(ClaudeForkJournalError):
        find_claude_fork(*args[:-1], max_sessions=args[-1])
