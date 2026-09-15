"""Goal reads must not fence later authoritative mutations."""

import pytest

from cc_remote import protocol as p, tui
from tests.test_tui_workspace import client, emit


@pytest.mark.asyncio
@pytest.mark.parametrize("read_first", [False, True])
@pytest.mark.parametrize("mutation", [
    p.SetGoal(sid="s", cmd_id="mutate", objective="new"),
    p.ClearGoal(sid="s", cmd_id="mutate"),
    p.DismissGoal(sid="s", cmd_id="mutate", goal_id="g"),
])
async def test_goal_mutation_survives_attach_read(mutation, read_first):
    c = client()
    await c._send(p.GetGoal(sid="s", cmd_id="read"))

    def old_read():
        emit(c, "goal_state", request_id="read", goal_id="old",
             goal={"status": "complete"})

    if read_first:
        old_read()
        assert ("s", "goal_state") not in c.read_tickets
    await c._send(mutation)
    goal = None if isinstance(mutation, p.ClearGoal) else {"status": "active"}
    dismissed = isinstance(mutation, p.DismissGoal)
    emit(c, "goal_state", request_id="mutate", goal_id="g",
         goal=goal, dismissed=dismissed)
    old_read()  # Delayed or duplicate read cannot replace the mutation.
    view = c.workspace.view("s").presentation
    assert view.goal_id == "g" and view.goal == goal
    assert view.goal_dismissed == dismissed
    assert ("s", "goal_state") not in c.read_tickets


@pytest.mark.asyncio
async def test_superseded_goal_reads_stay_fenced_after_ticket_retirement():
    c = client()
    for request in ("older", "newer"):
        await c._send(p.GetGoal(sid="s", cmd_id=request))
    emit(c, "goal_state", request_id="newer", goal_id="new")
    emit(c, "goal_state", request_id="older", goal_id="old")
    assert c.workspace.view("s").presentation.goal_id == "new"
    assert ("s", "goal_state") not in c.read_tickets


@pytest.mark.asyncio
async def test_rejected_goal_read_preserves_admitted_ticket(monkeypatch):
    c = client()
    await c._send(p.GetGoal(sid="s", cmd_id="admitted"))
    monkeypatch.setattr(tui, "TUI_OUTBOX_CAP", 0)
    assert not await c._send(p.GetGoal(sid="s", cmd_id="rejected"))
    assert c.read_tickets["s", "goal_state"][0] == "admitted"
    emit(c, "goal_state", request_id="admitted", goal_id="g")
    assert c.workspace.view("s").presentation.goal_id == "g"
