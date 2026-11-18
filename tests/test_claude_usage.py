"""Active quota reads never create, resume, interrupt or write a model turn."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import sys
import time

import pytest

from cc_remote.claude_profiles import ClaudeProfile, ClaudeProfileRegistry
from cc_remote.protocol import GetStatus, StatusReport, deserialize, serialize
from cc_remote.wrapper import claude_usage
from cc_remote.wrapper.claude_rate_limits import ClaudeRateLimitStore
from cc_remote.wrapper.claude_usage import ClaudeUsageError, ClaudeUsageReader
from tests.test_claude_rate_limits import _event
from tests.test_multisession import _mk_ctx, _mk_machine


def usage(value=37):
    return {"five_hour": {"utilization": value, "resets_at": datetime.fromtimestamp(
        time.time() + 3600, timezone.utc).isoformat()},
        "seven_day": {"utilization": 0, "resets_at": None},
        "seven_day_sonnet": None, "ignored_secret": "never-copy"}


def configure(reader, profile, command):
    reader.path.parent.mkdir(parents=True, exist_ok=True)
    reader.path.write_text(json.dumps({"version": 1, "profiles": {
        profile.id: {"config_dir": str(profile.config_dir), "command": command},
    }}))
    reader.path.chmod(0o600)


def enable_queries(machine):
    machine._claude_profiles = ClaudeProfileRegistry(tuple(
        replace(profile, usage_query_enabled=True)
        for profile in machine._claude_profiles
    ))
    return machine._claude_profiles.default


@pytest.fixture(autouse=True)
def isolate_native_account(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


def test_usage_percentages_and_null_windows_are_not_sdk_fractions(tmp_path):
    store = ClaudeRateLimitStore(tmp_path)
    store.observe(_event("seven_day_sonnet", .8).rate_limit_info)
    store.observe_usage(usage(1), dict(store.revisions))
    row, = store.snapshot()
    assert row.primary.used_percent == 1
    assert row.secondary.used_percent == 0
    assert "never-copy" not in store.path.read_text()
    assert "seven_day_sonnet" not in store.path.read_text()
    restored, = ClaudeRateLimitStore(tmp_path).snapshot()
    assert restored.primary == row.primary and restored.secondary == row.secondary


def test_account_plan_and_fable_use_reported_values_only(tmp_path):
    store = ClaudeRateLimitStore(tmp_path)
    now = int(time.time())
    reset = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
    payload = {
        "plan_display_name": "Ultra",
        "subscriptionType": "pro",
        "limits": [{
            "kind": "weekly_scoped",
            "scope": {"model": {"display_name": "Fable"}},
            "percent": 0,
            "resets_at": reset,
        }],
    }
    store.observe_usage(payload, {}, now=now)
    fable, plan = store.snapshot(now=now, include_plan=True)
    assert fable.limit_id == "claude-seven-day-fable"
    assert fable.primary.used_percent == 0
    assert fable.primary.resets_at == now + 86400
    assert plan.limit_id == "claude" and plan.plan_type == "Ultra"
    assert "Ultra" not in store.path.read_text()
    assert [row.plan_type for row in ClaudeRateLimitStore(tmp_path).snapshot(
        now=now, include_plan=True)] == [None]
    store.observe_usage({
        "subscriptionType": "pro",
        "seven_day_fable": {"utilization": 50, "resets_at": None},
    }, {}, now=now)
    assert store.snapshot(now=now, include_plan=True) == ()


@pytest.mark.parametrize("invalid", [True, "5", -1, 101, float("nan"), None])
def test_invalid_usage_is_atomic(tmp_path, invalid):
    store = ClaudeRateLimitStore(tmp_path)
    store.observe_usage(usage(), {})
    original = store.path.read_bytes()
    bad = usage(50)
    bad["seven_day"] = {"utilization": invalid}
    with pytest.raises(ValueError):
        store.observe_usage(bad, {})
    assert store.path.read_bytes() == original
    assert store.snapshot()[0].primary.used_percent == 37


def test_new_native_observation_wins_over_inflight_snapshot(tmp_path):
    store = ClaudeRateLimitStore(tmp_path)
    before = dict(store.revisions)
    store.observe(_event("five_hour", .65).rate_limit_info)
    store.observe_usage(usage(1), before)
    row, = store.snapshot()
    assert row.primary.used_percent == 65
    assert row.secondary.used_percent == 0


@pytest.mark.asyncio
async def test_real_helper_receives_only_profile_binding(monkeypatch, tmp_path):
    reader = ClaudeUsageReader(tmp_path)
    profile = ClaudeProfile("company", "Company", tmp_path / "native",
                            usage_query_enabled=True)
    monkeypatch.setenv("WRAPPER_TOKEN", "must-not-reach-helper")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "wrong-account")
    code = ("import json,os,sys; d=json.load(sys.stdin); "
            "assert d['profile_id']=='company'; "
            "assert 'WRAPPER_TOKEN' not in os.environ; "
            "assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ; "
            "print(json.dumps({'seven_day':{'utilization':0,'resets_at':None}}))")
    configure(reader, profile, [sys.executable, "-c", code])
    seen = []
    async def apply(read):
        seen.append(await read())
    assert await reader.refresh(profile, apply) is None
    assert seen == [{"seven_day": {"utilization": 0, "resets_at": None}}]
    other = replace(profile, config_dir=tmp_path / "other-account")
    with pytest.raises(ClaudeUsageError, match="configuration invalid"):
        await reader.refresh(other, apply)
    reader.path.chmod(0o644)
    with pytest.raises(ClaudeUsageError, match="configuration invalid"):
        await reader.refresh(profile, apply)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["large", "fail", "timeout"])
async def test_helper_bounds_output_time_and_private_errors(monkeypatch, tmp_path, mode):
    reader = ClaudeUsageReader(tmp_path)
    profile = ClaudeProfile("primary", "Primary", tmp_path, usage_query_enabled=True)
    code = {"large": "print('secret' * 30000)",
            "fail": "import sys; print('private'); sys.exit(1)",
            "timeout": "import time; time.sleep(10)"}[mode]
    configure(reader, profile, [sys.executable, "-c", code])
    monkeypatch.setattr(claude_usage, "TIMEOUT", .2)
    async def apply(read):
        await read()
    error = await reader.refresh(profile, apply)
    assert error and "secret" not in error and "private" not in error


@pytest.mark.asyncio
async def test_busy_session_read_and_parallel_sessions_share_one_get(
    monkeypatch, tmp_path, isolate_native_account,
):
    machine, transport = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    profile = enable_queries(machine)
    configure(reader, profile, [sys.executable])
    (isolate_native_account / ".claude.json").write_text(
        '{"oauthAccount":{"planDisplayName":"Max"}}')
    one, two = _mk_ctx("one", "one"), _mk_ctx("two", "two")
    one.state = "running"
    one.active_msg_id = "active-user"
    # Any access to SDK controls or query would fail; an active worker is not
    # necessary for this read and must not be created as a side effect.
    one.sdk = two.sdk = None
    machine.sessions = {"one": one, "two": two}
    calls = []
    async def read(profile, command):
        calls.append(profile.id)
        await asyncio.sleep(.01)
        await machine._observe_claude_rate_limit_message(one, _event("five_hour", .65))
        return {**usage(1), "plan_display_name": "Ultra"}
    monkeypatch.setattr(reader, "_read", read)
    reports = await asyncio.gather(*(machine._handle_get_status(GetStatus(
        sid=sid, cmd_id="request-" + sid, client_id="browser-" + sid))
        for sid in ("one", "two")))
    assert calls == [profile.id]
    for report, sid in zip(reports, ("one", "two")):
        assert isinstance(report, StatusReport)
        assert not report.component_errors
        assert report.rate_limits[0].primary.used_percent == 65
        assert report.rate_limits[0].secondary.used_percent == 0
        assert report.rate_limits[0].plan_type == "Ultra"
        assert report.to == "browser-" + sid
        assert report.request_id == "request-" + sid
    assert one.state == "running" and one.active_msg_id == "active-user"
    assert one.buffer.tail_seq == two.buffer.tail_seq == 0
    assert {e.type for e in transport.sent} <= {"status_report", "rate_limit_update"}


@pytest.mark.asyncio
async def test_failed_or_unconfigured_read_retains_native_cache(
    monkeypatch, tmp_path, isolate_native_account,
):
    machine, _ = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    profile = machine._claude_profiles.default
    ctx = _mk_ctx("session", "session")
    machine.sessions = {ctx.key: ctx}
    await machine._observe_claude_rate_limit_message(ctx, _event("five_hour", .42))
    (isolate_native_account / ".claude.json").write_text(
        '{"oauthAccount":{"planDisplayName":"Ultra"}}')
    request = GetStatus(sid=ctx.key, cmd_id="read", client_id="browser")
    report = await machine._handle_get_status(request)
    assert report.quota_query_state == "disabled"
    assert report.component_errors == []
    assert report.rate_limits[0].primary.used_percent == 42
    profile = enable_queries(machine)
    report = await machine._handle_get_status(request)
    assert report.quota_query_state == "unconfigured"
    assert report.component_errors == []
    assert report.rate_limits[0].primary.used_percent == 42
    configure(reader, profile, [sys.executable])
    async def malformed(*args):
        return {"error": "private upstream detail"}
    monkeypatch.setattr(reader, "_read", malformed)
    report = await machine._handle_get_status(request)
    assert report.component_errors == ["rate_limits: usage response invalid"]
    assert report.rate_limits[0].primary.used_percent == 42
    assert report.rate_limits[0].plan_type == "Ultra"
    assert "private" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_disabled_queries_read_native_plan_without_helper(
    monkeypatch, tmp_path, isolate_native_account,
):
    machine, _ = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    profile = machine._claude_profiles.default
    configure(reader, profile, [sys.executable, "-c", "raise AssertionError()"])
    def forbidden(*args):
        pytest.fail("disabled account must not access the helper or model")
    monkeypatch.setattr(reader, "command", forbidden)
    monkeypatch.setattr(reader, "_read", forbidden)
    ctx = _mk_ctx("native", "native")
    ctx.sdk = None
    machine.sessions = {ctx.key: ctx}
    for _ in range(3):
        report = await machine._handle_get_status(GetStatus(sid=ctx.key))
        assert report.quota_query_state == "disabled"
        assert report.rate_limits == [] and not report.component_errors
        assert await reader.refresh(profile, forbidden) == "usage query disabled"

    account_path = isolate_native_account / ".claude.json"
    account_path.write_text(json.dumps({"oauthAccount": {
        "planDisplayName": "Ultra", "subscriptionType": "pro",
        "accountUuid": "private-account-id",
    }}))
    report = await machine._handle_get_status(GetStatus(sid=ctx.key))
    row, = report.rate_limits
    assert report.quota_query_state == "disabled" and not report.component_errors
    assert row.limit_id == "claude" and row.plan_type == "Ultra"
    assert row.primary is None and row.secondary is None
    assert "private-account-id" not in report.model_dump_json()

    await machine._observe_claude_rate_limit_message(ctx, _event("five_hour", .42))
    report = await machine._handle_get_status(GetStatus(sid=ctx.key))
    assert report.rate_limits[0].primary.used_percent == 42
    assert report.rate_limits[0].plan_type == "Ultra"

    account_path.write_text('{"oauthAccount":{"subscriptionType":"pro"}}')
    report = await machine._handle_get_status(GetStatus(sid=ctx.key))
    assert report.rate_limits[0].primary.used_percent == 42
    assert report.rate_limits[0].plan_type is None
    account_path.write_text('{"oauthAccount":')
    report = await machine._handle_get_status(GetStatus(sid=ctx.key))
    assert report.rate_limits[0].primary.used_percent == 42
    assert report.rate_limits[0].plan_type is None


@pytest.mark.asyncio
async def test_passive_plan_respects_explicit_claude_account_root(
    monkeypatch, tmp_path, isolate_native_account,
):
    home = isolate_native_account
    native_root = home / ".claude"
    other_root = tmp_path / "other-account"
    for root, plan in ((home, "Ultra"), (native_root, "Pro"), (other_root, "Max")):
        root.mkdir(exist_ok=True)
        (root / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "planDisplayName": plan,
        }}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other_root))
    monkeypatch.setenv("CC_REMOTE_CLAUDE_PROFILES_JSON", json.dumps({
        "native": {"label": "Native", "config_dir": str(native_root), "default": True},
        "other": {"label": "Other", "config_dir": str(other_root)},
    }))
    machine, _ = _mk_machine()
    for profile_id, expected in (("native", "Ultra"), ("other", "Max")):
        sid = profile_id + "-session"
        ctx = _mk_ctx(sid, sid)
        ctx.claude_profile_id = profile_id
        ctx.sdk = None
        machine.sessions[ctx.key] = ctx
        report = await machine._handle_get_status(GetStatus(sid=ctx.key))
        assert [row.plan_type for row in report.rate_limits] == [expected]


@pytest.mark.parametrize("windows", [
    ("monthly",), ("five_hour", "monthly"), ("seven_day", "monthly"),
    ("five_hour", "seven_day", "monthly"),
])
def test_monthly_usage_preserves_named_cycle_through_cache_and_protocol(tmp_path, windows):
    now = int(time.time())
    reset = datetime.fromtimestamp(now + 27 * 86400, timezone.utc).isoformat()
    payload = {name: {"utilization": 16, "resets_at": reset} for name in windows}
    store = ClaudeRateLimitStore(tmp_path)
    store.observe_usage(payload, {}, now=now)
    update, = ClaudeRateLimitStore(tmp_path).snapshot(now=now)
    wire = deserialize(serialize(update))
    assert wire.monthly.used_percent == 16
    assert wire.monthly.resets_at == now + 27 * 86400
    assert wire.monthly.window_duration_mins is None
    assert (wire.primary is not None) == ("five_hour" in windows)
    assert (wire.secondary is not None) == ("seven_day" in windows)
    assert store.snapshot(now=now + 27 * 86400) == ()


def test_monthly_clear_invalid_snapshot_and_native_updates(tmp_path):
    now = int(time.time())
    store = ClaudeRateLimitStore(tmp_path)
    store.observe_usage({**usage(), "monthly": {"utilization": 22, "resets_at": None}}, {})
    original = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.observe_usage({"five_hour": None, "monthly": {"utilization": 101}}, {})
    assert store.path.read_bytes() == original
    store.observe(_event("five_hour", .6).rate_limit_info)
    row, = store.snapshot()
    assert row.monthly.used_percent == 22 and row.primary.used_percent == 60
    store.observe_usage({"seven_day": None, "monthly": None}, dict(store.revisions))
    row, = store.snapshot()
    assert row.primary.used_percent == 60 and row.secondary is None and row.monthly is None
    store.observe_usage({"monthly": {"utilization": 0, "resets_at": None}}, {}, now=now)
    assert ClaudeRateLimitStore(tmp_path).snapshot(now=now)[0].monthly.used_percent == 0
    assert store.snapshot(now=now + 901)[0].monthly is None


@pytest.mark.asyncio
async def test_opted_in_monthly_snapshot_is_account_scoped_and_read_only(monkeypatch, tmp_path):
    machine, _ = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    native = machine._claude_profiles.default
    custom = ClaudeProfile("custom-account", "Custom", tmp_path / "custom",
                           usage_query_enabled=True)
    machine._claude_profiles = ClaudeProfileRegistry((native, custom))
    machine._claude_rate_limit_stores[custom.id] = ClaudeRateLimitStore(tmp_path / "custom-cache")
    configure(reader, custom, [sys.executable])
    ctx = _mk_ctx("custom-session", "custom-session")
    ctx.claude_profile_id = custom.id
    ctx.sdk = None
    other = _mk_ctx("native-session", "native-session")
    other.claude_profile_id = native.id
    other.sdk = None
    machine.sessions = {ctx.key: ctx, other.key: other}
    calls = []
    async def read(profile, command):
        calls.append(profile.id)
        return {"five_hour": {"utilization": 28}, "seven_day": None,
                "monthly": {"utilization": 16}}
    monkeypatch.setattr(reader, "_read", read)
    result = await machine._handle_get_status(GetStatus(sid=ctx.key))
    assert result.quota_query_state == "enabled" and not result.component_errors
    wire = deserialize(serialize(result))
    assert wire.rate_limits[0].monthly.used_percent == 16
    assert wire.rate_limits[0].primary.used_percent == 28
    assert wire.rate_limits[0].secondary is None
    other_result = await machine._handle_get_status(GetStatus(sid=other.key))
    assert other_result.rate_limits == [] and other_result.quota_query_state == "disabled"
    assert calls == [custom.id]
