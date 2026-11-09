"""Device-local message dispatch through the Wrapper's ordinary command lane."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4

from cc_remote.log import logger
from cc_remote.protocol import Error, Query
from cc_remote.session_messages import (
    MAX_FRAME, envelope, make_server, reference, request_id, split_reference,
)

log = logger("cc_remote.wrapper.session_messages")
MAX_PENDING = 32


class MessageStore:
    """At-most-once receipts survive lost acknowledgments and Wrapper restarts."""
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def db(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Message state must be a private directory owned by this user")
        path = self.root / "receipts.sqlite3"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_uid != os.getuid():
                raise ValueError("Invalid message store")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        db = sqlite3.connect(path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS messages (
                source TEXT NOT NULL, request TEXT NOT NULL, spec TEXT NOT NULL,
                id TEXT NOT NULL UNIQUE, target TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                PRIMARY KEY(source, request))""")
            if "prompt" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN prompt TEXT")
            yield db
            db.commit()
        finally:
            db.close()

    def claim(self, source, key, target, prompt, *, queue=False):
        spec = hashlib.sha256(json.dumps([target, prompt], ensure_ascii=False).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE source=? AND request=?", (source, key)).fetchone()
            if row is not None:
                if row["spec"] != spec:
                    raise ValueError("request_id was already used with different arguments")
                return self.public(row), False
            # Keep every idempotency key. Do not silently make old sends repeatable.
            if db.execute("SELECT count(*) FROM messages").fetchone()[0] >= 10000:
                raise ValueError("Message receipt store is full; no message was sent")
            if queue and db.execute("SELECT count(*) FROM messages WHERE status IN ('queued','dispatching')").fetchone()[0] >= MAX_PENDING:
                raise ValueError("Message inbox is full; no message was queued")
            message_id = f"message-{uuid4().hex}"
            db.execute("INSERT INTO messages(source, request, spec, id, target, status, prompt) VALUES (?,?,?,?,?,?,?)",
                       (source, key, spec, message_id, target, "queued" if queue else "unknown", prompt if queue else None))
        return self.get(source, key), True

    @staticmethod
    def public(row):
        return {"request_id": row["request"], "message_id": row["id"],
                "session_ref": row["target"], "status": "unknown" if row["status"] == "dispatching" else row["status"],
                "reason": row["reason"]}

    def get(self, source, key):
        result = self.find(source, key)
        if result is None:
            raise ValueError("No receipt for this caller and request_id")
        return result

    def find(self, source, key):
        with self.db() as db:
            row = db.execute("SELECT * FROM messages WHERE source=? AND request=?", (source, key)).fetchone()
            return self.public(row) if row is not None else None

    def settle(self, message_id, status, reason=None):
        with self.db() as db:
            db.execute("UPDATE messages SET status=?, reason=?, prompt=NULL WHERE id=? AND status IN ('unknown','queued','dispatching')",
                       (status, reason, message_id))

    def pending(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE status='queued' ORDER BY rowid LIMIT ?", (MAX_PENDING,))]

    def begin_delivery(self, message_id):
        # Commit before any native write. After a crash this claim is uncertain,
        # never automatically replayed, even if the acknowledgment was lost.
        with self.db() as db:
            return db.execute("UPDATE messages SET status='dispatching', prompt=NULL WHERE id=? AND status='queued'",
                              (message_id,)).rowcount == 1

    def unsubmitted(self, message_id, prompt):
        # Only the SDK's explicit launch=False fence proves no input was written.
        with self.db() as db:
            db.execute("UPDATE messages SET status='queued', prompt=? WHERE id=? AND status='dispatching'",
                       (prompt, message_id))

    def recover(self):
        # Called only after taking the exclusive Wrapper message-server lease.
        # A dead controller's in-flight native input is uncertain, never pending.
        with self.db() as db:
            db.execute("UPDATE messages SET status='unknown', prompt=NULL WHERE status='dispatching'")


class SessionMessages:
    def __init__(self, machine):
        self.machine = machine
        self.store = MessageStore(Path(machine.cfg.state_dir) / "messages")
        self.socket = self.store.root / "control.sock"
        self.server = None
        self.lock_fd = None
        self.commands = {}
        self.waiters = {}
        self.clients = set()
        self.active = set()
        self.scheduled = set()
        self.pump = None
        self.wakeup = asyncio.Event()
        self.closing = False
        self.retry_after = {}

    async def start(self):
        # Validate the directory before creating the socket or lifetime lock.
        with self.store.db():
            pass
        if len(os.fsencode(self.socket)) > 103:
            raise ValueError("Message socket path exceeds the platform limit")
        self.lock_fd = os.open(self.store.root / "server.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            lock_info = os.fstat(self.lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
                raise ValueError("Invalid message server lock")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.store.recover()
            if self.socket.exists() or self.socket.is_symlink():
                info = self.socket.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("Refusing to replace an unrelated message socket")
                self.socket.unlink()
            self.server = await asyncio.start_unix_server(self._client, str(self.socket), limit=MAX_FRAME)
            os.chmod(self.socket, 0o600)
            self.pump = asyncio.create_task(self._drain(), name="session-message-inbox")
        except BaseException:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise

    async def close(self):
        self.closing = True
        if self.pump is not None:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.socket.unlink(missing_ok=True)
        for client in list(self.clients):
            client.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def owns(self, message_id):
        return message_id in self.active

    def _busy(self, ctx):
        return (ctx.state != "idle" or ctx.queued_queries or ctx.queued_query_starting_msg_id
                or any(task is not None and not task.done() for task in
                       (ctx.turn_task, ctx.codex_spontaneous_task))
                or self.machine._claude_autonomous_followup_pending(ctx)
                or (getattr(ctx.sdk, "needs_message_tools_refresh", False)
                    and self.machine._claude_has_background_work(ctx)))

    async def _schedule(self, message_id, target, prompt):
        if self.closing or target in self.scheduled:
            return False
        engine, sid = split_reference(target)
        cmd = Query(sid=sid, prompt=prompt, msg_id=message_id,
                    cmd_id=message_id, client_id="local-session-messages")
        self.commands[id(cmd)] = (cmd, engine, target)
        self.scheduled.add(target)
        self.active.add(message_id)
        try:
            await self.machine._command_scheduler.submit(cmd, target=sid, serial=True)
        except BaseException:
            self.commands.pop(id(cmd), None)
            self.scheduled.discard(target)
            raise
        return True

    async def _drain(self):
        while True:
            self.wakeup.clear()
            try:
                seen = set()
                for row in self.store.pending():
                    target = row["target"]
                    if target in seen:
                        continue
                    seen.add(target)  # FIFO per target, independent across targets.
                    if self.retry_after.get(target, 0) > asyncio.get_running_loop().time():
                        continue
                    _, sid = split_reference(target)
                    ctx = self.machine.sessions.get(sid)
                    if ctx is not None and self._busy(ctx):
                        continue
                    await self._schedule(row["id"], target, row["prompt"])
            except Exception:
                log.warning("Message inbox temporarily unavailable")
            try:
                await asyncio.wait_for(self.wakeup.wait(), 1)
            except TimeoutError:
                pass

    async def _client(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            if len(self.clients) > 16:
                return
            async with asyncio.timeout(40):
                request = json.loads(await reader.readline())
                if not isinstance(request, dict) or set(request) != {"codex_home", "thread_id", "tool", "arguments"}:
                    raise ValueError("Invalid message request")
                from cc_remote.task_store import thread_id
                native = thread_id(request["thread_id"])
                home = request["codex_home"]
                profiles = [p for p in self.machine._codex_profiles if str(p.home.resolve()) == home]
                if len(profiles) != 1:
                    raise ValueError("Caller account is not configured on this Wrapper")
                source = reference("codex", self.machine._codex_wire_sid(profiles[0], native))
                result = await self.call(source, request["tool"], request["arguments"])
        except ValueError:
            result = {"error": "Invalid caller or message request; inspect the configured account and arguments"}
        except Exception:
            result = {"status": "unknown", "error": "Message service unavailable; retry only with the same request_id"}
        finally:
            self.clients.discard(task)
            try:
                if "result" in locals():
                    writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                    await writer.drain()
            except (OSError, ConnectionError):
                pass
            writer.close()
            await writer.wait_closed()

    def claude_server(self, ctx):
        async def call(name, args, _meta):
            if not self.machine._is_resident_context(ctx) or not ctx.session_id or ctx.btw or ctx.space != "code":
                raise ValueError("Caller is not an active Code session")
            return await self.call(reference("claude", self.machine._ctx_wire_sid(ctx)), name, args)
        return make_server(call)

    async def catalog(self):
        machine = self.machine
        rows = {}
        for engine in ("claude", "codex"):
            work = await asyncio.to_thread(machine._work.for_engine(engine).records_by_profile_session)
            if engine == "codex":
                candidates = await machine._read_all_codex_profile_sessions()
            else:
                candidates = []
                if not machine._claude_profile_migration_ok:
                    continue
                for profile in machine._claude_profiles:
                    try:
                        infos = await asyncio.to_thread(machine._claude_catalog_list_sessions, profile, limit=200)
                        blocked = await asyncio.to_thread(machine._bg_blocked_session_ids, profile.config_dir)
                    except Exception:
                        log.warning("Message catalog profile unavailable", profile_id=profile.id)
                        continue
                    for info in infos:
                        if info.session_id in blocked:
                            continue
                        candidates.append({"session_id": machine._claude_wire_sid(profile, info.session_id),
                                           "native_session_id": info.session_id, "claude_profile_id": profile.id,
                                           "cwd": info.cwd, "tag": info.tag,
                                           "summary": getattr(info, "custom_title", None) or info.summary or info.first_prompt})
            for row in candidates:
                sid = row["session_id"]
                native = row.get("native_session_id") or sid
                profile_id = row.get(f"{engine}_profile_id")
                ctx = machine.sessions.get(sid)
                if (row.get("tag") == "archived" or row.get("archived")
                        or (profile_id, native) in work or sid in machine._private_btw_sessions
                        or (ctx is not None and (ctx.btw or ctx.space != "code"))):
                    continue
                ref = reference(engine, sid)
                rows[ref] = {"session_ref": ref, "engine": engine, "account": profile_id,
                             "title": str(row.get("summary") or row.get("first_prompt") or native)[:200],
                             "cwd": row.get("cwd"), "state": ctx.state if ctx else "not_loaded"}
        return rows

    async def call(self, source, name, args):
        split_reference(source)
        if not isinstance(args, dict):
            raise ValueError("Invalid tool arguments")
        allowed = {"list_sessions": {"search"}, "send_message": {"session_ref", "message", "request_id"},
                   "message_status": {"request_id"}}
        if name not in allowed or set(args) - allowed[name]:
            raise ValueError("Unknown message tool or arguments")
        if name == "message_status":
            return self.store.get(source, request_id(args.get("request_id")))
        if name == "send_message":
            key = request_id(args.get("request_id"))
            target = args.get("session_ref")
            split_reference(target)
            prompt = envelope(source, args.get("message"))
            # A retry must recover its receipt even if a participant was later
            # archived or deleted. It never revalidates into a second send.
            if self.store.find(source, key) is not None:
                return self.store.claim(source, key, target, prompt)[0]
            if len(self.waiters) >= MAX_PENDING:
                raise ValueError("Too many unresolved sends; no message was submitted")
        rows = await self.catalog()
        if source not in rows:
            raise ValueError("Caller is not an eligible Code session on this device")
        if name == "list_sessions":
            search = args.get("search", "")
            if not isinstance(search, str) or len(search) > 200:
                raise ValueError("search must contain at most 200 characters")
            matches = []
            size = 256
            truncated = False
            for ref, row in rows.items():
                encoded = json.dumps(row)
                if ref == source or search.casefold() not in json.dumps(row, ensure_ascii=False).casefold():
                    continue
                if len(matches) >= 200 or size + len(encoded) > MAX_FRAME - 1024:
                    truncated = True
                    break
                matches.append(row)
                size += len(encoded) + 2
            return {"sessions": matches, "truncated": truncated}
        if target == source or target not in rows:
            raise ValueError("Choose another eligible Code session from list_sessions")
        if len(self.waiters) >= MAX_PENDING:
            raise ValueError("Too many unresolved sends; no message was submitted")
        receipt, fresh = self.store.claim(source, key, target, prompt, queue=True)
        if fresh:
            future = asyncio.get_running_loop().create_future()
            self.waiters[receipt["message_id"]] = future
            # Only the oldest queued message can claim this target's next turn.
            first = next(row for row in self.store.pending() if row["target"] == target)
            if first["id"] == receipt["message_id"]:
                if not await self._schedule(receipt["message_id"], target, prompt):
                    self._resolve_waiter(receipt["message_id"])
            else:
                self._resolve_waiter(receipt["message_id"])
            self.wakeup.set()
            try:
                await asyncio.wait_for(asyncio.shield(future), 25)
            except TimeoutError:
                pass  # The original handler owns delivery; never resend it.
        return self.store.get(source, key)

    def accepted(self, message_id):
        if message_id not in self.active:
            return
        self._settle(message_id, "accepted")

    def _settle(self, message_id, status, reason=None):
        try:
            self.store.settle(message_id, status, reason)
        except Exception:
            # A native input already ran. Preserve unknown rather than failing
            # the engine reader, retrying, or claiming durable acceptance.
            log.warning("Message receipt could not be saved")
        self.active.discard(message_id)
        self._resolve_waiter(message_id)
        self.wakeup.set()

    def _resolve_waiter(self, message_id):
        waiter = self.waiters.pop(message_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    async def dispatch(self, cmd):
        _cmd, engine, target = self.commands.pop(id(cmd))
        machine = self.machine
        claimed = False
        try:
            if self.closing:
                return  # Durable queued item belongs to the next Wrapper.
            updates = getattr(machine, "_ui_updates", None)
            if updates is not None and updates.guarded():
                self._resolve_waiter(cmd.msg_id)
                self.retry_after[target] = asyncio.get_running_loop().time() + 5
                return
            directory_actions = getattr(machine, "_directory_actions", None)
            if directory_actions is not None and cmd.sid in directory_actions.mutating:
                self._resolve_waiter(cmd.msg_id)
                self.retry_after[target] = asyncio.get_running_loop().time() + 5
                return
            current = await self.catalog()
            if target not in current:
                self._settle(cmd.msg_id, "rejected", "Target is no longer an eligible Code session")
                return
            row = current[target]
            ctx = machine.sessions.get(cmd.sid)
            if ctx is None:
                profile, native = (machine._codex_target(cmd.sid) if engine == "codex"
                                   else machine._claude_target(cmd.sid))
                if not row.get("cwd") or not os.path.isdir(row["cwd"]):
                    self._settle(cmd.msg_id, "rejected", "Target working directory is unavailable")
                    return
                # Ordinary resume only: no bootstrap fresh-session fallback,
                # takeover, focus change or replacement of native settings.
                kwargs = (machine._codex_spawn_profile_kwargs(profile) if engine == "codex"
                          else machine._claude_spawn_profile_kwargs(profile))
                ctx = await machine._spawn(resume_id=native, cwd=row["cwd"], engine=engine,
                                           raise_on_failure=True, **kwargs)
            if (ctx is None or ctx.engine != engine or ctx.btw or ctx.space != "code"
                    or ctx.write_state != "writable"
                    or getattr(ctx.sdk, "is_claude_broker", False)):
                self._settle(cmd.msg_id, "rejected", "Target is externally owned or unavailable; no message was sent")
                return
            async with ctx.query_lock:
                if not machine._is_resident_context(ctx):
                    self._settle(cmd.msg_id, "rejected", "Target is no longer resident")
                    return
                if self._busy(ctx):
                    self._resolve_waiter(cmd.msg_id)
                    return
                if self.closing:
                    return
                if not self.store.begin_delivery(cmd.msg_id):
                    return
                claimed = True
                launched = asyncio.get_running_loop().create_future()
                result = await machine._handle_immediate_query(ctx, cmd, launch_receipt=launched)
            if isinstance(result, Error):
                if result.code == "message_tools_pending" or (
                    result.code == "busy" and self._busy(ctx)
                    and ctx.write_state == "writable" and not getattr(ctx.sdk, "is_claude_broker", False)
                ):
                    self.store.unsubmitted(cmd.msg_id, cmd.prompt)
                    self._resolve_waiter(cmd.msg_id)
                    self.retry_after[target] = asyncio.get_running_loop().time() + 5
                else:
                    self._settle(cmd.msg_id, "rejected", result.message)
            elif ctx.turn_task:
                def finished(_):
                    if cmd.msg_id not in self.active:
                        return
                    if launched.done() and not launched.cancelled() and launched.result() is False:
                        self.store.unsubmitted(cmd.msg_id, cmd.prompt)
                        self._resolve_waiter(cmd.msg_id)
                        self.wakeup.set()
                    else:
                        self._settle(cmd.msg_id, "unknown")
                ctx.turn_task.add_done_callback(finished)
            elif cmd.msg_id in self.active:
                self._settle(cmd.msg_id, "unknown")
        except Exception:
            self._settle(cmd.msg_id, "unknown" if claimed else "rejected",
                         None if claimed else "Target could not be prepared; no input was submitted")
            log.warning("Message delivery unconfirmed", message_id=cmd.msg_id)
        finally:
            self.scheduled.discard(target)
