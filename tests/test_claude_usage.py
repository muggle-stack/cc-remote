"""Active quota reads never create, resume, interrupt or write a model turn."""
import asyncio
from datetime import datetime, timezone
import json
import sys
import time

import pytest

from cc_remote.claude_profiles import ClaudeProfile
from cc_remote.protocol import GetStatus, StatusReport
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
    profile = ClaudeProfile("company", "Company", tmp_path / "native")
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
    other = ClaudeProfile(profile.id, profile.label, tmp_path / "other-account")
    with pytest.raises(ClaudeUsageError, match="configuration invalid"):
        await reader.refresh(other, apply)
    reader.path.chmod(0o644)
    with pytest.raises(ClaudeUsageError, match="configuration invalid"):
        await reader.refresh(profile, apply)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["large", "fail", "timeout"])
async def test_helper_bounds_output_time_and_private_errors(monkeypatch, tmp_path, mode):
    reader = ClaudeUsageReader(tmp_path)
    profile = ClaudeProfile("primary", "Primary", tmp_path)
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
async def test_busy_session_read_and_parallel_sessions_share_one_get(monkeypatch, tmp_path):
    machine, transport = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    profile = machine._claude_profiles.default
    configure(reader, profile, [sys.executable])
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
        return usage(1)
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
        assert report.to == "browser-" + sid
        assert report.request_id == "request-" + sid
    assert one.state == "running" and one.active_msg_id == "active-user"
    assert one.buffer.tail_seq == two.buffer.tail_seq == 0
    assert {e.type for e in transport.sent} <= {"status_report", "rate_limit_update"}


@pytest.mark.asyncio
async def test_failed_or_unconfigured_read_retains_native_cache(monkeypatch, tmp_path):
    machine, _ = _mk_machine()
    reader = machine._claude_usage_reader = ClaudeUsageReader(tmp_path)
    profile = machine._claude_profiles.default
    ctx = _mk_ctx("session", "session")
    machine.sessions = {ctx.key: ctx}
    await machine._observe_claude_rate_limit_message(ctx, _event("five_hour", .42))
    request = GetStatus(sid=ctx.key, cmd_id="read", client_id="browser")
    report = await machine._handle_get_status(request)
    assert report.component_errors == ["rate_limits: usage helper unavailable"]
    assert report.rate_limits[0].primary.used_percent == 42
    configure(reader, profile, [sys.executable])
    async def malformed(*args):
        return {"error": "private upstream detail"}
    monkeypatch.setattr(reader, "_read", malformed)
    report = await machine._handle_get_status(request)
    assert report.component_errors == ["rate_limits: usage response invalid"]
    assert report.rate_limits[0].primary.used_percent == 42
    assert "private" not in report.model_dump_json()
