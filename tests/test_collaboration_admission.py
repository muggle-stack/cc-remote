"""Stop acknowledgement follows the delivery worker's native admission."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import CollaborationCommand, Error, TurnEnd, TurnResult
from tests.test_collaboration import native_named_member, send, store_named_member
from tests.test_multisession import _mk_ctx


async def blocked_delivery(monkeypatch, engine, phase):
    machine, _transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    ctx.cwd = "/tmp"
    ctx.state = "idle"
    machine.sessions[sid] = ctx
    delivery = send(store, room_id)
    entered, release = asyncio.Event(), asyncio.Event()
    thread_release = threading.Event()
    timeline = []
    outcome = {}

    async def query(command):
        if phase == "native_admission":
            entered.set()
            await release.wait()
        if outcome.get("unknown"):
            return Error(code="mutation_unknown", message="native admission is uncertain")
        async with ctx.query_lock:
            timeline.append("query")
            ctx.state = "running"
            store.consume(command.msg_id, "native-turn")

    async def interrupt(_command):
        timeline.append("interrupt")
        await machine._emit(ctx, TurnEnd(
            turn_id="native-turn", checkpoint_id="native-turn" if engine == "claude" else None,
            result=TurnResult(subtype="interrupted", is_error=False, duration_ms=1),
        ))
        ctx.state = "idle"

    machine._handle_query = AsyncMock(side_effect=query)
    machine._handle_interrupt = AsyncMock(side_effect=interrupt)
    machine._dsh.connection = AsyncMock()
    machine._dsh.handle_interrupt = AsyncMock(side_effect=interrupt)
    if phase == "before_check":
        context = controller.context

        async def delayed_context(*args):
            entered.set()
            await release.wait()
            return await context(*args)

        monkeypatch.setattr(controller, "context", delayed_context)
    elif phase == "after_check":
        can_submit = store.can_submit
        loop = asyncio.get_running_loop()

        def approved_before_stop(delivery_id):
            approved = can_submit(delivery_id)
            loop.call_soon_threadsafe(entered.set)
            if not thread_release.wait(5):
                raise TimeoutError("native admission was not released")
            return approved

        monkeypatch.setattr(store, "can_submit", approved_before_stop)
    worker = asyncio.create_task(controller.deliver(delivery))
    controller.workers[member["id"]] = worker
    await asyncio.wait_for(entered.wait(), 5)

    async def stop():
        result = await controller.handle(CollaborationCommand(
            operation="stop", owner_id="owner", client_id="browser", cmd_id="stop",
            room_id=room_id, member_id=member["id"],
        ))
        timeline.append("ack")
        return result

    return SimpleNamespace(machine=machine, controller=controller, store=store, room_id=room_id,
                           member=member, delivery=delivery, worker=worker, stop=stop,
                           release=release, thread_release=thread_release, timeline=timeline, outcome=outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("phase", ["before_check", "after_check", "native_admission"])
async def test_stop_cannot_acknowledge_before_a_later_native_submission(monkeypatch, engine, phase):
    probe = await blocked_delivery(monkeypatch, engine, phase)
    stop = asyncio.create_task(probe.stop())
    try:
        try:
            await asyncio.wait_for(asyncio.shield(stop), 0.2)
        except TimeoutError:
            pass
        probe.release.set()
        probe.thread_release.set()
        await asyncio.wait_for(probe.worker, 5)
        result = await asyncio.wait_for(stop, 5)
        assert result.error is None
        assert probe.store.delivery(probe.delivery["id"])["status"] == "cancelled"
        if phase == "before_check":
            assert probe.timeline == ["ack"]
            probe.machine._handle_query.assert_not_awaited()
        else:
            assert probe.timeline == ["query", "interrupt", "ack"]
            probe.machine._handle_query.assert_awaited_once()
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_the_stop_request_keeps_native_admission_owned(monkeypatch):
    probe = await blocked_delivery(monkeypatch, "codex", "native_admission")
    stop = asyncio.create_task(probe.stop())
    try:
        await asyncio.sleep(0.1)
        assert not stop.done()
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        assert not probe.worker.cancelled()
        probe.release.set()
        await asyncio.wait_for(probe.worker, 5)
        result = await probe.controller.handle(CollaborationCommand(
            operation="stop", owner_id="owner", client_id="browser", cmd_id="retry-stop",
            room_id=probe.room_id, member_id=probe.member["id"],
        ))
        assert result.error is None
        assert probe.store.delivery(probe.delivery["id"])["status"] == "cancelled"
        probe.machine._handle_query.assert_awaited_once()
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, stop, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_admission_worker_cannot_produce_a_successful_stop_ack(monkeypatch):
    probe = await blocked_delivery(monkeypatch, "codex", "native_admission")
    stop = asyncio.create_task(probe.stop())
    try:
        await asyncio.sleep(0.1)
        probe.worker.cancel()
        result = await asyncio.wait_for(stop, 5)
        assert result.error
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, stop, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
async def test_uncertain_native_admission_cannot_produce_a_successful_stop_ack(monkeypatch, engine):
    probe = await blocked_delivery(monkeypatch, engine, "native_admission")
    probe.outcome["unknown"] = True
    stop = asyncio.create_task(probe.stop())
    try:
        await asyncio.sleep(0.1)
        probe.release.set()
        result = await asyncio.wait_for(stop, 5)
        assert result.error
        assert probe.store.delivery(probe.delivery["id"])["status"] == "stopping"
        assert (await probe.stop()).error
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, stop, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
async def test_stop_does_not_ack_an_uncertain_input_after_its_worker_retires(monkeypatch, engine):
    probe = await blocked_delivery(monkeypatch, engine, "native_admission")
    probe.outcome["unknown"] = True
    try:
        probe.release.set()
        await asyncio.wait_for(probe.worker, 5)
        probe.controller.workers.pop(probe.member["id"])
        result = await probe.stop()
        assert result.error
        assert probe.store.delivery(probe.delivery["id"])["status"] == "stopping"
        assert (await probe.stop()).error
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("queued", "cancelled"), ("accepted", "stopping"), ("unknown", "stopping")])
async def test_admission_outcome_preserves_a_persisted_stop_request(monkeypatch, status, expected):
    probe = await blocked_delivery(monkeypatch, "codex", "before_check")
    try:
        # Exercise the durable submitting-to-stop race independently of the
        # resident worker, including a late uncertain native acknowledgement.
        probe.store.set_delivery(probe.delivery["id"], "submitting")
        probe.store.stop_pending("owner", "stop", probe.room_id, probe.member["id"])
        probe.store.set_delivery(probe.delivery["id"], status)
        row = probe.store.delivery(probe.delivery["id"])
        assert row["status"] == expected
        if status == "unknown":
            assert row["error"]
    finally:
        probe.release.set()
        probe.thread_release.set()
        await asyncio.gather(probe.worker, return_exceptions=True)
