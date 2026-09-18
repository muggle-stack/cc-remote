"""Request readable Claude summaries without changing native thinking controls."""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from cc_remote.config import WrapperConfig
from cc_remote.wrapper import sdk as sdk_module
from cc_remote.wrapper.sdk import SdkHandle
from tests.test_claude_permission_state import _FakeClaudeClient


@pytest.mark.parametrize("profile", ["legacy", "isolated", "work"])
@pytest.mark.parametrize("resume", [None, "12345678-1234-4234-8234-123456789abc"])
def test_summary_display_reaches_cli_without_overriding_thinking(
    tmp_path, monkeypatch, profile, resume,
):
    monkeypatch.setenv("MAX_THINKING_TOKENS", "0")
    handle = SdkHandle(
        WrapperConfig(claude_bin=str(tmp_path / "claude")),
        claude_config_dir=str(tmp_path / "account") if profile == "isolated" else None,
        isolate_account_env=profile == "isolated",
    )
    handle.work_mode = profile == "work"
    handle.permission_mode = "plan"
    options = handle._options(resume, str(tmp_path), effort_override="low")
    # Inspect the real pinned SDK's argv without launching a CLI or model.
    argv = SubprocessCLITransport(prompt="unused", options=options)._build_command()

    assert argv.count("--thinking-display") == 1
    assert argv[argv.index("--thinking-display") + 1] == "summarized"
    assert "--thinking" not in argv
    assert "--max-thinking-tokens" not in argv
    assert options.thinking is None
    assert options.max_thinking_tokens is None
    assert options.effort == "low"
    assert options.permission_mode == "plan"


def test_summary_display_survives_connect_reconnect_and_private_fork(monkeypatch):
    async def go():
        _FakeClaudeClient.created = []
        monkeypatch.setattr(sdk_module, "ClaudeSDKClient", _FakeClaudeClient)
        handle = SdkHandle(WrapperConfig())
        handle.effort = "low"
        try:
            await handle.connect(cwd="/tmp")
            await handle.force_reconnect(None, "/tmp", reason="test")
            for client in _FakeClaudeClient.created:
                assert client.options.extra_args["thinking-display"] == "summarized"
                assert client.options.thinking is None
                assert client.options.effort == "low"
            fork = handle._options("parent", "/tmp", fork=True)
            assert fork.extra_args["thinking-display"] == "summarized"
            assert fork.fork_session is True
        finally:
            await handle.disconnect()

    asyncio.run(go())
