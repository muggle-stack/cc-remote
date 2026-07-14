"""Live bridge regressions for Codex goal/automatic continuation turns."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cc_remote.protocol import (
    Delta, ProcessEvent, StateEvent, ToolDelta, ToolResult, ToolUse, TurnDiff,
    TurnEnd, TurnPlan, UserMsg,
)
from cc_remote.wrapper.codex_handle import (
    CodexHandle, CodexSpontaneousClosed, CodexSpontaneousOverflow,
)
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    tool_result_max = 8_000
    turn_reader_queue_cap = 4
    ws_max_size_bytes = 16 * 1024 * 1024


def _notification(method: str, turn_id: str, **params):
    return {
        "method": method,
        "params": {
            "threadId": "thread-spontaneous",
            "turnId": turn_id,
            **params,
        },
    }


def test_spontaneous_bridge_is_bounded_nonblocking_and_keeps_terminal_frame():
    async def run():
        lifecycle = []

        async def on_lifecycle(phase, turn_id):
            lifecycle.append((phase, turn_id))

        handle = CodexHandle(_Cfg(), turn_lifecycle_callback=on_lifecycle)
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/started", "auto-overflow",
            turn={"id": "auto-overflow"},
        )), timeout=0.1)

        # Charge one otherwise-small frame above the bridge's byte ceiling. The
        # stdout path must return immediately instead of waiting for a consumer.
        await asyncio.wait_for(handle._dispatch(_notification(
            "item/agentMessage/delta", "auto-overflow",
            itemId="answer", delta="not retained",
        ), raw_size=8 * 1024 * 1024), timeout=0.1)
        await asyncio.wait_for(handle._dispatch(_notification(
            "turn/completed", "auto-overflow",
            turn={"id": "auto-overflow", "status": "completed"},
        )), timeout=0.1)

        items = [item async for item in
                 handle.receive_spontaneous_response("auto-overflow")]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"
        assert all(not (isinstance(item, dict)
                        and item.get("method") == "item/agentMessage/delta")
                   for item in items)
        assert lifecycle == [
            ("started", "auto-overflow"),
            ("completed", "auto-overflow"),
        ]

    asyncio.run(run())


def test_stdout_reader_drains_burst_when_spontaneous_consumer_is_stalled():
    async def run():
        turn_id = "auto-burst"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            *[
                _notification("item/agentMessage/delta", turn_id,
                              itemId="answer", delta=str(index))
                for index in range(96)
            ],
            _notification("turn/completed", turn_id,
                          turn={"id": turn_id, "status": "completed"}),
        ]
        lines = [json.dumps(message).encode() + b"\n" for message in messages]
        lines.append(b"")

        class Stdout:
            reads = 0

            async def readline(self):
                self.reads += 1
                return lines.pop(0)

        stdout = Stdout()
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await asyncio.wait_for(handle._read_loop(
            SimpleNamespace(stdout=stdout), handle._generation), timeout=0.5)
        assert stdout.reads == len(messages) + 1

        items = [item async for item in
                 handle.receive_spontaneous_response(turn_id)]
        assert isinstance(items[0], CodexSpontaneousOverflow)
        assert items[-1]["method"] == "turn/completed"

    asyncio.run(run())


def test_spontaneous_bridge_closes_on_disconnect_without_raw_error_data():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        await handle._dispatch(_notification(
            "turn/started", "auto-closed", turn={"id": "auto-closed"},
        ))
        await handle.disconnect()
        items = [item async for item in
                 handle.receive_spontaneous_response("auto-closed")]
        assert items[0]["method"] == "turn/started"
        assert isinstance(items[-1], CodexSpontaneousClosed)

    asyncio.run(run())


def test_managed_turn_never_double_routes_into_spontaneous_bridge():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        handle.turn_active = True
        handle._turn_q = asyncio.Queue()
        await handle._dispatch(_notification(
            "turn/started", "managed-turn", turn={"id": "managed-turn"},
        ))
        delta = _notification(
            "item/agentMessage/delta", "managed-turn",
            itemId="answer", delta="managed",
        )
        await handle._dispatch(delta)
        assert handle._spontaneous_q is None
        assert handle._spontaneous_turn_id is None
        assert (await handle._turn_q.get())["method"] == "turn/started"
        assert await handle._turn_q.get() == delta

    asyncio.run(run())


def test_old_managed_consumer_cannot_clear_new_spontaneous_active_flag():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-spontaneous"
        old_queue = asyncio.Queue()
        handle._turn_q = old_queue
        handle.turn_active = False

        async def consume_old_queue():
            return [message async for message in handle.receive_response()]

        consumer = asyncio.create_task(consume_old_queue())
        await asyncio.sleep(0)
        await handle._dispatch(_notification(
            "turn/started", "auto-after-managed",
            turn={"id": "auto-after-managed"},
        ))
        old_queue.put_nowait(None)
        assert await consumer == []
        assert handle.turn_active is True
        assert handle._spontaneous_turn_id == "auto-after-managed"

    asyncio.run(run())


def test_machine_streams_rich_spontaneous_turn_and_unlocks_on_matching_terminal():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = (
            lambda phase, turn_id: machine._on_codex_turn_lifecycle(
                ctx, phase, turn_id))

        turn_id = "auto-rich"
        messages = [
            _notification("turn/started", turn_id, turn={"id": turn_id}),
            _notification("item/reasoning/summaryPartAdded", turn_id,
                          itemId="reasoning-1", summaryIndex=0),
            _notification("item/reasoning/summaryTextDelta", turn_id,
                          itemId="reasoning-1", summaryIndex=0,
                          delta="公开思考摘要"),
            _notification("turn/plan/updated", turn_id,
                          explanation="执行计划",
                          plan=[{"step": "检查", "status": "inProgress"}]),
            _notification("item/started", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "inProgress",
                "commandActions": [],
            }),
            _notification("item/commandExecution/outputDelta", turn_id,
                          itemId="command-1", delta="/repo\n"),
            _notification("item/completed", turn_id, item={
                "type": "commandExecution", "id": "command-1",
                "command": "pwd", "cwd": "/repo", "status": "completed",
                "commandActions": [], "aggregatedOutput": "/repo\n",
                "exitCode": 0, "durationMs": 4,
            }),
            _notification("turn/diff/updated", turn_id,
                          diff="@@ -1 +1 @@\n-old\n+new"),
            _notification("item/started", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "inProgress",
                "arguments": {"query": "sdk"},
            }),
            _notification("item/mcpToolCall/progress", turn_id,
                          itemId="mcp-1", message="50%"),
            _notification("item/completed", turn_id, item={
                "type": "mcpToolCall", "id": "mcp-1", "server": "docs",
                "tool": "lookup", "status": "completed",
                "arguments": {"query": "sdk"},
                "result": {"content": [{"type": "text", "text": "ok"}]},
                "durationMs": 5,
            }),
            _notification("item/started", turn_id, item={
                "type": "collabAgentToolCall", "id": "agent-1",
                "tool": "spawnAgent", "status": "inProgress",
                "senderThreadId": "thread-spontaneous",
                "receiverThreadIds": ["child-1"], "prompt": "inspect",
            }),
            _notification("hook/completed", turn_id, run={
                "id": "hook-1", "eventName": "preToolUse",
                "handlerType": "command", "status": "completed",
                "durationMs": 2,
            }),
            _notification("item/completed", turn_id, item={
                "type": "agentMessage", "id": "answer-1",
                "text": "最终答案", "phase": "final_answer",
            }),
            _notification("turn/completed", turn_id, turn={
                "id": turn_id, "status": "completed", "durationMs": 25,
            }),
        ]
        for message in messages:
            await handle._dispatch(message)

        task = ctx.codex_spontaneous_task
        assert task is not None
        await asyncio.wait_for(task, timeout=1)

        assert ctx.state == "idle"
        assert ctx.codex_spontaneous_turn_id is None
        anchors = [event for event in transport.sent if isinstance(event, UserMsg)]
        assert [(event.msg_id, event.prompt) for event in anchors] == [(turn_id, "")]
        assert [event.state for event in transport.sent
                if isinstance(event, StateEvent)] == ["running", "idle"]
        assert any(isinstance(event, Delta) and event.text == "最终答案"
                   for event in transport.sent)
        assert any(isinstance(event, TurnPlan) for event in transport.sent)
        assert any(isinstance(event, TurnDiff) for event in transport.sent)
        assert {event.category for event in transport.sent
                if isinstance(event, ToolUse)} == {"command", "mcp"}
        assert any(isinstance(event, ToolDelta) for event in transport.sent)
        assert len([event for event in transport.sent
                    if isinstance(event, ToolResult)]) == 2
        assert {event.kind for event in transport.sent
                if isinstance(event, ProcessEvent)} >= {"reasoning", "agent", "hook"}
        terminal = [event for event in transport.sent if isinstance(event, TurnEnd)]
        assert len(terminal) == 1
        assert terminal[0].turn_id == turn_id
        assert terminal[0].result.subtype == "success"

    asyncio.run(run())
