from __future__ import annotations

import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from cc_remote.protocol import RollbackResult
from cc_remote.wrapper import rollback_commands as journal_module
from cc_remote.wrapper.rollback_commands import (
    RollbackCommandJournal,
    RollbackJournalError,
)


def _codex_result(
    *,
    session_id: str = "session-1",
    conversation: str = "succeeded",
    detail: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "engine": "codex",
        "restore": "conversation",
        "conversation": conversation,
        "files": "skipped",
        "restored_turns": 1 if conversation == "succeeded" else 0,
        "conflicts": [],
        "prefill_text": None,
        "detail": detail,
    }


def _begin_codex(
    journal: RollbackCommandJournal,
    cmd_id: str,
    *,
    client_id: str = "browser-1",
    session_id: str = "session-1",
) -> dict:
    return journal.begin(
        client_id,
        cmd_id,
        session_id,
        "codex",
        "conversation",
        1,
    )


def test_rollback_journal_survives_restart_without_reclaiming_submission(
    tmp_path,
):
    journal = RollbackCommandJournal(tmp_path)
    intent = _begin_codex(journal, "cmd-1")

    assert intent["status"] == "intent"
    assert intent["identity"] == {
        "session_id": "session-1",
        "engine": "codex",
        "restore": "conversation",
        "num_turns": 1,
        "checkpoint_id": None,
    }
    if sys.platform != "win32":
        assert (
            stat.S_IMODE(
                (tmp_path / "rollback-commands.json").stat().st_mode
            )
            == 0o600
        )
        assert (
            stat.S_IMODE(
                (tmp_path / "rollback-commands.lock").stat().st_mode
            )
            == 0o600
        )
    assert journal.mark_submitted("browser-1", "cmd-1") is True

    reloaded = RollbackCommandJournal(tmp_path)
    assert reloaded.mark_submitted("browser-1", "cmd-1") is False
    assert reloaded.get("browser-1", "cmd-1")["status"] == "submitted"
    assert reloaded.mark_uncertain("browser-1", "cmd-1")["status"] == "uncertain"

    # The journal accepts the protocol model but stores only the stable,
    # structured rollback result, not transport timestamps or routing fields.
    complete = reloaded.complete(
        "browser-1",
        "cmd-1",
        RollbackResult(
            session_id="session-1",
            engine="codex",
            restore="conversation",
            conversation="succeeded",
            files="skipped",
            restored_turns=1,
            to="browser-1",
        ),
    )
    assert complete["status"] == "complete"
    assert complete["result"] == _codex_result()

    final = RollbackCommandJournal(tmp_path)
    assert final.mark_submitted("browser-1", "cmd-1") is False
    assert _begin_codex(final, "cmd-1") == final.get("browser-1", "cmd-1")

    # Callers receive deep copies and cannot corrupt future durable state.
    returned = final.get("browser-1", "cmd-1")
    returned["result"]["conflicts"].append("not-persisted")
    assert final.get("browser-1", "cmd-1")["result"]["conflicts"] == []


def test_rollback_journal_rejects_identity_reuse_and_invalid_engine_target(
    tmp_path,
):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")

    with pytest.raises(RollbackJournalError, match="another intent"):
        journal.begin(
            "browser-1", "cmd-1", "session-2", "codex",
            "conversation", 1,
        )
    with pytest.raises(RollbackJournalError, match="cannot use a checkpoint"):
        journal.begin(
            "browser-1", "cmd-2", "session-1", "codex",
            "conversation", 1, "checkpoint-1",
        )
    with pytest.raises(RollbackJournalError, match="checkpoint id"):
        journal.begin(
            "browser-1", "cmd-3", "session-1", "claude",
            "conversation", 1,
        )

    claude = journal.begin(
        "browser-1", "cmd-4", "session-1", "claude",
        "both", 1, "checkpoint-1",
    )
    assert claude["identity"]["checkpoint_id"] == "checkpoint-1"


def test_rollback_journal_enforces_state_transitions_and_one_result(tmp_path):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")

    with pytest.raises(RollbackJournalError, match="submission was not claimed"):
        journal.complete("browser-1", "cmd-1", _codex_result())
    with pytest.raises(RollbackJournalError, match="submitted rollback"):
        journal.mark_uncertain("browser-1", "cmd-1")

    assert journal.mark_submitted("browser-1", "cmd-1") is True
    first = journal.complete("browser-1", "cmd-1", _codex_result())
    assert first["status"] == "complete"
    assert journal.complete(
        "browser-1", "cmd-1", _codex_result())["status"] == "complete"
    assert journal.mark_uncertain(
        "browser-1", "cmd-1")["status"] == "complete"

    with pytest.raises(RollbackJournalError, match="two different results"):
        journal.complete(
            "browser-1", "cmd-1", _codex_result(detail="different"))
    with pytest.raises(RollbackJournalError, match="session differs"):
        journal.complete(
            "browser-1", "cmd-1", _codex_result(session_id="session-2"))

    _begin_codex(journal, "cmd-2")
    journal.mark_submitted("browser-1", "cmd-2")
    impossible = _codex_result()
    impossible["restored_turns"] = 2
    with pytest.raises(RollbackJournalError, match="restored turn count"):
        journal.complete("browser-1", "cmd-2", impossible)


def test_rollback_journal_serializes_cross_instance_submission_claim(tmp_path):
    owner = RollbackCommandJournal(tmp_path)
    _begin_codex(owner, "cmd-1")
    retry = RollbackCommandJournal(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(
            lambda journal: journal.mark_submitted("browser-1", "cmd-1"),
            (owner, retry),
        ))

    assert sorted(claimed) == [False, True]
    assert RollbackCommandJournal(tmp_path).get(
        "browser-1", "cmd-1")["status"] == "submitted"


def test_rollback_journal_stays_readable_when_wall_clock_moves_backwards(
    tmp_path, monkeypatch,
):
    journal = RollbackCommandJournal(tmp_path)
    intent = _begin_codex(journal, "cmd-1")
    monkeypatch.setattr(journal_module.time, "time", lambda: 0.0)

    assert journal.mark_submitted("browser-1", "cmd-1") is True
    submitted = RollbackCommandJournal(tmp_path).get("browser-1", "cmd-1")
    assert submitted["updated_at"] == intent["updated_at"]


def test_rollback_journal_capacity_never_evicts_idempotency_evidence(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(journal_module, "_MAX_ENTRIES", 2)
    journal = RollbackCommandJournal(tmp_path / "completed")
    _begin_codex(journal, "old")
    journal.mark_submitted("browser-1", "old")
    journal.complete("browser-1", "old", _codex_result())
    _begin_codex(journal, "keep")
    before = (tmp_path / "completed" / "rollback-commands.json").read_bytes()
    with pytest.raises(RollbackJournalError, match="capacity exhausted"):
        _begin_codex(journal, "new")
    assert (tmp_path / "completed" / "rollback-commands.json").read_bytes() == before

    reloaded = RollbackCommandJournal(tmp_path / "completed")
    assert set(reloaded.entries) == {"browser-1/old", "browser-1/keep"}
    assert reloaded.mark_submitted("browser-1", "old") is False
    assert _begin_codex(reloaded, "old")["status"] == "complete"

    exhausted = RollbackCommandJournal(tmp_path / "exhausted")
    _begin_codex(exhausted, "one")
    _begin_codex(exhausted, "two")
    before = (tmp_path / "exhausted" / "rollback-commands.json").read_bytes()
    with pytest.raises(RollbackJournalError, match="capacity exhausted"):
        _begin_codex(exhausted, "three")
    assert (tmp_path / "exhausted" / "rollback-commands.json").read_bytes() == before


def test_rollback_journal_file_limit_failure_keeps_submitted_boundary(
    tmp_path, monkeypatch,
):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")
    journal.mark_submitted("browser-1", "cmd-1")
    path = tmp_path / "rollback-commands.json"
    before = path.read_bytes()
    monkeypatch.setattr(journal_module, "_MAX_FILE_BYTES", len(before) + 8)

    with pytest.raises(RollbackJournalError, match="capacity exhausted"):
        journal.complete("browser-1", "cmd-1", _codex_result())

    assert path.read_bytes() == before
    assert json.loads(before)["entries"]["browser-1/cmd-1"]["status"] == "submitted"


def test_rollback_journal_atomic_replace_failure_preserves_previous_state(
    tmp_path, monkeypatch,
):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")
    journal.mark_submitted("browser-1", "cmd-1")
    path = tmp_path / "rollback-commands.json"
    before = path.read_bytes()

    with monkeypatch.context() as patch:
        patch.setattr(
            journal_module.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("disk failure")),
        )
        with pytest.raises(RollbackJournalError, match="could not be persisted"):
            journal.complete("browser-1", "cmd-1", _codex_result())

    assert path.read_bytes() == before
    assert RollbackCommandJournal(tmp_path).get(
        "browser-1", "cmd-1")["status"] == "submitted"
    assert not list(tmp_path.glob("rollback-commands.*.tmp"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda path: path.write_text("{not-json"),
        lambda path: path.write_text(json.dumps({"version": 1, "entries": {
            "browser-1/cmd-1": {
                "client_id": "browser-1",
                "cmd_id": "cmd-1",
                "identity": {
                    "session_id": "session-1",
                    "engine": "codex",
                    "restore": "conversation",
                    "num_turns": 1,
                    "checkpoint_id": None,
                },
                "status": "submitted",
                "created_at": 1,
                "updated_at": 2,
                "result": _codex_result(),
            },
        }})),
        lambda path: path.write_text(path.read_text().replace(
            '"session_id":"session-1"',
            '"session_id":""',
            1,
        )),
    ],
    ids=("invalid-json", "result-before-complete", "invalid-identity"),
)
def test_rollback_journal_corruption_fails_closed(tmp_path, mutate):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")
    mutate(tmp_path / "rollback-commands.json")

    with pytest.raises(RollbackJournalError, match="unreadable"):
        RollbackCommandJournal(tmp_path)


def test_rollback_journal_rejects_symlink_state_file(tmp_path):
    target = tmp_path / "foreign.json"
    target.write_text("{}")
    try:
        os.symlink(target, tmp_path / "rollback-commands.json")
    except OSError as exc:
        if sys.platform == "win32":
            pytest.skip(f"Windows symlink privilege is unavailable: {exc}")
        raise

    with pytest.raises(RollbackJournalError, match="unreadable"):
        RollbackCommandJournal(tmp_path)


def test_rollback_journal_missing_after_initialization_fails_closed(tmp_path):
    journal = RollbackCommandJournal(tmp_path)
    _begin_codex(journal, "cmd-1")
    journal.mark_submitted("browser-1", "cmd-1")
    journal.complete("browser-1", "cmd-1", _codex_result())
    (tmp_path / "rollback-commands.json").unlink()

    with pytest.raises(RollbackJournalError, match="journal is missing"):
        RollbackCommandJournal(tmp_path)
