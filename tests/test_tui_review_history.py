"""Discontinuous projections must not retain artifacts from old sources."""

import pytest

from cc_remote.tui_app import WorkspaceApp
from cc_remote.tui_inline_images import TranscriptViewport
from cc_remote.tui_state import Block, SessionView
from tests.test_tui_workspace import client, emit


def page(revision, **kwargs):
    return dict(
        type="history", session_id="s", revision=revision, generation="g",
        turns=[dict(id=revision, prompt=revision, blocks=[], done=True)],
        **kwargs,
    )


@pytest.mark.parametrize("continuity,keep", [(None, False), ("old", True)])
def test_history_requires_explicit_continuity_for_old_rows(continuity, keep):
    view = SessionView()
    view.history(page("old"))
    view.history(page("new", build_seq=1, continuity_revision=continuity))
    assert ("user:old" in {b.id for b in view.blocks}) is keep
    assert "user:new" in {b.id for b in view.blocks}


def test_repeated_alias_revisions_preserve_same_continuity_epoch():
    view = SessionView()
    view.history(page("old"))
    view.history(page("alias1", build_seq=1, continuity_revision="old"))
    view.history(page("alias2", build_seq=2, continuity_revision="old"))
    assert {b.id for b in view.blocks} == {
        "user:old", "user:alias1", "user:alias2",
    }


def test_discontinuity_retains_only_proven_newer_live_tail():
    view = SessionView()
    view.history(page("old"))
    view.put(Block("live", "assistant", "new delta", "active", seq=11))
    view.history(page("new", build_seq=1, live_seq=10))
    assert {b.id for b in view.blocks} == {"user:new", "live"}


@pytest.mark.asyncio
async def test_artifact_invalidation_resets_inline_image_identity():
    c = client()
    view = c.workspace.view("s")
    view.put(Block("a", "assistant", "image path"))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        original = viewport.identity
        viewport.errors[("a", "image.png")] = "old failure"
        emit(c, "artifact_invalidated")
        app.paint()
        await pilot.pause()
        assert viewport.identity != original
        assert not viewport.errors


@pytest.mark.asyncio
async def test_idle_paints_do_not_rebuild_projection_after_invalidation(monkeypatch):
    c = client()
    c.workspace.view("s").put(Block("a", "assistant", "**stable** body"))
    app = WorkspaceApp(c, connect=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        viewport = app.query_one(TranscriptViewport)
        calls = []
        original = viewport.project

        def project(*args):
            calls.append(args[-1])
            return original(*args)

        monkeypatch.setattr(viewport, "project", project)
        for _ in range(10):
            app.paint()
        assert not calls
        emit(c, "artifact_invalidated")
        app.paint()
        assert calls == [("s", None, 1)]
        for _ in range(10):
            app.paint()
        assert len(calls) == 1
