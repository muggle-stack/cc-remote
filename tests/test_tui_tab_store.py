"""Tab-only persistence, concurrent terminal edits and guarded restoration."""

import json
from unittest.mock import AsyncMock

import pytest

from cc_remote.tui_app import WorkspaceApp, WorkspaceClient
from cc_remote.tui_tab_store import TabStore


def store(tmp_path, **kwargs):
    return TabStore(kwargs.get("url", "ws://localhost:8766/ws"),
                    kwargs.get("machine", "default"),
                    kwargs.get("username", "user"), directory=tmp_path)


def client():
    return WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", None)


def test_private_atomic_tabs_exclude_transient_ids_and_secrets(tmp_path):
    saved = store(tmp_path)
    assert saved.load() is None
    saved.save(["a", "a", "b", "tmp-new", "btw-aside"], "b", ("claude", "work"))
    assert saved.load() == {
        "tabs": ["a", "b"], "active": "b", "scope": ["claude", "work"]}
    assert saved.path.stat().st_mode & 0o777 == 0o600
    assert "localhost" not in saved.path.read_text()
    assert len(list(tmp_path.glob(".tabs-*"))) == 0


@pytest.mark.parametrize("boundary", ["url", "machine", "username"])
def test_scopes_do_not_restore_other_device_or_account(tmp_path, boundary):
    first = store(tmp_path)
    first.save(["private"], "private", ("codex", "code"))
    assert store(tmp_path, **{boundary: "different"}).load() is None


def test_two_terminals_merge_edits_without_resurrecting_closed_tabs(tmp_path):
    first = store(tmp_path)
    first.save(["a", "b"], "a", ("codex", "code"))
    second = store(tmp_path)
    second.load()
    first.save(["a"], "a", ("codex", "code"))
    second.save(["a", "b", "c"], "c", ("codex", "code"))
    assert store(tmp_path).load()["tabs"] == ["a", "c"]
    first.save(["a", "d"], "d", ("codex", "code"))
    assert store(tmp_path).load()["tabs"] == ["a", "c", "d"]


@pytest.mark.asyncio
async def test_restore_waits_for_catalog_and_attaches_only_saved_active(tmp_path):
    saved = store(tmp_path)
    saved.save(["a", "b"], "b", ("claude", "work"))
    c = client()
    c.restore_tabs(store(tmp_path))
    c._attach = AsyncMock()
    assert c.scope == ("claude", "work")
    assert c.buffers.ids == ["a", "b"] and not c._outbox
    await c.restore_surface()
    c._attach.assert_not_awaited()
    c.workspace.catalog.update({sid: {
        "session_id": sid, "engine": "claude", "space": "work",
    } for sid in ("a", "b", "newer")})
    c.catalog_ready.add(c.scope)
    await c.restore_surface()
    c._attach.assert_awaited_once_with("b", "claude")


@pytest.mark.asyncio
@pytest.mark.parametrize("tabs", [[], ["deleted"]])
async def test_empty_or_deleted_working_set_does_not_open_recent_session(tmp_path, tabs):
    saved = store(tmp_path)
    saved.save(tabs, None, ("codex", "code"))
    c = client()
    c.restore_tabs(store(tmp_path))
    c._attach = AsyncMock()
    c.workspace.catalog["unrelated"] = {
        "session_id": "unrelated", "engine": "codex", "space": "code"}
    c.catalog_ready.add(c.scope)
    await c.restore_surface()
    c._attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_close_rekey_and_exit_persist_without_model_commands(tmp_path):
    c = client()
    c.demo = True
    c.restore_pending = False
    c.restore_tabs(store(tmp_path))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await app.attach_session("a")
        await app.attach_session("b")
        assert store(tmp_path).load()["tabs"] == ["a", "b"]
        await app.action_close_buffer()
        assert store(tmp_path).load()["tabs"] == ["a"]
        c.buffers.rekey("a", "real")
        c.attached_sid = "real"
        app.paint()
        await pilot.pause()
    assert store(tmp_path).load()["tabs"] == ["real"]
    assert not c._outbox


def test_corrupt_state_reported_without_overwriting(tmp_path):
    saved = store(tmp_path)
    saved.save(["a"], "a", ("codex", "code"))
    saved.path.write_text("not json")
    c = client()
    c.restore_tabs(saved)
    assert c.tab_store is None and "Cannot read" in c.notice
    c.save_tabs()
    assert saved.path.read_text() == "not json"


def test_explicit_session_overrides_restored_focus(tmp_path):
    saved = store(tmp_path)
    saved.save(["a"], "a", ("claude", "work"))
    c = WorkspaceClient("ws://localhost:8766/ws", "", "", "codex", "explicit")
    c.restore_tabs(store(tmp_path))
    assert c.attached_sid == "explicit" and c.scope == ("codex", "code")
    assert not c.restore_pending


@pytest.mark.parametrize("engine,space,expected", [
    (True, False, ("codex", "work")),
    (False, True, ("claude", "code")),
    (True, True, ("codex", "code")),
])
def test_explicit_scope_axes_override_saved_scope(
    tmp_path, engine, space, expected,
):
    saved = store(tmp_path)
    saved.save(["a"], "a", ("claude", "work"))
    c = client()
    c.explicit_engine, c.explicit_space = engine, space
    c.restore_tabs(saved)
    assert c.scope == expected
    assert c.buffers.ids == ["a"]
    assert c.scope not in c.last_focus


def test_save_only_on_working_set_change(tmp_path):
    c = client()
    c.restore_tabs(store(tmp_path))
    c.restore_pending = False
    c.buffers.open("a")
    c.attached_sid = "a"
    c.save_tabs()
    before = c.tab_store.path.stat().st_mtime_ns
    c.save_tabs()
    assert c.tab_store.path.stat().st_mtime_ns == before
    assert json.loads(c.tab_store.path.read_text())["active"] == "a"
