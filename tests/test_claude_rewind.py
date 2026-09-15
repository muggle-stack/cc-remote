"""Claude checkpoint and native conversation rewind tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import cc_remote.wrapper.machine as machine_module
from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    Error,
    History,
    HistoryInvalidated,
    RollbackResult,
    RollbackSession,
)
from cc_remote.wrapper.claude_rewind import ClaudeRewindError
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_multisession import _mk_ctx, _mk_machine


SESSION_ID = "019f60cf-85f5-7881-8521-7d943df84a4b"
TARGET_ID = "12345678-1234-4234-8234-123456789abc"
NEWER_TARGET_ID = "22345678-1234-4234-8234-123456789abc"
LATEST_TARGET_ID = "32345678-1234-4234-8234-123456789abc"
ASSISTANT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PROBE_ID = "00000000-0000-0000-0000-000000000000"


def test_claude_options_enable_file_checkpoints_and_user_message_replay():
    options = SdkHandle(WrapperConfig())._options(None, "/tmp")

    assert options.enable_file_checkpointing is True
    assert options.extra_args == {
        "replay-user-messages": None,
        "thinking-display": "summarized",
    }


def test_claude_file_rewind_uses_public_sdk_and_structured_errors():
    class Client:
        def __init__(self):
            self.targets = []

        async def rewind_files(self, target):
            self.targets.append(target)

    async def go():
        handle = SdkHandle(WrapperConfig())
        client = Client()
        handle.client = client

        await handle.rewind_files(TARGET_ID.upper())
        assert client.targets == [TARGET_ID]

        with pytest.raises(ClaudeRewindError) as invalid:
            await handle.rewind_files("not-a-message-id")
        assert invalid.value.as_dict() == {
            "code": "invalid_target",
            "message": "The rewind target is not a valid message id.",
            "operation": "files",
            "retryable": False,
        }

        handle.client = None
        with pytest.raises(ClaudeRewindError) as disconnected:
            await handle.rewind_files(TARGET_ID)
        assert disconnected.value.code == "not_connected"
        assert disconnected.value.operation == "files"

    asyncio.run(go())


def test_claude_conversation_rewind_probes_then_normalizes_success():
    class Query:
        def __init__(self):
            self.calls = []

        async def _send_control_request(self, request, timeout):
            self.calls.append((request, timeout))
            if request["target_message_uuid"] == PROBE_ID:
                return {"rewound": False, "error": "target not found"}
            return {
                "rewound": True,
                "targetMessageUuid": request["target_message_uuid"],
                "prefillText": "continue here",
                "precedingAssistantUuid": ASSISTANT_ID,
            }

    async def go():
        query = Query()
        handle = SdkHandle(WrapperConfig())
        handle.client = SimpleNamespace(_query=query)

        result = await handle.rewind_conversation(TARGET_ID)

        assert result.as_dict() == {
            "target_message_uuid": TARGET_ID,
            "prefill_text": "continue here",
            "preceding_assistant_uuid": ASSISTANT_ID,
        }
        assert query.calls == [
            (
                {
                    "subtype": "rewind_conversation",
                    "target_message_uuid": PROBE_ID,
                    "interrupt_if_running": False,
                },
                5.0,
            ),
            (
                {
                    "subtype": "rewind_conversation",
                    "target_message_uuid": TARGET_ID,
                    "interrupt_if_running": False,
                },
                30.0,
            ),
        ]
        # The capability is cached; a second read is not another control RPC.
        assert await handle.supports_rewind_conversation() is True
        assert len(query.calls) == 2

    asyncio.run(go())


def test_claude_conversation_rewind_guards_unsupported_private_subtype():
    class Query:
        calls = 0

        async def _send_control_request(self, request, timeout):
            self.calls += 1
            raise RuntimeError(
                "Unsupported control request subtype: rewind_conversation"
            )

    async def go():
        query = Query()
        handle = SdkHandle(WrapperConfig())
        handle.client = SimpleNamespace(_query=query)

        capability = await handle.conversation_rewind_capability()
        assert capability.as_dict() == {
            "supported": False,
            "reason": "unsupported_control_subtype",
        }
        assert await handle.supports_rewind_conversation() is False
        assert query.calls == 1

        with pytest.raises(ClaudeRewindError) as rejected:
            await handle.rewind_conversation(TARGET_ID)
        assert rejected.value.code == "capability_unavailable"
        assert rejected.value.operation == "conversation"
        assert query.calls == 1

    asyncio.run(go())


@pytest.mark.parametrize(
    ("cli_error", "code", "retryable"),
    [
        ("commands queued", "commands_queued", True),
        ("turn running", "turn_running", True),
        ("target not found", "target_not_found", False),
        ("stale target", "stale_target", True),
        ("no preceding assistant", "no_preceding_assistant", False),
        ("failed to persist rewind anchor", "persistence_failed", True),
        ("state changed", "state_changed", True),
    ],
)
def test_claude_conversation_rewind_maps_cli_rejections(
    cli_error, code, retryable
):
    class Query:
        async def _send_control_request(self, request, timeout):
            if request["target_message_uuid"] == PROBE_ID:
                return {"rewound": False, "error": "target not found"}
            return {"rewound": False, "error": cli_error}

    async def go():
        handle = SdkHandle(WrapperConfig())
        handle.client = SimpleNamespace(_query=Query())

        with pytest.raises(ClaudeRewindError) as rejected:
            await handle.rewind_conversation(TARGET_ID)
        assert rejected.value.code == code
        assert rejected.value.retryable is retryable
        assert cli_error not in rejected.value.as_dict().keys()

    asyncio.run(go())


def test_claude_conversation_rewind_rejects_malformed_success():
    class Query:
        async def _send_control_request(self, request, timeout):
            if request["target_message_uuid"] == PROBE_ID:
                return {"rewound": False, "error": "target not found"}
            return {
                "rewound": True,
                "targetMessageUuid": "not-a-uuid",
                "prefillText": [],
            }

    async def go():
        handle = SdkHandle(WrapperConfig())
        handle.client = SimpleNamespace(_query=Query())

        with pytest.raises(ClaudeRewindError) as malformed:
            await handle.rewind_conversation(TARGET_ID)
        assert malformed.value.code == "malformed_response"

    asyncio.run(go())


def test_claude_rewind_error_exposes_safe_chinese_reason():
    error = ClaudeRewindError(
        "state_changed", operation="conversation", retryable=True)
    assert "会话发生了变化" in error.user_message_zh
    assert "state changed" not in error.user_message_zh


def test_broker_owned_combined_rewind_fails_before_mutating_files():
    class BrokerSdk:
        is_claude_broker = True
        rewind_files_calls = 0

        async def rewind_files(self, _target):
            self.rewind_files_calls += 1

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/broker-rewind"
        ctx.sdk = BrokerSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        machine._claude_code_context = code_context
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="both",
            checkpoint_id=TARGET_ID,
            cmd_id="broker-both-rewind",
            client_id="client-1",
        ))
        result = (
            outcome if isinstance(outcome, RollbackResult)
            else next(item for item in outcome if isinstance(item, RollbackResult))
        )
        assert result.conversation == "failed"
        assert result.files == "skipped"
        assert "capability_unavailable" in (result.detail or "")
        assert ctx.sdk.rewind_files_calls == 0

    asyncio.run(go())


def test_conversation_rewind_surfaces_structured_native_failure():
    class RejectedSdk:
        async def rewind_conversation(self, *_args, **_kwargs):
            raise ClaudeRewindError(
                "state_changed", operation="conversation", retryable=True)

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/rejected-rewind"
        ctx.sdk = RejectedSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        machine._claude_code_context = code_context
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="conversation",
            checkpoint_id=TARGET_ID,
            cmd_id="rejected-conversation-rewind",
            client_id="client-1",
        ))
        result = next(
            item for item in outcome if isinstance(item, RollbackResult)
        )
        assert result.conversation == "failed"
        assert result.files == "skipped"
        assert "state_changed" in (result.detail or "")
        assert "会话发生了变化" in (result.detail or "")

    asyncio.run(go())


def test_claude_combined_rewind_never_touches_files_after_stale_target():
    class RejectedSdk:
        def __init__(self):
            self.calls = []

        async def prepare_conversation_rewind(self, **_kwargs):
            self.calls.append("prepare")

        async def rewind_conversation(self, *_args, **_kwargs):
            self.calls.append("conversation")
            raise ClaudeRewindError(
                "stale_target", operation="conversation", retryable=True)

        async def rewind_files(self, _target):
            self.calls.append("files")

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/stale-combined-rewind"
        ctx.sdk = RejectedSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        machine._claude_code_context = code_context
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="both",
            checkpoint_id=TARGET_ID,
            cmd_id="stale-combined-rewind",
            client_id="client-1",
        ))
        result = (
            outcome if isinstance(outcome, RollbackResult)
            else next(item for item in outcome if isinstance(item, RollbackResult))
        )

        assert result.conversation == "failed"
        assert result.files == "skipped"
        assert ctx.sdk.calls == ["prepare", "conversation"]

    asyncio.run(go())


def test_claude_stale_rewind_targets_follow_visible_human_turns(monkeypatch):
    def user_row(message_id, content):
        return SimpleNamespace(
            type="user",
            uuid=message_id,
            session_id=SESSION_ID,
            message={"role": "user", "content": content},
            parent_tool_use_id=None,
        )

    messages = [
        user_row(TARGET_ID, "first visible prompt"),
        user_row(
            "42345678-1234-4234-8234-123456789abc",
            "<command-name>/model</command-name>",
        ),
        user_row(NEWER_TARGET_ID, "second visible prompt"),
        user_row(LATEST_TARGET_ID, "latest visible prompt"),
    ]
    monkeypatch.setattr(
        machine_module, "get_session_messages",
        lambda *_args, **_kwargs: messages,
    )
    monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: None)
    monkeypatch.setattr(
        machine_module, "transcript_timestamps", lambda _sid: {})

    async def go():
        machine, _transport = _mk_machine()
        targets = await machine._claude_rewind_targets_after_stale(
            SESSION_ID, "/tmp/visible-history", TARGET_ID
        )
        assert targets == [LATEST_TARGET_ID, NEWER_TARGET_ID, TARGET_ID]

    asyncio.run(go())


def test_claude_stale_target_is_rewound_newest_to_selected_before_files():
    class SequentialSdk:
        def __init__(self):
            self.calls = []
            self.rewind_attempts = 0

        async def prepare_conversation_rewind(self, **_kwargs):
            self.calls.append("prepare")

        async def rewind_conversation(self, target, *, interrupt_if_running):
            assert interrupt_if_running is False
            self.calls.append(("conversation", target))
            self.rewind_attempts += 1
            if self.rewind_attempts == 1:
                raise ClaudeRewindError(
                    "stale_target", operation="conversation", retryable=True)
            return SimpleNamespace(prefill_text=f"prefill:{target}")

        async def rewind_files(self, target):
            self.calls.append(("files", target))

        async def force_reconnect(self, **_kwargs):
            self.calls.append("reconnect")

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/sequential-rewind"
        ctx.sdk = SequentialSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        async def rewind_targets(_sid, _cwd, _selected):
            return [LATEST_TARGET_ID, TARGET_ID]

        machine._claude_code_context = code_context
        machine._claude_rewind_targets_after_stale = rewind_targets
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="both",
            num_turns=2,
            checkpoint_id=TARGET_ID,
            cmd_id="sequential-stale-combined-rewind",
            client_id="client-1",
        ))
        result = next(
            item for item in outcome if isinstance(item, RollbackResult)
        )

        assert result.conversation == "succeeded"
        assert result.files == "succeeded"
        assert result.restored_turns == 2
        assert result.prefill_text == f"prefill:{TARGET_ID}"
        assert ctx.sdk.calls == [
            "prepare",
            ("conversation", TARGET_ID),
            ("conversation", LATEST_TARGET_ID),
            ("conversation", TARGET_ID),
            ("files", TARGET_ID),
            "reconnect",
        ]

    asyncio.run(go())


def test_claude_combined_rewind_orders_conversation_before_files():
    class RewoundSdk:
        def __init__(self):
            self.calls = []

        async def prepare_conversation_rewind(self, **_kwargs):
            self.calls.append("prepare")

        async def rewind_conversation(self, *_args, **_kwargs):
            self.calls.append("conversation")
            return SimpleNamespace(prefill_text=None)

        async def rewind_files(self, _target):
            self.calls.append("files")

        async def force_reconnect(self, **_kwargs):
            self.calls.append("reconnect")

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/successful-combined-rewind"
        ctx.sdk = RewoundSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        machine._claude_code_context = code_context
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="both",
            checkpoint_id=TARGET_ID,
            cmd_id="successful-combined-rewind",
            client_id="client-1",
        ))
        result = next(
            item for item in outcome if isinstance(item, RollbackResult)
        )

        assert result.conversation == "succeeded"
        assert result.files == "succeeded"
        assert ctx.sdk.calls == [
            "prepare", "conversation", "files", "reconnect",
        ]

    asyncio.run(go())


def test_claude_rewind_preparation_reconnects_and_reprobes_capability():
    async def go():
        handle = SdkHandle(WrapperConfig())
        calls = []

        async def reconnect(**kwargs):
            calls.append(("reconnect", kwargs))

        async def capability(*, refresh=False):
            calls.append(("capability", refresh))
            return SimpleNamespace(supported=True)

        handle.force_reconnect = reconnect
        handle.conversation_rewind_capability = capability
        await handle.prepare_conversation_rewind(
            resume_id=SESSION_ID, cwd="/tmp/project")

        assert calls == [
            ("reconnect", {
                "resume_id": SESSION_ID,
                "cwd": "/tmp/project",
                "reason": "prepare conversation rewind",
                "preserve_model": True,
            }),
            ("capability", True),
        ]

    asyncio.run(go())


def test_claude_rewind_remains_successful_when_only_reconnect_fails():
    class RewoundSdk:
        async def rewind_conversation(self, target, *, interrupt_if_running):
            assert target == TARGET_ID
            assert interrupt_if_running is False
            return SimpleNamespace(prefill_text="从这里继续")

        async def force_reconnect(self, **_kwargs):
            raise RuntimeError("temporary spawn failure")

    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/claude-rewind-reconnect"
        ctx.sdk = RewoundSdk()
        machine.sessions[SESSION_ID] = ctx

        async def code_context(_cmd, _action):
            return ctx

        async def history(_sid, **_kwargs):
            return History(
                session_id=SESSION_ID,
                revision="test-history-revision",
                events=[],
                has_more=False,
            )

        machine._claude_code_context = code_context
        machine._build_history = history
        outcome = await machine._handle_rollback_session(
                RollbackSession(
                    session_id=SESSION_ID,
                    engine="claude",
                    restore="conversation",
                    checkpoint_id=TARGET_ID,
                    cmd_id="claude-reconnect-rewind",
                    client_id="client-1",
                )
        )

        assert isinstance(outcome, tuple)
        invalidated, reset, result = outcome
        assert isinstance(invalidated, HistoryInvalidated)
        assert isinstance(reset, History) and reset.reset is True
        assert isinstance(result, RollbackResult)
        assert result.conversation == "succeeded"
        assert result.prefill_text == "从这里继续"
        assert "运行时重连失败" in (result.detail or "")
        assert ctx.needs_reload is True
        assert any(
            isinstance(item, History) and item.reset for item in transport.sent
        )

    asyncio.run(go())


def test_claude_rewind_reloads_stale_context_before_native_mutation():
    class ReloadedSdk:
        def __init__(self):
            self.calls = []

        async def force_reconnect(self, **kwargs):
            self.calls.append(("reconnect", kwargs))

        async def rewind_conversation(self, target, *, interrupt_if_running):
            self.calls.append(("rewind", target, interrupt_if_running))
            return SimpleNamespace(prefill_text=None)

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/claude-stale-rewind"
        ctx.needs_reload = True
        ctx.sdk = ReloadedSdk()
        machine.sessions[SESSION_ID] = ctx

        ownership_checks = 0

        async def no_external(_sid):
            nonlocal ownership_checks
            ownership_checks += 1
            return False

        async def history(_sid, **_kwargs):
            return History(
                session_id=SESSION_ID,
                revision="test-history-revision",
                events=[],
                has_more=False,
            )

        machine._prime_claude_ownership = no_external
        machine._build_history = history
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="conversation",
            checkpoint_id=TARGET_ID,
            cmd_id="claude-stale-rewind",
            client_id="client-1",
        ))

        result = next(
            item for item in outcome if isinstance(item, RollbackResult)
        )
        assert result.conversation == "succeeded"
        assert ownership_checks == 2
        assert ctx.sdk.calls[0] == (
            "reconnect",
            {
                "resume_id": SESSION_ID,
                "cwd": "/tmp/claude-stale-rewind",
                "reason": "external transcript change before 回滚",
                "preserve_model": True,
            },
        )
        assert ctx.sdk.calls[1] == ("rewind", TARGET_ID, False)
        assert ctx.sdk.calls[2][0] == "reconnect"
        assert ctx.needs_reload is False

    asyncio.run(go())


def test_claude_rewind_is_rejected_when_stale_context_reload_fails():
    class FailedReloadSdk:
        rewind_calls = 0

        async def force_reconnect(self, **_kwargs):
            raise RuntimeError("simulated reconnect failure")

        async def rewind_conversation(self, *_args, **_kwargs):
            self.rewind_calls += 1
            raise AssertionError("rewind must not run on stale context")

    async def go():
        machine, _transport = _mk_machine()
        ctx = _mk_ctx(SESSION_ID, SESSION_ID)
        ctx.cwd = "/tmp/claude-stale-rewind"
        ctx.needs_reload = True
        ctx.sdk = FailedReloadSdk()
        machine.sessions[SESSION_ID] = ctx

        async def no_external(_sid):
            return False

        machine._prime_claude_ownership = no_external
        outcome = await machine._handle_rollback_session(RollbackSession(
            session_id=SESSION_ID,
            engine="claude",
            restore="conversation",
            checkpoint_id=TARGET_ID,
            cmd_id="claude-stale-reload-failure",
            client_id="client-1",
        ))

        assert isinstance(outcome, Error)
        assert outcome.code == "not_running"
        assert ctx.sdk.rewind_calls == 0
        assert ctx.needs_reload is True

    asyncio.run(go())
