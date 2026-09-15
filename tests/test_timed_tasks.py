import asyncio
import json
import sqlite3
from uuid import uuid4

import pytest

from cc_remote import timed_tasks as timed
from cc_remote.protocol import ConversationTurn, SessionInfo, TimedMessage, UserMsg, deserialize, serialize


def task(tmp_path, monkeypatch, *, count=3):
    monkeypatch.setattr(timed.time, "time", lambda: 1000)
    home = tmp_path / "codex"
    home.mkdir()
    store = timed.TimedTaskStore(tmp_path / "state")
    sid = str(uuid4())
    task_id = store.create(str(home), sid, "每分钟测试", "测试", 60, 60, count)
    return store, str(home), sid, task_id


def test_receipts_are_account_session_and_message_scoped(tmp_path, monkeypatch):
    store, home, sid, task_id = task(tmp_path, monkeypatch)
    delivery = store.begin_delivery(task_id)
    store.accepted(task_id, delivery)
    source = store.source(home, sid, [delivery], bind="native-message")
    assert source == {"task_id": task_id, "title": "每分钟测试", "scheduled_at": 1060}
    assert store.source(home, sid, ["native-message"]) == source
    assert store.source(home, str(uuid4()), [delivery]) is None
    assert store.source(home + "-other", sid, [delivery]) is None
    # Identical text and an unrelated user message never inherit a timer badge.
    assert store.source(home, sid, ["测试", "manual-message"]) is None
    event = UserMsg(msg_id="native-message", prompt="测试", timed_task=TimedMessage(**source))
    assert deserialize(serialize(event)).timed_task == event.timed_task
    assert ConversationTurn(id="native-message", timedTask=source).timedTask.task_id == task_id
    assert SessionInfo(session_id=sid, timed_tasks=store.public_tasks(home, sid)).state is None


def test_completion_cancel_and_expired_lease_remove_glow_but_keep_receipt(tmp_path, monkeypatch):
    store, home, sid, task_id = task(tmp_path, monkeypatch, count=1)
    assert store.public_tasks(home, sid)[0]["next_message_at"] == 1060
    assert store.active(now=1091) == {}
    delivery = store.begin_delivery(task_id)
    store.accepted(task_id, delivery)
    store.accepted(task_id, delivery)  # Duplicate acknowledgment is harmless.
    assert store.get(task_id)["sent"] == 1
    assert store.get(task_id)["state"] == "completed"
    assert store.active() == {}
    assert store.source(home, sid, [delivery])
    second = store.create(home, sid, "Later", "hi", 60, 60, 1)
    store.finish(second, "cancelled")
    assert store.public_tasks(home, sid) == []
    with pytest.raises(ValueError):
        store.begin_delivery(second)


def test_ambiguous_delivery_cannot_be_retried_and_wakeup_does_not_burst(tmp_path, monkeypatch):
    store, home, sid, task_id = task(tmp_path, monkeypatch)
    delivery = store.begin_delivery(task_id)
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_delivery(task_id)
    monkeypatch.setattr(timed.time, "time", lambda: 1600)
    store.accepted(task_id, delivery)
    assert store.get(task_id)["next_at"] == 1660


def test_read_missing_store_has_no_side_effects_and_symlinks_are_rejected(tmp_path):
    store = timed.TimedTaskStore(tmp_path / "missing")
    assert store.active() == {}
    assert store.source("home", "sid", ["id"], bind="native") is None
    assert not store.path.parent.exists()
    store.path.parent.mkdir()
    store.path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        store.active()
    assert not (tmp_path / "elsewhere").exists()


def test_worker_marks_unknown_once_without_resending(tmp_path, monkeypatch):
    store, home, sid, task_id = task(tmp_path, monkeypatch)
    monkeypatch.setattr(timed.time, "time", lambda: 1061)
    calls = []
    async def rejected(task, delivery, on_accepted):
        calls.append(delivery)
        raise ConnectionResetError()
    monkeypatch.setattr(timed, "deliver", rejected)
    asyncio.run(timed.run_task(store, task_id))
    asyncio.run(timed.run_task(store, task_id))
    assert len(calls) == 1
    assert store.get(task_id)["state"] == "unknown"
    assert store.active() == {}


def test_queue_uses_existing_thread_exact_client_id_and_accepts_before_start(tmp_path, monkeypatch):
    import os
    import stat
    from types import SimpleNamespace
    store, home, sid, task_id = task(tmp_path, monkeypatch, count=1)
    socket_path = tmp_path / "codex" / "app-server-control" / "app-server-control.sock"
    original_lstat = timed.Path.lstat
    monkeypatch.setattr(timed.Path, "lstat", lambda path, *args, **kwargs:
                        SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid())
                        if path == socket_path else original_lstat(path, *args, **kwargs))
    delivery = store.begin_delivery(task_id)
    methods = []
    class WS:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, raw):
            self.message = json.loads(raw)
            methods.append(self.message["method"])
        async def recv(self):
            msg = self.message
            if msg["method"] == "thread/queue/start":
                assert store.get(task_id)["state"] == "completed"
                raise ConnectionResetError()
            if msg["method"] == "thread/queue/add":
                assert msg["params"]["threadId"] == sid
                assert msg["params"]["clientUserMessageId"] == delivery
                result = {"queuedSubmission": {"id": "queue-id", "clientUserMessageId": delivery}}
            else:
                result = {"thread": {"status": {"type": "idle"}}}
            return json.dumps({"id": msg["id"], "result": result})
    monkeypatch.setattr(timed, "unix_connect", lambda *a, **kw: WS())
    asyncio.run(timed.deliver(store.get(task_id), delivery, lambda: store.accepted(task_id, delivery)))
    assert methods == ["initialize", "initialized", "thread/read", "thread/queue/add",
                       "thread/read", "thread/queue/start"]
    assert store.get(task_id)["sent"] == 1


def test_history_overlay_uses_receipts_for_summary_and_full_pages(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cc_remote.protocol import History
    from cc_remote.wrapper.machine import WrapperMachine
    store, home, sid, task_id = task(tmp_path, monkeypatch)
    delivery = store.begin_delivery(task_id)
    store.accepted(task_id, delivery)
    machine = object.__new__(WrapperMachine)
    machine._timed_tasks = store
    machine._ctx_by_sid = lambda sid: None
    machine._codex_target = lambda sid: (SimpleNamespace(home=home), sid)
    machine._build_history_source = AsyncMock(return_value=History(
        session_id=sid, revision="r", detail="summary",
        turns=[ConversationTurn(id="native", clientMsgId=delivery, prompt="测试"),
               ConversationTurn(id="manual", prompt="测试")],
        events=[{"type": "user_msg", "msg_id": "native", "client_msg_id": delivery, "prompt": "测试"}],
    ))
    history = asyncio.run(machine._build_history(sid, detail="summary"))
    assert history.turns[0].timedTask.task_id == task_id
    assert history.turns[1].timedTask is None
    assert history.events[0]["timed_task"]["task_id"] == task_id
    assert machine._build_history_source.await_count == 1


def test_official_history_request_preserves_timed_message_receipts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cc_remote.protocol import History
    from cc_remote.wrapper.machine import WrapperMachine

    store, home, sid, task_id = task(tmp_path, monkeypatch)
    delivery = store.begin_delivery(task_id)
    store.accepted(task_id, delivery)
    machine = object.__new__(WrapperMachine)
    machine._timed_tasks = store
    machine._ctx_by_sid = lambda sid: None
    machine._codex_target = lambda sid: (SimpleNamespace(home=home), sid)
    machine._watch_session = lambda sid: None
    machine._watch = {sid: {"engine": "codex"}}
    machine._history_revision = lambda sid: "r"
    machine._history_continuity_revisions = {}
    machine._codex_rollout_history_active = lambda sid: False
    machine._codex_terminal_snapshot = AsyncMock(return_value=[])
    machine._build_official_codex_history = AsyncMock(return_value=History(
        session_id=sid, revision="r", detail="summary",
        turns=[ConversationTurn(id="native", clientMsgId=delivery, prompt="测试"),
               ConversationTurn(id="manual", prompt="测试")],
    ))
    machine._build_history_source = AsyncMock(side_effect=AssertionError("Must use official history"))
    history = asyncio.run(machine._build_requested_history(
        sid, before=None, limit=4, cwd=None, detail="summary"))
    assert history.turns[0].timedTask is not None
    assert history.turns[0].timedTask.task_id == task_id
    assert history.turns[1].timedTask is None
    machine._build_official_codex_history.assert_awaited_once()
    machine._build_history_source.assert_not_awaited()
