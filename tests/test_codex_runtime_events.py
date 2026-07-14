"""Sanitization and routing guards for ephemeral Codex runtime events."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    NewSession,
    Notice,
    RateLimitUpdate,
    deserialize,
    is_downstream,
    serialize,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_handle import (
    CodexHandle,
    _RUNTIME_EVENT_PENDING_MAX,
    _RUNTIME_EVENT_SEEN_MAX,
)
from tests.test_multisession import _mk_ctx, _mk_machine


class _Cfg:
    cc_cwd = "/tmp"
    turn_reader_queue_cap = 4


def test_runtime_event_protocol_is_strict_bounded_and_not_replayable():
    notice = Notice(
        notice_id="notice-1",
        severity="warning",
        category="config",
        title="配置警告",
        message="summary",
        detail="/tmp/config.toml",
        thread_id="thread-1",
    )
    assert deserialize(serialize(notice)) == notice
    assert is_downstream(notice) is False
    with pytest.raises(ValidationError):
        Notice(
            notice_id="notice-2", severity="warning", category="runtime",
            title="x", message="x" * (2 * 1024 + 1),
        )
    with pytest.raises(ValidationError):
        Notice(
            notice_id="notice-3", severity="error", category="runtime",
            title="x", message="x",
        )

    rate = RateLimitUpdate(
        limit_id="codex", name="Codex", plan_type="pro",
        primary={"used_percent": 80, "resets_at": 999},
    )
    assert deserialize(serialize(rate)) == rate
    assert is_downstream(rate) is False
    with pytest.raises(ValidationError):
        RateLimitUpdate(credits={"balance": "must-not-pass"})
    with pytest.raises(ValidationError):
        RateLimitUpdate()


def test_initialize_notices_are_bounded_deduplicated_and_flushed_once():
    async def run():
        delivered = []
        async def capture(event):
            delivered.append(event)
        handle = CodexHandle(_Cfg(), runtime_event_callback=capture)

        # The callback is present, but Machine has not assigned/activated a sid.
        for index in range(_RUNTIME_EVENT_PENDING_MAX + 4):
            frame = {
                "method": "warning",
                "params": {"message": f"warning {index}"},
            }
            await handle._dispatch(frame)
            await handle._dispatch(frame)  # exact duplicate
        assert delivered == []
        assert len(handle._runtime_event_pending) == _RUNTIME_EVENT_PENDING_MAX
        assert len(handle._runtime_event_seen) <= _RUNTIME_EVENT_SEEN_MAX

        await handle.activate_runtime_events()
        assert len(delivered) == _RUNTIME_EVENT_PENDING_MAX
        assert delivered[0].message == "warning 4"
        assert delivered[-1].message == (
            f"warning {_RUNTIME_EVENT_PENDING_MAX + 3}")

        # Delivered ids stay in the bounded LRU and cannot double-render.
        await handle._dispatch({
            "method": "warning",
            "params": {"message": f"warning {_RUNTIME_EVENT_PENDING_MAX + 3}"},
        })
        assert len(delivered) == _RUNTIME_EVENT_PENDING_MAX
        await handle._dispatch({
            "method": "warning", "params": {"message": "new warning"},
        })
        assert len(delivered) == _RUNTIME_EVENT_PENDING_MAX + 1

    asyncio.run(run())


def test_notice_methods_copy_only_bounded_public_fields():
    async def run():
        delivered = []
        async def capture(event):
            delivered.append(event)
        handle = CodexHandle(_Cfg(), runtime_event_callback=capture)
        await handle.activate_runtime_events()
        await handle._dispatch({
            "method": "warning",
            "params": {"message": "runtime", "threadId": "thread-1"},
        })
        await handle._dispatch({
            "method": "guardianWarning",
            "params": {"message": "guardian", "threadId": "thread-1"},
        })
        await handle._dispatch({
            "method": "configWarning",
            "params": {
                "summary": "config summary",
                "path": "/" + "p" * 2000,
                "details": "SECRET_CONFIG_DETAILS",
                "range": {"start": {"line": 1, "column": 1}},
            },
        })
        await handle._dispatch({
            "method": "deprecationNotice",
            "params": {"summary": "deprecated", "details": "migration"},
        })
        await handle._dispatch({
            "method": "windows/worldWritableWarning",
            "params": {
                "samplePaths": ["/one", "/two", "/three", "/SECRET_FOUR"],
                "extraCount": 7,
                "failedScan": False,
            },
        })

        assert [event.category for event in delivered] == [
            "runtime", "guardian", "config", "deprecation", "security",
        ]
        config = delivered[2]
        assert config.message == "config summary"
        assert len(config.detail) == 1024
        assert "SECRET_CONFIG_DETAILS" not in serialize(config)
        security = delivered[4]
        assert "/one" in security.detail and "/three" in security.detail
        assert "SECRET_FOUR" not in security.detail
        assert security.message.endswith("10 个可被所有用户写入的目录")

    asyncio.run(run())


def test_rate_update_drops_account_secrets_and_emits_one_reached_notice():
    async def run():
        delivered = []
        async def capture(event):
            delivered.append(event)
        handle = CodexHandle(_Cfg(), runtime_event_callback=capture)
        handle.thread_id = "thread-1"
        await handle.activate_runtime_events()
        await handle._dispatch({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "codex", "limitName": "Codex", "planType": "pro",
                "rateLimitReachedType": "rate_limit_reached",
                "primary": {
                    "usedPercent": 100, "resetsAt": 999,
                    "windowDurationMins": 300, "future": "SECRET_WINDOW",
                },
                "credits": {"balance": "SECRET_CREDIT"},
                "individualLimit": {"limit": "SECRET_SPEND"},
                "future": "SECRET_FUTURE",
            }},
        })
        assert [event.type for event in delivered] == [
            "rate_limit_update", "notice",
        ]
        rate, warning = delivered
        assert rate.limit_id == "codex" and rate.primary.used_percent == 100
        assert warning.category == "rate_limit"
        encoded = json.dumps(
            [event.model_dump() for event in delivered], ensure_ascii=False)
        for secret in (
            "SECRET_WINDOW", "SECRET_CREDIT", "SECRET_SPEND", "SECRET_FUTURE",
            "credits", "individualLimit",
        ):
            assert secret not in encoded

        # Sparse window merge preserves reset metadata and updates percent. The
        # reached warning has a stable id, so the second frame adds only Rate.
        await handle._dispatch({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 99},
            }},
        })
        assert len(delivered) == 3
        latest = delivered[-1]
        assert latest.type == "rate_limit_update"
        assert latest.primary.used_percent == 99
        assert latest.primary.resets_at == 999
        await handle._dispatch({
            "method": "account/rateLimits/updated",
            "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100},
            }},
        })
        assert len(delivered) == 4
        assert delivered[-1].primary.used_percent == 100

    asyncio.run(run())


def test_foreign_thread_notice_is_dropped_before_callback():
    async def run():
        delivered = []
        async def capture(event):
            delivered.append(event)
        handle = CodexHandle(_Cfg(), runtime_event_callback=capture)
        handle.thread_id = "thread-current"
        await handle.activate_runtime_events()
        await handle._dispatch({
            "method": "guardianWarning",
            "params": {
                "message": "belongs elsewhere", "threadId": "thread-foreign",
            },
        })
        assert delivered == []

        pending = CodexHandle(_Cfg(), runtime_event_callback=capture)
        await pending._dispatch({
            "method": "guardianWarning",
            "params": {
                "message": "arrived before thread/start",
                "threadId": "thread-foreign",
            },
        })
        pending.thread_id = "thread-current"
        await pending.activate_runtime_events()
        assert delivered == []

    asyncio.run(run())


def test_runtime_callback_failure_logs_no_notice_or_exception_body(caplog):
    async def run():
        async def fail(event):
            raise RuntimeError(f"SECRET_CALLBACK_BODY {event.message}")

        handle = CodexHandle(_Cfg(), runtime_event_callback=fail)
        await handle.activate_runtime_events()
        await handle._dispatch({
            "method": "warning",
            "params": {"message": "SECRET_NOTICE_BODY"},
        })

    asyncio.run(run())
    logged = "\n".join(
        record.getMessage() + repr(getattr(record, "extra_data", {}))
        for record in caplog.records
    )
    assert "runtime event callback failed" in logged
    assert "SECRET_CALLBACK_BODY" not in logged
    assert "SECRET_NOTICE_BODY" not in logged


def test_machine_never_broadcasts_runtime_event_without_session_route():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("placeholder")
        ctx.key = None
        notice = Notice(
            notice_id="notice-route", severity="warning", category="runtime",
            title="warning", message="message",
        )
        await machine._on_codex_runtime_event(ctx, notice)
        assert transport.sent == []

        ctx.key = "tmp-safe-route"
        machine.sessions[ctx.key] = ctx
        await machine._on_codex_runtime_event(ctx, notice)
        assert transport.sent == [notice]
        assert notice.sid == "tmp-safe-route"
        assert notice.seq is None
        assert ctx.buffer.tail_seq == 0

    asyncio.run(run())


def test_new_session_flushes_pending_notice_after_client_runtime_exists(
        monkeypatch):
    class FakeCodexHandle:
        def __init__(self, _cfg, cwd=None):
            self.cwd = cwd
            self.thread_id = None
            self.model = None
            self.effort = None
            self.applied_effort = None
            self.approval = "never"
            self.collaboration_mode = "default"
            self.service_tier = None
            self.runtime_event_callback = None

        async def connect(self, **_kwargs):
            return None

        async def activate_runtime_events(self):
            assert self.runtime_event_callback is not None
            await self.runtime_event_callback(Notice(
                notice_id="notice-after-focus",
                severity="warning",
                category="config",
                title="配置警告",
                message="summary",
            ))

        async def disconnect(self):
            return None

    async def run():
        monkeypatch.setattr(machine_module, "CodexHandle", FakeCodexHandle)
        machine, transport = _mk_machine()
        await machine._handle_new_session(NewSession(
            engine="codex", request_id="create-1"))
        types = [message.type for message in transport.sent]
        assert types.count("notice") == 1
        assert types.index("snapshot") < types.index("session_focus")
        assert types.index("session_focus") < types.index("notice")
        notice = next(message for message in transport.sent
                      if message.type == "notice")
        assert notice.sid and notice.sid.startswith("tmp-")

    asyncio.run(run())
