"""Callback recovery over the real private socket, without a live model."""

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from cc_remote.claude_service.client import callback_identity
from cc_remote.claude_service.wire import decode_sdk, encode_sdk
from tests.test_claude_service import environment, released


def callback_payload(kind):
    if kind == "permission":
        return {"name": "Bash", "input": {"command": "true"}, "context": encode_sdk(
            ToolPermissionContext(tool_use_id="tool-1"))}
    message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    if kind == "mcp":
        message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    return {"name": "ask", "message": message}


def callback_answer(kind):
    if kind == "permission":
        return PermissionResultAllow()
    if kind == "mcp":
        return {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    return None


@pytest.mark.parametrize("kind", ["permission", "mcp"])
def test_callback_handler_failure_retries_without_reconnecting(kind, monkeypatch):
    async def go():
        identities = []

        async def flaky(*args):
            identities.append(callback_identity.get())
            if len(identities) == 1:
                raise RuntimeError("temporary callback failure")
            return callback_answer(kind)

        async with environment() as (service, attach):
            client = await attach(permission=flaky)
            monkeypatch.setattr(client, "_mcp", flaky)
            worker = service.sessions[client.id]
            controller = worker.controller
            result = await asyncio.wait_for(worker.callback(kind, callback_payload(kind)), 2)
            assert decode_sdk(result) == callback_answer(kind)
            assert len(identities) == 2
            assert identities[0] and identities[0] == identities[1]
            assert worker.controller is controller
            assert not client.connection.task.done()
            assert not worker.client.closed
            assert not worker.callbacks
            async with asyncio.timeout(2):
                while client.callback_tasks:
                    await asyncio.sleep(0.001)

    asyncio.run(go())


@pytest.mark.parametrize("kind", ["permission", "mcp", "mcp_notification"])
def test_callback_answer_timeout_resends_same_result_without_rerunning_handler(kind, monkeypatch):
    async def go():
        executions = []
        answers = []
        release_first_answer = asyncio.Event()

        async def handler(*args):
            executions.append(callback_identity.get())
            return callback_answer(kind)

        async with environment() as (service, attach):
            client = await attach(permission=handler)
            monkeypatch.setattr(client, "_mcp", handler)
            worker = service.sessions[client.id]
            original_call = client.connection.call
            original_dispatch = service.dispatch

            async def short_answer_timeout(method, params=None, **kwargs):
                if method == "answer":
                    kwargs["timeout"] = 0.05
                return await original_call(method, params, **kwargs)

            async def delay_first_answer(owner, request_id, method, params):
                if method == "answer":
                    answers.append((request_id, params))
                    if len(answers) == 1:
                        await release_first_answer.wait()
                return await original_dispatch(owner, request_id, method, params)

            monkeypatch.setattr(client.connection, "call", short_answer_timeout)
            monkeypatch.setattr(service, "dispatch", delay_first_answer)
            try:
                result = await asyncio.wait_for(worker.callback(
                    "permission" if kind == "permission" else "mcp", callback_payload(kind)), 2)
                assert decode_sdk(result) == callback_answer(kind)
                assert len(executions) == 1
                assert len(answers) == 2
                assert answers[0] == answers[1]
                assert answers[0][0] == "answer-" + executions[0]
                assert not client.connection.task.done()
                assert not worker.callbacks
            finally:
                release_first_answer.set()

    asyncio.run(go())


def test_detaching_during_callback_retry_preserves_pending_native_request():
    async def go():
        attempted = asyncio.Event()
        attempts = 0

        async def unavailable(*args):
            nonlocal attempts
            attempts += 1
            attempted.set()
            raise RuntimeError("temporary permission handler failure")

        async def allow(*args):
            return PermissionResultAllow()

        async with environment() as (service, attach):
            first = await attach(permission=unavailable)
            worker = service.sessions[first.id]
            permission = asyncio.create_task(worker.client.options.can_use_tool(
                "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id="tool-1")))
            try:
                await asyncio.wait_for(attempted.wait(), 2)
                keys = list(worker.callbacks)
                await first.detach()
                await released(worker)
                assert not permission.done()
                assert list(worker.callbacks) == keys
                assert all(task.done() for task in first.callback_tasks.values())
                await attach(permission=allow)
                assert isinstance(await asyncio.wait_for(permission, 2), PermissionResultAllow)
                assert attempts == 1
                assert not worker.client.closed
            finally:
                permission.cancel()
                await asyncio.gather(permission, return_exceptions=True)

    asyncio.run(go())


def test_callback_retry_does_not_block_other_callbacks_and_stops_when_closed():
    async def go():
        attempted = asyncio.Event()

        async def permission(name, arguments, context):
            if arguments["command"] == "retry":
                attempted.set()
                raise RuntimeError("temporary permission handler failure")
            return PermissionResultAllow()

        async with environment() as (service, attach):
            client = await attach(permission=permission)
            worker = service.sessions[client.id]
            native = asyncio.create_task(worker.client.options.can_use_tool(
                "Bash", {"command": "retry"}, ToolPermissionContext(tool_use_id="tool-1")))
            try:
                await asyncio.wait_for(attempted.wait(), 2)
                retrying = next(iter(client.callback_tasks.values()))
                result = await asyncio.wait_for(worker.client.options.can_use_tool(
                    "Bash", {"command": "true"}, ToolPermissionContext(tool_use_id="tool-2")), 2)
                assert isinstance(result, PermissionResultAllow)
                assert not native.done()
                native.cancel()
                await asyncio.gather(native, return_exceptions=True)
                await asyncio.wait_for(asyncio.gather(retrying, return_exceptions=True), 2)
                assert retrying.cancelled()
                assert not worker.callbacks
            finally:
                native.cancel()
                await asyncio.gather(native, return_exceptions=True)

    asyncio.run(go())
