"""Zero-token coverage for the official Codex /status bridge."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    CommandAck,
    GetStatus,
    StatusContext,
    StatusReport,
    StatusRuntime,
    StatusThread,
    deserialize,
    serialize,
)
from cc_remote.wrapper.codex_handle import (
    CodexHandle,
    _app_server_version,
    _status_error_message,
)
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    turn_reader_queue_cap = 4


def _minimal_status(thread_id: str = "thread-1") -> dict:
    return {
        "thread": {
            "thread_id": thread_id,
            "status": "idle",
            "active_flags": [],
        },
        "runtime": {"model": "gpt-test", "approval_policy": "never"},
        "context": {"used_tokens": 10, "max_tokens": 100, "percentage": 10.0},
        "account": None,
        "rate_limits": [],
        "usage": None,
        "component_errors": [],
    }


def test_status_protocol_is_strict_and_round_trips():
    command = GetStatus(
        sid="thread-1", cmd_id="status-1", client_id="client-1")
    assert deserialize(serialize(command)) == command

    report = StatusReport(
        sid="thread-1",
        to="client-1",
        thread=StatusThread(thread_id="thread-1", status="active",
                            active_flags=["waitingOnApproval"]),
        runtime=StatusRuntime(model="gpt-test", sandbox_mode="workspace-write"),
        context=StatusContext(used_tokens=25, max_tokens=100, percentage=25.0),
        component_errors=["usage: unsupported by this Codex app-server"],
    )
    assert deserialize(serialize(report)) == report
    with pytest.raises(ValidationError):
        StatusThread(thread_id="thread-1", secret="must-not-pass")
    with pytest.raises(ValidationError):
        StatusReport(
            thread=StatusThread(thread_id="thread-1"),
            runtime=StatusRuntime(), context=StatusContext(),
            component_errors=["x" * 385],
        )


def test_app_server_version_extracts_only_version():
    initialized = {
        "userAgent": "codex_cli_rs/0.144.1 (Linux 6.8; secret-host)",
        "codexHome": "/home/nancy/.codex",
    }
    assert _app_server_version(initialized) == "0.144.1"


def test_status_rpcs_are_staged_by_auth_and_sensitive_fields_are_dropped():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-1"
        handle._cwd = "/workspace/project"
        handle.app_server_version = "0.144.1"
        handle.model = "session-model"
        handle.effort = "high"
        handle.service_tier = "fast"
        handle.approval = "on-request"
        handle.last_token_usage = {
            "last": {"totalTokens": 250}, "modelContextWindow": 1000,
        }

        started: list[str] = []
        core_started = asyncio.Event()
        stats_started = asyncio.Event()
        replies = {
            "thread/read": {"thread": {
                "id": "thread-1", "sessionId": "session-1",
                "cwd": "/workspace/project", "source": "appServer",
                "cliVersion": "0.144.0", "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnUserInput", "futureFlag"],
                },
                "ephemeral": False, "createdAt": 10, "updatedAt": 20,
                "path": "/secret/rollout.jsonl", "preview": "SECRET_PREVIEW",
            }},
            "config/read": {"config": {
                "model": "config-model", "model_provider": "openai",
                "model_reasoning_effort": "medium", "service_tier": "default",
                "approval_policy": "never", "sandbox_mode": "workspace-write",
                "web_search": "live", "instructions": "SECRET_INSTRUCTIONS",
                "developer_instructions": "SECRET_DEVELOPER",
            }, "origins": {}},
            "account/read": {"account": {
                "type": "chatgpt", "email": "secret@example.com", "planType": "pro",
            }, "requiresOpenaiAuth": True},
            "account/rateLimits/read": {"rateLimits": {
                "limitId": "codex", "limitName": "Codex", "planType": "pro",
                "primary": {"usedPercent": 42, "resetsAt": 1234,
                            "windowDurationMins": 300},
                "credits": {"balance": "SECRET_BALANCE", "hasCredits": True,
                            "unlimited": False},
            }},
            "account/usage/read": {"summary": {
                "lifetimeTokens": 123456, "peakDailyTokens": 3000,
                "currentStreakDays": 2, "longestStreakDays": 8,
                "longestRunningTurnSec": 90,
            }, "dailyUsageBuckets": [{"startDate": "SECRET_DAY", "tokens": 1}]},
        }

        async def request(method, params=None):
            started.append(method)
            if method in {"thread/read", "config/read", "account/read"}:
                if len(started) == 3:
                    core_started.set()
                await asyncio.wait_for(core_started.wait(), timeout=1)
            else:
                assert "account/read" in started
                if len(started) == 5:
                    stats_started.set()
                await asyncio.wait_for(stats_started.wait(), timeout=1)
            return replies[method]

        handle._request = request
        status = await handle.get_status()
        assert set(started) == set(replies)
        assert status["thread"]["status"] == "active"
        assert status["thread"]["active_flags"] == ["waitingOnUserInput"]
        assert status["runtime"] == {
            "app_server_version": "0.144.1",
            "model": "session-model", "model_provider": "openai",
            "reasoning_effort": "high", "service_tier": "fast",
            "approval_policy": "on-request", "sandbox_mode": "workspace-write",
            "web_search": "live",
        }
        assert status["context"] == {
            "used_tokens": 250, "max_tokens": 1000, "percentage": 25.0,
        }
        assert status["account"] == {
            "auth_type": "chatgpt", "plan_type": "pro",
            "requires_openai_auth": True,
        }
        assert status["rate_limits"][0]["primary"]["used_percent"] == 42
        assert status["usage"]["lifetime_tokens"] == 123456
        assert status["component_errors"] == []

        wire = json.dumps(status, sort_keys=True)
        for secret in (
            "secret@example.com", "SECRET_PREVIEW", "SECRET_INSTRUCTIONS",
            "SECRET_DEVELOPER", "SECRET_BALANCE", "SECRET_DAY",
            "/secret/rollout.jsonl",
        ):
            assert secret not in wire
        # The sanitized dictionary must satisfy the strict wire model.
        StatusReport(**status)

    asyncio.run(run())


@pytest.mark.parametrize("auth_type", ["apiKey", "amazonBedrock"])
def test_status_skips_chatgpt_stats_for_explicit_non_chatgpt_auth(auth_type):
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-1"
        handle.last_rate_limits = {
            "limitId": "stale-chatgpt", "primary": {"usedPercent": 99},
        }
        calls: list[str] = []

        async def request(method, params=None):
            calls.append(method)
            if method == "thread/read":
                return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
            if method == "config/read":
                return {"config": {}, "origins": {}}
            if method == "account/read":
                return {"account": {"type": auth_type}, "requiresOpenaiAuth": False}
            raise AssertionError(f"unexpected ChatGPT-only request: {method}")

        handle._request = request
        status = await handle.get_status()
        assert set(calls) == {"thread/read", "config/read", "account/read"}
        assert status["account"]["auth_type"] == auth_type
        assert status["rate_limits"] == []
        assert status["usage"] is None
        assert status["component_errors"] == []
        StatusReport(**status)

    asyncio.run(run())


@pytest.mark.parametrize("account_mode", ["unknown", "failure"])
def test_status_attempts_chatgpt_stats_when_account_is_unknown_or_fails(account_mode):
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-1"
        calls: list[str] = []

        async def request(method, params=None):
            calls.append(method)
            if method == "thread/read":
                return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
            if method == "config/read":
                return {"config": {}, "origins": {}}
            if method == "account/read":
                if account_mode == "failure":
                    raise RuntimeError("account endpoint failed with secret")
                return {
                    "account": {"type": "futureAuth"},
                    "requiresOpenaiAuth": False,
                }
            if method == "account/rateLimits/read":
                return {"rateLimits": {
                    "limitId": "codex", "primary": {"usedPercent": 7},
                }}
            if method == "account/usage/read":
                return {"summary": {"lifetimeTokens": 123}}
            raise AssertionError(method)

        handle._request = request
        status = await handle.get_status()
        assert set(calls) == {
            "thread/read", "config/read", "account/read",
            "account/rateLimits/read", "account/usage/read",
        }
        assert status["rate_limits"][0]["primary"]["used_percent"] == 7
        assert status["usage"]["lifetime_tokens"] == 123
        expected_errors = (
            ["account: app-server request failed"] if account_mode == "failure" else []
        )
        assert status["component_errors"] == expected_errors
        StatusReport(**status)

    asyncio.run(run())


def test_status_error_maps_chatgpt_auth_requirement_to_account_unavailable():
    assert _status_error_message(RuntimeError(
        "chatgpt authentication required to read rate limits"
    )) == "unavailable for the current account"


def test_status_partial_rpc_failure_uses_notification_cache_without_raw_error():
    async def run():
        handle = CodexHandle(_Cfg())
        handle.thread_id = "thread-1"
        handle._cwd = "/tmp"
        await handle._dispatch({
            "method": "thread/status/changed",
            "params": {"threadId": "thread-1", "status": {
                "type": "active", "activeFlags": ["waitingOnApproval"],
            }},
        })
        await handle._dispatch({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "codex", "planType": "pro",
                "primary": {"usedPercent": 77, "resetsAt": 999},
            }},
        })
        # Rolling updates are sparse: a new percentage must not erase the last
        # known reset timestamp from the same window.
        await handle._dispatch({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 78},
            }},
        })

        async def request(method, params=None):
            if method == "thread/read":
                raise RuntimeError("provider sk-secret-thread failed")
            if method == "account/rateLimits/read":
                raise RuntimeError("HTTP 500 sk-secret-rate")
            if method == "config/read":
                return {"config": {}, "origins": {}}
            if method == "account/read":
                return {"account": None, "requiresOpenaiAuth": True}
            return {"summary": {}}

        handle._request = request
        status = await handle.get_status()
        assert status["thread"]["status"] == "active"
        assert status["thread"]["active_flags"] == ["waitingOnApproval"]
        assert status["rate_limits"][0]["primary"] == {
            "used_percent": 78,
            "resets_at": 999,
            "window_duration_mins": None,
        }
        assert status["component_errors"] == [
            "thread: app-server request failed",
            "rate_limits: app-server request failed",
        ]
        assert "sk-secret" not in json.dumps(status)
        StatusReport(**status)

    asyncio.run(run())


def test_machine_status_is_routed_and_safe_retry_reexecutes_read():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-1", "thread-1")
        ctx.engine = "codex"
        calls = 0

        async def get_status():
            nonlocal calls
            calls += 1
            return _minimal_status()

        ctx.sdk = type("StatusSdk", (), {"get_status": staticmethod(get_status)})()
        machine.sessions[ctx.key] = ctx
        command = GetStatus(
            sid="thread-1", cmd_id="status-1", client_id="client-1")
        await machine._process_command(command)
        await machine._process_command(command)

        assert calls == 2
        reports = [message for message in transport.sent
                   if isinstance(message, StatusReport)]
        assert len(reports) == 2
        assert all(report.sid == "thread-1" and report.to == "client-1"
                   and report.seq is None for report in reports)
        acks = [message for message in transport.sent
                if isinstance(message, CommandAck)]
        assert len(acks) == 2

    asyncio.run(run())
