"""Catalog mutations must refresh the projected tree after their real ACK."""

import asyncio
import json

import pytest

from cc_remote import protocol as p
from cc_remote.tui_app import WorkspaceClient


def frames(client):
    return [json.loads(raw) for raw, _ in client._outbox.values()]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,space", [
    ("codex", "code"), ("claude", "code"),
    ("codex", "work"), ("claude", "work"),
])
@pytest.mark.parametrize("kind,extra", [
    (p.RenameSession, {"title": "New title"}),
    (p.DeleteSession, {}),
    (p.ArchiveSession, {"archived": True}),
    (p.PinSession, {"pinned": True}),
])
async def test_mutation_ack_refreshes_catalog_without_accepting_stale_lists(
    engine, space, kind, extra,
):
    client = WorkspaceClient("ws://localhost/ws", "", "", engine, None,
                             space=space)
    client.restore_pending = False
    await client._send(p.ListSessions(engine=engine, space=space))
    initial = client.session_catalog_requests[engine, space]
    row = dict(session_id="target", summary="Old title", cwd="/project")
    if kind is p.DeleteSession:
        row["tag"] = "archived"  # Exercise the deletion phase, not archival.
    listing = dict(type="session_list", engine=engine, space=space,
                   request_id=initial, sessions=[row])
    client._handle(listing)
    await client.execute_action(kind(
        session_id="target", engine=engine, space=space, **extra,
    ))
    await asyncio.sleep(0)  # Mutations may run as non-blocking workflows.
    mutation = frames(client)[-1]
    updated = dict(row, summary="New title")
    result = [] if kind is p.DeleteSession else [updated]
    client._handle(dict(listing, request_id=mutation["cmd_id"],
                        sessions=result))
    assert client.workspace.catalog["target"]["summary"] == "Old title"
    ack = dict(type="command_ack", cmd_id=mutation["cmd_id"],
               client_id=client.client_id, to=client.client_id)
    client._handle(dict(ack, client_id="other", to="other"))
    assert not client.catalog_dirty
    client._handle(ack)
    assert client.catalog_dirty == {(engine, space)}
    client._handle(dict(listing, sessions=[]))
    assert "target" in client.workspace.catalog
    await client._flush_history_refreshes()
    refresh = client.session_catalog_requests[engine, space]
    assert refresh != initial
    client._handle(dict(listing, request_id=refresh, sessions=result))
    expected = {item["session_id"]: item["summary"] for item in result}
    assert {sid: item["summary"]
            for sid, item in client.workspace.catalog.items()} == expected
    client._handle(listing)
    client._handle(ack)
    assert not client.catalog_dirty  # Duplicate ACK cannot refetch forever.
    assert {sid: item["summary"]
            for sid, item in client.workspace.catalog.items()} == expected
