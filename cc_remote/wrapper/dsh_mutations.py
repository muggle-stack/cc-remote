"""Durable intent receipts for DSH writes without native idempotency keys.

An intent is committed before native I/O. A lost result is never permission to
repeat a write. Receipts are not evicted: browser recovery has no expiry bound.
Only hashes, native session identities and bounded replies are stored, never
prompts, attachments, commands or pairing credentials.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from cc_remote.wrapper.dsh_client import DshError


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


class DshMutationJournal:
    def __init__(self, state_dir: Path, *, engine="dsh", label="DSH", error_type=DshError):
        self.path = Path(state_dir) / f"{engine}-mutations.sqlite3"
        self.label = label
        self.error_type = error_type

    @contextmanager
    def connection(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS receipts ("
                           "identity TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                           "sid TEXT, result TEXT)")
                yield db
            # Include the initial database's directory entry in durability.
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise self.error_type("mutation_journal", f"无法确认 {self.label} 操作记录，已停止自动执行。请检查设备状态。") from exc

    def begin(self, identity: str, fingerprint: str) -> tuple[bool, dict]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = db.execute(
                "INSERT OR IGNORE INTO receipts (identity, fingerprint) VALUES (?, ?)",
                (identity, fingerprint)).rowcount == 1
            row = dict(db.execute("SELECT * FROM receipts WHERE identity = ?", (identity,)).fetchone())
            if row["fingerprint"] != fingerprint:
                raise self.error_type("mutation_identity", f"这个请求编号已用于另一项 {self.label} 操作，请刷新后重新操作。")
            db.commit()
            return inserted, row

    def discard_unsent(self, identity: str):
        """Only the claiming caller may release an intent proven never written."""
        with self.connection() as db:
            db.execute("DELETE FROM receipts WHERE identity = ? AND sid IS NULL AND result IS NULL",
                       (identity,))
            db.commit()

    def created(self, identity: str, sid: str):
        with self.connection() as db:
            if db.execute("UPDATE receipts SET sid = ? WHERE identity = ? AND result IS NULL",
                          (sid, identity)).rowcount != 1:
                raise ValueError("missing mutation intent")
            db.commit()

    def finish(self, identity: str, result: dict):
        raw = json.dumps(result, ensure_ascii=True)
        if len(raw) > 256 * 1024:
            raise self.error_type("mutation_journal", f"{self.label} 操作结果过大，请查看原生状态；不会自动重试。")
        with self.connection() as db:
            if db.execute("UPDATE receipts SET result = ? WHERE identity = ?", (raw, identity)).rowcount != 1:
                raise ValueError("missing mutation intent")
            db.commit()
