"""Journal acknowledgements must not overtake a failed controller projection."""

import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage

from cc_remote.config import WrapperConfig
from cc_remote.protocol import Error
from cc_remote.wrapper.sdk import ClaudeServiceReplayRequired, SdkHandle
from tests.test_claude_autocompact import _machine_with_sdk
from tests.test_claude_service import environment, released


def notification(number):
    return {
        "type": "system", "subtype": "task_notification", "task_id": f"task-{number}",
        "status": "completed", "output_file": "/tmp/output", "summary": "done",
        "uuid": f"notification-{number}", "session_id": "native-session",
    }


def result():
    return {
        "type": "result", "subtype": "success", "duration_ms": 20,
        "duration_api_ms": 19, "is_error": False, "num_turns": 1,
        "session_id": "native-session", "origin": {"kind": "human"},
    }


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def start_handle(client, callback):
    handle = SdkHandle(WrapperConfig())
    handle.client = client
    handle.background_message_callback = callback
    handle._start_message_pump()
    return handle


@pytest.mark.parametrize("failure", ["projection", "ack", "ack_response"])
def test_background_delivery_failure_retains_later_rows_for_reattachment(failure, monkeypatch):
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]
            delivered = []

            async def project(message, _turn):
                delivered.append(message._cc_service_seq)
                if failure == "projection" and message._cc_service_seq == 1:
                    raise ValueError("temporary projection failure")

            original_call = client.call

            async def flaky_ack(method, params=None, **kwargs):
                if failure == "ack" and method == "ack" and params["seq"] == 1:
                    raise ConnectionError("temporary ACK failure")
                value = await original_call(method, params, **kwargs)
                if failure == "ack_response" and method == "ack" and params["seq"] == 1:
                    raise ConnectionError("accepted ACK response lost")
                return value

            monkeypatch.setattr(client, "call", flaky_ack)
            handle = start_handle(client, project)
            try:
                for number in range(1, 4):
                    await worker.client.queue.put(notification(number))
                await wait_until(lambda: client.last_seq == 3 and handle._background_callbacks_pending == 0)
                acknowledged = 1 if failure == "ack_response" else 0
                assert worker.ack == acknowledged
                expected = list(range(acknowledged + 1, 4))
                assert [row["seq"] for row in worker.journal.after(0)] == expected
                assert delivered == [1]
                assert not handle.message_pump_failed
                assert not worker.client.closed
                assert worker.client.interrupts == 0
                assert worker.client.prompts == []
            finally:
                await handle.detach_for_shutdown()
            await released(worker)

            recovered = []

            async def project_replay(message, _turn):
                if (getattr(message, "subtype", None) == "task_notification"
                        and not getattr(message, "_cc_service_seed", False)):
                    recovered.append(message._cc_service_seq)

            handle.client = await attach()
            handle.background_message_callback = project_replay
            handle._start_message_pump()
            try:
                await wait_until(lambda: worker.ack == 3)
                assert recovered == expected
                handle.next_turn_id = "new-human"
                await handle.query("next")
                assert worker.client.prompts == ["next"]
            finally:
                await handle.detach_for_shutdown()

    asyncio.run(go())


def test_failed_background_terminal_retains_its_full_prefix():
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]

            async def project(message, _turn):
                if isinstance(message, ResultMessage):
                    raise ValueError("terminal projection failed")

            handle = start_handle(client, project)
            rows = [
                {"type": "user", "message": {"role": "user", "content": "task done"},
                 "uuid": "background-user", "origin": {"kind": "task-notification"}},
                {"type": "assistant", "message": {"id": "background", "model": "test",
                 "content": [{"type": "text", "text": "findings"}]}},
                {**result(), "origin": {"kind": "task-notification"}},
                notification(2),
            ]
            try:
                for row in rows:
                    await worker.client.queue.put(row)
                await wait_until(lambda: client.last_seq == 4 and handle._background_callbacks_pending == 0)
                assert worker.ack == 2
                assert worker.description()["after"] == 0
                assert [row["seq"] for row in worker.journal.after(0)] == [1, 2, 3, 4]
                with pytest.raises(ClaudeServiceReplayRequired):
                    await handle.steer("continue", native_id="steer", metadata={})
                assert worker.client.prompts == []
            finally:
                await handle.detach_for_shutdown()

    asyncio.run(go())


def test_waiting_query_rechecks_failed_delivery_without_hanging():
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]
            started, release = asyncio.Event(), asyncio.Event()

            async def project(_message, _turn):
                started.set()
                await release.wait()
                raise ValueError("projection failed")

            handle = start_handle(client, project)
            pending = None
            try:
                await worker.client.queue.put(notification(1))
                await asyncio.wait_for(started.wait(), 2)
                handle.next_turn_id = "must-not-run"
                pending = asyncio.create_task(handle.query("must not run"))
                await asyncio.sleep(0)
                assert not pending.done()
                release.set()
                with pytest.raises(ClaudeServiceReplayRequired):
                    await asyncio.wait_for(pending, 2)
                assert worker.client.prompts == []
                assert worker.ack == 0
            finally:
                release.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await handle.detach_for_shutdown()

    asyncio.run(go())


def test_in_process_background_callback_failure_keeps_existing_tolerance():
    async def go():
        queue, delivered = asyncio.Queue(), []

        async def receive_messages():
            while True:
                yield await queue.get()

        async def project(message, _turn):
            delivered.append(message.task_id)
            if len(delivered) == 1:
                raise ValueError("malformed notification")

        handle = start_handle(SimpleNamespace(_query=SimpleNamespace(
            receive_messages=receive_messages)), project)
        try:
            await queue.put(notification(1))
            await queue.put(notification(2))
            await wait_until(lambda: len(delivered) == 2 and handle._background_callbacks_pending == 0)
            handle.check_service_delivery()
            assert delivered == ["task-1", "task-2"]
            assert not handle.message_pump_failed
        finally:
            await handle._stop_message_pump()

    asyncio.run(go())


def test_machine_reports_replay_requirement_without_restarting_native_work():
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]

            async def project(_message, _turn):
                raise ValueError("projection failed")

            handle = start_handle(client, project)
            machine, transport, ctx = _machine_with_sdk(handle)
            ctx.state = "running"
            ctx.claude_background_followup_pending = True
            handle.message_pump_failure_callback = lambda error: machine._on_claude_message_pump_failure(ctx, error)
            try:
                await worker.client.queue.put(notification(1))
                await wait_until(lambda: client.last_seq == 1 and handle._background_callbacks_pending == 0)
                errors = [item for item in transport.sent if isinstance(item, Error)]
                assert len(errors) == 1 and "Wrapper" in errors[0].message
                assert not ctx.claude_background_followup_pending
                assert ctx.state == "idle"
                # A later query must not convert a UI failure into a native
                # reconnect, even if transcript growth suggests a reload.
                ctx.needs_reload = True
                ctx.active_msg_id = "new-query"
                await machine._run_turn(ctx, "must not run")
                assert worker.client.prompts == []
                assert not worker.client.closed
                assert not handle.message_pump_failed
                assert worker.journal.after(0)
            finally:
                await handle.detach_for_shutdown()

    asyncio.run(go())


@pytest.mark.parametrize("fails", [False, True])
def test_human_commit_waits_for_an_earlier_background_projection(fails):
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]
            started, release = asyncio.Event(), asyncio.Event()

            async def project(_message, _turn):
                started.set()
                await release.wait()
                if fails:
                    raise ValueError("temporary projection failure")

            handle = start_handle(client, project)
            try:
                handle.next_turn_id = "human"
                await handle.query("hello")
                await worker.client.queue.put(notification(1))
                await asyncio.wait_for(started.wait(), 2)
                await worker.client.queue.put(result())
                stream = handle.receive_response()
                terminal = await asyncio.wait_for(anext(stream), 2)
                assert isinstance(terminal, ResultMessage)
                await stream.aclose()
                # The managed Result may release its barrier, but must not
                # prune the earlier callback which is still executing.
                await asyncio.wait_for(handle.ack_service_message(terminal, turn_id="human"), 2)
                assert worker.turn["id"] == "human"
                assert worker.ack == 0
                handle.release_background_messages()
                release.set()
                await asyncio.wait_for(handle._background_callbacks_drained.wait(), 2)
                if fails:
                    assert worker.ack == 0
                    assert [row["seq"] for row in worker.journal.after(0)] == [1, 2]
                    handle.next_turn_id = "must-not-run"
                    with pytest.raises(RuntimeError, match="Wrapper"):
                        await asyncio.wait_for(handle.query("must not run"), 2)
                    with pytest.raises(RuntimeError, match="Wrapper"):
                        await handle.ack_service_message(terminal, turn_id="human")
                    assert worker.client.prompts == ["hello"]
                else:
                    assert worker.turn is None
                    assert worker.ack == 2
                    handle.next_turn_id = "next-human"
                    await handle.query("next")
                    assert worker.client.prompts == ["hello", "next"]
                assert not handle.message_pump_failed
                assert not worker.client.closed
            finally:
                release.set()
                await handle.detach_for_shutdown()

    asyncio.run(go())


def test_deferred_commit_does_not_deadlock_on_the_managed_result_barrier():
    async def go():
        async with environment() as (service, attach):
            client = await attach()
            worker = service.sessions[client.id]
            projected = []

            async def project(message, _turn):
                projected.append(message._cc_service_seq)

            handle = start_handle(client, project)
            try:
                handle.next_turn_id = "human"
                await handle.query("hello")
                for row in [
                    {"type": "user", "message": {"role": "user", "content": "hello"},
                     "uuid": "human", "origin": {"kind": "human"}},
                    result(), notification(1),
                ]:
                    await worker.client.queue.put(row)
                messages = [message async for message in handle.receive_response()]
                await wait_until(lambda: client.last_seq == 3 and handle._background_callbacks_pending == 1)
                await asyncio.wait_for(handle.ack_service_message(messages[-1], turn_id="human"), 2)
                assert projected == []
                assert worker.turn["id"] == "human"
                handle.release_background_messages()
                await asyncio.wait_for(handle._background_callbacks_drained.wait(), 2)
                assert projected == [3]
                assert worker.turn is None
                assert worker.ack == 3
                handle.next_turn_id = "next-human"
                await handle.query("next")
                assert worker.client.prompts == ["hello", "next"]
            finally:
                await handle.detach_for_shutdown()

    asyncio.run(go())
