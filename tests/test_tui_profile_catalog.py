"""Partial account failures must not look like native session deletion."""

import pytest

from cc_remote.tui_state import WorkspaceState


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("space", ["code", "work"])
def test_failed_profile_keeps_sessions_and_control_revision(engine, space):
    state = WorkspaceState()
    profile_key, profiles_key = engine + "_profile_id", engine + "_profiles"

    def listing(rows, profiles=None):
        event = dict(type="session_list", engine=engine, space=space, sessions=rows)
        if profiles is not None:
            event[profiles_key] = profiles
        state.event(event)

    saved = dict(session_id="saved", **{profile_key: "offline"})
    healthy = dict(session_id="healthy", **{profile_key: "ok"})
    provisional = dict(session_id="tmp", provisional_fork=True,
                       **{profile_key: "offline"})
    removed = dict(session_id="removed", **{profile_key: "removed-profile"})
    listing([saved, healthy, provisional, removed])
    control = dict(type="session_control", sid="saved", generation="g",
                   revision=3, write_state="writable")
    state.event(control)
    listing([], [{"id": "offline", "error": "unreachable"}, {"id": "ok"}])
    assert set(state.catalog) == {"saved"}
    assert state.view("saved").write_state == "writable"
    assert state.view("healthy").write_state == "unavailable"
    listing([])  # Legacy/partial response without refreshed profile metadata.
    assert "saved" in state.catalog
    state.event(control)
    assert state.view("saved").write_state == "writable"
    listing([{**saved, "summary": "fresh"}], [{"id": "offline"}, {"id": "ok"}])
    assert state.catalog["saved"]["summary"] == "fresh"
    state.event(control)
    assert state.view("saved").write_state == "writable"
    listing([], [{"id": "offline"}, {"id": "ok"}])
    assert "saved" not in state.catalog
    assert state.view("saved").write_state == "unavailable"
