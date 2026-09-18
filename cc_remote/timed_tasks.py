"""Local scheduled-message receipts; prompts and account paths stay on the host.

The clock is a detached helper, not a Goal. Delivery uses the existing official
Codex daemon's queue and an explicit client message id. Reads never start an
engine, and ambiguous delivery is never retried automatically.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
from uuid import UUID, uuid4

from websockets.asyncio.client import unix_connect

MAX_TASKS = 32
MAX_SENDS = 1000
LEASE_SECONDS = 90


class TimedTaskStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "timed-tasks.sqlite3"

    @contextmanager
    def connect(self, *, write=False):
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Invalid task store")
        elif not write:
            yield None
            return
        db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if write else "?mode=ro"),
                             uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            if write:
                os.chmod(self.path, 0o600)
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY, home TEXT NOT NULL, sid TEXT NOT NULL,
                        title TEXT NOT NULL, prompt TEXT NOT NULL,
                        interval REAL NOT NULL, count INTEGER NOT NULL,
                        sent INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
                        next_at REAL, lease_until REAL NOT NULL, updated REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS task_owner ON tasks(home,sid,updated);
                    CREATE TABLE IF NOT EXISTS deliveries (
                        id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL, scheduled_at REAL NOT NULL,
                        native_id TEXT, turn_id TEXT, accepted INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(task_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS delivery_task ON deliveries(task_id);
                    CREATE INDEX IF NOT EXISTS delivery_native ON deliveries(native_id);
                    CREATE INDEX IF NOT EXISTS delivery_turn ON deliveries(turn_id);
                """)
            else:
                db.execute("PRAGMA query_only=ON")
            yield db
            if write:
                db.commit()
        finally:
            db.close()

    def create(self, home: str, sid: str, title: str, prompt: str,
               delay: float, interval: float, count: int) -> str:
        UUID(sid)
        if not (1 <= count <= MAX_SENDS and 1 <= delay <= 31_536_000
                and 1 <= interval <= 31_536_000 and 0 < len(prompt) <= 32_768
                and 0 < len(title) <= 120):
            raise ValueError("Invalid scheduled message")
        task_id, now = str(uuid4()), time.time()
        home = str(Path(home).expanduser().resolve(strict=True))
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT count(*) FROM tasks WHERE home=? AND sid=? "
                                "AND state='running' AND lease_until>?", (home, sid, now)).fetchone()[0]
            if active >= MAX_TASKS:
                raise ValueError("Too many active scheduled tasks")
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,0,'running',?,?,?)",
                       (task_id, home, sid, title, prompt, interval, count,
                        now + delay, now + LEASE_SECONDS, now))
        return task_id

    def get(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if db else None
            return dict(row) if row else None

    def heartbeat(self, task_id: str) -> bool:
        now = time.time()
        with self.connect(write=True) as db:
            return bool(db.execute("UPDATE tasks SET lease_until=?,updated=? "
                                   "WHERE id=? AND state='running'",
                                   (now + LEASE_SECONDS, now, task_id)).rowcount)

    def finish(self, task_id: str, state: str) -> None:
        if state not in {"completed", "failed", "cancelled", "unknown"}:
            raise ValueError("Invalid terminal task state")
        with self.connect(write=True) as db:
            db.execute("UPDATE tasks SET state=?,next_at=NULL,updated=? WHERE id=?",
                       (state, time.time(), task_id))

    def begin_delivery(self, task_id: str) -> str:
        delivery = str(uuid4())
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] != "running" or row["sent"] >= row["count"]:
                raise ValueError("Task is no longer sending")
            # Persist before I/O. A crashed/ambiguous attempt at this ordinal
            # cannot be sent again by a restarted helper.
            db.execute("INSERT INTO deliveries(id,task_id,ordinal,scheduled_at) VALUES(?,?,?,?)",
                       (delivery, task_id, row["sent"], row["next_at"]))
        return delivery

    def accepted(self, task_id: str, delivery: str, turn_id: str | None = None) -> None:
        with self.connect(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE deliveries SET accepted=1,turn_id=COALESCE(?,turn_id) "
                                 "WHERE id=? AND task_id=? AND accepted=0",
                                 (turn_id, delivery, task_id)).rowcount
            if changed:
                now = time.time()
                db.execute("UPDATE tasks SET sent=sent+1,"
                           "state=CASE WHEN state='running' AND sent+1>=count THEN 'completed' ELSE state END,"
                           "next_at=CASE WHEN sent+1>=count THEN NULL ELSE MAX(next_at+interval,?+interval) END,"
                           "updated=? WHERE id=?", (now, now, task_id))

    def active(self, *, now: float | None = None) -> dict[tuple[str, str], list[dict]]:
        """One bounded read for the sidebar; private ownership keys stay local."""
        now = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks WHERE state='running' AND lease_until>? "
                              "AND sent<count ORDER BY next_at LIMIT 1024", (now,)).fetchall() if db else []
        result = {}
        for r in rows:
            group = result.setdefault((r["home"], r["sid"]), [])
            if len(group) < MAX_TASKS:
                group.append({"task_id": r["id"], "title": r["title"], "next_message_at": r["next_at"],
                              "interval_seconds": r["interval"], "sent_count": r["sent"],
                              "total_count": r["count"], "valid_until": r["lease_until"]})
        return result

    def public_tasks(self, home: str, sid: str, *, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.connect() as db:
            if db is None:
                return []
            rows = db.execute("SELECT * FROM tasks WHERE home=? AND sid=? AND state='running' "
                              "AND lease_until>? AND sent<count ORDER BY next_at LIMIT ?",
                              (home, sid, now, MAX_TASKS)).fetchall()
        return [{"task_id": r["id"], "title": r["title"], "next_message_at": r["next_at"],
                 "interval_seconds": r["interval"], "sent_count": r["sent"],
                 "total_count": r["count"], "valid_until": r["lease_until"]} for r in rows]

    def source(self, home: str, sid: str, ids: list[str], *, bind: str | None = None) -> dict | None:
        ids = list(dict.fromkeys(v for v in ids if isinstance(v, str) and 0 < len(v) <= 512))[:4]
        if not ids:
            return None
        marks = ",".join("?" for _ in ids)
        with self.connect(write=bind is not None and self.path.exists()) as db:
            if db is None:
                return None
            rows = db.execute(f"SELECT d.*,t.title FROM deliveries d JOIN tasks t ON t.id=d.task_id "
                              f"WHERE t.home=? AND t.sid=? AND (d.id IN ({marks}) "
                              f"OR d.native_id IN ({marks}) OR d.turn_id IN ({marks})) LIMIT 2",
                              (home, sid, *ids, *ids, *ids)).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
            if bind and row["native_id"] is None:
                db.execute("UPDATE deliveries SET native_id=? WHERE id=?", (bind, row["id"]))
            return {"task_id": row["task_id"], "title": row["title"],
                    "scheduled_at": row["scheduled_at"]}


class NativeQueueError(RuntimeError):
    pass


async def deliver(task: dict, delivery_id: str, on_accepted) -> None:
    """Queue on the existing account daemon, without resuming or creating a thread."""
    socket = Path(task["home"]) / "app-server-control/app-server-control.sock"
    info = socket.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise NativeQueueError("Shared daemon unavailable")
    async with asyncio.timeout(30), unix_connect(
        str(socket), uri="ws://localhost/rpc", compression=None,
        max_size=2**20, open_timeout=5, close_timeout=1,
    ) as ws:
        seq = 0

        async def rpc(method, params):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == seq:
                    if "error" in message:
                        raise NativeQueueError("Native queue request rejected")
                    return message["result"]

        await rpc("initialize", {"clientInfo": {"name": "cc-remote-timed-task", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        await ws.send(json.dumps({"method": "initialized"}))
        await rpc("thread/read", {"threadId": task["sid"], "includeTurns": False})
        reply = await rpc("thread/queue/add", {"threadId": task["sid"],
            "input": [{"type": "text", "text": task["prompt"], "text_elements": []}],
            "clientUserMessageId": delivery_id})
        queued = reply["queuedSubmission"]
        if queued.get("clientUserMessageId") != delivery_id:
            raise NativeQueueError("Unconfirmed scheduled message identity")
        on_accepted()
        # Queue acceptance is durable. An idle thread can start now; otherwise
        # the official queue waits for its native current-turn boundary.
        try:
            state = await rpc("thread/read", {"threadId": task["sid"], "includeTurns": False})
            if state["thread"]["status"]["type"] == "idle":
                await rpc("thread/queue/start", {
                    "threadId": task["sid"], "queuedSubmissionId": queued["id"]})
                return
        except Exception:
            # Another client may already have consumed this exact queue entry.
            # Do not add it again or substitute an ordinary turn/start.
            pass
        return None


async def run_task(store: TimedTaskStore, task_id: str) -> None:
    UUID(task_id)
    # One helper owns a task. The persistent delivery ordinal additionally
    # fences retries after process death or an uncertain socket response.
    lock_path = store.path.parent / f"timed-task-{task_id}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        await _run_task(store, task_id)
    finally:
        os.close(fd)


async def _run_task(store: TimedTaskStore, task_id: str) -> None:
    while task := store.get(task_id):
        if task["state"] != "running":
            return
        if task["sent"] >= task["count"]:
            store.finish(task_id, "completed")
            return
        if not store.heartbeat(task_id):
            return
        delay = task["next_at"] - time.time()
        if delay > 0:
            await asyncio.sleep(min(delay, 20))
            continue
        try:
            delivery_id = store.begin_delivery(task_id)
            await deliver(task, delivery_id, lambda: store.accepted(task_id, delivery_id))
        except Exception:
            current = store.get(task_id)
            if current and current["state"] == "running" and current["sent"] == task["sent"]:
                store.finish(task_id, "unknown")
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduled messages to an existing Codex session")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".cc-remote")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--codex-home", type=Path, required=True)
    start.add_argument("--thread", required=True)
    start.add_argument("--title", default="定时任务")
    start.add_argument("--message", required=True)
    start.add_argument("--after", type=float, required=True, help="Seconds until the first message")
    start.add_argument("--every", type=float, default=60, help="Seconds between messages")
    start.add_argument("--count", type=int, default=1)
    for name in ("run", "cancel", "status"):
        commands.add_parser(name).add_argument("task_id")
    args = parser.parse_args()
    store = TimedTaskStore(args.state_dir.expanduser().resolve())
    if args.command == "start":
        task_id = store.create(str(args.codex_home), args.thread, args.title, args.message,
                               args.after, args.every, args.count)
        try:
            subprocess.Popen([sys.executable, "-m", "cc_remote.timed_tasks", "--state-dir",
                              str(store.path.parent), "run", task_id],
                             cwd=Path(__file__).resolve().parent.parent,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            store.finish(task_id, "failed")
            raise
        print(json.dumps({"task_id": task_id, "next_message_at": store.get(task_id)["next_at"]}))
    elif args.command == "run":
        asyncio.run(run_task(store, args.task_id))
    elif args.command == "cancel":
        store.finish(args.task_id, "cancelled")
    else:
        task = store.get(args.task_id)
        print(json.dumps({k: v for k, v in (task or {}).items() if k not in {"home", "prompt", "sid"}}))


if __name__ == "__main__":
    main()
