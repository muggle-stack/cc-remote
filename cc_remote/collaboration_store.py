"""Device-local durable rooms, native bindings and input receipts."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from cc_remote.collaboration import ACTIVE_DELIVERIES, MAX_ROOM_CONTEXT
from cc_remote.collaboration_questions import answer_text, match_async_answer, native_question, question_text

_MESSAGE_COLUMNS = (
    "id,room_id,seq,role,member_id,text,targets,source,reply_to,created,attachments,question"
)


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CollaborationStore:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "collaboration.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Invalid collaboration database")
        with self.db() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("聊天室数据版本与当前程序不兼容。")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rooms(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    default_member TEXT, revision INTEGER NOT NULL DEFAULT 1,
                    created REAL NOT NULL, updated REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    context_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS members(
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id),
                    spec TEXT NOT NULL, account_key TEXT NOT NULL, sid TEXT,
                    native_sid TEXT, cwd TEXT, synced_seq INTEGER NOT NULL DEFAULT 0, source_delivery TEXT,
                    removed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    pending_title TEXT
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id),
                    seq INTEGER NOT NULL, role TEXT NOT NULL, member_id TEXT,
                    text TEXT NOT NULL, targets TEXT NOT NULL DEFAULT '[]',
                    payload TEXT NOT NULL DEFAULT '{}', attachments TEXT NOT NULL DEFAULT '[]', input_fingerprint TEXT,
                    source TEXT UNIQUE, reply_to TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL,
                    UNIQUE(room_id,seq)
                );
                CREATE TABLE IF NOT EXISTS deliveries(
                    id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id),
                    message_id TEXT NOT NULL REFERENCES messages(id), member_id TEXT NOT NULL REFERENCES members(id),
                    status TEXT NOT NULL, mode TEXT, sid TEXT, native_turn TEXT, native_input_id TEXT,
                    context_before INTEGER NOT NULL, context_through INTEGER NOT NULL, context_version INTEGER NOT NULL,
                    payload TEXT NOT NULL, error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(message_id,member_id)
                );
                CREATE TABLE IF NOT EXISTS message_versions(
                    message_id TEXT NOT NULL REFERENCES messages(id), version INTEGER NOT NULL,
                    text TEXT NOT NULL, PRIMARY KEY(message_id,version)
                );
                CREATE TABLE IF NOT EXISTS receipts(
                    owner TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    room_id TEXT, created REAL NOT NULL, PRIMARY KEY(owner,request_id)
                );
                CREATE INDEX IF NOT EXISTS members_sid ON members(sid);
                CREATE INDEX IF NOT EXISTS deliveries_status ON deliveries(status,created);
                CREATE INDEX IF NOT EXISTS deliveries_member ON deliveries(member_id,created);
                CREATE INDEX IF NOT EXISTS members_room ON members(room_id,created);
                PRAGMA user_version=1;
            """)
            if "pending_title" not in {row[1] for row in db.execute("PRAGMA table_info(members)")}:
                db.execute("ALTER TABLE members ADD COLUMN pending_title TEXT")
            if "question" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN question TEXT")
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _room(db, owner, room_id):
        row = db.execute(
            "SELECT * FROM rooms WHERE id=? AND owner=?", (room_id, owner)
        ).fetchone()
        if row is None:
            raise ValueError("聊天室不存在或当前账号无权访问。")
        return row

    @staticmethod
    def _member(db, room_id, member_id):
        row = db.execute(
            "SELECT * FROM members WHERE id=? AND room_id=?", (member_id, room_id)
        ).fetchone()
        if row is None:
            raise ValueError("参与者不存在。")
        return row

    @staticmethod
    def _add_member(db, room_id, spec, account_key, binding=None, pending_title=None):
        if (
            db.execute(
                "SELECT count(*) FROM members WHERE room_id=? AND removed=0", (room_id,)
            ).fetchone()[0]
            >= 16
        ):
            raise ValueError("每个聊天室最多添加 16 位参与者。")
        if binding and db.execute(
            "SELECT 1 FROM members WHERE account_key=? AND native_sid=?",
            (account_key, binding["native_sid"]),
        ).fetchone():
            raise ValueError("该会话已关联聊天，请恢复原成员或选择另一个会话。")
        member_id = str(uuid4())
        db.execute(
            "INSERT INTO members(id,room_id,spec,account_key,sid,native_sid,cwd,created,pending_title) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                member_id, room_id, encoded(spec), account_key,
                binding["sid"] if binding else None,
                binding["native_sid"] if binding else None,
                binding["cwd"] if binding else None,
                time.time(),
                pending_title,
            ),
        )
        return member_id

    @staticmethod
    def _message(
        db,
        room_id,
        message_id,
        role,
        text,
        *,
        member_id=None,
        targets=(),
        source=None,
        reply_to=(),
        payload=None,
        question=None,
    ):
        seq = db.execute(
            "SELECT coalesce(max(seq),0)+1 FROM messages WHERE room_id=?", (room_id,)
        ).fetchone()[0]
        payload = payload or {}
        attachments = [
            {"kind": "file", "name": file["filename"]}
            for file in payload.get("files") or []
        ] + [
            {"kind": "image", "name": f"图片 {index + 1}"}
            for index, _image in enumerate(payload.get("images") or [])
        ]
        db.execute(
            "INSERT INTO messages(id,room_id,seq,role,member_id,text,targets,source,reply_to,created,payload,attachments,question) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                room_id,
                seq,
                role,
                member_id,
                text,
                encoded(list(targets)),
                source,
                encoded(list(reply_to)),
                time.time(),
                encoded(payload),
                encoded(attachments),
                encoded(question) if question is not None else None,
            ),
        )
        return seq

    def mutate(
        self,
        owner: str,
        request_id: str,
        operation: str,
        body: dict,
        *,
        prepared_members=(),
        native_title_confirmed=False,
    ):
        fingerprint = hashlib.sha256(encoded([operation, body]).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM receipts WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("该请求编号已用于其他操作，请刷新后重试。")
                return prior["room_id"]
            room_id = body.get("room_id")
            if operation == "create":
                if (
                    db.execute(
                        "SELECT count(*) FROM rooms WHERE owner=? AND archived=0",
                        (owner,),
                    ).fetchone()[0]
                    >= 100
                ):
                    raise ValueError("请先归档已有聊天室。")
                room_id = str(uuid4())
                now = time.time()
                db.execute(
                    "INSERT INTO rooms(id,owner,name,created,updated) VALUES(?,?,?,?,?)",
                    (room_id, owner, body["name"].strip(), now, now),
                )
                first = None
                for prepared in prepared_members:
                    member_id = self._add_member(db, room_id, *prepared)
                    first = first or member_id
                db.execute(
                    "UPDATE rooms SET default_member=? WHERE id=?", (first, room_id)
                )
            else:
                room = self._room(db, owner, room_id)
                expected = body.get("revision")
                if expected is not None and expected != room["revision"]:
                    raise ValueError("聊天室设置已更新，请刷新后重试。")
                if room["archived"] and operation != "update":
                    raise ValueError("请先恢复该聊天室。")
                if operation == "add_member":
                    self._add_member(db, room_id, *prepared_members[0])
                elif operation == "remove_member":
                    member = self._member(db, room_id, body["member_id"])
                    db.execute(
                        "UPDATE members SET removed=1 WHERE id=?", (member["id"],)
                    )
                    db.execute(
                        "UPDATE deliveries SET status='cancelled',payload='{}',updated=? WHERE member_id=? AND status IN ('queued','preparing')",
                        (time.time(), member["id"]),
                    )
                    if room["default_member"] == member["id"]:
                        next_member = db.execute(
                            "SELECT id FROM members WHERE room_id=? AND removed=0 ORDER BY created LIMIT 1",
                            (room_id,),
                        ).fetchone()
                        db.execute(
                            "UPDATE rooms SET default_member=? WHERE id=?",
                            (next_member[0] if next_member else None, room_id),
                        )
                elif operation == "restore_member":
                    member = self._member(db, room_id, body["member_id"])
                    if member["removed"]:
                        if db.execute(
                            "SELECT count(*) FROM members WHERE room_id=? AND removed=0",
                            (room_id,),
                        ).fetchone()[0] >= 16:
                            raise ValueError("每个聊天室最多添加 16 位参与者。")
                        db.execute("UPDATE members SET removed=0 WHERE id=?", (member["id"],))
                    if room["default_member"] is None:
                        db.execute(
                            "UPDATE rooms SET default_member=? WHERE id=?", (member["id"], room_id)
                        )
                elif operation == "rename_member":
                    member = self._member(db, room_id, body["member_id"])
                    name = body["name"].strip()
                    if not name or len(name) > 80:
                        raise ValueError("会话名称须为 1 至 80 个字符。")
                    spec = json.loads(member["spec"])
                    spec["name"] = name
                    db.execute(
                        "UPDATE members SET spec=?,pending_title=? WHERE id=?",
                        (
                            encoded(spec),
                            None if native_title_confirmed else name,
                            member["id"],
                        ),
                    )
                elif operation == "update":
                    if body.get("name") is not None:
                        db.execute(
                            "UPDATE rooms SET name=? WHERE id=?",
                            (body["name"].strip(), room_id),
                        )
                    if body.get("default_member_id"):
                        member = self._member(db, room_id, body["default_member_id"])
                        if member["removed"]:
                            raise ValueError("请选择聊天室中的参与者。")
                        db.execute(
                            "UPDATE rooms SET default_member=? WHERE id=?",
                            (member["id"], room_id),
                        )
                    if body.get("archived") is not None:
                        db.execute(
                            "UPDATE rooms SET archived=? WHERE id=?",
                            (int(body["archived"]), room_id),
                        )
                elif operation == "dismiss":
                    delivery = db.execute(
                        "SELECT id FROM deliveries WHERE id=? AND room_id=? AND status='unknown'",
                        (body["delivery_id"], room_id),
                    ).fetchone()
                    if not delivery:
                        raise ValueError("这条输入的状态已更新，请刷新后核对。")
                    db.execute(
                        "UPDATE deliveries SET status='cancelled',error=NULL,updated=? WHERE id=?",
                        (time.time(), delivery["id"]),
                    )
                elif operation == "send":
                    previous_message = db.execute(
                        "SELECT room_id,input_fingerprint FROM messages WHERE id=?",
                        (body["message_id"],),
                    ).fetchone()
                    if previous_message:
                        if (
                            previous_message["room_id"] != room_id
                            or previous_message["input_fingerprint"] != fingerprint
                        ):
                            raise ValueError(
                                "消息编号与先前的内容不一致，请先核对已发送记录。"
                            )
                        return room_id
                    targets = list(
                        dict.fromkeys(body.get("targets") or [room["default_member"]])
                    )
                    if not targets or targets == [None]:
                        raise ValueError("请先添加参与者。")
                    selected = [self._member(db, room_id, target) for target in targets]
                    if any(member["removed"] for member in selected):
                        raise ValueError("所选参与者已经退出聊天。")
                    question_row = None
                    if body.get("ask_id"):
                        question_row = db.execute(
                            "SELECT * FROM messages WHERE id=? AND room_id=? AND member_id=?",
                            (body["ask_id"], room_id, targets[0]),
                        ).fetchone()
                        question = json.loads(question_row["question"]) if question_row and question_row["question"] else None
                        if (len(targets) != 1 or not question or question["kind"] != "async"
                                or not match_async_answer(question, body.get("prompt") or "")):
                            raise ValueError("回答与当前成员的问题不匹配。")
                        previous_answer = db.execute(
                            "SELECT status FROM deliveries WHERE id=?", (question.get("answer_delivery"),)
                        ).fetchone()
                        retryable = previous_answer and previous_answer["status"] in {"failed", "cancelled"}
                        if question["status"] == "answered" and not retryable:
                            raise ValueError("该问题已经回答。")
                        if previous_answer and previous_answer["status"] not in {"failed", "cancelled"}:
                            raise ValueError("先前的回答仍在确认送达，请稍后查看会话。")
                    active_count = db.execute(
                        "SELECT count(*) FROM deliveries WHERE status IN ('queued','preparing','submitting','accepted','running','stopping','unknown')"
                    ).fetchone()[0]
                    if active_count + len(targets) > 128:
                        raise ValueError("待处理消息较多，请稍后重试。")
                    payload = {"images": body.get("images"), "files": body.get("files")}
                    retained = db.execute(
                        "SELECT coalesce(sum(length(payload)),0) FROM messages"
                    ).fetchone()[0]
                    if retained + len(encoded(payload).encode()) > 64 * 1024 * 1024:
                        raise ValueError("待处理附件较多，请等现有消息完成后重试。")
                    seq = self._message(
                        db,
                        room_id,
                        body["message_id"],
                        "user",
                        body.get("prompt") or "",
                        targets=targets,
                        payload=payload,
                        source=f"async-answer:{targets[0]}:{body['message_id']}" if question_row else None,
                        reply_to=[question_row["id"]] if question_row else [],
                    )
                    db.execute(
                        "UPDATE messages SET input_fingerprint=? WHERE id=?",
                        (fingerprint, body["message_id"]),
                    )
                    for member in selected:
                        delivery_id = str(uuid4())
                        db.execute(
                            "INSERT INTO deliveries(id,room_id,message_id,member_id,status,context_before,context_through,context_version,payload,created,updated) "
                            "VALUES(?,?,?,?,'queued',?,?,?,?,?,?)",
                            (
                                delivery_id,
                                room_id,
                                body["message_id"],
                                member["id"],
                                member["synced_seq"],
                                seq,
                                room["context_version"],
                                "{}",
                                time.time(),
                                time.time(),
                            ),
                        )
                        if question_row:
                            question.update(status="submitting", answer_delivery=delivery_id)
                            db.execute("UPDATE messages SET question=? WHERE id=?",
                                       (encoded(question), question_row["id"]))
                else:
                    raise ValueError("未知聊天室操作。")
                db.execute(
                    "UPDATE rooms SET revision=revision+1,updated=? WHERE id=?",
                    (time.time(), room_id),
                )
            self._release_payloads(db)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (owner, request_id, fingerprint, room_id, time.time()),
            )
            return room_id

    def rooms(self, owner):
        with self.db() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,default_member,revision,updated,archived FROM rooms WHERE owner=? ORDER BY archived,updated DESC LIMIT 200",
                    (owner,),
                )
            ]

    def bound_sessions(self, account_key):
        with self.db() as db:
            return {
                row[0] for row in db.execute(
                    "SELECT native_sid FROM members WHERE account_key=? AND native_sid IS NOT NULL",
                    (account_key,),
                )
            }

    def rename_target(self, owner, request_id, body):
        """Validate ownership/revision before writing the native session title."""
        fingerprint = hashlib.sha256(encoded(["rename_member", body]).encode()).hexdigest()
        with self.db() as db:
            prior = db.execute("SELECT * FROM receipts WHERE owner=? AND request_id=?",
                               (owner, request_id)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("该请求编号已用于其他操作，请刷新后重试。")
                return None
            room = self._room(db, owner, body["room_id"])
            if room["archived"]:
                raise ValueError("请先恢复该聊天室。")
            if body.get("revision") is not None and body["revision"] != room["revision"]:
                raise ValueError("聊天室设置已更新，请刷新后重试。")
            name = body["name"].strip()
            if not name or len(name) > 80:
                raise ValueError("会话名称须为 1 至 80 个字符。")
            return self.member(self._member(db, body["room_id"], body["member_id"]))

    def sync_native_title(self, member_id, title, *, clear_pending=False):
        """Native names are display projections, independent of room settings revisions."""
        name = title.strip()[:80]
        if not name:
            return
        with self.db() as db:
            row = db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if row is None:
                return
            spec = json.loads(row["spec"])
            if spec["name"] == name and (not clear_pending or row["pending_title"] is None):
                return
            spec["name"] = name
            db.execute("UPDATE members SET spec=?,pending_title=? WHERE id=?",
                       (encoded(spec), None if clear_pending else row["pending_title"], member_id))

    @staticmethod
    def member(row):
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["name"] = result["spec"]["name"]
        return result

    @staticmethod
    def message(row):
        result = dict(row)
        result["targets"] = json.loads(result["targets"])
        result["reply_to"] = json.loads(result["reply_to"])
        result["attachments"] = json.loads(result["attachments"])
        question = result.pop("question", None)
        if question is not None:
            result["question"] = json.loads(question)
        result.pop("payload", None)
        return result

    def payload(self, message_id):
        with self.db() as db:
            return json.loads(
                db.execute(
                    "SELECT payload FROM messages WHERE id=?", (message_id,)
                ).fetchone()[0]
            )

    @staticmethod
    def _release_payloads(db):
        db.execute(
            "UPDATE messages SET payload='{}' WHERE payload!='{}' AND NOT EXISTS "
            "(SELECT 1 FROM deliveries WHERE message_id=messages.id AND status IN "
            "('queued','preparing','submitting','accepted','running','stopping','unknown'))"
        )

    def room(self, owner, room_id):
        with self.db() as db:
            room = dict(self._room(db, owner, room_id))
            room["members"] = [
                self.member(row)
                for row in db.execute(
                    "SELECT * FROM members WHERE room_id=? ORDER BY created", (room_id,)
                )
            ]
            return room

    def messages(self, owner, room_id, *, before=None, limit=40):
        with self.db() as db:
            self._room(db, owner, room_id)
            columns = _MESSAGE_COLUMNS.replace(
                ",text,", ",substr(text,1,65537) AS text,"
            )
            rows = db.execute(
                f"SELECT {columns} FROM messages WHERE room_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                (room_id, before or 2**62, limit),
            ).fetchall()
            messages = [self.message(row) for row in reversed(rows)]
            for message in messages:
                question = message.get("question")
                if question and question.get("answer_delivery"):
                    delivery = db.execute("SELECT status,error FROM deliveries WHERE id=?",
                                          (question["answer_delivery"],)).fetchone()
                    if delivery and delivery["status"] in {"failed", "cancelled"}:
                        question["status"] = "pending"
                        question["error"] = delivery["error"] or "回答未送达，请重试。"
            return messages

    def question_states(self, owner, room_id, message_ids):
        """Return receipts for questions referenced outside the visible page."""
        identities = list(dict.fromkeys(message_ids))[:128]
        if not identities:
            return {}
        with self.db() as db:
            self._room(db, owner, room_id)
            rows = db.execute(
                "SELECT id,member_id,question FROM messages WHERE room_id=? "
                "AND question IS NOT NULL AND id IN ("
                + ",".join("?" for _ in identities) + ")",
                (room_id, *identities),
            )
            states = {}
            for row in rows:
                question = json.loads(row["question"])
                state = {key: question[key] for key in ("kind", "ask_id", "status")}
                state["member_id"] = row["member_id"]
                if question.get("answer_delivery"):
                    delivery = db.execute(
                        "SELECT status,error FROM deliveries WHERE id=?",
                        (question["answer_delivery"],),
                    ).fetchone()
                    if delivery and delivery["status"] in {"failed", "cancelled"}:
                        state.update(status="pending", error=delivery["error"] or "回答未送达，请重试。")
                states[row["id"]] = state
            return states

    def context(self, delivery):
        with self.db() as db:
            rows = db.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE room_id=? AND seq>? AND seq<=? ORDER BY seq",
                (
                    delivery["room_id"],
                    delivery["context_before"],
                    delivery["context_through"],
                ),
            )
            messages, total = [], 0
            for row in rows:
                message = self.message(row)
                if (
                    message["role"] == "assistant"
                    and message["member_id"] == delivery["member_id"]
                    and delivery["context_before"] > 0
                ):
                    continue
                if message["role"] == "assistant":
                    version = db.execute(
                        "SELECT text FROM message_versions WHERE message_id=? AND version<=? ORDER BY version DESC LIMIT 1",
                        (message["id"], delivery["context_version"]),
                    ).fetchone()
                    if version is None:
                        continue
                    message["text"] = version[0]
                total += len(message["text"].encode("utf-8"))
                if total > MAX_ROOM_CONTEXT:
                    raise ValueError(
                        "公开讨论超过本次输入预算，请整理讨论后开启新聊天。"
                    )
                messages.append(message)
            current = db.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id=?",
                (delivery["message_id"],),
            ).fetchone()
            return messages, self.message(current)

    def deliveries(self, room_id=None, *, active=False):
        with self.db() as db:
            clauses, args = [], []
            if room_id:
                clauses.append("room_id=?")
                args.append(room_id)
            if active:
                clauses.append(
                    "status IN ('queued','preparing','submitting','accepted','running','stopping','unknown')"
                )
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute(
                "SELECT * FROM deliveries"
                + where
                + " ORDER BY created DESC,rowid DESC LIMIT 256",
                args,
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def delivery(self, delivery_id):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            return dict(row) if row else None

    def member_by_id(self, member_id):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM members WHERE id=?", (member_id,)
            ).fetchone()
            return self.member(row) if row else None

    def owner(self, room_id):
        with self.db() as db:
            return db.execute(
                "SELECT owner FROM rooms WHERE id=?", (room_id,)
            ).fetchone()[0]

    def bound_members(self):
        with self.db() as db:
            return [
                self.member(row)
                for row in db.execute("SELECT * FROM members WHERE sid IS NOT NULL")
            ]

    def bind(self, member_id, sid, native_sid, cwd):
        with self.db() as db:
            db.execute(
                "UPDATE members SET sid=?,native_sid=?,cwd=? WHERE id=?",
                (sid, native_sid, cwd, member_id),
            )
            db.execute(
                "UPDATE deliveries SET sid=? WHERE member_id=? AND status IN ('queued','preparing','submitting','accepted','running','unknown')",
                (sid, member_id),
            )

    def set_delivery(
        self, delivery_id, status, *, mode=None, native_turn=None, error=None
    ):
        with self.db() as db:
            db.execute(
                "UPDATE deliveries SET status=?,mode=coalesce(?,mode),native_turn=coalesce(?,native_turn),error=?,updated=? WHERE id=? AND status NOT IN ('succeeded','failed','cancelled')",
                (status, mode, native_turn, error, time.time(), delivery_id),
            )
            if status not in ACTIVE_DELIVERIES | {"unknown"}:
                db.execute(
                    "UPDATE deliveries SET payload='{}' WHERE id=?", (delivery_id,)
                )
                self._release_payloads(db)

    def claim(self, delivery_id):
        with self.db() as db:
            changed = db.execute(
                "UPDATE deliveries SET status='preparing',updated=? WHERE id=? AND status='queued'",
                (time.time(), delivery_id),
            ).rowcount
            return bool(changed)

    def can_submit(self, delivery_id):
        with self.db() as db:
            row = db.execute(
                "SELECT status FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row and row[0] == "stopping":
                db.execute(
                    "UPDATE deliveries SET status='cancelled',updated=? WHERE id=?",
                    (time.time(), delivery_id),
                )
                self._release_payloads(db)
                return False
            return bool(row and row[0] == "submitting")

    def expect_input(self, delivery_id, native_input_id):
        with self.db() as db:
            db.execute(
                "UPDATE deliveries SET native_input_id=? WHERE id=?",
                (native_input_id, delivery_id),
            )

    def submit(self, delivery_id, mode):
        """Freeze incremental context only after a cancellable prepare completes."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row is None or row["status"] != "preparing":
                return None
            member = db.execute(
                "SELECT * FROM members WHERE id=?", (row["member_id"],)
            ).fetchone()
            if member["removed"]:
                return None
            message = db.execute("SELECT source FROM messages WHERE id=?", (row["message_id"],)).fetchone()
            native_answer = bool(message["source"] and message["source"].startswith("async-answer:"))
            through = member["synced_seq"] if native_answer else row["context_through"]
            before = through if native_answer else min(member["synced_seq"], through - 1)
            db.execute(
                "UPDATE deliveries SET status='submitting',context_before=?,context_through=?,mode=?,error=NULL,updated=? WHERE id=?",
                (before, through, mode, time.time(), delivery_id),
            )
            return dict(
                db.execute(
                    "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
                ).fetchone()
            )

    def consume(self, delivery_id, native_turn=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if row and row["status"] in ACTIVE_DELIVERIES | {"unknown"}:
                db.execute(
                    "UPDATE deliveries SET status=CASE WHEN status='stopping' THEN status ELSE 'running' END,"
                    "native_turn=coalesce(?,native_turn),error=NULL,updated=? WHERE id=?",
                    (native_turn, time.time(), delivery_id),
                )
                db.execute(
                    "UPDATE members SET synced_seq=max(synced_seq,?),source_delivery=? WHERE id=?",
                    (row["context_through"], row["id"], row["member_id"]),
                )
                message = db.execute("SELECT source,reply_to,text FROM messages WHERE id=?",
                                     (row["message_id"],)).fetchone()
                if message["source"] and message["source"].startswith("async-answer:"):
                    question_id = json.loads(message["reply_to"])[0]
                    question_row = db.execute("SELECT question FROM messages WHERE id=?", (question_id,)).fetchone()
                    question = json.loads(question_row["question"])
                    answers = match_async_answer(question, message["text"])
                    if answers:
                        question.update(status="answered", answers=[item["answer"] for item in answers])
                        db.execute("UPDATE messages SET question=? WHERE id=?", (encoded(question), question_id))
                        return question["ask_id"]

    def resume_source(self, member_id, identities=(), *, autonomous=False, native_turn=None):
        with self.db() as db:
            row = db.execute(
                "SELECT deliveries.* FROM members JOIN deliveries ON deliveries.id=members.source_delivery WHERE members.id=?",
                (member_id,),
            ).fetchone()
            if (
                row
                and row["status"] == "succeeded"
                and (
                    autonomous
                    or {row["id"], row["native_turn"]} & set(filter(None, identities))
                )
            ):
                db.execute(
                    "UPDATE deliveries SET status='running',native_turn=coalesce(?,native_turn),updated=? WHERE id=?",
                    (native_turn, time.time(), row["id"]),
                )

    def clear_source(self, member_id):
        with self.db() as db:
            db.execute(
                "UPDATE members SET source_delivery=NULL WHERE id=?", (member_id,)
            )

    def publish(self, member_id, source, text, reply_to, *, question=None, new_only=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT room_id FROM members WHERE id=?", (member_id,)
            ).fetchone()
            if not row:
                return
            previous = db.execute(
                "SELECT id,text,question FROM messages WHERE source=?", (source,)
            ).fetchone()
            if previous:
                if new_only:
                    return
                if question is not None and previous["question"]:
                    old_question = json.loads(previous["question"])
                    for field in ("status", "answers", "answer_delivery"):
                        if field in old_question:
                            question[field] = old_question[field]
                if previous["text"] == text and (
                    question is None or previous["question"] == encoded(question)
                ):
                    return
                message_id = previous["id"]
                db.execute(
                    "UPDATE messages SET text=?,reply_to=?,question=coalesce(?,question) WHERE source=?",
                    (text, encoded(reply_to), encoded(question) if question is not None else None, source),
                )
            else:
                message_id = str(uuid4())
                self._message(
                    db,
                    row["room_id"],
                    message_id,
                    "assistant",
                    text,
                    member_id=member_id,
                    source=source,
                    reply_to=reply_to,
                    question=question,
                )
            version = db.execute(
                "SELECT context_version+1 FROM rooms WHERE id=?", (row["room_id"],)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO message_versions VALUES(?,?,?)",
                (message_id, version, text),
            )
            db.execute(
                "UPDATE rooms SET updated=?,context_version=? WHERE id=?",
                (time.time(), version, row["room_id"]),
            )

    def record_native_question(self, member_id, event):
        question = native_question(event)
        source = f"question:{member_id}:{event.ask_id}"
        self.publish(member_id, source, question_text(question["questions"]), [],
                     question=question, new_only=True)

    def record_question_answer(self, member_id, event, answer):
        self.record_native_question(member_id, event)
        answers = answer if event.questions else [answer]
        source = f"question:{member_id}:{event.ask_id}"
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM messages WHERE source=?", (source,)).fetchone()
            if row is None:
                return
            question = json.loads(row["question"])
            public_answers = ["已提交私密回答" if page.get("secret") else value
                              for page, value in zip(question["questions"], answers, strict=True)]
            if question["status"] == "answered":
                if question.get("answers") != public_answers:
                    raise ValueError("该问题已经收到其他回答。")
                return
            self._message(db, row["room_id"], str(uuid4()), "user",
                          answer_text(question["questions"], public_answers),
                          source=f"question-answer:{row['id']}", reply_to=[row["id"]])
            question.update(status="answered", answers=public_answers)
            db.execute("UPDATE messages SET question=? WHERE id=?", (encoded(question), row["id"]))
            db.execute("UPDATE rooms SET updated=? WHERE id=?", (time.time(), row["room_id"]))

    def close_question(self, member_id, ask_id, reason):
        if reason == "answered":
            return
        with self.db() as db:
            source = f"question:{member_id}:{ask_id}"
            row = db.execute("SELECT id,question FROM messages WHERE source=?", (source,)).fetchone()
            if row is None:
                return
            question = json.loads(row["question"])
            if question["status"] != "pending":
                return
            question["status"] = reason
            db.execute("UPDATE messages SET question=? WHERE id=?", (encoded(question), row["id"]))

    def record_async_answer(self, member_id, prompt, native_input_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            candidates = []
            for row in db.execute(
                "SELECT * FROM messages WHERE member_id=? AND question IS NOT NULL "
                "AND json_extract(question,'$.kind')='async'",
                (member_id,),
            ):
                question = json.loads(row["question"])
                answers = match_async_answer(question, prompt)
                if answers:
                    candidates.append((row, question, answers))
            if len(candidates) != 1:
                return
            row, question, answers = candidates[0]
            previous_delivery = db.execute(
                "SELECT id,status FROM deliveries WHERE id=?", (question.get("answer_delivery"),)
            ).fetchone()
            retryable = previous_delivery and previous_delivery["status"] in {"failed", "cancelled"}
            if question["status"] == "answered" and not retryable:
                return
            if previous_delivery and previous_delivery["status"] in {"queued", "preparing"}:
                db.execute(
                    "UPDATE deliveries SET status='cancelled',error=?,updated=? WHERE id=?",
                    ("问题已在原生会话回答。", time.time(), previous_delivery["id"]),
                )
                self._release_payloads(db)
            source = f"native-question-answer:{member_id}:{native_input_id}"
            if not db.execute("SELECT 1 FROM messages WHERE source=?", (source,)).fetchone():
                self._message(db, row["room_id"], str(uuid4()), "user", prompt,
                              source=source, reply_to=[row["id"]])
            question.update(status="answered", answers=[item["answer"] for item in answers], answer_delivery=None)
            db.execute("UPDATE messages SET question=? WHERE id=?", (encoded(question), row["id"]))
            db.execute("UPDATE rooms SET updated=? WHERE id=?", (time.time(), row["room_id"]))
            return question["ask_id"]

    def finish(self, member_id, status, error=None, *, delivery_ids=None):
        scope = ""
        args = [status, error, time.time(), member_id]
        if delivery_ids is not None:
            if not delivery_ids:
                return
            scope = " AND id IN (" + ",".join("?" for _ in delivery_ids) + ")"
            args.extend(delivery_ids)
        with self.db() as db:
            db.execute(
                "UPDATE deliveries SET status=?,error=?,payload='{}',updated=? WHERE member_id=? AND status IN ('running','stopping')"
                + scope,
                args,
            )
            self._release_payloads(db)
            if status in {"failed", "cancelled"}:
                db.execute(
                    "UPDATE members SET synced_seq=(SELECT coalesce(max(context_through),0) FROM deliveries "
                    "WHERE member_id=? AND status IN ('succeeded','running')) WHERE id=?",
                    (member_id, member_id),
                )

    def stop_pending(self, owner, request_id, room_id, member_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._room(db, owner, room_id)
            member = self.member(self._member(db, room_id, member_id))
            fingerprint = hashlib.sha256(
                encoded(["stop", room_id, member_id]).encode()
            ).hexdigest()
            prior = db.execute(
                "SELECT * FROM receipts WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("停止请求编号已用于另一项操作。")
                return member, False
            db.execute(
                "UPDATE deliveries SET status='cancelled',payload='{}',updated=? WHERE member_id=? AND status IN ('queued','preparing')",
                (time.time(), member_id),
            )
            db.execute(
                "UPDATE deliveries SET status='stopping',updated=? WHERE member_id=? AND status IN ('submitting','accepted','running')",
                (time.time(), member_id),
            )
            self._release_payloads(db)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (owner, request_id, fingerprint, room_id, time.time()),
            )
            return member, True

    def public_room(self, owner, room_id):
        room = self.room(owner, room_id)
        room.pop("owner")
        room.pop("context_version")
        for member in room["members"]:
            member.pop("account_key")
            member.pop("synced_seq")
            member.pop("source_delivery")
            member.pop("pending_title", None)
        return room

    def public_deliveries(self, room_id):
        rows = {row["id"]: row for row in self.deliveries(room_id)}
        rows.update({row["id"]: row for row in self.deliveries(room_id, active=True)})
        return [
            {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "payload",
                    "context_before",
                    "context_through",
                    "context_version",
                    "native_input_id",
                }
            }
            for item in sorted(rows.values(), key=lambda row: row["created"])
        ]
