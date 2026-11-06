"""Deferred prompts remain owned when a healthy child's background work blocks reload."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ResultMessage

from cc_remote.protocol import Error, Query, UserMsg
from tests.test_claude_external import _ClaudeRunSdk
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.mark.parametrize("reason", ["reload", "effort"])
@pytest.mark.parametrize("space,delivery", [("code", "queue"), ("work", "replace")])
def test_queued_prompt_survives_background_reconnect_race(tmp_path, reason, space, delivery):
    async def run():
        class Sdk(_ClaudeRunSdk):
            def __init__(self):
                super().__init__()
                self.prompts = []

            async def query(self, prompt):
                self.prompts.append(prompt)
                await super().query(prompt)

        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.cwd = str(tmp_path.resolve())
        ctx.space = space
        ctx.sdk = Sdk()
        machine.sessions[ctx.key] = ctx
        machine._watch_session = AsyncMock()
        machine._adopt_claude_broker_handle = AsyncMock()
        machine._prime_claude_ownership = AsyncMock(return_value=False)
        machine._prepare_claude_context_before_query = AsyncMock(return_value=None)
        immediate = machine._handle_immediate_query
        attempts = 0

        async def race(context, cmd, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # Native state changes after the queue's initial idle check.
                context.claude_active_tasks.add("background-task")
                if reason == "reload":
                    context.needs_reload = True
                else:
                    context.sdk.effort = "high"
            return await immediate(context, cmd, **kwargs)

        machine._handle_immediate_query = race
        queued = Query(sid="sid", prompt="keep this exact follow-up", msg_id="queued-human",
                       delivery=delivery, cmd_id="enqueue", client_id="browser",
                       files=[{"filename": "context.txt", "data": "cXVldWVkIGF0dGFjaG1lbnQ="}])
        expected = queued.model_dump()
        try:
            await machine._handle_query(queued)
            size = machine._queued_query_size(queued)
            async def settled_attempt():
                while not (attempts and ctx.turn_task is None and ctx.queued_query_starting_msg_id is None):
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(settled_attempt(), 1)
            assert [item.model_dump() for item in ctx.queued_queries] == [expected]
            assert ctx.queued_query_bytes == machine._queued_query_bytes == size
            assert machine._queued_query_count == 1
            assert ctx.sdk.queries == ctx.sdk.reconnects == 0
            assert not [event for event in transport.sent if isinstance(event, (UserMsg, Error))]
            for _ in range(50):
                await asyncio.sleep(0)
            assert attempts == 1, "do not spin through rejected turns while background work remains"

            # Native completion wakes the retained submission without browser input.
            ctx.claude_active_tasks.clear()
            await machine._on_claude_background_message(ctx, ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="sid"), "background-turn")
            drain = ctx.queued_query_drain_task
            if drain:
                await asyncio.wait_for(asyncio.shield(drain), 2)
            turn = ctx.turn_task
            if turn:
                await asyncio.wait_for(asyncio.shield(turn), 2)
            assert ctx.sdk.queries == ctx.sdk.reconnects == 1
            assert queued.prompt in json.dumps(ctx.sdk.prompts)
            assert ctx.queued_queries == []
            assert ctx.queued_query_bytes == machine._queued_query_bytes == machine._queued_query_count == 0
            users = [event for event in transport.sent if isinstance(event, UserMsg)]
            assert len(users) == 1 and users[0].msg_id == queued.msg_id
            assert users[0].files == [{"filename": "context.txt"}]
        finally:
            tasks = [task for task in (ctx.queued_query_drain_task, ctx.turn_task) if task and not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("release", ["effort-reverted", "reader-failed"])
def test_waiting_queue_rechecks_without_native_result_or_browser_wakeup(release):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = _ClaudeRunSdk()
        ctx.sdk.effort = "high"
        ctx.claude_active_tasks.add("background-task")
        machine.sessions[ctx.key] = ctx
        machine._watch_session = AsyncMock()
        machine._adopt_claude_broker_handle = AsyncMock()
        machine._prime_claude_ownership = AsyncMock(return_value=False)
        machine._prepare_claude_context_before_query = AsyncMock(return_value=None)
        queued = Query(sid="sid", prompt="held until usable", msg_id="waiting-human",
                       delivery="queue", cmd_id="enqueue", client_id="browser")
        await machine._handle_query(queued)
        drain = ctx.queued_query_drain_task
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert ctx.turn_task is None and ctx.sdk.queries == ctx.sdk.reconnects == 0
            assert ctx.queued_queries == [queued]
            if release == "effort-reverted":
                ctx.sdk.effort = ctx.sdk.applied_effort
            else:
                ctx.sdk.message_pump_failed = True
            # No wakeup or Result is injected. A failed generation or reverted
            # option must not wait forever behind a stale task projection.
            await asyncio.wait_for(asyncio.shield(drain), 3)
            turn = ctx.turn_task
            if turn:
                await asyncio.wait_for(asyncio.shield(turn), 1)
            assert ctx.sdk.queries == 1
            assert ctx.sdk.reconnects == (0 if release == "effort-reverted" else 1)
            assert ctx.queued_queries == []
            assert len([event for event in transport.sent if isinstance(event, UserMsg)]) == 1
        finally:
            for task in (drain, ctx.turn_task):
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
