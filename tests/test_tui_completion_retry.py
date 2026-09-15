"""Outbox backpressure must not permanently suppress read receipts."""

import pytest

from cc_remote.tui_app import WorkspaceApp
from tests.test_tui_workspace import client


@pytest.mark.asyncio
async def test_failed_completion_ack_can_be_retried():
    c = client()
    app = WorkspaceApp(c, connect=False)
    key = ("s", "completion")
    outcomes = iter([False, True])

    async def send(command):
        assert command.type == "acknowledge_completion"
        return next(outcomes)

    c._send = send
    app.acknowledged.add(key)
    await app.acknowledge_completion(*key)
    assert key not in app.acknowledged
    app.acknowledged.add(key)
    await app.acknowledge_completion(*key)
    assert key in app.acknowledged
