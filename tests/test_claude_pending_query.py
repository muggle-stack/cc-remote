"""Buffered native continuations must not finish an unconsumed browser query."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import ResultMessage

from cc_remote.config import WrapperConfig
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_service import environment, released
from tests.test_claude_steering import NativeClient, assistant, requesting, result, until, user


STARTS = [
    requesting("old-request"),
    assistant("old-assistant", [{"type": "text", "text": "old report"}]),
    {"type": "stream_event", "uuid": "old-stream", "session_id": "native-session",
     "event": {"type": "message_start", "message": {"id": "old-response", "content": []}}},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("start", STARTS, ids=["request", "assistant", "stream"])
async def test_buffered_continuation_cannot_finish_pending_query(persistent, start):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
        sdk.client = client
        background = []
        seen_terminal = asyncio.Event()

        async def project(message, turn_id):
            background.append((message, turn_id))
            if isinstance(message, ResultMessage):
                seen_terminal.set()

        sdk.background_message_callback = project
        sdk._start_message_pump()
        consumer = None
        try:
            sdk.next_turn_id = "parent"
            await sdk.query("earlier question")
            await native.queue.put(user("parent-echo"))
            await native.queue.put(result())
            previous = [message async for message in sdk.receive_response()]
            await sdk.ack_service_message(previous[-1], turn_id="parent")
            sdk.release_background_messages()

            sdk.next_turn_id = "new-human"
            await sdk.query("new question")

            async def read():
                return [message async for message in sdk.receive_response()]

            consumer = asyncio.create_task(read())
            # Already-buffered native work can reach either sole reader after
            # the browser write, without replaying its internal user input.
            await native.queue.put(start.copy())
            await native.queue.put(result())
            await until(lambda: seen_terminal.is_set() or consumer.done())
            assert not consumer.done(), "background Result finished the pending browser query"
            assert seen_terminal.is_set()
            assert {turn_id for _, turn_id in background} == {"parent"}
            assert background[0][0]._cc_background_start["managed"] is False
            assert background[-1][0]._cc_background_ends
            assert not sdk._steers.background_id
            if worker:
                assert worker.terminal_seq is None and worker.turn["id"] == "new-human"
                with pytest.raises(ValueError, match="terminal was not acknowledged"):
                    await worker.mutate("wrong-commit", "commit", {
                        "turn_id": "new-human", "seq": worker.journal.seq,
                    })

            await native.queue.put(user("new-human-echo", "new question"))
            await native.queue.put(assistant("new-answer", [{"type": "text", "text": "new report"}]))
            await native.queue.put(result())
            managed = await asyncio.wait_for(consumer, 3)
            assert [m.uuid for m in managed if getattr(m, "uuid", None)] == ["new-human-echo", "new-answer"]
            assert sum(isinstance(m, ResultMessage) for m in managed) == 1
            await sdk.ack_service_message(managed[-1], turn_id="new-human")
            sdk.release_background_messages()
            await until(lambda: sdk._background_callbacks_pending == 0)
            if worker:
                assert worker.turn is None and worker.background_start is None
            inputs = native.prompts if persistent else native.inputs
            assert inputs == ["earlier question", "new question"]
            assert native.interrupts == 0
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_pending_query_recovery_preserves_pre_echo_background_identity():
    async with environment() as (service, attach):
        first = await attach()
        worker = service.sessions[first.id]
        native = worker.client
        worker.origin_id = "parent"
        first.next_turn = {"id": "new-human", "prompt": "new question"}
        await first.query("new question")
        await first.detach()
        await released(worker)
        await native.queue.put(STARTS[1].copy())
        await native.queue.put(result())
        await until(lambda: worker.journal.seq == 2)
        assert worker.terminal_seq is None

        sdk = SdkHandle(WrapperConfig(claude_service_socket=first.connection.socket_path))
        sdk.service_metadata = worker.metadata.copy()
        sdk.refresh_goal = AsyncMock(return_value=None)
        background = []

        async def project(message, turn_id):
            background.append((message, turn_id))

        sdk.background_message_callback = project
        consumer = None
        try:
            await sdk.connect(resume_id="native-session", cwd="/tmp")

            async def read():
                return [message async for message in sdk.receive_response()]

            consumer = asyncio.create_task(read())
            await until(lambda: any(isinstance(m, ResultMessage) for m, _ in background) or consumer.done())
            assert not consumer.done()
            assert {turn_id for _, turn_id in background} == {"parent"}
            await native.queue.put(user("new-human-echo", "new question"))
            await native.queue.put(assistant("new-answer", [{"type": "text", "text": "new report"}]))
            await native.queue.put(result())
            managed = await asyncio.wait_for(consumer, 3)
            assert [m.uuid for m in managed if getattr(m, "uuid", None)] == ["new-human-echo", "new-answer"]
            await sdk.ack_service_message(managed[-1], turn_id="new-human")
            sdk.release_background_messages()
            await until(lambda: worker.turn is None and worker.background_start is None)
            assert native.prompts == ["new question"] and native.interrupts == 0
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await sdk.detach_for_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_unannounced_response_between_root_result_and_steer_echo(persistent):
    async with environment() as (service, attach):
        client = await attach() if persistent else NativeClient()
        worker = service.sessions[client.id] if persistent else None
        native = worker.client if worker else client
        sdk = SdkHandle(WrapperConfig(turn_reader_queue_cap=1))
        sdk.client = client
        background = []

        async def project(message, turn_id):
            background.append((message, turn_id))

        sdk.background_message_callback = project
        sdk._start_message_pump()
        consumer = None
        try:
            sdk.next_turn_id = "parent"
            await sdk.query("first question")

            async def read():
                return [message async for message in sdk.receive_response()]

            consumer = asyncio.create_task(read())
            await native.queue.put(user("parent-echo", "first question"))
            await until(lambda: sdk._managed_input_seen)
            await sdk.steer("new question", native_id="guide-echo", metadata={"id": "guide"})
            await native.queue.put(result())
            await native.queue.put(STARTS[1].copy())
            await native.queue.put(result())
            await until(lambda: any(isinstance(m, ResultMessage) for m, _ in background))
            assert not consumer.done()
            assert {turn_id for _, turn_id in background} == {"parent"}
            assert not sdk._steers.background_id
            if worker:
                assert worker.terminal_seq is None and worker.turn["id"] == "parent"
            await native.queue.put(user("guide-echo", "new question"))
            await native.queue.put(assistant("guided-answer", [{"type": "text", "text": "new report"}]))
            await native.queue.put(result())
            managed = await asyncio.wait_for(consumer, 3)
            assert [m.uuid for m in managed if getattr(m, "uuid", None)] == [
                "parent-echo", "guide-echo", "guided-answer"]
            assert sum(isinstance(m, ResultMessage) for m in managed) == 1
            await sdk.ack_service_message(managed[-1], turn_id="guide")
            sdk.release_background_messages()
            await until(lambda: sdk._background_callbacks_pending == 0)
            if worker:
                assert worker.turn is None and worker.background_start is None
            assert native.interrupts == 0
        finally:
            if consumer:
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            await sdk._stop_message_pump()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["compact", "error"])
async def test_persistent_commands_can_finish_without_user_replay(finish):
    async with environment() as (service, attach):
        client = await attach()
        worker = service.sessions[client.id]
        sdk = SdkHandle(WrapperConfig())
        sdk.client = client
        sdk._start_message_pump()
        try:
            sdk.next_turn_id = "command"
            await sdk.query("/compact" if finish == "compact" else "bad request")
            if finish == "compact":
                await worker.client.queue.put({"type": "system", "subtype": "status", "status": "compacting"})
                await worker.client.queue.put({"type": "system", "subtype": "compact_boundary",
                                               "compact_metadata": {"trigger": "manual", "pre_tokens": 100}})
            terminal = {**result(), "is_error": finish == "error"}
            await worker.client.queue.put(terminal)

            async def read():
                return [message async for message in sdk.receive_response()]

            managed = await asyncio.wait_for(read(), 3)
            assert managed[-1].is_error is (finish == "error")
            assert worker.origin_id == "command"
            assert worker.background_start is None
            await sdk.ack_service_message(managed[-1], turn_id="command")
            sdk.release_background_messages()
            assert worker.turn is None
        finally:
            await sdk._stop_message_pump()
