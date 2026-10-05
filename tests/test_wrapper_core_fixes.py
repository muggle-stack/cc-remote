"""Zero-token regressions for wrapper drain and Codex rollout history."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import (
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)

from cc_remote.protocol import (
    AssistantMsgStart, ERR_DRAIN_TIMEOUT, Error, Interrupt, StateEvent,
    TurnBinding, TurnEnd, UserMsg,
)
from cc_remote.config import WrapperConfig
from cc_remote.wrapper import codex_sessions as codex_sessions_module
from cc_remote.wrapper import codex_stream as codex_stream_module
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.codex_sessions import codex_session_settings
from cc_remote.wrapper.codex_stream import (
    codex_history_native_witness, codex_history_window,
    codex_native_rollback_turns,
    codex_translate_history,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input
from cc_remote.wrapper.session import (
    _session_file,
    load_session_id,
    load_session_state,
    save_session_id,
)
from cc_remote.wrapper.stream import replayed_user_message_id
from tests.test_multisession import _StubTransport, _mk_ctx, _mk_machine


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

    async def query(
        self, prompt, images=None, *, client_user_message_id=None,
    ):
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


class _ClaudeStalledSdk(_StalledSdk):
    model = None
    next_turn_id = None

    def observe_goal_message(self, _message, _thread_id):
        return False, None

    async def refresh_goal(self, _session_id):
        return None

    def release_background_messages(self):
        return None


def test_claude_replayed_user_id_excludes_tool_protocol_envelopes():
    native_id = "2259073b-7676-455f-b7b0-b9b3892dbe93"
    assert replayed_user_message_id(UserMessage(
        content="继续", uuid=native_id,
    )) == native_id
    assert replayed_user_message_id(UserMessage(
        content=[ToolResultBlock(tool_use_id="tool-1", content="done")],
        uuid=native_id,
    )) is None
    assert replayed_user_message_id(UserMessage(
        content="subagent input",
        uuid=native_id,
        parent_tool_use_id="tool-1",
    )) is None
    assert replayed_user_message_id(UserMessage(
        content="<task-notification>done</task-notification>",
        uuid=native_id,
        origin={"kind": "task-notification"},
    )) is None
    assert replayed_user_message_id(UserMessage(
        content=[TextBlock(text="[Request interrupted by user]")],
        uuid=native_id,
    )) is None


def test_claude_live_replayed_user_uuid_is_persisted_as_browser_alias(
    monkeypatch, tmp_path,
):
    session_id = "fa800ca3-18e3-4391-b401-a33fe52e2f56"
    interrupt_id = "2259073b-7676-455f-b7b0-010101010101"
    native_id = "2259073b-7676-455f-b7b0-b9b3892dbe93"
    client_id = "6b09ee37-f861-4422-b98a-21f509c951b0"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(
        machine_module, "transcript_path", lambda _sid: str(transcript))

    class ReplaySdk:
        effort = "high"
        applied_effort = "high"
        model = None
        next_turn_id = None

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            # A replacement turn can see the preceding turn's late synthetic
            # interrupt row before its own replayed user UUID.
            yield UserMessage(
                content=[TextBlock(text="[Request interrupted by user]")],
                uuid=interrupt_id,
            )
            yield UserMessage(content="继续", uuid=native_id)
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=session_id,
            )

        def observe_goal_message(self, _message, _thread_id):
            return False, None

        async def refresh_goal(self, _session_id):
            return None

        def release_background_messages(self):
            return None

    async def run():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(session_id, session_id)
        ctx.sdk = ReplaySdk()
        ctx.state = "running"
        ctx.active_msg_id = client_id
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(machine, "_prime_claude_ownership", no_external_owner)
        await machine._run_turn(ctx, "继续")

        assert machine._claude_client_messages.get(
            session_id, transcript) == {native_id: client_id}
        assert ctx.claude_client_message_ids == {}
        assert ctx.state == "idle"

    asyncio.run(run())


def test_claude_run_turn_binds_transcript_before_delayed_sdk_replay(
    monkeypatch, tmp_path,
):
    session_id = "fa800ca3-18e3-4391-b401-010101010101"
    native_id = "2259073b-7676-455f-b7b0-020202020202"
    client_id = "6b09ee37-f861-4422-b98a-030303030303"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        machine_module, "transcript_path", lambda _sid: str(transcript))

    async def run():
        class DelayedReplaySdk:
            effort = "high"
            applied_effort = "high"
            model = None
            next_turn_id = None

            def __init__(self):
                self.query_written = asyncio.Event()
                self.release_replay = asyncio.Event()

            async def query(self, _prompt):
                with transcript.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "type": "user",
                        "uuid": native_id,
                        "entrypoint": "sdk-py",
                        "message": {"role": "user", "content": "hello"},
                    }) + "\n")
                self.query_written.set()

            async def receive_response(self):
                await self.release_replay.wait()
                yield UserMessage(content="hello", uuid=native_id)
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id=session_id,
                )

            def observe_goal_message(self, _message, _thread_id):
                return False, None

            async def refresh_goal(self, _session_id):
                return None

            def release_background_messages(self):
                return None

        machine, transport = _mk_machine()
        sdk = DelayedReplaySdk()
        ctx = _mk_ctx(session_id, session_id)
        ctx.sdk = sdk
        ctx.state = "running"
        ctx.active_msg_id = client_id
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(machine, "_prime_claude_ownership", no_external_owner)
        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.query_written.wait(), timeout=1)
        assert ctx.claude_client_alias_probe is not None
        await machine._advance_claude_client_alias_probe(ctx)
        assert machine._claude_client_messages.get(
            session_id, transcript) == {native_id: client_id}

        sdk.release_replay.set()
        await asyncio.wait_for(turn, timeout=1)
        assert ctx.state == "idle"
        assert not [
            message for message in transport.sent
            if isinstance(message, Error)
        ]
        assert [
            (message.msg_id, message.turn_id)
            for message in transport.sent
            if isinstance(message, TurnBinding)
        ] == [(client_id, native_id)]

    asyncio.run(run())


def test_claude_alias_flush_never_enters_multi_profile_codex_history(
    monkeypatch, tmp_path,
):
    session_id = "f6cd73f7-86d7-4115-8512-3cf357fbd542"
    native_id = "759d1121-1009-4882-8218-b31296d7e20b"
    client_id = "6b09ee37-f861-4422-b98a-21f509c951b0"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("{}\n")
    primary = tmp_path / "codex-primary"
    stack = tmp_path / "codex-stack"
    primary.mkdir()
    stack.mkdir()
    cfg = WrapperConfig()
    cfg.state_dir = tmp_path / "state"
    cfg.claude_work_root = cfg.state_dir / "work" / "claude"
    cfg.codex_work_root = cfg.state_dir / "work" / "codex"
    cfg.codex_profiles_json = json.dumps({
        "primary": {
            "label": "Primary",
            "home": str(primary),
            "default": True,
        },
        "stack": {
            "label": "Stack",
            "home": str(stack),
        },
    })
    transport = _StubTransport()
    machine = WrapperMachine(cfg, transport)
    monkeypatch.setattr(
        machine_module, "transcript_path", lambda _sid: str(transcript))
    ctx = _mk_ctx(session_id, session_id)
    ctx.active_msg_id = client_id
    ctx.claude_client_message_ids[native_id] = client_id
    machine.sessions[ctx.key] = ctx

    async def run():
        await machine._flush_claude_client_message_ids(ctx)

        assert machine._claude_client_messages.get(
            session_id, transcript) == {native_id: client_id}
        assert ctx.claude_client_message_ids == {}
        assert not [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]

    asyncio.run(run())


def test_claude_alias_metadata_failure_keeps_turn_binding_idempotent(
    monkeypatch,
):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-alias-failure", "claude-alias-failure")
        ctx.active_msg_id = "browser-message"
        machine.sessions[ctx.key] = ctx

        async def fail_flush(_ctx):
            raise RuntimeError("metadata store unavailable")

        monkeypatch.setattr(
            machine, "_flush_claude_client_message_ids", fail_flush)
        assert await machine._remember_claude_client_message_id(
            ctx, "native-message") is True
        assert await machine._remember_claude_client_message_id(
            ctx, "native-message") is False

        assert ctx.claude_client_message_ids == {
            "native-message": "browser-message",
        }
        assert [
            (message.msg_id, message.turn_id)
            for message in transport.sent
            if isinstance(message, TurnBinding)
        ] == [("browser-message", "native-message")]

    asyncio.run(run())


def test_claude_alias_binding_rejects_stale_turn_generation():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-generation", "claude-generation")
        ctx.active_msg_id = "browser-old"
        machine.sessions[ctx.key] = ctx
        machine._start_claude_client_alias_probe(ctx)
        old_generation = ctx.claude_client_alias_generation

        ctx.active_msg_id = "browser-new"
        machine._start_claude_client_alias_probe(ctx)
        assert await machine._remember_claude_client_message_id(
            ctx,
            "native-old",
            expected_msg_id="browser-old",
            expected_generation=old_generation,
        ) is False
        assert ctx.claude_client_message_ids == {}
        assert not [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]

    asyncio.run(run())


def test_new_claude_session_flushes_pending_browser_alias_after_rekey(
    monkeypatch, tmp_path,
):
    session_id = "fa800ca3-18e3-4391-b401-a33fe52e2f56"
    native_id = "2259073b-7676-455f-b7b0-b9b3892dbe93"
    client_id = "6b09ee37-f861-4422-b98a-21f509c951b0"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr(
        machine_module, "transcript_path", lambda _sid: str(transcript))

    class NewSessionReplaySdk:
        effort = "high"
        applied_effort = "high"
        model = None
        next_turn_id = None

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            yield UserMessage(content="继续", uuid=native_id)
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=session_id,
            )

        def observe_goal_message(self, _message, _thread_id):
            return False, None

        def rekey_goal(self, _session_id):
            return None

        async def refresh_goal(self, _session_id):
            return None

        def release_background_messages(self):
            return None

    async def run():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("tmp-client-alias", None)
        ctx.sdk = NewSessionReplaySdk()
        ctx.state = "running"
        ctx.active_msg_id = client_id
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(machine, "_prime_claude_ownership", no_external_owner)
        await machine._run_turn(ctx, "继续")

        assert ctx.key == session_id
        assert machine.sessions[session_id] is ctx
        assert "tmp-client-alias" not in machine.sessions
        assert machine._claude_client_messages.get(
            session_id, transcript,
        ) == {native_id: client_id}
        assert ctx.claude_client_message_ids == {}

    asyncio.run(run())


def test_codex_session_id_accepts_app_server_thread_id_notifications():
    assert codex_stream_module.codex_session_id({
        "method": "turn/started",
        "params": {"threadId": "thread-current", "turnId": "turn-1"},
    }) == "thread-current"
    assert codex_stream_module.codex_session_id({
        "method": "thread/started",
        "params": {"thread": {"id": "thread-object"}},
    }) == "thread-object"


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


def test_managed_finalizer_reconciles_background_result_race(monkeypatch):
    class FinalizerRaceSdk:
        effort = "max"
        applied_effort = "max"
        model = None
        next_turn_id = None
        is_claude_broker = False

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session-race",
            )

        def observe_goal_message(self, _message, _thread_id):
            return False, None

        async def refresh_goal(self, _session_id):
            return None

        def release_background_messages(self):
            return None

    async def run():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx("session-race", "session-race")
        ctx.sdk = FinalizerRaceSdk()
        ctx.state = "running"
        ctx.active_msg_id = "message-race"
        ctx.claude_background_followup_pending = True
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", no_external_owner)
        original_set_idle = machine._set_idle_after_managed_turn
        delivered = False

        async def settle_then_deliver_background(
            target, *, claude_terminal=False,
        ):
            nonlocal delivered
            await original_set_idle(
                target, claude_terminal=claude_terminal)
            if claude_terminal and not delivered:
                delivered = True
                assert target.state == "running"
                await machine._on_claude_background_message(
                    target,
                    ResultMessage(
                        subtype="success",
                        duration_ms=1,
                        duration_api_ms=1,
                        is_error=False,
                        num_turns=1,
                        session_id="session-race",
                    ),
                    "message-race",
                )
                assert target.state == "running"

        monkeypatch.setattr(
            machine, "_set_idle_after_managed_turn",
            settle_then_deliver_background)
        turn = asyncio.create_task(machine._run_turn(ctx, "race"))
        ctx.turn_task = turn
        await asyncio.wait_for(turn, timeout=1)

        assert delivered is True
        assert ctx.turn_task is None
        assert ctx.claude_background_followup_pending is False
        assert ctx.state == "idle"

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


def test_session_state_persists_claude_profile_revision(tmp_path):
    cwd = "/workspace/profiled"
    save_session_id(
        tmp_path,
        cwd,
        "nyx@session-1",
        claude_profile_id="nyx",
        claude_profile_revision=4,
    )

    state = load_session_state(tmp_path, cwd)
    assert state is not None
    assert state.session_id == "nyx@session-1"
    assert state.claude_profile_id == "nyx"
    assert state.claude_profile_revision == 4


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


def test_all_files_diff_includes_untracked_regular_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Test",
        "-c", "user.email=test@example.com", "commit", "-qm", "initial",
    ], check=True)
    tracked.write_text("after\n")
    (repo / "chat.html").write_text("<h1>交付物</h1>\n")

    async def run():
        machine, _ = _mk_machine()
        diff = await machine._git_diff(str(repo), "")
        assert "diff --git a/tracked.txt b/tracked.txt" in diff
        assert "+after" in diff
        assert "diff --git a/chat.html b/chat.html" in diff
        assert "new file mode" in diff
        assert "+<h1>交付物</h1>" in diff

    asyncio.run(run())


def test_explicit_file_diff_works_outside_git_repository(tmp_path):
    workspace = tmp_path / "work" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "chat.html").write_text("<h1>Work artifact</h1>\n")

    async def run():
        machine, _ = _mk_machine()
        diff = await machine._git_diff(str(workspace), "chat.html")
        assert "diff --git a/chat.html b/chat.html" in diff
        assert "new file mode" in diff
        assert "+<h1>Work artifact</h1>" in diff

    asyncio.run(run())


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


@pytest.mark.parametrize("watchdog_first", [False, True])
def test_claude_managed_interrupt_owns_recovery_with_background_activity(watchdog_first):
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.drain_timeout = 0.03
        ctx = _mk_ctx("claude-stop-owner", "claude-stop-owner")
        ctx.engine = "claude"
        ctx.state = "running"
        ctx.active_msg_id = "stop-message"
        machine.sessions[ctx.key] = ctx

        class StalledSdk(_ClaudeStalledSdk):
            async def force_reconnect(self, resume_id, cwd, **_kwargs):
                self.reconnects += 1
                # Real disconnect/connect yields before lifecycle reset. Both
                # expired consumers must not reconnect the same native child.
                await asyncio.sleep(0.02)
                machine._reset_claude_task_lifecycle(ctx)

        sdk = StalledSdk()
        ctx.sdk = sdk

        async def no_external_owner(_sid):
            return False

        machine._prime_claude_ownership = no_external_owner
        turn = asyncio.create_task(machine._run_turn(ctx, "inspect"))
        ctx.turn_task = turn
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=1)
        ctx.claude_background_followups["background-request"] = "active"
        if watchdog_first:
            # An existing watchdog can gain a managed owner before its deadline
            # when an already accepted steering input completes its handoff.
            ctx.turn_task = None
        await machine._handle_interrupt(SimpleNamespace(sid=ctx.key))
        watchdog = ctx.claude_autonomous_interrupt_task
        ctx.turn_task = turn
        await asyncio.wait_for(turn, timeout=1)
        if watchdog is not None:
            await asyncio.gather(watchdog, return_exceptions=True)

        assert sdk.reconnects == 1
        assert ctx.state == "idle"
        assert ctx.claude_autonomous_interrupt_task is None
        failures = [msg for msg in transport.sent
                    if getattr(msg, "code", None) == ERR_DRAIN_TIMEOUT]
        assert len(failures) == 1
        assert failures[0].msg_id == "stop-message"

    asyncio.run(run())


def test_claude_interrupt_hands_remaining_background_work_the_original_deadline():
    async def run():
        machine, transport = _mk_machine()
        machine.cfg.drain_timeout = 0.1
        ctx = _mk_ctx("claude-stop-handoff", "claude-stop-handoff")
        ctx.engine = "claude"
        ctx.state = "running"
        ctx.active_msg_id = "stop-message"
        machine.sessions[ctx.key] = ctx

        class StalledSdk(_ClaudeStalledSdk):
            async def force_reconnect(self, resume_id, cwd, **_kwargs):
                self.reconnects += 1
                machine._reset_claude_task_lifecycle(ctx)

        sdk = StalledSdk()
        sdk.responses = [ResultMessage(
            subtype="error_during_execution", duration_ms=1,
            duration_api_ms=1, is_error=True, num_turns=1,
            session_id=ctx.session_id,
        )]
        ctx.sdk = sdk

        async def no_external_owner(_sid):
            return False

        machine._prime_claude_ownership = no_external_owner
        turn = asyncio.create_task(machine._run_turn(ctx, "inspect"))
        ctx.turn_task = turn
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=1)
        ctx.claude_background_followups["background-request"] = "active"
        await machine._handle_interrupt(SimpleNamespace(sid=ctx.key))
        deadline = ctx.interrupt_deadline
        # A handoff must retain the accepted Stop's deadline, not start a fresh
        # wait using the config value at the managed terminal.
        machine.cfg.drain_timeout = 100
        sdk.release.set()
        await asyncio.wait_for(turn, timeout=1)

        assert ctx.state == "interrupting"
        assert ctx.interrupt_deadline == deadline
        watchdog = ctx.claude_autonomous_interrupt_task
        assert watchdog is not None
        await asyncio.wait_for(watchdog, timeout=1)
        assert sdk.reconnects == 1
        assert ctx.state == "idle"
        assert ctx.claude_autonomous_interrupt_task is None
        assert len([msg for msg in transport.sent
                    if getattr(msg, "code", None) == ERR_DRAIN_TIMEOUT]) == 1

    asyncio.run(run())


def test_codex_silence_emits_no_synthetic_waiting_notice_and_later_completes(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
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

        real_wait = machine_module.asyncio.wait
        wait_options = []

        async def wait_without_idle_deadline(tasks, *args, **kwargs):
            wait_options.append(dict(kwargs))
            return await real_wait(tasks, *args, **kwargs)

        monkeypatch.setattr(
            machine_module.asyncio, "wait", wait_without_idle_deadline,
        )
        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await asyncio.sleep(0.04)

        assert wait_options
        assert all("timeout" not in options for options in wait_options)
        notices = [msg for msg in transport.sent
                   if isinstance(msg, StateEvent) and msg.phase == "waiting"]
        assert notices == []
        assert ctx.state == "running"
        assert sdk.reconnects == 0

        sdk.release.set()
        await asyncio.wait_for(turn, timeout=0.5)

        assert ctx.state == "idle"
        assert sdk.reconnects == 0
        assert [msg.text for msg in transport.sent
                if getattr(msg, "type", None) == "delta"] == ["done"]

    asyncio.run(run())


def test_claude_silence_emits_neutral_notices_without_ending_or_reconnecting(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.CLAUDE_SILENCE_NOTICE_SECONDS = 0.015
        machine.CLAUDE_SILENCE_WARNING_SECONDS = 0.04
        sdk = _ClaudeStalledSdk()
        sdk.responses = [ResultMessage(
            subtype="success",
            duration_ms=50,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="claude-stalled",
        )]
        ctx = _mk_ctx("claude-stalled", "claude-stalled")
        ctx.sdk = sdk
        ctx.state = "running"
        ctx.active_msg_id = "message-stalled"
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", no_external_owner)

        async def wait_for_notices(count: int):
            while len([
                message for message in transport.sent
                if isinstance(message, StateEvent)
                and message.phase == "waiting"
            ]) < count:
                await asyncio.sleep(0.002)

        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await asyncio.wait_for(wait_for_notices(2), timeout=0.3)

        notices = [
            message for message in transport.sent
            if isinstance(message, StateEvent) and message.phase == "waiting"
        ]
        assert [message.msg_id for message in notices] == [
            "message-stalled", "message-stalled",
        ]
        assert "思考" in notices[0].detail
        assert "等待上游响应" in notices[0].detail
        assert "停止本回合后重试" in notices[1].detail
        assert ctx.state == "running"
        assert sdk.reconnects == 0

        sdk.release.set()
        await asyncio.wait_for(turn, timeout=0.5)

        assert ctx.state == "idle"
        assert sdk.reconnects == 0
        assert not [
            message for message in transport.sent
            if isinstance(message, Error)
        ]
        assert any(
            isinstance(message, StateEvent) and message.detail is None
            for message in transport.sent
        )

    asyncio.run(run())


def test_claude_silence_notice_is_suppressed_while_question_is_open(
        monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        machine.CLAUDE_SILENCE_NOTICE_SECONDS = 0.01
        machine.CLAUDE_SILENCE_WARNING_SECONDS = 0.025
        sdk = _ClaudeStalledSdk()
        sdk.responses = [ResultMessage(
            subtype="success",
            duration_ms=50,
            duration_api_ms=50,
            is_error=False,
            num_turns=1,
            session_id="claude-question-wait",
        )]
        ctx = _mk_ctx("claude-question-wait", "claude-question-wait")
        ctx.sdk = sdk
        ctx.state = "running"
        ctx.active_msg_id = "message-question-wait"
        pending = asyncio.get_running_loop().create_future()
        ctx.pending_asks["open-question"] = pending
        machine.sessions[ctx.key] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", no_external_owner)
        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await asyncio.sleep(0.05)
        assert not [
            message for message in transport.sent
            if isinstance(message, StateEvent) and message.phase == "waiting"
        ]

        ctx.pending_asks.pop("open-question")
        pending.cancel()
        machine._mark_claude_activity(ctx)

        async def wait_for_notice():
            while not any(
                isinstance(message, StateEvent)
                and message.phase == "waiting"
                for message in transport.sent
            ):
                await asyncio.sleep(0.002)

        await asyncio.wait_for(wait_for_notice(), timeout=0.2)
        sdk.release.set()
        await asyncio.wait_for(turn, timeout=0.5)
        assert ctx.state == "idle"
        assert sdk.reconnects == 0

    asyncio.run(run())


def test_codex_silence_preserves_later_authoritative_provider_error():
    async def run():
        machine, transport = _mk_machine()
        sdk = _StalledSdk()
        sdk.responses = [
            {"method": "error", "params": {
                "willRetry": False,
                "error": {
                    "message": "stream disconnected before completion",
                    "codexErrorInfo": {
                        "responseStreamDisconnected": {"httpStatusCode": 502},
                    },
                },
            }},
            {"method": "turn/completed", "params": {
                "turn": {
                    "id": "provider-failed-turn",
                    "status": "failed",
                    "durationMs": 30,
                    "error": {"message": "HTTP 502"},
                }}},
        ]
        ctx = _mk_ctx("codex-provider-error", "codex-provider-error")
        ctx.sdk = sdk
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "message-provider-error"
        machine.sessions[ctx.key] = ctx

        turn = asyncio.create_task(machine._run_turn(ctx, "hello"))
        await asyncio.wait_for(sdk.reader_started.wait(), timeout=0.2)
        await asyncio.sleep(0.04)
        assert not [msg for msg in transport.sent
                    if isinstance(msg, StateEvent) and msg.phase == "waiting"]

        sdk.release.set()
        await asyncio.wait_for(turn, timeout=0.5)

        errors = [msg for msg in transport.sent if isinstance(msg, Error)]
        assert errors
        assert errors[0].msg_id == "message-provider-error"
        assert errors[0].message == "Codex 上游服务暂时不可用，请稍后重试。"
        terminal = [msg for msg in transport.sent if isinstance(msg, TurnEnd)][-1]
        assert terminal.result.subtype == "error"
        assert terminal.result.is_error is True

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
                     "duration_ms": 3000,
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
    # Visible partial output is not completion evidence. The synthetic error
    # boundary keeps turn_id=None so it never steals turn-next's id.
    assert [event.result.subtype for event in terminals] == ["error", "success"]
    assert [event.result.is_error for event in terminals] == [True, False]


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


def test_codex_history_new_goal_objective_is_a_durable_user_prompt(tmp_path):
    rollout = tmp_path / "rollout-goal-objective.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": "session-goal-objective"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated",
                     "threadId": "session-goal-objective",
                     "goal": {
                         "threadId": "session-goal-objective",
                         "objective": "证明泰勒展开",
                         "status": "active", "tokensUsed": 0,
                         "timeUsedSeconds": 0,
                         "createdAt": 1, "updatedAt": 1,
                     }}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-goal"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "证明过程"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-goal"}},
        # Resuming the same objective may start another automatic turn, but it
        # must remain an assistant-only continuation rather than repeating the
        # original user request.
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated",
                     "threadId": "session-goal-objective",
                     "goal": {
                         "threadId": "session-goal-objective",
                         "objective": "证明泰勒展开",
                         "status": "active", "tokensUsed": 10,
                         "timeUsedSeconds": 30,
                         "createdAt": 1, "updatedAt": 2,
                     }}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-resume"}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "继续处理"}},
        {"timestamp": "2026-01-01T00:01:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-resume"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    prompts = [event for event in events if isinstance(event, UserMsg)]
    assert [(event.msg_id, event.prompt) for event in prompts] == [
        ("turn-goal", "证明泰勒展开"),
    ]
    assert [event.turn_id for event in events if isinstance(event, TurnEnd)] == [
        "turn-goal", "turn-resume",
    ]


def test_codex_history_goal_metadata_without_a_turn_never_steals_next_user(
        tmp_path):
    rollout = tmp_path / "rollout-paused-goal.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             "objective": "旧目标", "status": "active",
             "createdAt": 1, "updatedAt": 1,
         }}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "goal-turn",
                     "started_at": 1}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "目标结果"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "goal-turn"}},
        # Editing paused metadata does not launch a Goal turn.
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             "objective": "暂停后的新目标", "status": "paused",
             "createdAt": 1, "updatedAt": 4,
         }}},
        {"timestamp": "2026-01-01T00:01:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "ordinary-turn",
                     "started_at": 61}},
        {"timestamp": "2026-01-01T00:01:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "普通问题"}},
        {"timestamp": "2026-01-01T00:01:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "普通答案"}},
        {"timestamp": "2026-01-01T00:01:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "ordinary-turn"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert [event.prompt for event in events if isinstance(event, UserMsg)] == [
        "旧目标", "普通问题",
    ]
    assert [event.result.subtype for event in events
            if isinstance(event, TurnEnd)] == ["success", "success"]


def test_codex_history_goal_candidate_yields_to_same_task_user_message(
        tmp_path):
    rollout = tmp_path / "rollout-goal-user-race.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             "objective": "候选目标", "status": "active",
             "createdAt": 1.0, "updatedAt": 1.0,
         }}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "ordinary-turn"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "普通问题"}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "普通答案"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "ordinary-turn"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert [event.prompt for event in events if isinstance(event, UserMsg)] == [
        "普通问题",
    ]
    assert [event.result.subtype for event in events
            if isinstance(event, TurnEnd)] == ["success"]


def test_codex_history_goal_update_inside_user_turn_does_not_split_prompt(
        tmp_path):
    rollout = tmp_path / "rollout-goal-inside-user-turn.jsonl"
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "managed-turn"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "创建目标"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "thread_goal_updated", "goal": {
             "objective": "稍后自动执行", "status": "active",
             "createdAt": 1, "updatedAt": 1,
         }}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "目标已创建"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "managed-turn"}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows))

    events, _ = codex_translate_history(str(rollout), 10_000)

    assert [event.prompt for event in events if isinstance(event, UserMsg)] == [
        "创建目标",
    ]
    assert [event.result.subtype for event in events
            if isinstance(event, TurnEnd)] == ["success"]


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


@pytest.mark.parametrize("tail_starts_inside_record", [False, True])
def test_codex_settings_skip_oversized_record_and_read_newer_selection(
        monkeypatch, tmp_path, tail_starts_inside_record):
    rollout = tmp_path / "rollout-session-1.jsonl"
    old = json.dumps({
        "type": "turn_context", "payload": {"model": "gpt-old"},
    }) + "\n"
    latest = json.dumps({
        "type": "event_msg", "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-selected", "reasoning_effort": "xhigh",
                "service_tier": "priority",
            },
        },
    }) + "\n"
    rollout.write_text(old + ("x" * 6000) + "\n" + latest)
    monkeypatch.setattr(codex_sessions_module, "MAX_JSONL_RECORD_BYTES", 256)
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings(
        "session-1", max_bytes=2000 if tail_starts_inside_record else 10000,
    ) == {
        "model": "gpt-selected", "effort": "xhigh",
        "service_tier": "priority",
    }


def test_codex_settings_tail_stops_at_captured_source_size(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    first = json.dumps({
        "type": "turn_context", "payload": {"model": "gpt-snapshot"},
    }) + "\n"
    appended = json.dumps({
        "type": "turn_context", "payload": {"model": "gpt-after-snapshot"},
    }) + "\n"
    rollout.write_text(first + appended)
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))
    monkeypatch.setattr(codex_sessions_module.os.path, "getsize", lambda _p: len(first))

    assert codex_session_settings("session-1", max_bytes=len(first)) == {
        "model": "gpt-snapshot",
    }


def test_codex_session_settings_restores_applied_update_before_next_turn(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "turn_context", "payload": {
            "model": "gpt-before", "effort": "low",
            "approval_policy": "on-request", "service_tier": "fast",
            "collaboration_mode": {"mode": "default"},
        }},
        # app-server persists this immediately. There is deliberately no newer
        # turn_context: this is the wrapper-restart window that used to restore
        # gpt-before until the user sent another message.
        {"type": "event_msg", "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-after",
                "reasoning_effort": "xhigh",
                "approval_policy": "never",
                "active_permission_profile": {"id": ":danger-full-access"},
                "service_tier": "default",
                "collaboration_mode": {"mode": "plan", "settings": {
                    "model": "gpt-after",
                    "developer_instructions": "must not escape into output",
                }},
            },
        }},
    ]) + "\n")
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings("session-1") == {
        "model": "gpt-after",
        "effort": "xhigh",
        "approval_policy": "never",
        "permission_profile": ":danger-full-access",
        "service_tier": None,
        "collaboration_mode": "plan",
    }


def test_codex_session_settings_marks_granular_approval_without_flattening(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text(json.dumps({
        "type": "turn_context",
        "payload": {
            "model": "gpt-test",
            "approval_policy": {"granular": {
                "mcp_elicitations": True,
                "rules": False,
                "sandbox_approval": True,
            }},
        },
    }) + "\n")
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings("session-1") == {
        "model": "gpt-test",
        "approval_policy_granular": True,
    }


def test_codex_session_settings_last_ordered_record_wins_after_update(
        monkeypatch, tmp_path):
    rollout = tmp_path / "rollout-session-1.jsonl"
    rollout.write_text("\n".join(json.dumps(row) for row in [
        {"type": "turn_context", "payload": {
            "model": "gpt-first", "effort": "low",
        }},
        {"type": "event_msg", "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "model": "gpt-second", "reasoning_effort": "high",
                "service_tier": "default",
                "collaboration_mode": {"mode": "plan"},
            },
        }},
        {"type": "turn_context", "payload": {
            "model": "gpt-third", "effort": "ultra",
            "service_tier": "fast",
            "collaboration_mode": {"mode": "default"},
        }},
    ]) + "\n")
    monkeypatch.setattr(
        codex_sessions_module, "_rollout_path", lambda _sid: str(rollout))

    assert codex_session_settings("session-1") == {
        "model": "gpt-third",
        "effort": "ultra",
        "service_tier": "fast",
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


@pytest.mark.parametrize("modern_marker", [False, True])
def test_account_switch_continuation_is_one_history_and_rollback_turn(
    tmp_path, modern_marker,
):
    rollout = tmp_path / "rollout-account-switch.jsonl"
    internal = machine_module.CODEX_ACCOUNT_SWITCH_CONTINUATION
    rows = [
        {"type": "session_meta", "payload": {"id": "session-1"}},
        {"type": "turn_context", "payload": {"turn_id": "turn-old"}},
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-old",
        }},
        {"type": "event_msg", "payload": {
            "type": "user_message", "turn_id": "turn-old",
            "message": "finish task A",
        }},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "turn_id": "turn-old",
            "message": "partial work",
        }},
        {"type": "event_msg", "payload": {
            "type": "turn_aborted", "turn_id": "turn-old",
        }},
        {"type": "turn_context", "payload": {"turn_id": "turn-new"}},
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-new",
        }},
        {"type": "event_msg", "payload": (
            {
                "type": "item_completed", "turn_id": "turn-new",
                "item": {
                    "id": "account-switch-continuation",
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": internal}],
                },
            }
            if modern_marker else
            {
                "type": "user_message", "turn_id": "turn-new",
                "message": internal,
            }
        )},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "id": "switch-call",
            "call_id": "switch-call", "name": "exec_command", "input": {},
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "switch-call",
            "output": "ok",
        }},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "turn_id": "turn-new",
            "message": "task A complete",
        }},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-new",
            "last_agent_message": "task A complete",
        }},
        {"type": "turn_context", "payload": {"turn_id": "turn-b"}},
        {"type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "turn-b",
        }},
        {"type": "event_msg", "payload": {
            "type": "user_message", "turn_id": "turn-b",
            "message": "task B",
        }},
        {"type": "event_msg", "payload": {
            "type": "agent_message", "turn_id": "turn-b",
            "message": "task B complete",
        }},
        {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "turn-b",
            "last_agent_message": "task B complete",
        }},
    ]
    rollout.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    events, _ = codex_translate_history(str(rollout), 10_000)
    assert [
        event.prompt for event in events if isinstance(event, UserMsg)
    ] == ["finish task A", "task B"]
    terminals = [
        event for event in events if isinstance(event, TurnEnd)
    ]
    assert [event.turn_id for event in terminals] == ["turn-new", "turn-b"]
    assert all(not event.result.is_error for event in terminals)
    assert [
        cursor
        for _offset, cursor in codex_stream_module._history_boundaries(
            str(rollout), use_turns=True)
    ] == ["turn-b", "turn-old"]
    start, end, has_more, forced, forced_offset = codex_history_window(
        str(rollout), before="turn-b", limit=1)
    assert (has_more, forced, forced_offset) == (False, None, None)
    witness = codex_history_native_witness(
        str(rollout),
        max_turns=4,
        max_scan_bytes=None,
        required_turn_ids=("turn-old", "turn-new"),
    )
    assert witness.turn_ids == ("turn-b", "turn-old")
    assert set(witness.process_by_visible_id) == {"turn-old"}
    previous_page, _ = codex_translate_history(
        str(rollout), 10_000, start_offset=start, end_offset=end)
    assert [
        event.prompt for event in previous_page if isinstance(event, UserMsg)
    ] == ["finish task A"]
    assert codex_native_rollback_turns(str(rollout), 1) == 1
    assert codex_native_rollback_turns(str(rollout), 2) == 3
