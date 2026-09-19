"""Manual compact lifecycle, service commits and native-turn attribution."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cc_remote.protocol import CompactSession, Delta, Error, Interrupt, ProcessEvent, Query, TurnEnd, UserMsg
from cc_remote.wrapper import claude_service
from cc_remote.wrapper.claude_compaction import manual_compact_prompt
from cc_remote.wrapper.codex_controls import CodexControlStore
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import replayed_user_message_id, translate_history
from tests.test_claude_autocompact import SESSION_ID, _machine_with_sdk
from tests.test_claude_compaction_flow import boundary, result, status
from tests.test_claude_service import environment, released
from tests.test_codex_spontaneous_stream import _notification
from tests.test_multisession import _mk_ctx, _mk_machine


def test_only_compact_command_is_visible_and_stdout_cannot_claim_its_identity():
    from claude_agent_sdk.types import TextBlock, UserMessage

    messages = [SimpleNamespace(type="user", uuid=str(index), message={
        "role": "user", "content": content,
    }) for index, content in enumerate([
        "<command-name>/context</command-name>",
        "<local-command-caveat>local command</local-command-caveat>",
        "<command-name>/compact</command-name><command-args>keep the plan</command-args>",
        "<local-command-stdout>Compacted</local-command-stdout>",
        "<command-name>/model</command-name><command-args>/compact</command-args>",
    ])]
    events = translate_history(messages, 4096)
    assert [e.prompt for e in events if isinstance(e, UserMsg)] == ["/compact keep the plan"]
    assert all(e.result.is_error for e in events if isinstance(e, TurnEnd))
    stdout = UserMessage(content=[TextBlock(text=messages[3].message["content"])], uuid=SESSION_ID)
    assert replayed_user_message_id(stdout) is None
    command = UserMessage(content=[TextBlock(text=messages[2].message["content"])], uuid=SESSION_ID)
    assert replayed_user_message_id(command) == SESSION_ID
    assert manual_compact_prompt([
        {"type": "text", "text": "/compact"}, {"type": "image", "source": {}},
    ]) is None


@pytest.mark.parametrize("outcome", ["success", "error", "interrupted", "missing-boundary"])
def test_manual_claude_compact_commits_service_and_next_message_is_accepted(outcome):
    async def run():
        async with environment() as (service, attach):
            remote = await attach(session_id=SESSION_ID)
            worker = service.sessions[remote.id]
            handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
            handle.client = remote
            handle.effort = handle.applied_effort = "max"
            handle.applied_auto_compact_mode = handle.auto_compact_mode
            handle.applied_auto_compact_threshold_tokens = handle.auto_compact_threshold_tokens
            handle._start_message_pump()
            machine, transport, ctx = _machine_with_sdk(handle)

            async def context(_cmd, _action):
                return ctx

            machine._claude_code_context = context
            try:
                await machine._handle_compact_session(CompactSession(
                    session_id=SESSION_ID, engine="claude"))
                task = ctx.turn_task
                async with asyncio.timeout(2):
                    while not any(isinstance(e, UserMsg) for e in transport.sent):
                        await asyncio.sleep(0)
                assert worker.client.prompts == ["/compact"]
                assert ctx.state == "running"
                assert [e.prompt for e in transport.sent if isinstance(e, UserMsg)] == ["/compact"]
                rejection = await machine._handle_query(Query(sid=SESSION_ID, prompt="too early", msg_id="early"))
                assert isinstance(rejection, Error)
                await worker.client.queue.put(status())
                if outcome == "success":
                    await worker.client.queue.put(boundary())
                if outcome == "interrupted":
                    async def interrupt():
                        worker.client.interrupts += 1

                    worker.client.interrupt = interrupt
                    await machine._handle_interrupt(Interrupt(sid=SESSION_ID))
                    assert worker.client.interrupts == 1
                    assert worker.turn is not None
                    assert not task.done()
                    assert isinstance(await machine._handle_query(Query(
                        sid=SESSION_ID, prompt="during drain", msg_id="draining")), Error)
                failed = outcome in {"error", "interrupted"}
                terminal = {**result(), "is_error": failed,
                            "subtype": "error_during_execution" if failed else "success"}
                await worker.client.queue.put(terminal)
                await asyncio.wait_for(task, 2)
                assert ctx.state == "idle"
                assert worker.turn is None
                ends = [e for e in transport.sent if isinstance(e, TurnEnd)]
                assert len(ends) == 1 and ends[0].result.is_error == (outcome != "success")
                receipt = [e for e in transport.sent if isinstance(e, Delta)
                           and e.text == "上下文已压缩，可以继续当前会话。"]
                assert bool(receipt) == (outcome == "success")
                handle.next_turn_id = "next-message"
                await handle.query("continue")
                assert worker.client.prompts == ["/compact", "continue"]
                await worker.client.queue.put(result())
                async for message in handle.receive_response():
                    await handle.ack_service_message(message, turn_id="next-message")
                assert worker.turn is None
            finally:
                await handle._stop_message_pump()

    asyncio.run(run())


def test_legacy_uncommitted_compact_recovers_without_resubmitting_stale_prompt():
    async def run():
        async with environment() as (service, attach):
            original = await attach(session_id=SESSION_ID)
            original.next_turn = {"id": "compact-legacy", "prompt": "previous unrelated question"}
            await original.query("/compact")
            worker = service.sessions[original.id]
            await worker.client.queue.put(status())
            await worker.client.queue.put(boundary())
            await worker.client.queue.put(result())
            async with asyncio.timeout(2):
                while worker.terminal_seq is None:
                    await asyncio.sleep(0)
            await original.detach()
            await released(worker)
            remote = await attach(session_id=SESSION_ID)
            remote.ready.clear()
            assert remote.id == original.id
            handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
            handle.client = remote
            handle.effort = handle.applied_effort = "max"
            handle.applied_auto_compact_mode = handle.auto_compact_mode
            handle.applied_auto_compact_threshold_tokens = handle.auto_compact_threshold_tokens
            handle.service_recovery = remote.recovery
            handle._start_message_pump()
            handle._turn_root_id = handle._turn_origin_id = remote.recovery["id"]
            handle._turn_active = True
            handle._message_route_owner = "managed"
            machine, transport, ctx = _machine_with_sdk(handle)
            try:
                await claude_service.activate(machine, ctx)
                await asyncio.wait_for(ctx.turn_task, 2)
                assert ctx.state == "idle" and worker.turn is None
                assert worker.client.prompts == ["/compact"]
                assert [e.prompt for e in transport.sent if isinstance(e, UserMsg)] == ["/compact"]
                assert any(isinstance(e, Delta) and "上下文已压缩" in e.text for e in transport.sent)
                handle.next_turn_id = "next-after-recovery"
                await handle.query("continue")
                assert worker.client.prompts == ["/compact", "continue"]
                await worker.client.queue.put(result())
                async for message in handle.receive_response():
                    await handle.ack_service_message(message, turn_id="next-after-recovery")
                assert worker.turn is None
            finally:
                await handle._stop_message_pump()

    asyncio.run(run())


@pytest.mark.parametrize("failed", [False, True])
def test_internal_claude_compact_commits_success_and_error_without_human_turn(failed):
    async def run():
        async with environment() as (service, attach):
            remote = await attach(session_id=SESSION_ID)
            worker = service.sessions[remote.id]
            handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
            handle.client = remote
            handle._start_message_pump()
            machine, transport, ctx = _machine_with_sdk(handle)
            try:
                task = asyncio.create_task(machine._compact_managed_claude_context(ctx, reason="test"))
                async with asyncio.timeout(2):
                    while not worker.client.prompts:
                        await asyncio.sleep(0)
                await worker.client.queue.put(status())
                if not failed:
                    await worker.client.queue.put(boundary())
                await worker.client.queue.put({**result(), "is_error": failed})
                if failed:
                    with pytest.raises(RuntimeError, match="compact failed"):
                        await asyncio.wait_for(task, 2)
                else:
                    await asyncio.wait_for(task, 2)
                assert worker.turn is None
                assert not any(isinstance(e, (UserMsg, TurnEnd)) for e in transport.sent)
                assert not ctx.claude_write_active
            finally:
                await handle._stop_message_pump()

    asyncio.run(run())


@pytest.mark.parametrize("early", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "interrupted", "failed"])
def test_codex_manual_compact_keeps_native_lifecycle_and_command_after_reload(early, outcome):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = lambda phase, tid: machine._on_codex_turn_lifecycle(ctx, phase, tid)
        turn = "manual-compact"

        async def start():
            await handle._dispatch(_notification("turn/started", turn, turn={"id": turn}))
            await handle._dispatch(_notification("item/started", turn,
                item={"id": "compact-item", "type": "contextCompaction"}))

        async def request(method, params):
            assert method == "thread/compact/start"
            assert params == {"threadId": ctx.session_id}
            if early:
                await start()
                await asyncio.sleep(0)
            return {}

        handle._request = request
        await handle.compact_thread()
        if not early:
            await start()
        async with asyncio.timeout(2):
            while not any(isinstance(e, UserMsg) and e.prompt == "/compact" for e in transport.sent):
                await asyncio.sleep(0)
        assert ctx.state == "running"
        if outcome == "interrupted":
            async def interrupt_request(method, params):
                assert method == "turn/interrupt"
                assert params["turnId"] == turn
                return {}

            handle._request = interrupt_request
            await handle.interrupt()
        await handle._dispatch(_notification("item/completed", turn,
            item={"id": "compact-item", "type": "contextCompaction"}))
        assert ctx.state == "running"
        task = ctx.codex_spontaneous_task
        await handle._dispatch(_notification("turn/completed", turn,
            turn={"id": turn, "status": outcome, "durationMs": 20}))
        await asyncio.wait_for(task, 2)
        assert ctx.state == "idle" and not handle.turn_active
        ends = [e for e in transport.sent if isinstance(e, TurnEnd)]
        assert len(ends) == 1 and ends[0].result.is_error == (outcome != "completed")
        assert any(isinstance(e, Delta) and "上下文已压缩" in e.text for e in transport.sent) == (outcome == "completed")
        machine._codex_controls = CodexControlStore(machine.cfg.state_dir)
        recovered = await machine._recover_official_codex_users(ctx.key, (turn,))
        assert recovered[turn].prompt == "/compact"
        # A later automatic compaction must never inherit the manual command.
        assert not await handle.manual_compact_for_turn("later-turn")
        assert any(isinstance(e, ProcessEvent) and e.phase == "start" for e in transport.sent)

    asyncio.run(run())


@pytest.mark.parametrize("early", [False, True])
def test_rejected_codex_compact_does_not_claim_an_automatic_turn(early):
    async def run():
        machine, _ = _mk_machine()
        handle = CodexHandle(machine.cfg)
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)

        async def request(_method, _params):
            if early:
                await handle._dispatch(_notification(
                    "turn/started", "automatic", turn={"id": "automatic"}))
            raise RuntimeError("rejected")

        handle._request = request
        with pytest.raises(RuntimeError, match="rejected"):
            await handle.compact_thread()
        if not early:
            await handle._dispatch(_notification(
                "turn/started", "automatic", turn={"id": "automatic"}))
        assert not await handle.manual_compact_for_turn("automatic")

    asyncio.run(run())


def test_codex_compact_attribution_does_not_cross_connection_generation():
    async def run():
        machine, _ = _mk_machine()
        handle = CodexHandle(machine.cfg)
        handle.thread_id = "thread-spontaneous"
        handle.proc = SimpleNamespace(returncode=None)
        started = asyncio.Event()
        response = asyncio.Event()

        async def request(_method, _params):
            await handle._dispatch(_notification("turn/started", "compact", turn={"id": "compact"}))
            started.set()
            await response.wait()
            return {}

        handle._request = request
        pending = asyncio.create_task(handle.compact_thread())
        await started.wait()
        claim = asyncio.create_task(handle.manual_compact_for_turn("compact"))
        await asyncio.sleep(0)
        handle._generation += 1
        response.set()
        await pending
        assert not await claim

    asyncio.run(run())


def test_codex_compact_history_is_account_scoped_and_survives_control_updates(tmp_path):
    from cc_remote.wrapper.codex_history import CodexOfficialHistory
    from tests.test_codex_history import _turn

    async def run():
        machine, _ = _mk_machine()
        store = machine._codex_controls = CodexControlStore(tmp_path)
        sid = "primary@thread-compact"
        store.remember_compaction(sid, "native-compact")
        store.update(sid, approval_policy="never", permission_profile=None, web_search="disabled")
        store.set_cwd_override(sid, "/tmp/project")
        store.set_context(sid, 200_000, 300_000)
        machine._codex_controls = CodexControlStore(tmp_path)
        assert machine._manual_codex_compact_users("other@thread-compact", ("native-compact",)) == {}

        async def rpc(method, _params, cwd=None):
            assert method == "thread/turns/list"
            return {"data": [_turn("native-compact", [
                {"type": "contextCompaction", "id": "native-boundary"},
            ])], "nextCursor": None}

        history = CodexOfficialHistory(4096, rpc=rpc, recover_users=machine._recover_official_codex_users)
        page = await history.summary_page(sid, before=None, limit=1)
        assert len(page.turns) == 1
        turn = page.turns[0]
        assert turn["prompt"] == "/compact" and turn["done"] and not turn.get("error")
        assert any(block.get("text") == "上下文已压缩，可以继续当前会话。" for block in turn["blocks"])

    asyncio.run(run())
