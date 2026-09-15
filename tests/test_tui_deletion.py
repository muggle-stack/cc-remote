"""Deleting unarchived Code sessions never skips authoritative confirmation."""

import asyncio
import json

import pytest

from cc_remote.protocol import DeleteSession, DeleteWorkSession
from cc_remote.tui_app import WorkspaceClient


def setup(engine="codex", space="code", archived=False):
    c = WorkspaceClient("ws://localhost/ws", "", "", engine, "other")
    c.space = space
    c.session_catalog_requests[c.scope] = "catalog-read"
    c.workspace.catalog["target"] = dict(
        session_id="target", engine=engine, space=space,
        tag="archived" if archived else None,
    )
    c.buffers.open("target")
    return c, DeleteSession(session_id="target", engine=engine, space=space)


def sent(c, kind):
    return [frame for raw, _ in c._outbox.values()
            if (frame := json.loads(raw))["type"] == kind]


def ack(c, command):
    c._handle(dict(type="command_ack", cmd_id=command["cmd_id"],
                   to=c.client_id, client_id=c.client_id))


def listing(c, command, *, archived=None, **overrides):
    event = dict(type="session_list", request_id=command["cmd_id"],
                 to=c.client_id, engine=command["engine"], space=command["space"],
                 sessions=[] if archived is None else [dict(
                     c.workspace.catalog["target"],
                     tag="archived" if archived else None)])
    c._handle(dict(event, **overrides))


async def tick():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_archive_proof_and_ack_then_delete_proof_retire_only_target():
    c, cmd = setup()
    c.session_catalog_requests[c.scope] = "unrelated-list"
    assert await c.execute_action(cmd)
    await tick()
    archive = sent(c, "archive_session")[0]
    assert archive["session_id"] == "target" and archive["archived"]
    listing(c, archive, archived=True)
    await tick()
    assert not sent(c, "delete_session")
    ack(c, archive)
    await tick()
    deletion = sent(c, "delete_session")[0]
    assert deletion["session_id"] == "target"
    listing(c, deletion)
    ack(c, deletion)
    await tick()
    assert "target" not in c.buffers.ids
    assert "target" not in c.workspace.catalog
    assert c.attached_sid == "other" and c.notice == "Session deleted"
    assert not c.deletions.tasks and not c.deletions.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["error", "ack-only", "wrong-client",
                                     "wrong-scope", "wrong-request", "active"])
async def test_failed_or_unconfirmed_archive_never_sends_delete(bad):
    c, cmd = setup()
    c.deletions.TIMEOUT = 0.03
    await c.execute_action(cmd)
    await tick()
    archive = sent(c, "archive_session")[0]
    if bad == "error":
        c._handle(dict(type="error", request_id=archive["cmd_id"],
                       to=c.client_id, sid="target", message="Session busy"))
        listing(c, archive, archived=True)
    elif bad != "ack-only":
        override = {"wrong-client": {"to": "foreign"},
                    "wrong-scope": {"space": "work"},
                    "wrong-request": {"request_id": "old"}}.get(bad, {})
        listing(c, archive, archived=bad != "active", **override)
    ack(c, archive)
    await asyncio.sleep(0.06)
    assert not sent(c, "delete_session")
    assert "target" in c.buffers.ids
    assert "target" in c.workspace.catalog
    assert not c.deletions.pending and not c.deletions.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,space,archived", [
    ("claude", "code", False), ("codex", "work", False),
    ("codex", "code", True),
])
async def test_other_delete_paths_do_not_need_archive(engine, space, archived):
    c, cmd = setup(engine, space, archived)
    await c.execute_action(cmd)
    await tick()
    assert not sent(c, "archive_session")
    deletion = sent(c, "delete_session")[0]
    listing(c, deletion)
    ack(c, deletion)
    await tick()
    assert c.notice == "Session deleted"


@pytest.mark.asyncio
async def test_rejection_does_not_optimistically_remove_tab_or_repeat_delete():
    c, cmd = setup(archived=True)
    await c.execute_action(cmd)
    assert not await c.execute_action(cmd)
    await tick()
    deletion = sent(c, "delete_session")[0]
    c._handle(dict(type="error", request_id=deletion["cmd_id"],
                   message="External owner is busy", to=c.client_id))
    listing(c, deletion)
    ack(c, deletion)
    await tick()
    assert "target" in c.buffers.ids and "target" in c.workspace.catalog
    assert "External owner is busy" in c.notice


@pytest.mark.asyncio
async def test_work_rejection_finishes_waiter_without_timeout():
    c, _ = setup(space="work")
    await c.execute_action(DeleteWorkSession(
        session_id="target", engine="codex",
    ))
    await tick()
    deletion = sent(c, "delete_work_session")[0]
    assert deletion["cmd_id"] in c.deletions.pending
    c._handle(dict(type="error", request_id=deletion["cmd_id"],
                   sid="target", to=c.client_id, message="Work session busy"))
    await tick()
    assert not c.deletions.pending and not c.deletions.tasks
    assert "Work session busy" in c.notice
    assert "target" in c.buffers.ids and "target" in c.workspace.catalog


@pytest.mark.asyncio
async def test_exit_during_archive_does_not_schedule_delete_later():
    c, cmd = setup()
    await c.execute_action(cmd)
    await tick()
    archive = sent(c, "archive_session")[0]
    c.deletions.close()
    await tick()
    listing(c, archive, archived=True)
    ack(c, archive)
    await tick()
    assert not sent(c, "delete_session") and not c.deletions.tasks


@pytest.mark.asyncio
async def test_partial_profile_catalog_does_not_confirm_deletion():
    c, cmd = setup(archived=True)
    c.deletions.TIMEOUT = 0.03
    await c.execute_action(cmd)
    await tick()
    deletion = sent(c, "delete_session")[0]
    listing(c, deletion)  # An earlier cached page is not enough either.
    listing(c, deletion, codex_profiles=[{"id": "default", "error": "offline"}])
    ack(c, deletion)
    await asyncio.sleep(0.06)
    assert "target" in c.buffers.ids
    assert "not confirmed" in c.notice


@pytest.mark.asyncio
async def test_rekeyed_request_deletes_real_identity_and_clears_current_focus():
    c, _ = setup(archived=True)
    c.workspace.rekeys["old"] = "target"
    c.attached_sid = "target"
    await c.execute_action(DeleteSession(session_id="old", engine="codex"))
    await tick()
    deletion = sent(c, "delete_session")[0]
    assert deletion["session_id"] == "target"
    listing(c, deletion)
    ack(c, deletion)
    await tick()
    assert c.attached_sid is None and not c.buffers.ids
