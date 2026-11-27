import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cc_remote.protocol import GetAntigravityQuota, deserialize, serialize
from cc_remote.wrapper.antigravity_client import AntigravityLaunch
from cc_remote.wrapper import antigravity_quota
from tests.test_multisession import _mk_machine


def usage_payload():
    return {
        "status": "SUCCESS",
        "num_turns": 0,
        "command": {"name": "usage", "data": {
            "plan_tier": "Antigravity Starter Quota",
            "email": "private@example.test",
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini 4 Hydrogen, Gemini 3.8 Flash",
                    "buckets": [{"id": "gemini-weekly", "window": "weekly", "remaining_fraction": .9419,
                                 "reset_time": "2026-12-04T12:43:22Z"}],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [{"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1,
                                 "reset_time": "1970-01-01T00:00:00Z"},
                                {"id": "3p-5h", "window": "5h", "remaining_fraction": 0}],
                },
            ],
        }},
    }


def test_quota_preserves_separate_native_groups_and_ignores_epoch_placeholder():
    result = antigravity_quota.parse_quota(usage_payload())
    assert result.plan == "Antigravity Starter Quota"
    assert result.groups[0].models == ["Gemini 4 Hydrogen", "Gemini 3.8 Flash"]
    assert [bucket.window for bucket in result.groups[0].buckets] == ["weekly"]
    assert result.groups[1].buckets[0].resets_at is None
    assert result.groups[1].buckets[1].remaining_fraction == 0
    payload = serialize(result)
    assert "private@example" not in payload
    assert deserialize(payload) == result


def test_quota_refuses_model_turn_output():
    with pytest.raises(ValueError):
        antigravity_quota.parse_quota({**usage_payload(), "num_turns": 1})


@pytest.mark.asyncio
async def test_automatic_quota_reads_share_cache_until_native_login_changes(monkeypatch):
    request_count = 0

    def fake_read(_launch):
        nonlocal request_count
        request_count += 1
        return antigravity_quota.parse_quota(usage_payload())

    monkeypatch.setattr(antigravity_quota, "read_native_quota", fake_read)
    reader = antigravity_quota.AntigravityQuotaReader()
    launch = AntigravityLaunch("/local/agy", {})
    results = await asyncio.gather(reader.refresh(launch), reader.refresh(launch))
    assert request_count == 1
    assert results[0] == results[1]
    with reader.account_change():
        assert not (await reader.refresh(launch)).groups
    assert (await reader.refresh(launch)).groups
    assert request_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_reliable_account_quota_is_served_by_wrapper(monkeypatch, managed):
    machine, transport = _mk_machine()
    if managed:
        machine._gemini_controller = SimpleNamespace(process=AsyncMock(
            side_effect=AssertionError("The Gemini worker does not handle Wrapper quota reads"),
        ))
    monkeypatch.setattr(machine._antigravity, "opted_out", lambda: False)
    launch = AntigravityLaunch("/local/agy", {})
    monkeypatch.setattr(machine._antigravity, "launch_factory", lambda: launch)
    native_read = Mock(return_value=antigravity_quota.parse_quota(usage_payload()))
    monkeypatch.setattr(antigravity_quota, "read_native_quota", native_read)

    command = GetAntigravityQuota(cmd_id="quota-1", client_id="browser")
    await machine._process_command(command)
    await machine._process_command(command)

    assert [frame.type for frame in transport.sent] == ["antigravity_quota", "command_ack"] * 2
    for result, ack in zip(transport.sent[::2], transport.sent[1::2], strict=True):
        assert result.request_id == ack.cmd_id == "quota-1"
        assert result.to == ack.to == "browser"
        assert result.plan == "Antigravity Starter Quota"
    native_read.assert_called_once_with(launch)
    assert not machine.sessions
