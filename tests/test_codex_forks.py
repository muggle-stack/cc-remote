from __future__ import annotations

import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from cc_remote.wrapper import codex_forks as codex_forks_module
from cc_remote.wrapper.codex_forks import (
    CodexForkJournal,
    ForkJournalError,
    find_rollout_fork,
)


def test_fork_journal_persists_intent_and_result_atomically(tmp_path):
    journal = CodexForkJournal(tmp_path)

    intent = journal.begin("request-1", "parent", "turn-1", "/repo")
    assert intent["status"] == "intent"
    assert intent["thread_source"] == "cc-remote-fork:request-1"
    assert stat.S_IMODE((tmp_path / "codex-forks.json").stat().st_mode) == 0o600

    journal.complete("request-1", "child")
    reloaded = CodexForkJournal(tmp_path)
    result = reloaded.begin("request-1", "parent", "turn-1", "/repo")
    assert result["status"] == "complete"
    assert result["session_id"] == "child"

    with pytest.raises(ForkJournalError, match="another source turn"):
        reloaded.begin("request-1", "parent", "different-turn", "/repo")

    submitted = reloaded.begin("request-2", "parent", "turn-2", "/repo")
    assert submitted["status"] == "intent"
    reloaded.mark_submitted("request-2")
    assert CodexForkJournal(tmp_path).entries["request-2"]["status"] == "submitted"
    reloaded.reject("request-2", "invalid lastTurnId")
    rejected = CodexForkJournal(tmp_path).entries["request-2"]
    assert rejected["status"] == "rejected"
    assert rejected["error_message"] == "invalid lastTurnId"


def test_rollout_marker_recovery_scans_active_and_archived_with_bounds(tmp_path):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    target_dir = archived / "2026" / "07" / "12"
    target_dir.mkdir(parents=True)
    target = target_dir / "rollout-child.jsonl"
    target.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": "child",
            "cwd": "/repo",
            "thread_source": "cc-remote-fork:request-1",
            "forked_from_id": "parent",
        },
    }) + "\n")

    meta = find_rollout_fork(
        "cc-remote-fork:request-1",
        "parent",
        "/repo",
        roots=(str(active), str(archived)),
        max_files_per_root=10,
    )

    assert meta is not None
    assert meta["session_id"] == "child"
    assert find_rollout_fork(
        "cc-remote-fork:other",
        "parent",
        "/repo",
        roots=(str(active), str(archived)),
        max_files_per_root=10,
    ) is None


def test_fork_journal_serializes_two_concurrent_requests(tmp_path, monkeypatch):
    journal = CodexForkJournal(tmp_path)
    original_persist = journal._persist

    def slow_persist(entries):
        time.sleep(0.01)
        original_persist(entries)

    monkeypatch.setattr(journal, "_persist", slow_persist)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda number: journal.begin(
                f"request-{number}", "parent", f"turn-{number}", "/repo"),
            (1, 2),
        ))

    assert {entry["last_turn_id"] for entry in results} == {"turn-1", "turn-2"}
    reloaded = CodexForkJournal(tmp_path)
    assert set(reloaded.entries) == {"request-1", "request-2"}


def test_fork_journal_aliases_same_unresolved_identity_to_one_canonical(tmp_path):
    journal = CodexForkJournal(tmp_path)
    first = journal.begin("request-old", "parent", "turn-1", "/repo")
    assert journal.claim_submission("request-old") is True

    alias = journal.begin("request-new", "parent", "turn-1", "/repo")

    assert alias["status"] == "alias"
    assert alias["canonical_request_id"] == "request-old"
    assert alias["thread_source"] == first["thread_source"]
    assert journal.claim_submission("request-new") is False

    unresolved_reload = CodexForkJournal(tmp_path)
    assert unresolved_reload.entries["request-new"]["status"] == "alias"
    assert unresolved_reload.entries["request-new"][
        "canonical_request_id"] == "request-old"
    assert unresolved_reload.claim_submission("request-new") is False

    unresolved_reload.complete("request-new", "shared-child")
    reloaded = CodexForkJournal(tmp_path)
    assert {reloaded.entries[key]["session_id"] for key in (
        "request-old", "request-new")} == {"shared-child"}
    assert {reloaded.entries[key]["status"] for key in (
        "request-old", "request-new")} == {"complete"}

    # A completed fork is historical, not an unresolved ownership claim: the
    # user may deliberately fork the same turn again later.
    later = reloaded.begin("request-later", "parent", "turn-1", "/repo")
    assert later["status"] == "intent"
    assert later["thread_source"] == "cc-remote-fork:request-later"


def test_fork_journal_rejects_orphaned_alias_on_reload(tmp_path):
    journal = CodexForkJournal(tmp_path)
    journal.begin("request-old", "parent", "turn-1", "/repo")
    journal.claim_submission("request-old")
    journal.begin("request-new", "parent", "turn-1", "/repo")
    path = tmp_path / "codex-forks.json"
    raw = json.loads(path.read_text())
    raw.pop("request-old")
    path.write_text(json.dumps(raw))

    with pytest.raises(ForkJournalError, match="unreadable"):
        CodexForkJournal(tmp_path)


def test_fork_journal_compacts_complete_alias_group_atomically(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(codex_forks_module, "_MAX_ENTRIES", 3)
    journal = CodexForkJournal(tmp_path)
    journal.begin("request-old", "parent", "turn-old", "/repo")
    journal.claim_submission("request-old")
    journal.begin("request-alias", "parent", "turn-old", "/repo")
    journal.complete("request-alias", "old-child")

    journal.begin("request-keep", "parent", "turn-keep", "/repo")
    # Capacity is now canonical+alias+keep. Adding one more request must remove
    # the WHOLE terminal source group, never only its canonical root.
    journal.begin("request-new", "parent", "turn-new", "/repo")

    assert "request-old" not in journal.entries
    assert "request-alias" not in journal.entries
    assert set(journal.entries) == {"request-keep", "request-new"}
    reloaded = CodexForkJournal(tmp_path)
    assert set(reloaded.entries) == {"request-keep", "request-new"}
