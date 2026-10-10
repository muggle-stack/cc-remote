"""Generic async task acceptance: local fake daemon, zero model calls."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from websockets.asyncio.server import unix_serve

from cc_remote import async_tasks as tasks
from cc_remote import task_rpc
from cc_remote.task_store import OUTPUT_BYTES, TaskStore, public_task, request_thread


@pytest.fixture
def store(tmp_path):
    home = tmp_path / "account"
    home.mkdir()
    return TaskStore(home)


def start(store, *, sid=None, code="print('done')", request_id=None, **kwargs):
    sid = sid or str(uuid4())
    key = store.create(sid, argv=[sys.executable, "-c", code], cwd=str(store.home),
                       title="Test task", request_id=request_id or str(uuid4()), **kwargs)
    return sid, key


@asynccontextmanager
async def daemon(store, *, delivery="ok", reject_exec=False, thread_state="idle", thread_path=None,
                 turn_status="completed"):
    root = store.home / "app-server-control"
    root.mkdir(exist_ok=True)
    socket = root / "app-server-control.sock"
    # AF_UNIX paths are limited to 104 bytes on macOS; pytest roots can be long.
    with tempfile.TemporaryDirectory(prefix="crt-", dir="/tmp") as short:
        actual = socket if len(os.fsencode(socket)) < 100 else Path(short) / "rpc.sock"
        if actual != socket:
            socket.symlink_to(actual)
        calls, receipts, commands = [], [], []
        outcomes = SimpleNamespace(status=turn_status, pages=None, reject=False, reads=[])

        async def handle(ws):
            jobs = set()
            processes = {}

            async def run_command(msg):
                p = msg["params"]
                commands.append(p)
                if reject_exec:
                    await ws.send(json.dumps({"id": msg["id"], "error": {"code": -32600}}))
                    return
                proc = await asyncio.create_subprocess_exec(*p["command"], cwd=p["cwd"],
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                processes[p["processId"]] = proc
                try:
                    while chunk := await proc.stdout.read(8192):
                        await ws.send(json.dumps({"method": "command/exec/outputDelta", "params": {
                            "processId": p["processId"], "deltaBase64": base64.b64encode(chunk).decode(),
                            "capReached": False, "stream": "stdout"}}))
                    code = await proc.wait()
                    await ws.send(json.dumps({"id": msg["id"], "result": {
                        "exitCode": code, "stdout": "", "stderr": ""}}))
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    method = msg.get("method")
                    if "id" not in msg:
                        continue
                    calls.append(method)
                    p = msg.get("params", {})
                    result = {}
                    if method == "thread/read":
                        result = {"thread": {"id": p["threadId"], "status": {"type": thread_state},
                                             "path": thread_path}}
                    elif method == "thread/resume":
                        result = {"thread": {"id": p["threadId"]}}
                    elif method == "thread/turns/list":
                        outcomes.reads.append(p)
                        if outcomes.reject:
                            await ws.send(json.dumps({"id": msg["id"], "error": {"code": -32601}}))
                            continue
                        result = (outcomes.pages[p.get("cursor")] if outcomes.pages is not None else
                                  {"data": [{"id": "native-turn", "status": outcomes.status}],
                                   "nextCursor": None})
                    elif method == "command/exec":
                        job = asyncio.create_task(run_command(msg))
                        jobs.add(job)
                        continue
                    elif method == "command/exec/terminate":
                        proc = processes.get(p["processId"])
                        if proc and proc.returncode is None:
                            proc.terminate()
                    elif method == "turn/start":
                        receipts.append(p)
                        if delivery == "lost":
                            await ws.close()
                            break
                        if delivery == "reject":
                            await ws.send(json.dumps({"id": msg["id"], "error": {"code": -32600}}))
                            continue
                        result = {"turn": {"id": "native-turn", "status": "inProgress"}}
                    await ws.send(json.dumps({"id": msg["id"], "result": result}))
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)

        # This fixture's short alias isn't a native Codex hashed alias. Inject
        # only its location; production alias validation is exercised separately.
        from websockets.asyncio.client import unix_connect
        original = tasks.task_rpc.connect

        @asynccontextmanager
        async def connect(home, on_output=None):
            assert home == store.home
            async with unix_connect(str(actual), uri="ws://localhost/rpc") as ws:
                client = task_rpc.NativeTasks(ws, on_output)
                try:
                    await client.rpc("initialize", {"clientInfo": {"name": "test"}})
                    await ws.send(json.dumps({"method": "initialized"}))
                    yield client
                finally:
                    await client.close()

        async with unix_serve(handle, str(actual)):
            actual.chmod(0o600)
            tasks.task_rpc.connect = connect
            try:
                yield SimpleNamespace(calls=calls, receipts=receipts, commands=commands, connect=connect,
                                      outcomes=outcomes)
            finally:
                tasks.task_rpc.connect = original
                socket.unlink(missing_ok=True)


def test_caller_metadata_and_owner_isolation(store, tmp_path, monkeypatch):
    sid, key = start(store)
    monkeypatch.setenv("CODEX_THREAD_ID", sid)
    assert request_thread({"threadId": sid, "progressToken": 1}) == sid
    assert request_thread({"x-codex-turn-metadata": {"thread_id": sid}}) == sid
    assert request_thread({"x-codex-turn-metadata": json.dumps({"thread_id": sid})}) == sid
    assert request_thread({"threadId": sid,
                           "x-codex-turn-metadata": json.dumps({"thread_id": sid})}) == sid
    for meta in (None, {}, {"thread_id": sid}, {"threadId": None}, {"threadId": 1},
                 {"threadId": "invalid"}, {"x-codex-turn-metadata": "not json"},
                 {"x-codex-turn-metadata": json.dumps({"thread_id": "invalid"})},
                 {"threadId": sid, "x-codex-turn-metadata": None},
                 {"threadId": sid, "x-codex-turn-metadata": {}},
                 {"threadId": sid, "x-codex-turn-metadata": {"thread_id": str(uuid4())}},
                 {"threadId": sid, "x-codex-turn-metadata": json.dumps({"thread_id": str(uuid4())})}):
        with pytest.raises(ValueError):
            request_thread(meta)
    other = str(uuid4())
    assert store.list_for(other) == []
    with pytest.raises(ValueError):
        store.get(key, other)
    with pytest.raises(ValueError):
        store.cancel(key, other)
    second_home = tmp_path / "other-account"
    second_home.mkdir()
    with pytest.raises(ValueError):
        TaskStore(second_home).get(key, sid)


def test_submission_idempotency_limits_and_private_state(store):
    sid, key = start(store, request_id="stable")
    assert start(store, sid=sid, request_id="stable")[1] == key
    with pytest.raises(ValueError, match="different task"):
        start(store, sid=sid, request_id="stable", code="print('other')")
    for _ in range(7):
        start(store)
    with pytest.raises(ValueError, match="eight"):
        start(store)
    assert store.root.stat().st_mode & 0o077 == 0
    assert store.path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("changes", [{"argv": []}, {"argv": ["bad\0command"]}, {"cwd": "relative"},
                                      {"timeout_seconds": True}, {"timeout_seconds": 86401}])
def test_validation_before_execution(store, changes):
    args = dict(argv=["echo", "hi"], cwd=str(store.home), title="test", request_id="r")
    args.update(changes)
    with pytest.raises(ValueError):
        store.create(str(uuid4()), **args)


@pytest.mark.parametrize("code,state", [("print('done')", "completed"),
                                         ("import sys; print('failed'); sys.exit(7)", "failed")])
def test_actual_command_then_native_tool_output(store, code, state):
    sid, key = start(store, code=code)

    async def run():
        async with daemon(store) as native:
            await tasks.process_task(store, key)
            result = store.get(key)
            assert result["state"] == state
            assert result["delivery"] == "delivered"
            assert native.receipts[0]["threadId"] == sid
            assert native.receipts[0]["input"] == []
            assert set(native.receipts[0]) == {"threadId", "input", "toolOutput"}
            output = native.receipts[0]["toolOutput"]
            assert output["namespace"] == "cc_remote_tasks"
            assert json.loads(output["output"])["task_id"] == key
            command = native.commands[0]
            assert command["cwd"] == str(store.home)
            assert not {"sandboxPolicy", "permissionProfile", "env", "disableTimeout"} & command.keys()
            await tasks.process_task(store, key)
            assert len(native.commands) == len(native.receipts) == 1
    asyncio.run(run())


def test_cancellation_stops_process_and_suppresses_wakeup(store, monkeypatch):
    monkeypatch.setattr(tasks, "POLL_SECONDS", .01)
    sid, key = start(store, code="import time; print('started', flush=True); time.sleep(60)")

    async def run():
        async with daemon(store) as native:
            job = asyncio.create_task(tasks.process_task(store, key))
            async with asyncio.timeout(5):
                while not store.get(key)["output"]:
                    await asyncio.sleep(.01)
            store.cancel(key, sid)
            await asyncio.wait_for(job, 5)
            assert store.get(key)["state"] == "cancelled"
            assert store.get(key)["delivery"] == "suppressed"
            assert "command/exec/terminate" in native.calls
            assert not native.receipts
    asyncio.run(run())


def test_timeout_output_bound_and_explicit_rejection(store, monkeypatch):
    monkeypatch.setattr(tasks, "POLL_SECONDS", .01)

    async def run():
        async with daemon(store):
            _, key = start(store, code="print('x' * 200000)")
            await tasks.execute(store, store.get(key))
            assert len(store.get(key)["output"]) == OUTPUT_BYTES
            assert store.get(key)["truncated"] == 1
            _, key = start(store, code="import time; time.sleep(60)", timeout_seconds=1)
            await asyncio.wait_for(tasks.process_task(store, key), 5)
            assert store.get(key)["state"] == "timed_out"
        async with daemon(store, reject_exec=True) as native:
            _, key = start(store)
            await tasks.process_task(store, key)
            assert store.get(key)["state"] == "failed"
            assert len(native.commands) == 1
    asyncio.run(run())


@pytest.mark.parametrize("exit_code,elapsed,cancel,expected", [
    (124, .9995, False, "timed_out"),
    (124, .2, False, "failed"),
    (7, .9995, False, "failed"),
    (0, .9995, False, "completed"),
    (124, .9995, True, "cancelled"),
])
def test_native_timeout_wins_before_local_deadline_poll(store, monkeypatch, exit_code, elapsed, cancel, expected):
    sid, key = start(store, timeout_seconds=1)
    task = store.get(key)
    task["created"] = 1000.0
    # Native timeoutMs rounds down to 999 ms; completion can arrive before
    # the wrapper's full one-second deadline and its next poll.
    now = SimpleNamespace(wall=1000.0001, monotonic=500.0)
    monkeypatch.setattr(tasks, "time", SimpleNamespace(time=lambda: now.wall, monotonic=lambda: now.monotonic))

    class Native:
        async def thread(self, thread_id):
            assert thread_id == sid

        async def rpc(self, method, params, *, timeout, before_send):
            assert method == "command/exec"
            before_send()
            assert params["timeoutMs"] == 999
            now.wall += elapsed
            now.monotonic += elapsed
            if cancel:
                store.cancel(key, sid)
            return {"exitCode": exit_code, "stdout": "", "stderr": ""}

    @asynccontextmanager
    async def connect(home, on_output):
        assert home == store.home
        yield Native()

    monkeypatch.setattr(task_rpc, "connect", connect)
    asyncio.run(tasks.execute(store, task))
    result = store.get(key)
    assert result["state"] == expected
    assert result["exit_code"] == exit_code
    if cancel:
        assert result["delivery"] == "suppressed"


@pytest.mark.parametrize("phase", ["connect", "thread", "schedule", "store"])
@pytest.mark.parametrize("delay", [.25, 1.25])
def test_execution_budget_is_rechecked_at_native_send(store, monkeypatch, phase, delay):
    marker = store.home / "must-not-start-after-deadline"
    sid, key = start(store, code=f"from pathlib import Path; Path({str(marker)!r}).touch()",
                     timeout_seconds=1)
    task = store.get(key)
    task["created"] = 1000.0
    clock = SimpleNamespace(wall=1000.0, monotonic=500.0)
    monkeypatch.setattr(tasks, "time", SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.monotonic))

    def advance(at):
        if phase == at:
            clock.wall += delay
            clock.monotonic += delay

    begin = store.begin_execution

    def delayed_begin(task_id):
        result = begin(task_id)
        advance("store")  # SQLite may wait for its write lock before returning.
        return result

    monkeypatch.setattr(store, "begin_execution", delayed_begin)

    async def run():
        async with daemon(store) as native:
            @asynccontextmanager
            async def delayed_connect(home, on_output):
                async with native.connect(home, on_output) as client:
                    advance("connect")  # Connection plus initialization budget.
                    original_thread, original_rpc = client.thread, client.rpc

                    async def thread(thread_id):
                        result = await original_thread(thread_id)
                        advance("thread")
                        return result

                    async def rpc(method, params, **kwargs):
                        if method == "command/exec":
                            advance("schedule")  # Between create_task and its first execution.
                        return await original_rpc(method, params, **kwargs)

                    client.thread, client.rpc = thread, rpc
                    yield client

            with monkeypatch.context() as patch:
                patch.setattr(task_rpc, "connect", delayed_connect)
                await tasks.execute(store, task)
            result = store.get(key, sid)
            if delay >= 1:
                assert native.commands == [], "expired tasks must not reach command/exec"
                assert not marker.exists(), "no side effects after pre-submission expiry"
                assert result["state"] == "timed_out" and result["exit_code"] is None
                assert result["delivery"] == "pending"
            else:
                assert len(native.commands) == 1 and marker.exists()
                assert native.commands[0]["timeoutMs"] == 750
                assert result["state"] == "completed"
    asyncio.run(run())


@pytest.mark.parametrize("mode,expected", [("lost", "unknown"), ("reject", "rejected")])
def test_completion_ack_loss_is_not_replayed(store, mode, expected):
    _, key = start(store)
    store.update(key, state="completed", output=b"retained result")

    async def run():
        async with daemon(store, delivery=mode) as native:
            await tasks.deliver(store, store.get(key))
            assert store.get(key)["delivery"] == expected
            store.recover()
            await tasks.deliver(store, store.get(key))
            assert len(native.receipts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("outcome,expected", [
    ("completed", "delivered"), ("failed", "failed"), ("interrupted", "failed"),
])
def test_callback_acceptance_is_not_success_and_outcome_survives_recovery(store, outcome, expected):
    sid, key = start(store)
    store.update(key, state="completed", output=b"result survives provider rejection")

    async def run():
        async with daemon(store, turn_status="inProgress") as native:
            await tasks.deliver(store, store.get(key))
            accepted = public_task(store.get(key))
            assert accepted["notification"] == "accepted"
            assert accepted["notification_turn_id"] == "native-turn"
            with pytest.raises(ValueError, match="cannot be recalled"):
                store.cancel(key, sid)
            await tasks.check_delivery(store, store.get(key))
            assert store.get(key)["delivery"] == "accepted"
            assert store.get(key)["next_attempt"] > tasks.time.time()
            # A new worker must read the accepted turn, never resend the result.
            reopened = TaskStore(store.home)
            reopened.recover()
            native.outcomes.status = outcome
            reopened.update(key, next_attempt=0)
            await asyncio.wait_for(tasks.run_worker(reopened), 5)
            receipt = public_task(reopened.get(key), output=True)
            assert receipt["state"] == "completed"
            assert receipt["notification"] == expected
            assert receipt["output"] == "result survives provider rejection"
            assert bool(receipt["notification_error"]) == (expected == "failed")
            assert len(native.receipts) == 1 and not native.commands
            assert native.calls.count("thread/resume") == 0
            assert all(p["threadId"] == sid and p["itemsView"] == "notLoaded"
                       for p in native.outcomes.reads)
    asyncio.run(run())


def test_callback_checks_exact_turn_across_pages_not_latest_success(store):
    _, key = start(store)
    store.update(key, state="completed")

    async def run():
        async with daemon(store) as native:
            await tasks.deliver(store, store.get(key))
            native.outcomes.pages = {
                None: {"data": [{"id": "newer-unrelated", "status": "completed"}], "nextCursor": "older"},
                "older": {"data": [{"id": "native-turn", "status": "failed"}], "nextCursor": None},
            }
            await tasks.process_task(store, key)
            assert store.get(key)["delivery"] == "failed"
            assert [p["cursor"] for p in native.outcomes.reads] == [None, "older"]
            assert len(native.receipts) == 1
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["history", "rpc", "socket", "malformed"])
def test_unconfirmed_callback_is_retained_with_bounded_tracking(store, missing):
    _, key = start(store)
    store.update(key, state="completed", output=b"keep me")

    async def run():
        async with daemon(store) as native:
            await tasks.deliver(store, store.get(key))
            if missing == "socket":
                return
            if missing == "rpc":
                native.outcomes.reject = True
            elif missing == "malformed":
                native.outcomes.status = {"unexpected": "completed"}
            else:
                # A repeating cursor must be bounded, not an infinite read loop.
                native.outcomes.pages = {
                    None: {"data": [{"id": "other", "status": "completed"}], "nextCursor": "same"},
                    "same": {"data": [], "nextCursor": "same"},
                }
            await tasks.process_task(store, key)
            assert store.get(key)["delivery"] == "accepted"
            assert len(native.outcomes.reads) <= 2
            assert len(native.receipts) == 1
        # Recover with no server at all: no successful outcome may be invented.

    asyncio.run(run())
    store.recover()
    assert store.has_pending()
    asyncio.run(tasks.process_task(store, key))
    assert store.get(key)["delivery"] == "accepted"
    store.update(key, delivery_accepted_at=tasks.time.time() - tasks.DELIVERY_TRACK_SECONDS - 1)
    asyncio.run(tasks.process_task(store, key))
    assert store.get(key)["delivery"] == "unknown"
    assert store.get(key)["output"] == b"keep me"
    assert not store.has_pending()


def test_callback_tracking_migrates_old_store_concurrently_without_reinterpreting_receipts(store):
    from concurrent.futures import ThreadPoolExecutor
    sid, key = start(store)
    store.update(key, state="completed", delivery="delivered", output=b"legacy receipt")
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN delivery_turn_id")
        db.execute("ALTER TABLE tasks DROP COLUMN delivery_accepted_at")
    assert store.activity_snapshot() == [], "read-only wrapper must support the old schema"
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: TaskStore(store.home).get(key, sid), range(4)))
    for row in rows:
        assert row["delivery"] == "delivered" and row["delivery_turn_id"] is None
        assert row["delivery_accepted_at"] is None and row["output"] == b"legacy receipt"
    assert not store.has_pending()


def test_pre_submission_disconnect_retains_pending_without_executing(store):
    _, key = start(store)
    asyncio.run(tasks.execute(store, store.get(key)))
    assert store.get(key)["state"] == "queued"
    assert store.get(key)["next_attempt"] > 0
    store.update(key, state="completed")
    asyncio.run(tasks.deliver(store, store.get(key)))
    assert store.get(key)["delivery"] == "pending"


def test_recovery_does_not_reexecute_running_or_resend_unknown(store):
    _, running = start(store)
    _, sending = start(store)
    store.update(running, state="running")
    store.update(sending, state="completed", delivery="sending")
    store.recover()
    assert store.get(running)["state"] == "interrupted"
    assert store.get(sending)["delivery"] == "unknown"

    async def run():
        async with daemon(store) as native:
            await tasks.run_worker(store)
            assert not native.commands
            assert len(native.receipts) == 1
    asyncio.run(run())


def test_cancel_before_delivery_wins_even_after_connect(store, monkeypatch):
    sid, key = start(store)
    store.update(key, state="completed")
    original = store.begin_delivery

    def cancel_then_send(task_id):
        store.cancel(task_id, sid)
        original(task_id)
    monkeypatch.setattr(store, "begin_delivery", cancel_then_send)

    async def run():
        async with daemon(store) as native:
            await tasks.deliver(store, store.get(key))
            assert not native.receipts
            assert store.get(key)["delivery"] == "suppressed"
    asyncio.run(run())


@pytest.mark.parametrize("archived", [True, False])
def test_cold_resume_only_for_active_account_transcript(store, archived):
    sid, key = start(store)
    folder = store.home / ("archived_sessions" if archived else "sessions")
    folder.mkdir()
    path = folder / f"{sid}.jsonl"
    path.write_text("")
    store.update(key, state="completed")

    async def run():
        async with daemon(store, thread_state="notLoaded", thread_path=str(path)) as native:
            await tasks.deliver(store, store.get(key))
            assert ("thread/resume" in native.calls) == (not archived)
            assert bool(native.receipts) == (not archived)
    asyncio.run(run())


def native_caller_metadata(sid, metadata_format):
    if metadata_format == "model_call":
        # Shape observed on a real model-driven Codex 0.159.2 MCP call, as
        # distinct from the app-server's direct mcpServer/tool/call route.
        return {"progressToken": 1, "threadId": sid, "sessionId": sid,
                "callId": "test-call", "x-codex-turn-metadata": {
                    "thread_id": sid, "session_id": sid, "turn_id": str(uuid4()),
                    "model": "test-model", "sandbox_mode": "read-only"}}
    if metadata_format == "app_server":
        return {"threadId": sid, "progressToken": 1}
    return {"x-codex-turn-metadata": json.dumps({"thread_id": sid})}


@pytest.mark.parametrize("metadata_format", ["app_server", "model_call", "legacy"])
def test_mcp_tools_bind_native_caller_and_reject_target_override(store, monkeypatch, metadata_format):
    from mcp.server.lowlevel.server import RequestContext, request_ctx
    from mcp.types import CallToolRequest, CallToolRequestParams, RequestParams

    monkeypatch.setattr(tasks, "ensure_worker", lambda store: None)
    sid = str(uuid4())
    server = tasks.make_server(store)

    async def call(name, args, thread=sid):
        raw = native_caller_metadata(thread, metadata_format)
        meta = RequestParams.Meta.model_validate(raw)
        token = request_ctx.set(RequestContext(request_id=1, meta=meta, session=None, lifespan_context=None))
        try:
            return (await server.request_handlers[CallToolRequest](CallToolRequest(
                params=CallToolRequestParams(name=name, arguments=args)))).root
        finally:
            request_ctx.reset(token)

    async def run():
        args = dict(argv=["echo", "hi"], cwd=str(store.home), title="MCP task", request_id="mcp-1")
        async with daemon(store):
            result = await call("task_start", args)
            assert not result.isError
            key = result.structuredContent["task_id"]
            assert (await call("task_start", args)).structuredContent["task_id"] == key
            assert (await call("task_start", {**args, "thread_id": str(uuid4())})).isError
            assert (await call("task_result", {"task_id": key}, str(uuid4()))).isError
            assert len((await call("task_status", {})).structuredContent["tasks"]) == 1
            assert not (await call("task_cancel", {"task_id": key})).isError
            assert store.get(key)["cancel_requested"] == 1
    asyncio.run(run())


def test_lifetime_lock_and_detached_worker_survive_mcp_parent(store, monkeypatch):
    fd = tasks.lock_worker(store)
    try:
        assert tasks.lock_worker(store) is None
        captured = []
        monkeypatch.setattr(tasks.subprocess, "Popen", lambda *a, **k: captured.append((a, k)))
        tasks.ensure_worker(store)
        assert captured == []
    finally:
        os.close(fd)
    tasks.ensure_worker(store)
    args, options = captured[0]
    assert options["start_new_session"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert options["pass_fds"]
    assert "cc_remote.async_tasks" in args[0]


def test_native_task_receipt_projection(store):
    from cc_remote.protocol import ProcessEvent
    from cc_remote.wrapper.codex_stream import CodexStreamTranslator
    _, key = start(store)
    store.update(key, state="completed", output=b"done")
    item = {"id": "receipt-native", "type": "functionCallOutput", "namespace": "cc_remote_tasks",
            "name": "task_result", "output": json.dumps(public_task(store.get(key), output=True))}
    translator = CodexStreamTranslator(8000)
    events = translator.feed({"method": "item/completed", "params": {"turnId": "turn-native", "item": item}})
    event = next(e for e in events if isinstance(e, ProcessEvent))
    assert event.kind == "task" and event.status == "succeeded" and event.detail == "done"
    assert event.item_id == "receipt-native" and event.turn_id == "turn-native"
    item["namespace"] = "unrelated"
    assert not CodexStreamTranslator(8000).feed({"method": "item/completed", "params": {"item": item}})


def test_receipt_retention_is_bounded_and_preserves_uncertain_tasks(store):
    _, uncertain = start(store)
    store.update(uncertain, state="completed", delivery="unknown")
    for _ in range(110):
        _, key = start(store)
        store.update(key, state="completed", delivery="delivered")
    with store.db() as db:
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 101
    assert store.get(uncertain)["delivery"] == "unknown"


@pytest.mark.parametrize("state", [[], {}, None, "not-a-terminal"])
def test_malformed_task_receipt_cannot_break_native_stream(state):
    from cc_remote.wrapper.codex_stream import CodexStreamTranslator
    item = {"id": "receipt", "type": "functionCallOutput", "namespace": "cc_remote_tasks",
            "name": "task_result", "output": json.dumps({"task_id": str(uuid4()), "state": state})}
    assert not CodexStreamTranslator(8000).feed({
        "method": "item/completed", "params": {"turnId": "turn", "item": item}})


def test_cli_tasks_dispatch_is_separate_from_updater(monkeypatch, store):
    from cc_remote.__main__ import main
    calls = []
    monkeypatch.setattr(tasks, "main", lambda args: calls.append(args) or 0)
    assert main(["tasks", "mcp", "--codex-home", str(store.home)]) == 0
    assert calls == [["mcp", "--codex-home", str(store.home)]]


@pytest.mark.parametrize("metadata_format", ["app_server", "model_call", "legacy"])
def test_real_stdio_mcp_protocol_and_native_call_metadata(store, metadata_format):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    sid, key = start(store)
    store.update(key, state="completed", delivery="delivered", output=b"native MCP transport")
    meta = native_caller_metadata(sid, metadata_format)

    async def run():
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "cc_remote.async_tasks", "mcp", "--codex-home", str(store.home)],
            cwd=str(Path(__file__).resolve().parents[1]))
        async with asyncio.timeout(10), stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {t.name for t in tools} == {"task_start", "task_status", "task_result", "task_cancel"}
                result = await session.call_tool("task_result", {"task_id": key}, meta=meta)
                assert not result.isError
                assert result.structuredContent["output"] == "native MCP transport"
                denied = await session.call_tool("task_result", {"task_id": key})
                assert denied.isError
    asyncio.run(run())


@pytest.mark.parametrize("alias", [False, True])
def test_production_socket_transport_uses_only_owned_existing_daemon(monkeypatch, alias):
    from cc_remote.wrapper import codex_daemon

    async def run():
        with tempfile.TemporaryDirectory(prefix="crt-wire-", dir="/tmp") as folder:
            store = TaskStore(Path(folder))
            socket = store.home / "app-server-control/app-server-control.sock"
            socket.parent.mkdir()
            protected = store.home / "p"
            protected.mkdir(mode=0o700)
            monkeypatch.setattr(codex_daemon, "_protected_socket_directory", lambda uid: protected)
            target = protected / hashlib.sha256(os.fsencode(socket)).hexdigest() if alias else socket
            if alias:
                socket.symlink_to(target)
            sid = str(uuid4())
            calls = []

            async def handle(ws):
                async for raw in ws:
                    msg = json.loads(raw)
                    calls.append(msg)
                    if "id" not in msg:
                        continue
                    result = {"thread": {"id": sid}} if msg["method"] == "thread/read" else {}
                    await ws.send(json.dumps({"id": msg["id"], "result": result}))

            with pytest.raises(FileNotFoundError):
                async with task_rpc.connect(store.home):
                    pass
            async with unix_serve(handle, str(target)):
                target.chmod(0o600)
                async with task_rpc.connect(store.home) as native:
                    assert (await native.thread(sid))["id"] == sid
                if alias:
                    socket.unlink()
                    socket.symlink_to(protected / "another-account")
                    with pytest.raises(ValueError, match="account address"):
                        async with task_rpc.connect(store.home):
                            pass
            assert [c["method"] for c in calls] == ["initialize", "initialized", "thread/read"]
            assert calls[0]["params"]["capabilities"]["experimentalApi"] is True
    asyncio.run(run())


def test_execution_cancellation_wins_at_send_boundary(store, monkeypatch):
    sid, key = start(store)
    original = store.begin_execution

    def cancel_first(task_id):
        store.cancel(task_id, sid)
        return original(task_id)
    monkeypatch.setattr(store, "begin_execution", cancel_first)

    async def run():
        async with daemon(store) as native:
            await tasks.execute(store, store.get(key))
            assert not native.commands
            assert store.get(key)["state"] == "cancelled"
    asyncio.run(run())


def test_official_history_keeps_task_receipt_and_native_identity(store):
    from cc_remote.wrapper.codex_history import _translate_turn
    # Public history and live events must project the exact same task receipt.
    _, key = start(store)
    store.update(key, state="failed", output=b"test failure", exit_code=2)
    item = {"id": "native-receipt", "type": "functionCallOutput", "namespace": "cc_remote_tasks",
            "name": "task_result", "output": json.dumps(public_task(store.get(key), output=True))}
    turn = {"id": "native-turn", "items": [item], "status": "completed", "itemsView": "full",
            "startedAt": 100, "completedAt": 102, "durationMs": 2000, "error": None}
    segments = _translate_turn(str(uuid4()), turn, tool_result_max=8000)
    events = [e for segment in segments for e in segment if e.get("type") == "process"]
    assert len(events) == 1
    assert events[0]["item_id"] == "native-receipt"
    assert events[0]["turn_id"] == "native-turn"
    assert events[0]["status"] == "failed" and events[0]["detail"] == "test failure"


def test_detached_task_finishes_and_notifies_after_real_mcp_disconnect():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        with tempfile.TemporaryDirectory(prefix="crt-e2e-", dir="/tmp") as folder:
            store = TaskStore(Path(folder))
            sid = str(uuid4())
            meta = {"x-codex-turn-metadata": json.dumps({"thread_id": sid})}
            async with daemon(store) as native:
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-m", "cc_remote.async_tasks", "mcp", "--codex-home", str(store.home)],
                    cwd=str(Path(__file__).resolve().parents[1]))
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("task_start", {
                            "argv": [sys.executable, "-c", "import time; time.sleep(.3); print('after disconnect')"],
                            "cwd": str(store.home), "title": "Detached task", "request_id": "e2e-1",
                        }, meta=meta)
                        assert not result.isError
                        key = result.structuredContent["task_id"]
                async with asyncio.timeout(10):
                    while store.get(key)["delivery"] != "delivered":
                        await asyncio.sleep(.02)
                    while True:
                        fd = tasks.lock_worker(store)
                        if fd is not None:
                            os.close(fd)
                            break
                        await asyncio.sleep(.02)
                assert store.get(key)["state"] == "completed"
                assert store.get(key)["output"] == b"after disconnect\n"
                assert len(native.commands) == len(native.receipts) == 1
                assert native.receipts[0]["threadId"] == sid
    asyncio.run(run())
