"""BTW metadata follows its parent regardless of Hello/catalog ordering."""

import pytest

from cc_remote.tui_state import WorkspaceState


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize("space", ["work", "code"])
@pytest.mark.parametrize("parent_first", [False, True])
def test_side_chat_scope_reconciles_after_parent_catalog(
    engine, space, parent_first,
):
    state = WorkspaceState()
    catalog = dict(type="session_list", engine=engine, space=space, sessions=[
        dict(session_id="parent", summary="Parent", cwd="/work"),
    ])
    side = dict(type="btw_sync", generation="g", revision=1, sessions=[
        dict(btw_sid="btw-side", parent_sid="parent", engine=engine,
             state="running"),
    ])
    for event in ([catalog, side] if parent_first else [side, catalog]):
        state.event(event)
    row = state.catalog["btw-side"]
    assert (row["space"], row["cwd"], row["summary"]) == (
        space, "/work", "BTW · Parent",
    )
    assert row["state"] == "running" and row["engine"] == engine
    state.event(dict(type="session_list", engine=engine,
                     space="code" if space == "work" else "work", sessions=[]))
    assert row["space"] == space
    catalog["sessions"][0].update(summary="Renamed", cwd="/new")
    state.event(catalog)
    assert (row["summary"], row["cwd"]) == ("BTW · Renamed", "/new")
    state.event(dict(type="session_migrated", session_id="parent", cwd="/moved"))
    assert row["cwd"] == "/moved"
    state.event(dict(type="btw_sync", generation="g", revision=2, sessions=[]))
    state.event(catalog)
    assert "btw-side" not in state.catalog
