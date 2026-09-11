"""Queue ordering changes server ownership, never prompt identity or payload."""

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    Error, QueryQueueState, ReorderQueuedQueries, deserialize, serialize,
)
from tests.test_multisession import _mk_ctx, _mk_machine
from tests.test_query_queue import _deferred


def command(**updates):
    return ReorderQueuedQueries(**{
        "sid": "session-queue", "cmd_id": "move", "client_id": "editor",
        "expected": ["a", "b", "c"], "order": ["b", "a", "c"],
        **updates,
    })


def test_reorder_schema_is_bounded_unique_permutation():
    cmd = command()
    assert deserialize(serialize(cmd)) == cmd
    for update in (
        {"order": ["a", "a", "c"]}, {"order": ["a", "b"]},
        {"order": ["a", "b", "other"]}, {"expected": ["a", "a", "c"]},
        {"expected": [str(i) for i in range(1000)]},
    ):
        with pytest.raises(ValidationError):
            command(**update)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", [None, "stale", "starting", "missing"])
async def test_reorder_atomic_broadcast_and_replay(monkeypatch, rejection):
    machine, transport = _mk_machine()
    ctx = _mk_ctx("session-queue", "session-queue")
    machine.sessions[ctx.key] = ctx
    monkeypatch.setattr(machine, "_schedule_query_queue_drain", lambda _: None)
    queries = [_deferred(mid) for mid in "abc"]
    queries[1] = queries[1].model_copy(update={
        "images": [{"media_type": "image/png", "data": "aGVsbG8="}],
        "delivery": "replace",
    })
    ctx.queued_queries[:] = queries
    ctx.queued_query_errors["b"] = "retryable"
    ctx.queued_query_bytes = sum(machine._queued_query_size(q) for q in queries)
    machine._queued_query_count = 3
    machine._queued_query_bytes = ctx.queued_query_bytes
    size = ctx.queued_query_bytes
    if rejection == "starting":
        ctx.queued_query_starting_msg_id = "a"
    if rejection == "missing":
        machine.sessions.clear()
    cmd = command(expected=["b", "a", "c"] if rejection == "stale"
                  else ["a", "b", "c"])
    await machine._process_command(cmd)
    assert [q.msg_id for q in ctx.queued_queries] == (
        list("abc") if rejection else list("bac")
    )
    assert all(any(q is original for original in queries)
               for q in ctx.queued_queries)
    assert ctx.queued_query_errors == {"b": "retryable"}
    assert ctx.queued_query_bytes == machine._queued_query_bytes == size
    assert machine._queued_query_count == 3
    if rejection:
        assert any(isinstance(r, Error) and r.request_id == cmd.cmd_id
                   and r.to == "editor" for r in transport.sent)
    else:
        state = next(r for r in transport.sent if isinstance(r, QueryQueueState))
        assert [q.msg_id for q in state.items] == list("bac")
        assert not state.to  # Every connected Web/TUI gets the same order.
    transport.sent.clear()
    await machine._process_command(cmd)
    assert not any(isinstance(r, QueryQueueState) for r in transport.sent)
    assert [q.msg_id for q in ctx.queued_queries] == (
        list("abc") if rejection else list("bac")
    )


@pytest.mark.asyncio
async def test_reordered_queue_drains_without_client_after_current_turn():
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session-queue", "session-queue")
    ctx.state = "running"
    machine.sessions[ctx.key] = ctx
    finish = asyncio.Event()
    launched = []
    started = asyncio.Event()

    async def turn():
        await finish.wait()
        await machine._set_state(ctx, "idle")

    async def launch(context, query, *, launch_receipt=None):
        launched.append(query.msg_id)
        context.state = "running"
        launch_receipt.set_result(True)
        started.set()

    ctx.turn_task = asyncio.create_task(turn())
    machine._handle_immediate_query = launch
    try:
        for mid in "abc":
            await machine._process_command(_deferred(mid))
        await machine._process_command(command(order=list("cab")))
        assert not launched
        finish.set()
        await asyncio.wait_for(started.wait(), 2)
        assert launched == ["c"]
        assert [q.msg_id for q in ctx.queued_queries] == list("ab")
    finally:
        finish.set()
        await ctx.turn_task
        await machine._discard_query_queue(ctx)
