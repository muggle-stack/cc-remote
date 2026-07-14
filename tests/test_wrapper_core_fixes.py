"""Zero-token regressions for wrapper drain and Codex rollout history."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from cc_remote.protocol import (
    AssistantMsgStart, ERR_DRAIN_TIMEOUT, Error, Interrupt, StateEvent,
    TurnEnd, UserMsg,
)
from cc_remote.wrapper import codex_sessions as codex_sessions_module
from cc_remote.wrapper import codex_stream as codex_stream_module
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_sessions import codex_session_settings
from cc_remote.wrapper.codex_stream import codex_translate_history
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input
from cc_remote.wrapper.session import _session_file, load_session_id, save_session_id
from tests.test_multisession import _mk_ctx, _mk_machine


class _StalledSdk:
    """A turn source that never yields a terminal response."""

    effort = "high"
    applied_effort = "high"
    model = "gpt-test"
    service_tier = None
    tier_dirty = False

    def __init__(self) -> None:
        self.reader_started = asyncio.Event()
        self.release = asyncio.Event()
        self.reconnects = 0
        self.responses = []

    async def query(self, prompt, images=None):
        return None

    async def receive_response(self):
        self.reader_started.set()
        await self.release.wait()
        for response in self.responses:
            yield response

    async def interrupt(self):
        return None

    async def force_reconnect(self, resume_id, cwd):
        self.reconnects += 1


def test_interrupt_during_preflight_reconnect_never_submits_query():
    class PreflightSdk:
        effort = "max"
        applied_effort = "low"

        def __init__(self):
            self.reconnect_started = asyncio.Event()
            self.release_reconnect = asyncio.Event()
            self.queries = 0
            self.interrupts = 0

        async def force_reconnect(
            self, resume_id, cwd, reason="", preserve_model=True,
        ):
            assert preserve_model is True
            self.reconnect_started.set()
            await self.release_reconnect.wait()
            self.applied_effort = self.effort

        async def interrupt(self):
            self.interrupts += 1

        async def query(self, _prompt):
            self.queries += 1

        async def receive_response(self):
            if False:
                yield None

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("session-1", session_id="session-1")
        sdk = PreflightSdk()
        ctx.sdk = sdk
        ctx.state = "running"
        ctx.active_msg_id = "message-1"
        machine.sessions[ctx.key] = ctx
        machine.focused_sid = ctx.key

        turn = asyncio.create_task(machine._run_turn(ctx, "must not run"))
        await asyncio.wait_for(sdk.reconnect_started.wait(), timeout=1)
        await machine._handle_interrupt(Interrupt(sid=ctx.key))
        sdk.release_reconnect.set()
        await asyncio.wait_for(turn, timeout=1)

        assert sdk.queries == 0
        assert ctx.state == "idle"
        # The aborted optimistic turn is echoed before its terminal marker so a
        # second client cannot accidentally close the prior visible turn.
        narrative = [message for message in transport.sent
                     if isinstance(message, (UserMsg, TurnEnd))]
        assert [message.type for message in narrative] == ["user_msg", "turn_end"]
        assert narrative[-1].result.subtype == "error_during_execution"

    asyncio.run(run())


def test_session_state_filename_is_utf8_safe_and_state_read_is_bounded(tmp_path):
    cwd = "/" + "界" * 500
    path = _session_file(tmp_path, cwd)
    assert len(path.name.encode("utf-8")) <= 255

    save_session_id(tmp_path, cwd, "session-1")
    assert load_session_id(tmp_path, cwd) == "session-1"

    path.write_text("x" * 20_000)
    assert load_session_id(tmp_path, cwd) is None
    path.write_text(json.dumps({"cc_session_id": "../invalid"}))
    assert load_session_id(tmp_path, cwd) is None


def test_session_alias_state_read_is_bounded_and_validated():
    machine, _ = _mk_machine()
    path = machine._alias_file()
    machine.SESSION_ALIAS_FILE_MAX_BYTES = 64
    path.write_text("x" * 65)
    assert machine._load_session_aliases() == {}

    machine.SESSION_ALIAS_FILE_MAX_BYTES = 1024
    valid_key = "tmp-" + "a" * 32
    path.write_text(json.dumps({
        valid_key: {
            "session_id": "session-1",
            "cwd": "/tmp/project",
            "created_at": time.time(),
        },
        "tmp-invalid": {
            "session_id": "../bad",
            "cwd": "/tmp/project",
            "created_at": time.time(),
        },
    }))
    aliases = machine._load_session_aliases()
    assert list(aliases) == [valid_key]


def test_tool_input_is_structurally_bounded_but_keeps_action_context():
    bounded = bounded_tool_input({
        "file_path": "/tmp/example.txt",
        "content": "x" * 2_000_000,
        "changes": {"/tmp/example.txt": "y" * 2_000_000},
    }, 64 * 1024)
    encoded = json.dumps(bounded).encode()
    assert len(encoded) <= 64 * 1024
    assert bounded["_truncated"] is True
    assert bounded["file_path"] == "/tmp/example.txt"


def test_tool_input_marks_structural_squeeze_even_when_result_fits_budget():
    bounded = bounded_tool_input({"content": "x" * 9000}, 64 * 1024)
    assert bounded["_truncated"] is True
    assert len(bounded["content"]) < 9000


def test_tool_output_is_structurally_bounded_without_calling_arbitrary_str():
    class Explosive:
        def __str__(self):
            raise AssertionError("must not stringify an arbitrary SDK object")

    text, truncated = bounded_text(
        {Explosive(): ["x" * 1000] * 1000, "tail": Explosive()}, 4096)

    assert len(text) <= 4096
    assert truncated is True
    assert "<Explosive>" in text


def test_tool_payload_sanitizers_cut_off_deep_lists_and_cycles():
    deep = "leaf"
    for _ in range(2000):
        deep = [deep]
    cycle = []
    cycle.append(cycle)

    text, text_truncated = bounded_text([deep, cycle], 4096)
    tool = bounded_tool_input({"content": deep, "cycle": cycle}, 4096)

    assert len(text) <= 4096 and text_truncated is True
    assert "omitted" in text
    assert tool["_truncated"] is True
    assert len(json.dumps(tool).encode()) <= 4096


def test_untracked_diff_filename_cannot_inject_git_output_option(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    target = tmp_path / "must-not-be-created"

    async def run():
        machine, _ = _mk_machine()
        await machine._git_diff(str(repo), f"--output={target}")

    asyncio.run(run())
    assert not target.exists()


def test_diff_rejects_paths_outside_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    async def run():
        machine, _ = _mk_machine()
        with pytest.raises(ValueError, match="outside"):
            await machine._git_diff(str(repo), str(outside))

    asyncio.run(run())


def test_get_diff_with_explicit_unknown_sid_never_reads_focused_repo():
    async def run():
        machine, transport = _mk_machine()
        focused = _mk_ctx("focused", session_id="focused")
        machine.sessions[focused.key] = focused
        machine.focused_sid = focused.key

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("must not fall back to the focused cwd")

        machine._git_diff = forbidden
        await machine._handle_get_diff(SimpleNamespace(
            sid="missing-session", client_id="client-1", file="", theme="light"))

        error = transport.sent[-1]
        assert error.type == "error" and error.code == "not_running"
        assert error.sid == "missing-session" and error.to == "client-1"

    asyncio.run(run())


def test_diff_rejects_untracked_fifo_without_opening_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    fifo = repo / "blocked"
    os.mkfifo(fifo)

    async def run():
        machine, _ = _mk_machine()
        with pytest.raises(ValueError, match="regular file"):
            await asyncio.wait_for(
                machine._git_diff(str(repo), str(fifo)), timeout=1.0)

    asyncio.run(run())


def test_bounded_subprocess_output_has_wall_clock_timeout():
    async def run():
        machine, _ = _mk_machine()
        with pytest.raises(asyncio.TimeoutError, match="time limit"):
            await machine._bounded_process_output(
                ("sh", "-c", "sleep 10"), 1024, timeout=0.03)

    asyncio.run(run())


def test_bounded_subprocess_discards_residual_output_without_communicate(monkeypatch):
    class FakeStdout:
        def __init__(self):
            self.data = bytearray(b"x" * 100)

        async def read(self, size):
            if not self.data:
                return b""
            result = bytes(self.data[:size])
            del self.data[:size]
            return result

    class FakeProcess:
        pid = 424242
        returncode = None

        def __init__(self):
            self.stdout = FakeStdout()

        async def wait(self):
            self.returncode = 0
            return 0

    process = FakeProcess()
    signals = []

    async def fake_spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr(
        machine_module.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(
        machine_module.os, "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    async def run():
        machine, _ = _mk_machine()
        text = await machine._bounded_process_output(("ignored",), 4)
        assert text.startswith("xxxx")
        assert "diff truncated" in text

    asyncio.run(run())
    assert not process.stdout.data
    assert signals[0] == (process.pid, machine_module.signal.SIGTERM)


def test_background_job_scan_caps_entries_and_state_file_size(
        monkeypatch, tmp_path):
    jobs = tmp_path / ".claude" / "jobs"
    jobs.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    valid = jobs / "valid"
    valid.mkdir()
    (valid / "state.json").write_text(json.dumps({
        "state": "running", "sessionId": "session-valid",
    }))
    oversized = jobs / "oversized"
    oversized.mkdir()
    (oversized / "state.json").write_text(
        " " * (machine_module.WrapperMachine.BG_JOB_STATE_MAX_BYTES + 1))

    assert machine_module.WrapperMachine._bg_blocked_session_ids() == {
        "session-valid"
    }

    for index in range(4):
        job = jobs / f"extra-{index}"
        job.mkdir()
        (job / "state.json").write_text(json.dumps({
            "state": "running", "sessionId": f"session-{index}",
        }))
    monkeypatch.setattr(machine_module.WrapperMachine, "BG_JOB_SCAN_MAX", 2)
    assert len(machine_module.WrapperMachine._bg_blocked_session_ids()) <= 2


def test_git_diff_output_is_streamed_to_a_hard_limit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    huge = repo / "huge.txt"
    huge.write_text("line changed\n" * 500_000)

    async def run():
        machine, _ = _mk_machine()
        machine.cfg.ws_max_size_bytes = 512 * 1024
        diff = await machine._git_diff(str(repo), str(huge))
        assert len(diff.encode()) < 300 * 1024
        assert "diff truncated at transport safety limit" in diff

    asyncio.run(run())


def test_interrupt_wakes_existing_queue_wait_and_enforces_drain_deadline():
    """Changing running -> interrupting must wake an already-blocked queue.get."""

    async def run():
        machine, transport = _mk_machine()
        machine.cfg.drain_timeout = 0.03
        sdk = _StalledSdk()
        ctx = _mk_ctx("sid-1", "sid-1")
        ctx.sdk = sdk
        ctx.state = "running"
        machine.sessions["sid-1"] = ctx
        machine.focused_sid = "sid-1"

        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.5)
        # Give _run_turn a scheduling turn to enter queue.get() while state=running.
        await asyncio.sleep(0)
        await machine._handle_interrupt(SimpleNamespace(sid="sid-1"))
        await asyncio.wait_for(turn, timeout=0.5)

        assert ctx.state == "idle"
        assert sdk.reconnects == 1
        assert any(
            getattr(msg, "type", None) == "error"
            and getattr(msg, "code", None) == ERR_DRAIN_TIMEOUT
            for msg in transport.sent
        )

    asyncio.run(run())


def test_codex_idle_watchdog_warns_without_ending_or_replaying_the_turn():
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.codex_turn_idle_warn_seconds = 0.02
        sdk = _StalledSdk()
        sdk.responses = [
            {"method": "item/reasoning/delta", "params": {
                "itemId": "reasoning", "delta": "internal progress"}},
            {"method": "item/agentMessage/delta", "params": {
                "itemId": "answer", "delta": "done"}},
            {"method": "turn/completed", "params": {
                "turn": {"status": "completed", "durationMs": 30}}},
        ]
        ctx = _mk_ctx("codex-stalled", "codex-stalled")
        ctx.sdk = sdk
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "message-stalled"
        machine.sessions[ctx.key] = ctx

        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await asyncio.sleep(0.04)

        notices = [msg for msg in transport.sent
                   if isinstance(msg, StateEvent) and msg.phase == "waiting"]
        assert len(notices) == 1
        assert notices[0].state == "running"
        assert notices[0].msg_id == "message-stalled"
        assert ctx.state == "running"
        assert sdk.reconnects == 0

        sdk.release.set()
        await asyncio.wait_for(turn, timeout=0.5)

        assert ctx.state == "idle"
        assert sdk.reconnects == 0
        clear_index = next(
            index for index, msg in enumerate(transport.sent)
            if isinstance(msg, StateEvent) and msg.msg_id == "message-stalled"
            and msg.detail is None)
        wait_index = transport.sent.index(notices[0])
        assert clear_index > wait_index

    asyncio.run(run())


def test_codex_idle_warning_boundary_never_drops_a_consumed_delta(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.codex_turn_idle_warn_seconds = 0.02
        sdk = _StalledSdk()
        sdk.responses = [
            {"method": "item/agentMessage/delta", "params": {
                "itemId": "boundary-answer", "delta": "visible answer"}},
            {"method": "turn/completed", "params": {
                "turn": {"status": "completed", "durationMs": 30}}},
        ]
        ctx = _mk_ctx("codex-boundary", "codex-boundary")
        ctx.sdk = sdk
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "message-boundary"
        machine.sessions[ctx.key] = ctx

        real_wait = machine_module.asyncio.wait
        forced_boundary = False

        async def boundary_wait(tasks, *args, **kwargs):
            nonlocal forced_boundary
            if kwargs.get("timeout") is not None and not forced_boundary:
                forced_boundary = True
                task = next(iter(tasks))
                sdk.release.set()
                for _ in range(50):
                    if task.done():
                        break
                    await asyncio.sleep(0)
                assert task.done()
                # Simulate timeout bookkeeping winning even though the get task
                # consumed and completed on the same event-loop boundary.
                return set(), {task}
            return await real_wait(tasks, *args, **kwargs)

        monkeypatch.setattr(machine_module.asyncio, "wait", boundary_wait)
        await asyncio.wait_for(machine._run_turn(ctx, "hello"), timeout=0.5)

        assert forced_boundary is True
        assert not [msg for msg in transport.sent if isinstance(msg, Error)]
        deltas = [msg.text for msg in transport.sent
                  if getattr(msg, "type", None) == "delta"]
        assert deltas == ["visible answer"]
        terminal = [msg for msg in transport.sent if isinstance(msg, TurnEnd)][-1]
        assert terminal.result.subtype == "success"

    asyncio.run(run())


def test_codex_retry_notice_never_regresses_interrupting_to_running():
    class RetryAfterInterruptSdk(_StalledSdk):
        async def interrupt(self):
            self.responses = [
                {"method": "error", "params": {"willRetry": True, "error": {
                    "message": "Reconnecting... 5/5",
                    "codexErrorInfo": {"responseStreamDisconnected": {
                        "httpStatusCode": 503}},
                }}},
                {"method": "turn/completed", "params": {
                    "turn": {"status": "interrupted", "durationMs": 20}}},
            ]
            self.release.set()

    async def run():
        machine, transport = _mk_machine()
        sdk = RetryAfterInterruptSdk()
        ctx = _mk_ctx("codex-interrupt-retry", "codex-interrupt-retry")
        ctx.sdk = sdk
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "message-interrupt-retry"
        machine.sessions[ctx.key] = ctx

        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await machine._handle_interrupt(SimpleNamespace(sid=ctx.key))
        await asyncio.wait_for(turn, timeout=0.5)

        states = [msg for msg in transport.sent if isinstance(msg, StateEvent)]
        interrupt_index = next(
            index for index, msg in enumerate(states)
            if msg.state == "interrupting")
        assert not any(
            msg.state == "running" and msg.phase == "retrying"
            for msg in states[interrupt_index + 1:])
        assert states[-1].state == "idle"
        assert ctx.state == "idle"

    asyncio.run(run())


def test_codex_empty_live_completion_is_correlated_to_the_active_turn():
    async def run():
        machine, transport = _mk_machine()
        sdk = _StalledSdk()
        sdk.responses = [{"method": "turn/completed", "params": {
            "turn": {"status": "completed", "durationMs": 237252}}}]
        sdk.release.set()
        ctx = _mk_ctx("codex-empty", "codex-empty")
        ctx.sdk = sdk
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "message-empty"
        machine.sessions[ctx.key] = ctx

        await asyncio.wait_for(machine._run_turn(ctx, "hello"), timeout=0.5)

        errors = [msg for msg in transport.sent if isinstance(msg, Error)]
        assert len(errors) == 1
        assert errors[0].msg_id == "message-empty"
        assert "没有返回任何内容" in errors[0].message
        terminal = [msg for msg in transport.sent if isinstance(msg, TurnEnd)][-1]
        assert terminal.result.is_error is True
        assert terminal.result.subtype == "error"
        assert ctx.state == "idle"

    asyncio.run(run())


def _write_rollout(path):
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "turn_context",
         "payload": {"turn_id": "turn-1", "model": "gpt-test"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "one"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "answer one"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1",
                     "duration_ms": 2000, "completed_at": 1767225605}},
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-2"}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "two"}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "partial two"}},
        {"timestamp": "2026-01-01T00:01:04Z", "type": "event_msg",
         "payload": {"type": "turn_aborted", "turn_id": "turn-2",
                     "reason": "interrupted", "duration_ms": 3000,
                     "completed_at": 1767225664}},
        {"timestamp": "2026-01-01T00:02:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-3"}},
        {"timestamp": "2026-01-01T00:02:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "three"}},
        {"timestamp": "2026-01-01T00:02:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "failed answer"}},
        {"timestamp": "2026-01-01T00:02:04Z", "type": "event_msg",
         "payload": {"type": "turn_aborted", "turn_id": "turn-3",
                     "reason": "failed", "duration_ms": 4000,
                     "completed_at": 1767225724}},
        {"timestamp": "2026-01-01T00:03:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-4"}},
        {"timestamp": "2026-01-01T00:03:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "four"}},
        {"timestamp": "2026-01-01T00:03:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "still running"}},
        # Automatic continuation: a new Codex turn id without a new user message
        # remains part of the same visible, still-open chat turn.
        {"timestamp": "2026-01-01T00:03:04Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-4-cont"}},
        {"timestamp": "2026-01-01T00:03:05Z", "type": "turn_context",
         "payload": {"turn_id": "turn-4-cont", "model": "gpt-test"}},
        {"timestamp": "2026-01-01T00:03:06Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "continuing"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_codex_rollout_ids_are_stable_and_terminal_statuses_are_preserved(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout)

    first, model = codex_translate_history(str(rollout), 10_000)
    second, _ = codex_translate_history(str(rollout), 10_000)

    assert model == "gpt-test"
    assert [e.msg_id for e in first if e.type == "user_msg"] == [
        "turn-1", "turn-2", "turn-3", "turn-4"
    ]
    def ids(events):
        return [
            (e.type, getattr(e, "msg_id", None), getattr(e, "message_id", None),
             getattr(e, "tool_use_id", None))
            for e in events
        ]
    assert ids(first) == ids(second)
    results = [e.result for e in first if e.type == "turn_end"]
    assert [(r.subtype, r.duration_ms, r.is_error) for r in results] == [
        ("success", 2000, False),
        ("error_during_execution", 3000, True),
        ("error", 4000, True),
    ]
    assert [e.turn_id for e in first if e.type == "turn_end"] == [
        "turn-1", "turn-2", "turn-3"]
    # No synthetic TurnEnd for turn-4: the client reducer must keep it not-done.
    assert len([e for e in first if e.type == "user_msg"]) == len(results) + 1
    assert first[-1].type == "assistant_msg_end"


def test_codex_empty_completed_history_is_a_correlated_error(tmp_path):
    rollout = tmp_path / "rollout-empty.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-empty"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-empty"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "现在是什么模型？"}},
        {"timestamp": "2026-01-01T00:03:59Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-empty",
                     "last_agent_message": None, "duration_ms": 237000,
                     "completed_at": 1767225839}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    first, _ = codex_translate_history(str(rollout), 10_000)
    second, _ = codex_translate_history(str(rollout), 10_000)

    errors = [event for event in first if isinstance(event, Error)]
    assert len(errors) == 1
    assert errors[0].msg_id == "turn-empty"
    assert "没有返回任何内容" in errors[0].message
    result = [event.result for event in first if isinstance(event, TurnEnd)][0]
    assert (result.subtype, result.duration_ms, result.is_error) == (
        "error", 237000, True)
    assert next(event for event in first
                if isinstance(event, TurnEnd)).turn_id == "turn-empty"
    assert [(event.type, getattr(event, "msg_id", None)) for event in first] == [
        (event.type, getattr(event, "msg_id", None)) for event in second]


def test_codex_tool_only_completed_history_remains_success(tmp_path):
    rollout = tmp_path / "rollout-tool.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-tool"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-tool"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "run it"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "call-1", "arguments": "{\"cmd\":\"true\"}"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-1",
                     "output": "ok"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-tool",
                     "last_agent_message": None, "duration_ms": 3000}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert not any(isinstance(event, Error) for event in events)
    result = [event.result for event in events if isinstance(event, TurnEnd)][0]
    assert (result.subtype, result.is_error) == ("success", False)
    assert next(event for event in events
                if isinstance(event, TurnEnd)).turn_id == "turn-tool"


def test_codex_history_uses_final_automatic_continuation_turn_id(tmp_path):
    rollout = tmp_path / "rollout-continuation.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-continuation"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-first"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "continue it"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "first part"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-cont"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "turn_context",
         "payload": {"turn_id": "turn-cont", "model": "gpt-test"}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "last part"}},
        {"timestamp": "2026-01-01T00:00:07Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-cont"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    terminal = next(event for event in events if isinstance(event, TurnEnd))
    assert terminal.turn_id == "turn-cont"


def test_codex_history_synthetic_boundary_never_steals_next_turn_id(tmp_path):
    rollout = tmp_path / "rollout-missing-terminal.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-missing-terminal"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-old"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "old"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "old answer"}},
        # No terminal for turn-old. Codex begins the next real user turn.
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-next"}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "turn_context",
         "payload": {"turn_id": "turn-next", "model": "gpt-test"}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "next"}},
        {"timestamp": "2026-01-01T00:01:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "next answer"}},
        {"timestamp": "2026-01-01T00:01:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-next"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    terminals = [event for event in events if isinstance(event, TurnEnd)]
    assert [event.turn_id for event in terminals] == [None, "turn-next"]
    assert [event.result.subtype for event in terminals] == ["error", "success"]


def test_codex_history_goal_continuation_after_completed_turn_is_own_turn(tmp_path):
    rollout = tmp_path / "rollout-goal-continuation.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-goal-continuation"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-user"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "start goal"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "first answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-user"}},
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-goal"}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "turn_context",
         "payload": {"turn_id": "turn-goal", "model": "gpt-test"}},
        # No user_message: this is an app-server goal/background continuation.
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "goal progress"}},
        {"timestamp": "2026-01-01T00:01:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-goal"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert len([event for event in events if isinstance(event, UserMsg)]) == 1
    assert len([event for event in events
                if isinstance(event, AssistantMsgStart)]) == 2
    assert [event.turn_id for event in events if isinstance(event, TurnEnd)] == [
        "turn-user", "turn-goal"]


def test_codex_history_goal_continuations_page_as_independent_turns(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-goal-pages.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-1"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-user"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "start goal"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "first answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-user"}},
    ]
    for index in range(1, 4):
        rows.extend([
            {"timestamp": f"2026-01-01T00:0{index}:01Z", "type": "event_msg",
             "payload": {"type": "task_started",
                         "turn_id": f"turn-goal-{index}"}},
            {"timestamp": f"2026-01-01T00:0{index}:02Z", "type": "event_msg",
             "payload": {"type": "agent_message",
                         "message": f"goal progress {index}"}},
            {"timestamp": f"2026-01-01T00:0{index}:03Z", "type": "event_msg",
             "payload": {"type": "task_complete",
                         "turn_id": f"turn-goal-{index}"}},
        ])
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))
    monkeypatch.setattr(
        "cc_remote.wrapper.machine.codex_rollout_path", lambda sid: str(rollout)
    )

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions["session-1"] = ctx

        newest = await machine._build_history("session-1", limit=2)
        assert newest.oldest_id == "turn-goal-2"
        assert newest.newest_id == "turn-goal-3"
        assert newest.has_more is True
        assert not any(row["type"] == "user_msg" for row in newest.events)
        assert [row.get("turn_id") for row in newest.events
                if row["type"] == "turn_end"] == [
                    "turn-goal-2", "turn-goal-3"]

        older = await machine._build_history(
            "session-1", before=newest.oldest_id, limit=2)
        assert older.oldest_id == "turn-user"
        assert older.newest_id == "turn-goal-1"
        assert older.has_more is False
        assert [row.get("turn_id") for row in older.events
                if row["type"] == "turn_end"] == [
                    "turn-user", "turn-goal-1"]

    asyncio.run(run())


def test_codex_history_restores_final_text_after_tools_from_task_complete(tmp_path):
    rollout = tmp_path / "rollout-tool-final.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-tool-final"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-tool-final"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "run it"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "call-final", "arguments": "{\"cmd\":\"true\"}"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-final",
                     "output": "ok"}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-tool-final",
                     "last_agent_message": "final answer", "duration_ms": 3000}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert [event.text for event in events if event.type == "delta"] == [
        "final answer"]
    assert not any(isinstance(event, Error) for event in events)
    assert [event.result.subtype for event in events
            if isinstance(event, TurnEnd)] == ["success"]


def test_codex_history_cursor_remains_valid_across_reparse(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout)
    monkeypatch.setattr(
        "cc_remote.wrapper.machine.codex_rollout_path", lambda sid: str(rollout)
    )

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("session-1", "session-1")
        ctx.engine = "codex"
        machine.sessions["session-1"] = ctx

        newest = await machine._build_history("session-1", limit=2)
        assert newest.oldest_id == "turn-3"
        assert newest.newest_id == "turn-4"
        older = await machine._build_history(
            "session-1", before=newest.oldest_id, limit=2
        )
        assert older.oldest_id == "turn-1"
        assert older.newest_id == "turn-2"
        assert older.has_more is False

    asyncio.run(run())


def test_codex_session_settings_reads_bounded_tail_of_oversized_source(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    old = json.dumps({
        "type": "turn_context",
        "payload": {"model": "gpt-old", "effort": "low"},
    }) + "\n"
    latest = json.dumps({
        "type": "turn_context",
        "payload": {
            "model": "gpt-latest",
            "effort": "ultra",
            "approval_policy": "on-request",
            "service_tier": "fast",
            "collaboration_mode": {"mode": "plan"},
        },
    }) + "\n"
    rollout.write_text(old + ("x" * 4096) + "\n" + latest)
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings("session-1", max_bytes=len(latest)) == {
        "model": "gpt-latest",
        "effort": "ultra",
        "approval_policy": "on-request",
        "service_tier": "fast",
        "collaboration_mode": "plan",
    }


def test_codex_session_settings_restores_last_valid_collaboration_mode(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "turn_context", "payload": {
            "model": "gpt-old", "effort": "high",
            "collaboration_mode": {"mode": "plan", "settings": {
                "model": "gpt-old", "developer_instructions": "do not replay",
            }},
        }},
        {"type": "turn_context", "payload": {
            "model": "gpt-new", "effort": "xhigh",
            "collaboration_mode": {"mode": "unsupported"},
        }},
        {"type": "turn_context", "payload": {
            "collaboration_mode": {"mode": "default"},
        }},
    ]) + "\n")
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings("session-1") == {
        "model": "gpt-new",
        "effort": "xhigh",
        "collaboration_mode": "default",
    }


def test_codex_history_skips_one_oversized_record_and_continues(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        "x" * 300,
        json.dumps({
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "session-1"},
        }),
        json.dumps({
            "timestamp": "2026-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "survived"},
        }),
    ]
    rollout.write_text("\n".join(rows) + "\n")
    monkeypatch.setattr(codex_stream_module, "_MAX_HISTORY_RECORD_CHARS", 180)

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert [event.prompt for event in events if event.type == "user_msg"] == [
        "survived"
    ]
