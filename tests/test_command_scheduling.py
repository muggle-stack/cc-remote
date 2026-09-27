"""Command intake stays responsive without reordering a session's lifecycle."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    AnswerQuestion, GetContext, Interrupt, NewSession, OpenBtw, Ping, Query, SetEffort,
    SwitchSession, serialize,
)
from cc_remote.wrapper.command_scheduler import CommandScheduler
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


def _query(sid="b", cmd_id="send"):
    return Query(sid=sid, prompt="private prompt", msg_id=f"msg-{cmd_id}",
                 client_id="browser", cmd_id=cmd_id)


@pytest.mark.parametrize("slow_command", [
    GetContext(sid="a", refresh=True),
    SwitchSession(session_id="a"),
    NewSession(),
    _query("a", "slow"),
])
def test_slow_session_does_not_block_another_sessions_query_stop_or_ping(
        slow_command, monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        a, b = _mk_ctx("a"), _mk_ctx("b")
        machine.sessions = {"a": a, "b": b}
        machine.focused_sid = "b"
        started, release, stopped = (asyncio.Event() for _ in range(3))
        calls = []

        async def slow(_cmd):
            started.set()
            await release.wait()

        async def query(cmd):
            if cmd.sid == "a":
                await slow(cmd)
            else:
                calls.append("query")
                b.state = "running"

        async def native_interrupt():
            assert b.state == "interrupting"
            assert b.interrupt_event.is_set()
            calls.append("interrupt")
            stopped.set()

        b.sdk = SimpleNamespace(interrupt=native_interrupt)
        monkeypatch.setattr(machine, "_handle_query", query)
        if slow_command.type != "query":
            monkeypatch.setattr(machine, f"_handle_{slow_command.type}", slow)
        try:
            await machine._dispatch_incoming_command(slow_command)
            await asyncio.wait_for(started.wait(), 1)
            await machine._dispatch_incoming_command(_query())
            await machine._dispatch_incoming_command(Interrupt(sid="b"))
            await machine._dispatch_incoming_command(Ping(n=7))
            await asyncio.wait_for(stopped.wait(), 1)
            assert calls == ["query", "interrupt"]
            assert not release.is_set()
            assert any(frame.type == "pong" and frame.n == 7
                       for frame in transport.sent)
            assert any(frame.type == "command_ack" and frame.cmd_id == "send"
                       for frame in transport.sent)
        finally:
            release.set()
            await machine._command_scheduler.close()

    asyncio.run(run())


@pytest.mark.parametrize("command", [
    NewSession(),
    OpenBtw(sid="a", request_id="open", client_id="browser"),
])
def test_cli_preflight_does_not_block_another_sessions_stop_or_heartbeat(
        command, monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        a, b = _mk_ctx("a", "a"), _mk_ctx("b", "b")
        machine.sessions = {"a": a, "b": b}
        machine.focused_sid = "b"
        b.state = "running"
        started, stopped = asyncio.Event(), asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def preflight(_path):
            loop.call_soon_threadsafe(started.set)
            try:
                # The timeout makes the old synchronous path fail without
                # hanging pytest. A responsive event loop releases us first.
                release.wait(2)
                raise RuntimeError("deliberate preflight failure")
            finally:
                finished.set()

        async def native_interrupt():
            stopped.set()

        b.sdk = SimpleNamespace(interrupt=native_interrupt)
        monkeypatch.setattr(SdkHandle, "preflight", preflight)
        try:
            await machine._dispatch_incoming_command(command)
            await asyncio.wait_for(started.wait(), 3)
            assert not finished.is_set(), "CLI preflight blocked the event loop"
            await machine._dispatch_incoming_command(Interrupt(sid="b"))
            await machine._dispatch_incoming_command(Ping(n=8))
            await asyncio.wait_for(stopped.wait(), 1)
            assert not finished.is_set()
            assert any(frame.type == "pong" and frame.n == 8
                       for frame in transport.sent)
        finally:
            release.set()
            await asyncio.wait_for(machine._command_scheduler.drain(), 3)
            await machine._command_scheduler.close()
        # The version gate still rejects startup; no SDK connect/model turn is
        # allowed merely because its blocking probe moved off the event loop.
        assert finished.is_set()
        assert set(machine.sessions) == {"a", "b"}
        assert any(frame.type == "error" and frame.code == "cc_crash"
                   for frame in transport.sent)

    asyncio.run(run())


def test_cold_resume_query_and_stop_preserve_same_session_order(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def switch(cmd):
            calls.append("resuming")
            started.set()
            await release.wait()
            machine.sessions[cmd.session_id] = _mk_ctx(cmd.session_id)
            machine.focused_sid = cmd.session_id
            calls.append("resumed")

        async def query(cmd):
            assert machine._ctx_for(cmd.sid) is not None
            calls.append("query")

        async def interrupt(_cmd):
            calls.append("interrupt")

        monkeypatch.setattr(machine, "_handle_switch_session", switch)
        monkeypatch.setattr(machine, "_handle_query", query)
        monkeypatch.setattr(machine, "_handle_interrupt", interrupt)
        await machine._dispatch_incoming_command(SwitchSession(session_id="cold"))
        await started.wait()
        await machine._dispatch_incoming_command(_query("cold"))
        await machine._dispatch_incoming_command(Interrupt(sid="cold"))
        await asyncio.sleep(0)
        assert calls == ["resuming"]
        release.set()
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert calls == ["resuming", "resumed", "query", "interrupt"]

    asyncio.run(run())


def test_legacy_query_resolves_focus_after_switch_and_mutations_stay_ordered(
        monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        machine.sessions = {key: _mk_ctx(key) for key in ("a", "b")}
        machine.focused_sid = "a"
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def switch(cmd):
            started.set()
            await release.wait()
            machine.focused_sid = cmd.session_id
            calls.append(cmd.session_id)

        async def query(cmd):
            calls.append(f"query:{machine._ctx_for(cmd.sid).key}")

        monkeypatch.setattr(machine, "_handle_switch_session", switch)
        monkeypatch.setattr(machine, "_handle_query", query)
        await machine._dispatch_incoming_command(SwitchSession(session_id="b"))
        await started.wait()
        await machine._dispatch_incoming_command(_query(None))
        await machine._dispatch_incoming_command(SwitchSession(session_id="a"))
        await asyncio.sleep(0)
        assert calls == []
        release.set()
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert calls == ["b", "query:b", "a"]

    asyncio.run(run())


def test_settings_and_query_remain_ordered_on_their_session(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def effort(cmd):
            started.set()
            await release.wait()
            calls.append(cmd.effort)

        async def query(_cmd):
            calls.append("query")

        monkeypatch.setattr(machine, "_handle_set_effort", effort)
        monkeypatch.setattr(machine, "_handle_query", query)
        await machine._dispatch_incoming_command(SetEffort(sid="b", effort="high"))
        await started.wait()
        await machine._dispatch_incoming_command(_query())
        await asyncio.sleep(0)
        assert calls == []
        release.set()
        await machine._command_scheduler.drain()
        assert calls == ["high", "query"]

    asyncio.run(run())


def test_question_answer_bypasses_the_handler_waiting_for_it(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        asked, answered = asyncio.Event(), asyncio.Event()

        async def effort(_cmd):
            asked.set()
            await answered.wait()

        async def answer(_cmd):
            answered.set()

        monkeypatch.setattr(machine, "_handle_set_effort", effort)
        monkeypatch.setattr(machine, "_handle_answer_question", answer)
        await machine._dispatch_incoming_command(SetEffort(sid="b", effort="high"))
        await asked.wait()
        await machine._dispatch_incoming_command(AnswerQuestion(
            sid="b", ask_id="permission", answer="allow"))
        await asyncio.wait_for(machine._command_scheduler.drain(), 1)
        assert answered.is_set()

    asyncio.run(run())


def test_waiting_query_is_not_evicted_before_it_can_take_query_lock(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.max_concurrent_sessions = 2
        a, b = _mk_ctx("a"), _mk_ctx("b")
        machine.sessions = {"a": a, "b": b}
        machine.focused_sid = "a"
        monkeypatch.setattr(SdkHandle, "preflight", lambda _path: None)

        async def pending_query(_cmd):
            await asyncio.Event().wait()

        monkeypatch.setattr(machine, "_handle_query", pending_query)
        await machine._dispatch_incoming_command(_query())
        assert not b.query_lock.locked()
        try:
            assert await machine._spawn(resume_id=None) is None
            assert machine.sessions.get("b") is b
            assert any(frame.type == "error" and frame.code == "busy"
                       for frame in transport.sent)
        finally:
            await machine._command_scheduler.close()

    asyncio.run(run())


def test_reconnect_retry_coalesces_inflight_and_only_acks_after_acceptance(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def query(cmd):
            calls.append(cmd.msg_id)
            started.set()
            await release.wait()

        monkeypatch.setattr(machine, "_handle_query", query)
        cmd = _query()
        await machine._dispatch_incoming_command(cmd)
        await started.wait()
        await machine._dispatch_incoming_command(cmd.model_copy())
        assert calls == [cmd.msg_id]
        assert not transport.sent
        release.set()
        await machine._command_scheduler.drain()
        await machine._dispatch_incoming_command(cmd.model_copy())
        await machine._command_scheduler.drain()
        assert calls == [cmd.msg_id]
        assert [frame.type for frame in transport.sent] == [
            "command_ack", "command_ack"]

    asyncio.run(run())


def test_rekey_does_not_split_the_command_order_or_residency_pin(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("tmp-a")
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def query(cmd):
            calls.append(cmd.cmd_id)
            if cmd.cmd_id == "first":
                started.set()
                await release.wait()

        monkeypatch.setattr(machine, "_handle_query", query)
        await machine._dispatch_incoming_command(_query("tmp-a", "first"))
        await started.wait()
        await machine._capture_session_id(ctx, "real-a")
        assert machine._command_scheduler.owns_target("real-a")
        await machine._dispatch_incoming_command(_query("real-a", "second"))
        await asyncio.sleep(0)
        assert calls == ["first"]
        release.set()
        await machine._command_scheduler.drain()
        assert calls == ["first", "second"]
        assert not machine._command_scheduler.owns_target("real-a")

    asyncio.run(run())


@pytest.mark.parametrize("bound", ["items", "bytes"])
def test_scheduler_applies_backpressure_and_releases_capacity(bound):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def process(cmd):
            calls.append(cmd.cmd_id)
            if cmd.cmd_id == "first":
                started.set()
                await release.wait()

        first = _query("a", "first")
        scheduler = CommandScheduler(
            process, lambda sid: sid,
            max_items=1 if bound == "items" else 128,
            max_bytes=(len(serialize(first).encode("utf-8"))
                       if bound == "bytes" else 32 * 1024 * 1024),
        )
        await scheduler.submit(first)
        await started.wait()
        blocked = asyncio.create_task(scheduler.submit(_query("b", "second")))
        await asyncio.sleep(0)
        assert not blocked.done()
        assert calls == ["first"]
        release.set()
        await asyncio.wait_for(blocked, 1)
        await scheduler.drain()
        assert calls == ["first", "second"]
        assert scheduler._bytes == 0

    asyncio.run(run())


def test_shutdown_cancels_waiting_commands_without_launching_them():
    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()
        calls = []

        async def process(cmd):
            calls.append(cmd.cmd_id)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        scheduler = CommandScheduler(process, lambda sid: sid)
        await scheduler.submit(_query("a", "first"), target="a")
        await started.wait()
        await scheduler.submit(_query("a", "second"), target="a")
        await scheduler.close()
        assert cancelled.is_set()
        assert calls == ["first"]
        assert not scheduler.owns_target("a")
        assert scheduler._bytes == 0

    asyncio.run(run())
