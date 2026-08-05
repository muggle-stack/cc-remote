from __future__ import annotations

import os
import sys

from cc_remote.wrapper.codex_turn_leases import CodexTurnLeaseStore


def test_codex_turn_lease_round_trip_and_matched_release(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.claim(
        "session",
        "turn-a",
        "message",
        daemon_epoch="a" * 32,
        automatic=True,
    )

    lease = store.get("session")
    assert lease is not None
    assert lease.turn_id == "turn-a"
    assert lease.msg_id == "message"
    assert lease.daemon_epoch == "a" * 32
    assert lease.automatic is True
    assert store.list() == (lease,)
    if sys.platform != "win32":
        assert os.stat(store.path).st_mode & 0o777 == 0o600

    assert store.release("session", turn_id="turn-b") is False
    assert store.get("session") == lease
    assert store.release("session", turn_id="turn-a") is True
    assert store.get("session") is None


def test_codex_turn_lease_rejects_corrupt_or_oversized_state(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.path.write_text('{"version":1,"leases":{"sid":{"turn_id":7}}}')
    assert store.get("sid") is None

    store.path.write_bytes(b"x" * (64 * 1024 + 1))
    assert store.get("sid") is None


def test_codex_turn_lease_reads_legacy_record_without_daemon_epoch(tmp_path):
    store = CodexTurnLeaseStore(tmp_path)
    store.path.write_text(
        '{"version":1,"leases":{"sid":{'
        '"turn_id":"turn","msg_id":"message","updated_at":1}}}'
    )

    lease = store.get("sid")
    assert lease is not None
    assert lease.daemon_epoch is None
    assert lease.automatic is False
