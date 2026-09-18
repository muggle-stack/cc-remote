"""Uploaded steering files follow the native owner's lifetime, not its socket."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import ERR_STEER_UNKNOWN, Steer
from cc_remote.wrapper import claude_steer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator
from tests.test_claude_service import environment, released
from tests.test_claude_steering import NativeClient, result, user
from tests.test_multisession import _mk_ctx, _mk_machine


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_write", [False, True])
@pytest.mark.parametrize("exit_path", ["disconnect", "drain_reconnect", "shutdown"])
async def test_abnormal_turn_files_are_removed_after_native_close(
    tmp_path, monkeypatch, uncertain_write, exit_path,
):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("sid", "sid")
    ctx.sdk = sdk = SdkHandle(machine.cfg)
    machine.sessions[ctx.key] = ctx
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    sdk.client = native = NativeClient()
    sdk._start_message_pump()
    sdk.next_turn_id = "root"
    await sdk.query("start")
    ctx.state = "running"
    ctx.active_msg_id = "root"
    ctx.translator = StreamTranslator(4000, turn_id="root")
    directory = tmp_path / "upload"
    directory.mkdir()
    monkeypatch.setattr(claude_steer.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    native.fail_write = uncertain_write

    async def close_native():
        assert directory.exists()

    native.disconnect = AsyncMock(side_effect=close_native)
    sdk.connect = AsyncMock()
    try:
        reply = await machine._handle_steer(Steer(
            sid="sid", cmd_id="command", client_id="browser", msg_id="guide", prompt="read",
            files=[{"filename": "note.txt", "data": "aGVsbG8="}],
        ))
        if uncertain_write:
            assert reply.code == ERR_STEER_UNKNOWN
        else:
            assert reply is None
        assert (directory / "00-note.txt").read_text() == "hello"
        # Losing the reader does not establish that the native owner stopped.
        await machine._on_claude_message_pump_failure(ctx, ConnectionError("reader lost"))
        assert directory.exists()
        assert ctx.claude_steer_attachment_dirs == [str(directory)]
        if exit_path == "drain_reconnect":
            await sdk.force_reconnect(ctx.session_id, ctx.cwd)
            sdk.connect.assert_awaited_once()
        elif exit_path == "shutdown":
            await sdk.detach_for_shutdown()
        else:
            await sdk.disconnect()
        native.disconnect.assert_awaited_once()
        assert not directory.exists()
        assert ctx.claude_steer_attachment_dirs == []
        assert len(native.inputs) == 2 and native.interrupts == 0
    finally:
        await sdk._stop_message_pump()


@pytest.mark.asyncio
async def test_failed_service_close_keeps_files_for_the_live_native_owner(tmp_path):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("sid", "sid")
    ctx.sdk = sdk = SdkHandle(machine.cfg)
    machine._configure_claude_sdk_callbacks(ctx, sdk)
    directory = tmp_path / "upload"
    directory.mkdir()
    ctx.claude_steer_attachment_dirs.append(str(directory))
    sdk.client = SimpleNamespace(disconnect=AsyncMock(side_effect=ConnectionError()))
    with pytest.raises(ConnectionError):
        await sdk.disconnect()
    assert directory.exists()
    assert ctx.claude_steer_attachment_dirs == [str(directory)]


@pytest.mark.asyncio
@pytest.mark.parametrize("echoed", [False, True])
@pytest.mark.parametrize("exit_path", ["close", "terminal"])
async def test_service_retains_detached_files_then_retires_them(
    tmp_path, echoed, exit_path,
):
    async with environment() as (service, attach):
        first = await attach()
        first.next_turn = {"id": "root"}
        await first.query("start")
        worker = service.sessions[first.id]
        directory = tmp_path / "upload"
        directory.mkdir()
        (directory / "note.txt").write_text("hello")
        await first.steer("read attachment", native_id="guide-native", turn_id="root",
                          metadata={"id": "guide", "prompt": "read attachment",
                                    "attachment_dir": str(directory)})
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = sdk = SdkHandle(machine.cfg)
        machine._configure_claude_sdk_callbacks(ctx, sdk)
        sdk.client = first
        ctx.claude_steer_attachment_dirs.append(str(directory))
        if echoed:
            await worker.client.queue.put(user("guide-native"))
            async with asyncio.timeout(2):
                while worker.steers.pending:
                    await asyncio.sleep(0.001)
        await sdk.detach_for_shutdown()
        await released(worker)
        assert directory.exists() and not worker.client.closed
        # A fresh Wrapper has not replayed the echo yet. The service must still
        # own cleanup even if the controller closes it before reading anything.
        second = await attach()
        if exit_path == "close":
            await second.disconnect()
            assert worker.client.closed
        else:
            # Background and intermediate Results are not human terminals.
            await worker.client.queue.put({**result(), "origin": {"kind": "task"}})
            if not echoed:
                await worker.client.queue.put(result())
            await worker.client.queue.put(user("guide-native"))
            await worker.client.queue.put(result())
            async with asyncio.timeout(2):
                while worker.terminal_seq is None:
                    await asyncio.sleep(0.001)
            assert directory.exists()
            await second.call("commit", {"turn_id": "root", "seq": worker.terminal_seq})
            assert not worker.client.closed
        assert not directory.exists()
        assert worker.client.interrupts == 0 and len(worker.client.prompts) == 2
