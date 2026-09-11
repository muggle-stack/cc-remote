"""Catalog authority for direct startup and removed tabs."""

import json

import pytest

from cc_remote import tui
from cc_remote.protocol import ListSessions
from tests.test_tui_workspace import client, emit


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,space", [
    ("claude", "code"), ("codex", "work"), ("claude", "work"),
])
async def test_direct_id_waits_for_catalog_scope(engine, space):
    c = client()
    sent = []

    async def send(command):
        sent.append(command.model_dump())
        return True

    async def raw(frame):
        sent.append(json.loads(frame))
        return True

    c._send, c._send_raw = send, raw
    await c._recovery_preamble()
    assert not any(f["type"] == "switch_session" for f in sent)
    assert c.attached_sid is None
    emit(c, "session_list", engine=engine, space=space, sessions=[
        dict(session_id="s", engine=engine, space=space, cwd="/work"),
    ])
    await c.restore_surface()
    switch = next(f for f in sent if f["type"] == "switch_session")
    assert (switch["session_id"], switch["engine"], switch["space"]) == (
        "s", engine, space,
    )
    assert c.scope == (engine, space) and c.attached_sid == "s"


@pytest.mark.asyncio
async def test_catalog_deletion_disables_retained_tab_without_erasing_draft():
    c = client()
    emit(c, "session_list", engine="codex", sessions=[dict(session_id="s")])
    view = c.workspace.view("s")
    view.write_state = "writable"
    view.draft = "keep draft"
    emit(c, "session_list", engine="claude", sessions=[])
    assert view.write_state == "writable"
    emit(c, "session_list", engine="codex", sessions=[])
    assert view.write_state == "unavailable" and view.draft == "keep draft"
    assert not await c.submit("must not send")
    assert not c._outbox


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["TUI_OUTBOX_CAP", "TUI_OUTBOX_BYTES"])
@pytest.mark.parametrize("existing", [False, True])
async def test_catalog_rejection_preserves_fence_and_retries(
    monkeypatch, cap, existing,
):
    c = client(None)
    scope = ("codex", "work")
    frames = []

    async def raw(frame):
        frames.append(json.loads(frame))
        return True

    c._send_raw = raw
    if existing:
        assert await c._send(ListSessions(
            engine="codex", space="work", cmd_id="previous",
        ))
    with monkeypatch.context() as blocked:
        blocked.setattr(tui, cap, 0)
        await c.switch_surface(*scope)
    assert c.session_catalog_requests.get(scope) == (
        "previous" if existing else None
    )
    assert scope in c.catalog_retries
    assert scope not in c.catalog_dirty
    assert scope not in c.catalog_ready
    if existing:
        emit(c, "session_list", engine="codex", space="work",
             request_id="previous", sessions=[])
        assert scope in c.catalog_ready
        emit(c, "ack", cmd_id="previous")
    await c._flush_history_refreshes()
    request = next(f for f in reversed(frames) if f["type"] == "list_sessions")
    assert request["cmd_id"] != "previous"
    assert c.session_catalog_requests[scope] == request["cmd_id"]
    assert scope not in c.catalog_retries
    emit(c, "session_list", engine="codex", space="work",
         request_id=request["cmd_id"], sessions=[])
    assert scope in c.catalog_ready


@pytest.mark.asyncio
async def test_catalog_fence_precedes_immediate_response():
    c = client(None)

    async def raw(frame):
        request = json.loads(frame)
        emit(c, "session_list", engine="codex", space="work",
             request_id=request["cmd_id"], sessions=[])
        return True

    c._send_raw = raw
    c.session_catalog_requests["codex", "work"] = "older"
    await c.switch_surface("codex", "work")
    assert ("codex", "work") in c.catalog_ready


@pytest.mark.asyncio
async def test_rejected_refresh_keeps_invalidation_fence(monkeypatch):
    c = client(None)
    scope = ("codex", "work")
    c.session_catalog_requests[scope] = "invalidated"
    c.catalog_dirty.add(scope)
    monkeypatch.setattr(tui, "TUI_OUTBOX_CAP", 0)
    await c._flush_history_refreshes()
    assert scope in c.catalog_dirty and scope in c.catalog_retries
    emit(c, "session_list", engine="codex", space="work",
         request_id="invalidated", sessions=[])
    assert scope not in c.catalog_ready
