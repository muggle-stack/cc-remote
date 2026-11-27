"""Gemini sidebar preferences shared by the local and service runtimes."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cc_remote.claude_service.wire import private_directory
from cc_remote.wrapper.antigravity_client import AcpError
from cc_remote.wrapper.session_pins import SessionPinStore, SessionPinStoreError
from cc_remote.wrapper.session_presentation import SessionPresentationStore, SessionPresentationStoreError


@dataclass(frozen=True)
class AntigravityMetadata:
    pinned: bool = False
    hidden: bool = False
    title: str | None = None


class AntigravityMetadataStore:
    def __init__(self, root: Path):
        self.root = root
        self.directory = root / "antigravity-metadata"

    @contextmanager
    def connection(self):
        db = None
        try:
            private_directory(self.directory)
            path = self.directory / "sessions.sqlite3"
            if path.is_symlink():
                raise ValueError("Gemini metadata database must be a local file")
            db = sqlite3.connect(path, timeout=5)
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                    sid TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0,
                    hidden INTEGER NOT NULL DEFAULT 0, title TEXT
                )""")
                db.execute("CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)")
                migrated = db.execute("SELECT 1 FROM migrations WHERE name='wrapper'").fetchone()
                if not migrated:
                    db.execute("BEGIN IMMEDIATE")
                    if not db.execute("SELECT 1 FROM migrations WHERE name='wrapper'").fetchone():
                        self.seed(db)
                yield db
        except (OSError, sqlite3.Error, SessionPinStoreError, SessionPresentationStoreError, ValueError):
            raise AcpError("sidebar", "无法读取或保存 Gemini 会话的侧栏设置。") from None
        finally:
            if db is not None:
                db.close()

    def seed(self, db):
        pins = SessionPinStore(self.root).ids("antigravity")
        presentation = SessionPresentationStore(self.root).entries_for_engine("antigravity")
        db.executemany(
            "INSERT OR IGNORE INTO sessions (sid, pinned, hidden, title) VALUES (?, ?, ?, ?)",
            [(sid, sid in pins, presentation[sid].hidden if sid in presentation else False,
              presentation[sid].custom_title if sid in presentation else None)
             for sid in pins | presentation.keys()],
        )
        db.execute("INSERT INTO migrations (name) VALUES ('wrapper')")

    def all(self) -> dict[str, AntigravityMetadata]:
        with self.connection() as db:
            return {sid: AntigravityMetadata(bool(pinned), bool(hidden), title)
                    for sid, pinned, hidden, title in db.execute("SELECT sid, pinned, hidden, title FROM sessions")}

    def get(self, sid: str) -> AntigravityMetadata:
        with self.connection() as db:
            row = db.execute("SELECT pinned, hidden, title FROM sessions WHERE sid=?", (sid,)).fetchone()
        return AntigravityMetadata(bool(row[0]), bool(row[1]), row[2]) if row else AntigravityMetadata()

    def set_pinned(self, sid: str, pinned: bool) -> None:
        self._set(sid, "pinned", pinned)

    def set_hidden(self, sid: str, hidden: bool) -> None:
        self._set(sid, "hidden", hidden)

    def set_title(self, sid: str, title: str) -> None:
        title = title.strip()
        if not (1 <= len(title) <= 200):
            raise AcpError("sidebar", "Gemini 会话名称无效。")
        self._set(sid, "title", title)

    def _set(self, sid, field, value):
        with self.connection() as db:
            db.execute(f"INSERT INTO sessions (sid, {field}) VALUES (?, ?) "
                       f"ON CONFLICT(sid) DO UPDATE SET {field}=excluded.{field}", (sid, value))
