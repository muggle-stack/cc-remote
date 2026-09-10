from __future__ import annotations

import json
import os
import threading

import pytest

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER, CodexTerminalFence
from cc_remote.wrapper import codex_lifecycle as lifecycle_module
from cc_remote.wrapper.codex_lifecycle import (
    CodexTerminalLedger,
    CodexTerminalLedgerError,
)


def _fence(
    turn_id: str,
    status: str = "completed",
    *,
    duration_ms: int | None = None,
) -> CodexTerminalFence:
    return CodexTerminalFence(
        turn_id=turn_id,
        status=status,
        duration_ms=duration_ms,
        completed_at=123.5,
    )


def test_persistent_terminal_survives_restart_and_append(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"task_complete"}\n')
    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("default@session-1", _fence("turn-1", duration_ms=42), rollout)

    rollout.write_bytes(rollout.read_bytes() + b'{"type":"world_state"}\n')
    restarted = CodexTerminalLedger(tmp_path)

    assert restarted.snapshot(
        "default@session-1", rollout, revision="a" * 32 + "-0",
    ) == (_fence("turn-1", duration_ms=42),)


def test_legacy_success_fences_are_rebuilt_after_error_semantics_change(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"task_complete","error":{}}\n')
    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("session-1", _fence("turn-1"), rollout)
    raw = json.loads(ledger.path.read_text())
    raw["version"] = 1
    ledger.path.write_text(json.dumps(raw))
    restarted = CodexTerminalLedger(tmp_path)
    assert restarted.snapshot("session-1", rollout, revision="a" * 32) == ()
    restarted.persist("session-1", _fence("turn-1", "failed"), rollout)
    assert restarted.snapshot(
        "session-1", rollout, revision="a" * 32,
    ) == (_fence("turn-1", "failed"),)


def test_terminal_witness_rejects_truncate_rotate_and_rollback(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    original = b"prefix\nterminal-boundary\n"
    rollout.write_bytes(original)
    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("session-1", _fence("turn-1"), rollout)

    rollout.write_bytes(b"short\n")
    assert ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    ) == ()

    rollout.write_bytes(original)
    ledger.persist("session-1", _fence("turn-2"), rollout)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(original + b"replacement\n")
    os.replace(replacement, rollout)
    assert ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    ) == ()

    ledger.persist("session-1", _fence("turn-3"), rollout)
    changed = bytearray(rollout.read_bytes())
    changed[-2] = ord("X")
    rollout.write_bytes(changed)
    assert ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    ) == ()


def test_terminal_persistence_cannot_rebind_after_source_rotation(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"old source\n")
    observed = rollout.stat()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"new source\n")
    os.replace(replacement, rollout)

    ledger = CodexTerminalLedger(tmp_path)
    with pytest.raises(
        CodexTerminalLedgerError,
        match="source changed before terminal persistence",
    ):
        ledger.persist(
            "session-1",
            _fence("turn-1"),
            rollout,
            expected_source_identity=(
                str(rollout), observed.st_dev, observed.st_ino,
                observed.st_size),
        )

    assert ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    ) == ()


def test_out_of_order_persist_tasks_never_regress_source_witness(
    tmp_path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"older boundary\n")
    older_witness = lifecycle_module._capture_witness(rollout)
    with rollout.open("ab") as stream:
        stream.write(b"newer boundary\n")

    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("session-1", _fence("newer-turn"), rollout)
    newer_size = rollout.stat().st_size
    monkeypatch.setattr(
        lifecycle_module, "_capture_witness", lambda _path: older_witness)
    ledger.persist("session-1", _fence("older-turn"), rollout)

    stored = json.loads(
        (tmp_path / "codex-terminal-ledger.json").read_text())
    assert stored["sessions"]["session-1"]["source"]["size"] == newer_size
    assert [fence.turn_id for fence in ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    )] == ["older-turn", "newer-turn"]


def test_late_persist_from_rotated_inode_cannot_overwrite_new_source(
    tmp_path, monkeypatch,
):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"old inode\n")
    old_witness = lifecycle_module._capture_witness(rollout)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"new inode\n")
    os.replace(replacement, rollout)

    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("session-1", _fence("new-source-turn"), rollout)
    monkeypatch.setattr(
        lifecycle_module, "_capture_witness", lambda _path: old_witness)
    with pytest.raises(
        CodexTerminalLedgerError,
        match="source changed before terminal persistence",
    ):
        ledger.persist("session-1", _fence("old-source-turn"), rollout)

    assert [fence.turn_id for fence in ledger.snapshot(
        "session-1", rollout, revision="a" * 32 + "-0",
    )] == ["new-source-turn"]


def test_volatile_terminal_is_immediate_revision_and_profile_scoped(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"source\n")
    st = rollout.stat()
    ledger = CodexTerminalLedger(tmp_path)
    revision = "b" * 32 + "-0"
    ledger.remember(
        "iris@same-native-id",
        _fence("turn-1", "interrupted"),
        revision=revision,
        source_identity=(
            str(rollout), st.st_dev, st.st_ino, st.st_size),
    )

    assert ledger.snapshot(
        "iris@same-native-id", rollout, revision=revision,
    ) == (_fence("turn-1", "interrupted"),)
    assert ledger.snapshot(
        "default@same-native-id", rollout, revision=revision,
    ) == ()
    assert ledger.snapshot(
        "iris@same-native-id", rollout, revision="b" * 32 + "-1",
    ) == ()


def test_volatile_terminal_never_rebinds_across_source_knowledge(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"source\n")
    st = rollout.stat()
    ledger = CodexTerminalLedger(tmp_path)
    revision = "f" * 32 + "-0"
    ledger.remember(
        "session-1", _fence("unbound-turn"), revision=revision)
    ledger.remember(
        "session-1",
        _fence("bound-turn"),
        revision=revision,
        source_identity=(
            str(rollout), st.st_dev, st.st_ino, st.st_size),
    )

    assert ledger.snapshot(
        "session-1", rollout, revision=revision,
    ) == (_fence("bound-turn"),)

    with rollout.open("ab") as stream:
        stream.write(b"append\n")
    appended = rollout.stat()
    ledger.remember(
        "session-1",
        _fence("later-bound-turn"),
        revision=revision,
        source_identity=(
            str(rollout), appended.st_dev, appended.st_ino,
            appended.st_size),
    )
    assert ledger.snapshot(
        "session-1", rollout, revision=revision,
    ) == (_fence("bound-turn"), _fence("later-bound-turn"))

    # Codex rollback/truncation commonly retains the inode. The observed size
    # boundary keeps the process-local fast path as fail-closed as the durable
    # SHA-256 witness.
    rollout.write_bytes(b"")
    assert ledger.snapshot(
        "session-1", rollout, revision=revision,
    ) == ()


def test_source_bound_terminal_rebases_across_read_side_revision(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"source\n")
    st = rollout.stat()
    ledger = CodexTerminalLedger(tmp_path)
    first_revision = "1" * 32 + "-0"
    second_revision = "1" * 32 + "-1"
    ledger.remember(
        "session-1",
        _fence("turn-1"),
        revision=first_revision,
        source_identity=(
            str(rollout), st.st_dev, st.st_ino, st.st_size),
    )

    assert ledger.rebase_revision(
        "session-1",
        previous_revision=first_revision,
        revision=second_revision,
    ) is True
    assert ledger.snapshot(
        "session-1", rollout, revision=second_revision,
    ) == (_fence("turn-1"),)

    # An unbound live fact cannot be carried into another History epoch: there
    # is no exact rollout identity with which to validate that transition.
    ledger.remember(
        "unbound-session", _fence("unbound-turn"),
        revision=first_revision,
    )
    assert ledger.rebase_revision(
        "unbound-session",
        previous_revision=first_revision,
        revision=second_revision,
    ) is False
    assert ledger.snapshot(
        "unbound-session", rollout, revision=second_revision,
    ) == ()


def test_volatile_remember_does_not_wait_for_durable_store_lock(tmp_path):
    ledger = CodexTerminalLedger(tmp_path)
    completed = threading.Event()

    def remember() -> None:
        ledger.remember(
            "session-1", _fence("turn-1"), revision="2" * 32 + "-0")
        completed.set()

    # Durable persistence holds this lock across JSON replacement and fsync.
    # Process-local publication must use an independent lock so the wrapper's
    # event loop can still emit the authoritative TurnEnd immediately.
    with ledger._lock:
        worker = threading.Thread(target=remember)
        worker.start()
        assert completed.wait(timeout=1.0)
    worker.join(timeout=1.0)
    assert not worker.is_alive()


def test_corrupt_store_degrades_to_unknown_and_never_fabricates_success(tmp_path):
    path = tmp_path / "codex-terminal-ledger.json"
    path.write_text('{"version":1,"sessions":{"session-1":')

    ledger = CodexTerminalLedger(tmp_path)

    assert ledger.snapshot(
        "session-1", tmp_path / "missing-rollout",
        revision="c" * 32 + "-0",
    ) == ()


@pytest.mark.parametrize("field", ["duration_ms", "completed_at"])
def test_non_finite_or_unsafe_terminal_metadata_is_rejected(field):
    for value in (float("inf"), float("nan"), MAX_SAFE_WIRE_INTEGER + 1):
        with pytest.raises(ValueError):
            CodexTerminalFence(
                turn_id="turn-1",
                status="completed",
                **{field: value},
            )


def test_terminal_ledger_keeps_only_newest_sixteen_exact_turns(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"source\n")
    ledger = CodexTerminalLedger(tmp_path)
    revision = "d" * 32 + "-0"
    for index in range(20):
        ledger.remember(
            "session-1", _fence(f"turn-{index}"), revision=revision)

    assert [fence.turn_id for fence in ledger.snapshot(
        "session-1", rollout, revision=revision,
    )] == [f"turn-{index}" for index in range(4, 20)]


def test_profile_migration_keeps_terminal_namespace_isolated(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"source\n")
    ledger = CodexTerminalLedger(tmp_path)
    ledger.persist("legacy-session", _fence("turn-1"), rollout)

    assert ledger.migrate_profile_sessions(
        lambda session_id: f"default@{session_id}",
        profile_revision=1,
    ) == 1
    stored = json.loads(
        (tmp_path / "codex-terminal-ledger.json").read_text())
    assert set(stored["sessions"]) == {"default@legacy-session"}
    assert ledger.snapshot(
        "legacy-session", rollout, revision="e" * 32 + "-0",
    ) == ()
    assert ledger.snapshot(
        "default@legacy-session", rollout,
        revision="e" * 32 + "-0",
    ) == (_fence("turn-1"),)
