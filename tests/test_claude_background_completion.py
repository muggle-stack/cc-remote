"""Native task inputs can share a terminal without blocking the next prompt."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.wrapper.sdk import ClaudeBackgroundBoundary, ClaudeServiceReplayRequired, SdkHandle
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_service import environment, released
from tests.test_claude_steering import NativeClient, assistant, requesting, result, until, user


def task_input(uid, origin):
    return {**user(uid, "background task finished"), "origin": origin}


@pytest.mark.asyncio
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("start", [
    requesting(),
    {"type": "stream_event", "uuid": "request", "session_id": "native-session",
     "event": {"type": "message_start", "message": {"id": "answer", "content": []}}},
], ids=["request", "stream"])
async def test_activity_before_human_echo_settles_and_accepts_next_prompt(replay, start):
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        native = worker.client
        sdk = SdkHandle(WrapperConfig(claude_service_socket=client.connection.socket_path))
        sdk.refresh_goal = AsyncMock(return_value=None)
        sdk.applied_auto_compact_mode = sdk.auto_compact_mode
        sdk.applied_auto_compact_threshold_tokens = sdk.auto_compact_threshold_tokens
        sdk.applied_effort = sdk.effort
        sdk.force_reconnect = AsyncMock(side_effect=AssertionError("must preserve native process"))
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        runner = None
        try:
            if replay:
                client.next_turn = {"id": "human", "prompt": "inspect"}
                await client.query("inspect")
                await client.detach()
                await released(worker)
            else:
                sdk.client = client
                sdk._start_message_pump()
                ctx.state = "running"
                ctx.active_msg_id = "human"
                runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "inspect"))
                await until(lambda: native.prompts == ["inspect"])

            for frame in [start.copy(), user("native-human", "inspect"), {
                "type": "system", "subtype": "task_started", "task_id": "still-running",
                "tool_use_id": "child-tool", "task_type": "local_bash",
                "description": "background check", "uuid": "child-start",
                "session_id": ctx.session_id,
            }, assistant("answer", [{"type": "text", "text": "done"}]), result()]:
                await native.queue.put(frame)
            await until(lambda: worker.journal.seq == 5)
            if replay:
                sdk.service_metadata = worker.metadata.copy()
                sdk.service_defer_events = True
                await sdk.connect(resume_id="native-session", cwd="/tmp")
                from cc_remote.wrapper.claude_service import activate

                await activate(machine, ctx)
            await until(lambda: ctx.turn_task is None and sdk._background_callbacks_pending == 0)
            assert ctx.state == "idle"
            assert not ctx.claude_background_followups
            assert not sdk._steers.background_id
            assert worker.turn is None and worker.background_start is None
            assert ctx.claude_active_tasks == {"still-running"}
            assert sum(e.type == "turn_end" for e in transport.sent) == 1

            # Completion releases the next human input without interrupting the
            # independent child or resubmitting the recovered prompt.
            ctx.state = "running"
            ctx.active_msg_id = "next-human"
            runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "next question"))
            await until(lambda: native.prompts == ["inspect", "next question"])
            await native.queue.put(user("native-next", "next question"))
            await native.queue.put(assistant("next-answer", [{"type": "text", "text": "next answer"}]))
            await native.queue.put(result())
            await asyncio.wait_for(runner, 3)
            await until(lambda: ctx.state == "idle")
            assert ctx.claude_active_tasks == {"still-running"}
            assert native.interrupts == 0 and not native.closed
        finally:
            if ctx.turn_task:
                ctx.turn_task.cancel()
                await asyncio.gather(ctx.turn_task, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("distinct_tasks", [False, True])
@pytest.mark.parametrize("slow_callback", [False, True])
async def test_batched_task_inputs_settle_and_background_job_does_not_block_next_prompt(
    persistent, managed, distinct_tasks, slow_callback,
):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig())
        sdk.applied_auto_compact_mode = sdk.auto_compact_mode
        sdk.applied_auto_compact_threshold_tokens = sdk.auto_compact_threshold_tokens
        sdk.applied_effort = sdk.effort
        sdk.force_reconnect = AsyncMock(side_effect=AssertionError("must preserve the native process"))
        sdk.connect = AsyncMock(side_effect=AssertionError("must use the fake native client"))
        sdk.refresh_goal = AsyncMock(return_value=None)
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        project = sdk.background_message_callback
        callback_entered, release_callback = asyncio.Event(), asyncio.Event()

        async def project_in_order(message, turn_id):
            if slow_callback and getattr(message, "uuid", None) == "notification-1":
                callback_entered.set()
                await release_callback.wait()
            await project(message, turn_id)

        sdk.background_message_callback = project_in_order
        sdk.client = client
        sdk._start_message_pump()
        runner = None
        try:
            if managed:
                ctx.state = "running"
                ctx.active_msg_id = "human"
                runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "inspect"))
                inputs = native.prompts if persistent else native.inputs
                await until(lambda: len(inputs) == 1)
                await native.queue.put(user("native-human", "inspect"))

            for index in range(2):
                origin = {"kind": "task-notification"}
                if distinct_tasks:
                    origin["taskId"] = f"task-{index}"
                await native.queue.put(task_input(f"notification-{index}", origin))
                await native.queue.put(assistant(f"answer-{index}", [
                    {"type": "text", "text": f"checked task {index}"}]))
            await until(lambda: bool(ctx.claude_background_followups))
            # A still-running child is independent from its parent's terminal.
            await native.queue.put({
                "type": "system", "subtype": "task_started", "task_id": "still-running",
                "tool_use_id": "child-tool", "task_type": "local_bash",
                "description": "background check", "uuid": "child-start",
                "session_id": ctx.session_id,
            })
            # Code 2.1.276 omits origin on this real shared terminal.
            await native.queue.put(result())
            if slow_callback:
                await asyncio.wait_for(callback_entered.wait(), 3)
                await until(lambda: sdk._steers.background_id is None)
                # The terminal has arrived, but earlier injected input has not
                # been projected yet. It must not revive a completed response.
                release_callback.set()
            if runner:
                await asyncio.wait_for(runner, 3)
            await until(lambda: ctx.state == "idle")
            await until(lambda: sdk._background_callbacks_pending == 0)
            assert ctx.state == "idle"
            assert ctx.claude_active_tasks == {"still-running"}
            assert not ctx.claude_background_followups
            assert not sdk._steers.background_id
            assert sum(e.type == "turn_end" for e in transport.sent) == int(managed)
            if worker:
                assert worker.turn is None and worker.background_start is None
                assert not worker.client.closed

            # No interrupt, takeover or wait for the background child is needed.
            ctx.state = "running"
            ctx.active_msg_id = "next-human"
            runner = ctx.turn_task = asyncio.create_task(machine._run_turn(ctx, "next question"))
            await until(lambda: len(native.prompts if persistent else native.inputs) == int(managed) + 1)
            inputs = native.prompts if persistent else native.inputs
            assert inputs[-1] == "next question"
            await native.queue.put(user("native-next", "next question"))
            await native.queue.put(assistant("next-answer", [{"type": "text", "text": "next answer"}]))
            await native.queue.put(result())
            await asyncio.wait_for(runner, 3)
            await until(lambda: ctx.state == "idle")
            assert ctx.claude_active_tasks == {"still-running"}

            # When the child returns, native continuation can own the main
            # activity again and then settle without a synthetic human turn.
            await native.queue.put({
                "type": "system", "subtype": "task_notification", "task_id": "still-running",
                "status": "completed", "output_file": "", "summary": "check passed",
                "uuid": "child-finished", "session_id": ctx.session_id,
            })
            await native.queue.put(task_input("last-notification", {"kind": "task-notification"}))
            await native.queue.put(assistant("last-answer", [{"type": "text", "text": "check passed"}]))
            await until(lambda: ctx.state == "running" and bool(ctx.claude_background_followups))
            await native.queue.put(result())
            await until(lambda: ctx.state == "idle" and sdk._background_callbacks_pending == 0)
            assert not ctx.claude_active_tasks
            assert not ctx.claude_background_followups
            assert sum(e.type == "turn_end" for e in transport.sent) == int(managed) + 1
            assert any(e.type == "state" and e.state == "running" and e.continuation for e in transport.sent)
            assert native.interrupts == 0
        finally:
            release_callback.set()
            if runner:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            await sdk._stop_message_pump()
            if persistent:
                await client.detach()


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("later_continuation", [False, True])
async def test_shared_terminal_replays_without_resubmission_or_retiring_later_activity(
    managed, later_continuation,
):
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        if managed:
            first.next_turn = {"id": "human", "prompt": "inspect"}
            await first.query("inspect")
            await native.queue.put(user("human-echo", "inspect"))
        for index in range(2):
            await native.queue.put(task_input(f"notification-{index}", {
                "kind": "task-notification", "taskId": f"task-{index}",
            }))
        await until(lambda: worker.journal.seq == 2 + int(managed))
        await first.detach()
        await released(worker)
        await native.queue.put(result())
        if later_continuation:
            await native.queue.put(task_input("later-input", {"kind": "task-notification", "taskId": "later"}))
            await native.queue.put(assistant("later-output", [{"type": "text", "text": "later report"}]))
        await until(lambda: worker.journal.seq == (5 if later_continuation else 3) + int(managed))

        sdk = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
        sdk.service_defer_events = True
        machine, transport, ctx = _machine_with_sdk(sdk)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk.service_metadata = worker.metadata.copy()
        sdk.refresh_goal = AsyncMock(return_value=None)
        try:
            # The native process continued offline. Activate replays its journal;
            # it must never submit the original prompt again.
            await sdk.connect(resume_id="native-session", cwd="/tmp")
            from cc_remote.wrapper.claude_service import activate

            await activate(machine, ctx)
            await until(lambda: worker.turn is None and ctx.turn_task is None)
            if later_continuation:
                await until(lambda: ctx.state == "running" and sdk._background_callbacks_pending == 0)
                assert len(ctx.claude_background_followups) == 1
                assert worker.background_start is not None
                await native.queue.put(result())
            await until(lambda: ctx.state == "idle" and sdk._background_callbacks_pending == 0
                        and worker.background_start is None)
            assert not ctx.claude_background_followups
            assert worker.background_start is None
            assert sum(e.type == "turn_end" for e in transport.sent) == int(managed)
            assert native.prompts == (["inspect"] if managed else [])
            assert native.interrupts == 0 and not native.closed
        finally:
            if ctx.turn_task:
                ctx.turn_task.cancel()
                await asyncio.gather(ctx.turn_task, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
async def test_failed_shared_boundary_retains_journal_for_recovery():
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        sdk = SdkHandle(WrapperConfig())
        sdk.client = client

        async def project(message, _turn):
            if isinstance(message, ClaudeBackgroundBoundary):
                raise ValueError("temporary lifecycle projection failure")

        sdk.background_message_callback = project
        sdk._start_message_pump()
        try:
            sdk.next_turn_id = "human"
            await sdk.query("inspect")
            await worker.client.queue.put(user("human", "inspect"))
            await worker.client.queue.put(task_input("task-input", {"kind": "task-notification"}))
            await worker.client.queue.put(result())
            messages = [message async for message in sdk.receive_response()]
            await until(lambda: sdk._service_delivery_error is not None)
            with pytest.raises(ClaudeServiceReplayRequired):
                await sdk.ack_service_message(messages[-1], turn_id="human")
            assert worker.turn["id"] == "human"
            assert len(worker.journal.after(0)) == 3
            assert not sdk.message_pump_failed
            assert not worker.client.closed and worker.client.interrupts == 0
            assert worker.client.prompts == ["inspect"]
        finally:
            await sdk.detach_for_shutdown()
