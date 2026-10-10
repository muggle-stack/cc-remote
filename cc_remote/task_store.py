"""Private, bounded execution and completion receipts for one Codex account."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import UUID, uuid4

OUTPUT_BYTES = 64 * 1024
MAX_ACTIVE = 8
MAX_RECORDS = 200
TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}


def thread_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("A native Codex thread identity is required")
    return value


def request_thread(meta):
    """Use native per-call metadata, never a model-supplied thread argument."""
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump()
    if not isinstance(meta, dict):
        raise ValueError("Missing native MCP thread identity; no task was started")
    identities = []
    try:
        # Official app-server MCP calls carry threadId directly in _meta.
        # This is transport context, distinct from the tool's arguments.
        if "threadId" in meta:
            identities.append(thread_id(meta["threadId"]))
        if "x-codex-turn-metadata" in meta:
            raw = meta["x-codex-turn-metadata"]
            # Model-driven calls send a structured object; older transports
            # serialize that same object as a JSON string.
            if isinstance(raw, dict):
                data = raw
            elif isinstance(raw, str) and len(raw) <= 8192:
                data = json.loads(raw)
            else:
                raise ValueError("Invalid turn metadata")
            identities.append(thread_id(data["thread_id"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("Invalid native Codex task caller") from exc
    if not identities:
        raise ValueError("Missing native MCP thread identity; no task was started")
    if len(set(identities)) != 1:
        raise ValueError("Conflicting native Codex task callers")
    return identities[0]


class TaskStore:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve(strict=True)
        if not self.home.is_dir() or self.home.stat().st_uid != os.getuid():
            raise ValueError("Codex home must be an existing directory owned by this user")
        self.root = self.home / "cc-remote-async-tasks"
        self.path = self.root / "tasks.sqlite3"

    @contextmanager
    def db(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Task state directory must be private and owned by this user")
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Invalid task database")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, sid TEXT NOT NULL, request_id TEXT NOT NULL,
                spec TEXT NOT NULL, state TEXT NOT NULL, output BLOB NOT NULL DEFAULT X'',
                truncated INTEGER NOT NULL DEFAULT 0, exit_code INTEGER,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                delivery TEXT NOT NULL DEFAULT 'pending', error TEXT, delivery_error TEXT,
                next_attempt REAL NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(sid, request_id))""")
            yield db
            db.commit()
        finally:
            db.close()

    def create(self, sid, *, argv, cwd, title, request_id, timeout_seconds=3600):
        thread_id(sid)
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 128
                or any(not isinstance(a, str) or '\0' in a for a in argv)
                or not argv[0] or sum(len(a) for a in argv) > 32768):
            raise ValueError("argv must be a bounded, nonempty command vector")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        if not isinstance(title, str) or not 1 <= len(title) <= 120:
            raise ValueError("title must contain 1–120 characters")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            raise ValueError("request_id must be a stable unique request key")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 86400:
            raise ValueError("timeout_seconds must be between 1 and 86400")
        spec = json.dumps(dict(argv=argv, cwd=cwd, title=title,
                               timeout_seconds=timeout_seconds), sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE sid=? AND request_id=?",
                             (sid, request_id)).fetchone()
            if old:
                if old["spec"] != spec:
                    raise ValueError("request_id already belongs to a different task")
                return old["id"]
            count = db.execute("SELECT count(*) FROM tasks WHERE state IN ('queued','running')").fetchone()[0]
            if count >= MAX_ACTIVE:
                raise ValueError("Account already has eight active tasks")
            # Keep uncertain / undelivered receipts, even under storage pressure.
            db.execute("DELETE FROM tasks WHERE id IN (SELECT id FROM tasks "
                       "WHERE delivery IN ('delivered','suppressed','rejected') "
                       "AND state NOT IN ('queued','running') ORDER BY created DESC LIMIT -1 OFFSET 99)")
            if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= MAX_RECORDS:
                raise ValueError("Task receipt limit reached; inspect pending/unknown notifications")
            task_id, now = str(uuid4()), time.time()
            db.execute("INSERT INTO tasks(id,sid,request_id,spec,state,created,updated) "
                       "VALUES(?,?,?,?,'queued',?,?)", (task_id, sid, request_id, spec, now, now))
        return task_id

    def get(self, task_id, sid=None):
        thread_id(task_id)
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or (sid is not None and row["sid"] != sid):
            raise ValueError("Task not found in this account and session")
        task = dict(row)
        task["spec"] = json.loads(task["spec"])
        return task

    def list_for(self, sid):
        thread_id(sid)
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE sid=? ORDER BY created DESC LIMIT 50",
                                          (sid,))]
        return [public_task(self.get(key, sid)) for key in ids]

    def activity_snapshot(self):
        """Read public activity without creating state or starting a worker.

        The Wrapper groups resident sessions by account and reads once per
        polling pass. Output and command arguments never enter this projection.
        Missing state is an authoritative empty level; unreadable state is not.
        """
        try:
            root = self.root.lstat()
        except FileNotFoundError:
            return []
        if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid()
                or root.st_mode & 0o077):
            raise ValueError("Invalid task state directory")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return []
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("Invalid task database")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            return [dict(row) for row in db.execute(
                "SELECT id,sid,state,delivery,created,updated,"
                "json_extract(spec,'$.title') AS title FROM tasks "
                "WHERE cancel_requested=0 AND (state IN ('queued','running') "
                "OR delivery IN ('pending','sending')) ORDER BY created LIMIT ?",
                (MAX_RECORDS,),
            )]
        finally:
            db.close()

    def update(self, task_id, **fields):
        if not fields or not fields.keys() <= {"state", "output", "truncated", "exit_code", "delivery", "error", "delivery_error", "next_attempt"}:
            raise ValueError("Invalid task update")
        with self.db() as db:
            db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in fields)
                       + ",updated=? WHERE id=?", (*fields.values(), time.time(), task_id))

    def cancel(self, task_id, sid):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=? AND sid=?", (task_id, sid)).fetchone()
            if row is None:
                raise ValueError("Task not found in this account and session")
            if row["delivery"] in {"sending", "unknown", "delivered"}:
                raise ValueError("Completion may already be delivered; it cannot be recalled")
            db.execute("UPDATE tasks SET cancel_requested=1,delivery='suppressed',next_attempt=0,updated=? WHERE id=?",
                       (time.time(), task_id))

    def begin_execution(self, task_id):
        with self.db() as db:
            return bool(db.execute("UPDATE tasks SET state='running',error=NULL,updated=? "
                                   "WHERE id=? AND state='queued' AND cancel_requested=0",
                                   (time.time(), task_id)).rowcount)

    def begin_delivery(self, task_id):
        with self.db() as db:
            changed = db.execute("UPDATE tasks SET delivery='sending',updated=? "
                                 "WHERE id=? AND delivery='pending' AND cancel_requested=0",
                                 (time.time(), task_id)).rowcount
            if not changed:
                raise ValueError("Task completion is no longer pending")

    def recover(self):
        """Only the exclusive worker calls this after acquiring its lifetime lock.

        Official command/exec processes terminate with their originating socket.
        Never re-execute commands whose previous worker died after submission.
        """
        with self.db() as db:
            db.execute("UPDATE tasks SET state='interrupted',error='Task worker stopped; command was not rerun' "
                       "WHERE state='running'")
            db.execute("UPDATE tasks SET delivery='unknown',delivery_error='Completion acknowledgment was lost' "
                       "WHERE delivery='sending'")

    def recoverable(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT id FROM tasks WHERE "
                    "(state IN ('queued','running') OR delivery IN ('pending','sending')) "
                    "AND next_attempt<=? ORDER BY created LIMIT ?", (time.time(), MAX_RECORDS))]

    def has_pending(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM tasks WHERE state IN ('queued','running') "
                                   "OR delivery IN ('pending','sending') LIMIT 1").fetchone())


def public_task(task, *, output=False):
    result = {"task_id": task["id"], "title": task["spec"]["title"],
              "state": task["state"], "exit_code": task["exit_code"],
              "cancel_requested": bool(task["cancel_requested"]),
              "notification": task["delivery"], "error": task["error"],
              "notification_error": task["delivery_error"],
              "output_truncated": bool(task["truncated"])}
    if output:
        result["output"] = task["output"].decode("utf-8", errors="replace")
    return result
