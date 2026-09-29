from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

from cc_remote.claude_service.wire import decode_sdk, encode_sdk
from cc_remote.wrapper.claude_rate_limits import (
    ClaudeRateLimitStore,
    ClaudeRateLimitStoreError,
)
from cc_remote.config import WrapperConfig
from cc_remote.wrapper.machine import WrapperMachine
from tests.test_multisession import _StubTransport, _mk_ctx, _mk_machine


def _info(
    rate_type: str,
    *,
    resets_at: int | None = 20_000,
    utilization: float | None = 0.37,
    status: str = "allowed",
):
    return SimpleNamespace(
        rate_limit_type=rate_type,
        resets_at=resets_at,
        utilization=utilization,
        status=status,
        raw={"credential": "must-not-persist"},
    )


def _unified_event(now: int, **overrides) -> RateLimitEvent:
    return parse_message({
        "type": "rate_limit_event",
        "uuid": "quota-event",
        "session_id": "native-claude",
        "rate_limit_info": {
            "status": "allowed",
            "rateLimitType": "five_hour",
            "resetsAt": now + 3_600,
            "unifiedWindows": {
                "five_hour": {"utilization": 0.01, "resetsAt": now + 3_600},
                "seven_day": {"utilization": 0, "resetsAt": now + 86_400},
            },
            **overrides,
        },
    })


@pytest.mark.parametrize("persistent_service", [False, True])
def test_native_unified_windows_survive_sdk_projection_and_cache_reload(
    tmp_path, persistent_service,
):
    now = int(time.time())
    event = _unified_event(now, credential="must-not-persist")
    if persistent_service:
        event = decode_sdk(encode_sdk(event))
    store = ClaudeRateLimitStore(tmp_path)
    store.observe(event.rate_limit_info, now=now)

    windows = {
        window.window_duration_mins: window
        for update in store.snapshot(now=now)
        for window in (update.primary, update.secondary) if window is not None
    }
    assert {duration: window.used_percent for duration, window in windows.items()} == {
        300: 1, 10_080: 0,
    }
    assert windows[300].resets_at == now + 3_600
    assert windows[10_080].resets_at == now + 86_400
    reloaded = ClaudeRateLimitStore(tmp_path).snapshot(now=now + 1)
    assert [(update.primary.used_percent, update.secondary.used_percent)
            for update in reloaded] == [(1, 0)]
    assert "credential" not in store.path.read_text()
    assert "must-not-persist" not in store.path.read_text()


@pytest.mark.parametrize("rate_type", [None, "overage", "future-window"])
def test_unified_windows_do_not_require_a_known_top_level_limit(tmp_path, rate_type):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    update, = store.observe(_unified_event(
        now, rateLimitType=rate_type, status="rejected",
    ).rate_limit_info, now=now)

    assert update.primary.used_percent == 1
    assert update.secondary.used_percent == 0
    assert update.reached_type == "", "an unrelated rejection must not exhaust these windows"


@pytest.mark.parametrize("rejected_type", ["five_hour", "seven_day", "seven_day_opus"])
def test_unified_rejection_applies_only_to_its_own_window(tmp_path, rejected_type):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    windows = {
        name: {"utilization": used, "resetsAt": now + 10_000}
        for name, used in (("five_hour", 0.01), ("seven_day", 0),
                          ("seven_day_opus", 0.23), ("seven_day_sonnet", 0.45))
    }
    windows["unknown"] = {"utilization": 1, "credential": "must-not-persist"}
    updates = store.observe(_unified_event(
        now, rateLimitType=rejected_type, status="rejected", unifiedWindows=windows,
    ).rate_limit_info, now=now)
    for projected in (updates, ClaudeRateLimitStore(tmp_path).snapshot(now=now)):
        main, opus, sonnet = projected
        assert main.primary.used_percent == (100 if rejected_type == "five_hour" else 1)
        assert main.secondary.used_percent == (100 if rejected_type == "seven_day" else 0)
        assert main.reached_type == (rejected_type if rejected_type != "seven_day_opus" else "")
        assert opus.limit_id == "claude-seven-day-opus"
        assert opus.primary.used_percent == (100 if rejected_type == "seven_day_opus" else 23)
        assert sonnet.primary.used_percent == 45 and sonnet.reached_type == ""
    assert "unknown" not in store.path.read_text()
    assert "credential" not in store.path.read_text()


@pytest.mark.parametrize("unified", [None, [], "invalid", {
    "five_hour": None, "seven_day": [], "seven_day_opus": {},
}])
def test_malformed_unified_shapes_retain_legacy_fields(tmp_path, unified):
    now = int(time.time())
    update, = ClaudeRateLimitStore(tmp_path).observe(_unified_event(
        now, utilization=0.25, unifiedWindows=unified,
    ).rate_limit_info, now=now)
    assert update.primary.used_percent == 25 and update.secondary is None


@pytest.mark.parametrize("used", [None, False, -0.1, 1.1, "0.5", float("nan"), float("inf")])
def test_unified_utilization_is_validated_without_guessing_units(tmp_path, used):
    now = int(time.time())
    update, = ClaudeRateLimitStore(tmp_path).observe(_unified_event(
        now, utilization=0.75, unifiedWindows={
            "five_hour": {"utilization": used, "resetsAt": now + 1_000},
            "seven_day": {"utilization": 0, "resetsAt": now + 10_000},
        },
    ).rate_limit_info, now=now)
    assert update.primary.used_percent is None
    assert update.secondary.used_percent == 0


def test_unified_windows_expire_independently_and_keep_unmentioned_buckets(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    store.observe(_info("seven_day_opus", resets_at=now + 20_000), now=now)
    update, = store.observe(_unified_event(now, utilization=0.9).rate_limit_info, now=now)
    assert update.primary.used_percent == 1, "nested utilization overrides the legacy field"
    event = _unified_event(now, unifiedWindows={
        "five_hour": {"utilization": 0.8, "resetsAt": now},
        "seven_day": {"utilization": 0, "resetsAt": now + 10_000},
    })
    update, = store.observe(event.rate_limit_info, now=now)
    assert update.primary is None and update.secondary.used_percent == 0
    main, opus = ClaudeRateLimitStore(tmp_path).snapshot(now=now)
    assert main.primary is None and main.secondary.used_percent == 0
    assert opus.primary.used_percent == 37
    assert [row.limit_id for row in store.snapshot(now=now + 10_000)] == [
        "claude-seven-day-opus",
    ]


def test_claude_rate_limit_store_sanitizes_and_replays_windows(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    five_hour, = store.observe(
        _info("five_hour", resets_at=now + 10_000), now=now)
    weekly, = store.observe(
        _info(
            "seven_day", resets_at=now + 20_000, utilization=0.82,
            status="rejected",
        ),
        now=now,
    )

    assert five_hour is not None
    assert five_hour.limit_id == "claude"
    assert five_hour.primary.used_percent == 37
    assert five_hour.primary.window_duration_mins == 300
    assert five_hour.secondary is None
    assert weekly is not None
    assert weekly.limit_id == "claude"
    assert weekly.secondary.used_percent == 100
    assert weekly.secondary.window_duration_mins == 10_080
    assert weekly.reached_type == "seven_day"

    payload = (tmp_path / "claude-rate-limits.json").read_text()
    assert "credential" not in payload
    restored = ClaudeRateLimitStore(tmp_path).snapshot(now=now + 1)
    assert [(event.limit_id, bool(event.primary), bool(event.secondary))
            for event in restored] == [
        ("claude", True, True),
    ]


def test_claude_rate_limit_store_handles_specialized_expiry_and_bad_usage(
    tmp_path,
):
    store = ClaudeRateLimitStore(tmp_path)
    opus, = store.observe(
        _info("seven_day_opus", resets_at=11_000, utilization=1.5),
        now=10_000,
    )
    sonnet, = store.observe(
        _info("seven_day_sonnet", resets_at=12_000, utilization=None),
        now=10_000,
    )

    assert opus is not None and opus.limit_id == "claude-seven-day-opus"
    assert opus.primary.used_percent is None
    assert sonnet is not None and sonnet.limit_id == "claude-seven-day-sonnet"
    assert sonnet.primary.used_percent is None
    assert [event.limit_id for event in store.snapshot(now=11_500)] == [
        "claude-seven-day-sonnet",
    ]
    assert store.snapshot(now=12_000) == ()


def test_claude_rejection_without_reset_or_usage_remains_visible(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)

    rejected, = store.observe(_info(
        "five_hour",
        resets_at=None,
        utilization=None,
        status="rejected",
    ), now=now)

    assert rejected is not None
    assert rejected.reached_type == "five_hour"
    assert rejected.primary.used_percent == 100
    assert rejected.primary.resets_at is None
    assert rejected.primary.window_duration_mins == 300
    restored = ClaudeRateLimitStore(tmp_path).snapshot(now=now + 1)
    assert len(restored) == 1
    assert restored[0].primary.used_percent == 100
    assert restored[0].primary.resets_at is None
    assert ClaudeRateLimitStore(tmp_path).snapshot(
        now=now + 300 * 60,
    ) == ()


def test_claude_allowed_without_reset_clears_rejection_state(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    store.observe(_info(
        "seven_day",
        resets_at=None,
        utilization=None,
        status="rejected",
    ), now=now)

    allowed, = store.observe(_info(
        "seven_day",
        resets_at=None,
        utilization=None,
        status="allowed",
    ), now=now + 1)

    assert allowed is not None
    assert allowed.reached_type == ""
    assert allowed.secondary.used_percent is None
    assert allowed.secondary.resets_at is None


def test_claude_rate_limit_store_ignores_unknown_or_stale_events(tmp_path):
    store = ClaudeRateLimitStore(tmp_path)

    assert store.observe(_info("overage"), now=10_000) == ()
    assert store.observe(
        _info("five_hour", resets_at=10_000), now=10_000,
    ) == ()
    assert store.snapshot(now=10_000) == ()


def test_claude_rate_limit_store_rejects_non_object_cache(tmp_path):
    (tmp_path / "claude-rate-limits.json").write_text("[]")

    with pytest.raises(ClaudeRateLimitStoreError, match="invalid shape"):
        ClaudeRateLimitStore(tmp_path)


def _event(rate_type: str, utilization: float) -> RateLimitEvent:
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed",
            resets_at=int(time.time()) + 3_600,
            rate_limit_type=rate_type,
            utilization=utilization,
        ),
        uuid=f"event-{rate_type}",
        session_id="native-claude",
    )


@pytest.mark.parametrize("unified", [False, True])
def test_machine_publishes_sdk_rate_limits_to_every_resident_claude_session(unified):
    async def run():
        machine, transport = _mk_machine()
        code = _mk_ctx("claude-code", "claude-code")
        work = _mk_ctx("claude-work", "claude-work")
        work.space = "work"
        btw = _mk_ctx("claude-btw", "claude-btw")
        btw.btw = True
        codex = _mk_ctx("codex-code", "codex-code")
        codex.engine = "codex"
        machine.sessions = {
            ctx.key: ctx for ctx in (code, work, btw, codex)
        }

        assert await machine._observe_claude_rate_limit_message(
            code, _unified_event(int(time.time())) if unified else _event("five_hour", 0.25)) is True
        published = [message for message in transport.sent
                     if message.type == "rate_limit_update"]
        assert {message.sid for message in published} == {
            "claude-code", "claude-work",
        }
        assert all(message.primary.used_percent == (1 if unified else 25) for message in published)
        if unified:
            assert all(message.secondary.used_percent == 0 for message in published)
        assert code.buffer.tail_seq == 0 and work.buffer.tail_seq == 0

        transport.sent.clear()
        await machine._on_claude_background_message(
            code, _event("seven_day", 0.7), None)
        background = [message for message in transport.sent
                      if message.type == "rate_limit_update"]
        assert {message.sid for message in background} == {
            "claude-code", "claude-work",
        }
        assert all(message.secondary.used_percent == 70
                   for message in background)

    asyncio.run(run())


@pytest.mark.parametrize("unified", [False, True])
def test_hello_reseeds_unexpired_claude_limits_without_a_model_probe(unified):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("claude-session", "claude-session")
        machine.sessions[ctx.key] = ctx
        await machine._observe_claude_rate_limit_message(
            ctx, _unified_event(int(time.time())) if unified else _event("five_hour", 0.4))
        transport.sent.clear()

        await machine._handle_client_hello(SimpleNamespace(
            client_id="client-1", route_id="route-1",
            cursors={}, generations={}, last_seq=None,
        ))

        limits = [message for message in transport.sent
                  if message.type == "rate_limit_update"]
        assert len(limits) == 1
        assert limits[0].sid == "claude-session"
        assert limits[0].to == "client-1"
        assert limits[0].route_id == "route-1"
        assert limits[0].primary.used_percent == (1 if unified else 40)
        if unified:
            assert limits[0].secondary.used_percent == 0

    asyncio.run(run())


@pytest.mark.parametrize("unified", [False, True])
def test_machine_keeps_rate_limits_inside_claude_profile(
    tmp_path, monkeypatch, unified,
):
    async def run():
        personal_root = tmp_path / "personal"
        company_root = tmp_path / "company"
        personal_root.mkdir()
        company_root.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(personal_root))
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.claude_profiles_json = json.dumps({
            "personal": {
                "label": "Personal",
                "config_dir": str(personal_root),
                "default": True,
            },
            "company": {
                "label": "Company",
                "config_dir": str(company_root),
            },
        })
        transport = _StubTransport()
        machine = WrapperMachine(cfg, transport)
        personal = _mk_ctx("personal@native-a", "native-a")
        personal.claude_profile_id = "personal"
        company = _mk_ctx("company@native-b", "native-b")
        company.claude_profile_id = "company"
        machine.sessions = {
            personal.key: personal,
            company.key: company,
        }

        assert await machine._observe_claude_rate_limit_message(
            company, _unified_event(int(time.time())) if unified else _event("five_hour", 0.6),
        ) is True

        published = [
            message for message in transport.sent
            if message.type == "rate_limit_update"
        ]
        assert [message.sid for message in published] == [company.key]
        assert await machine._claude_rate_limit_snapshot(personal) == ()
        company_snapshot = await machine._claude_rate_limit_snapshot(company)
        assert len(company_snapshot) == 1
        assert company_snapshot[0].primary.used_percent == (1 if unified else 60)
        if unified:
            assert company_snapshot[0].secondary.used_percent == 0

    asyncio.run(run())
