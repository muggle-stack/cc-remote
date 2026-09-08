"""History refreshes must not change page families or erase painted turns."""
from __future__ import annotations

import asyncio

import pytest

from cc_remote.protocol import GetHistory, History, deserialize, serialize
from cc_remote.wrapper.codex_history import CodexOfficialHistory
from tests.test_codex_history import _agent, _turn, _user
from tests.test_multisession import _mk_ctx, _mk_machine


def test_codex_background_refresh_publishes_an_official_usable_cursor(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("official-refresh", "official-refresh")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        calls = []

        async def rpc(method, params, cwd=None):
            calls.append((method, params))
            assert method == "thread/turns/list"
            older = params["cursor"] == "opaque-older"
            number = "1" if older else "2"
            return {
                "data": [_turn(
                    f"native-{number}",
                    [_user(f"user-{number}", f"question {number}"),
                     _agent(f"answer-{number}", "done")],
                    items_view=params["itemsView"],
                )],
                "nextCursor": None if older else "opaque-older",
            }

        machine._codex_history = CodexOfficialHistory(64 * 1024, rpc=rpc)
        monkeypatch.setattr(machine, "_codex_rollout_for_wire", lambda _sid: None)

        async def raw_history(*_args, **_kwargs):
            pytest.fail("a send-side refresh bypassed the official page reader")

        monkeypatch.setattr(machine, "_build_history", raw_history)
        # This is the generic send/steer repair path, not a GetHistory request.
        machine._schedule_history_refresh(
            ctx.key, before=None, limit=4, cwd=ctx.cwd, detail="summary")
        await asyncio.wait_for(asyncio.gather(
            *list(machine._history_refresh_tasks.values())), timeout=2)
        head, = [frame for frame in transport.sent if isinstance(frame, History)]
        assert head.authoritative is True
        assert head.oldest_id == "user-2"
        assert head.continuity_revision == head.revision
        assert deserialize(serialize(head)).continuity_revision == head.revision

        await machine._handle_get_history(GetHistory(
            session_id=ctx.key, before=head.oldest_id, limit=4, detail="summary"))
        older = transport.sent[-1]
        assert isinstance(older, History)
        assert older.error is None and older.authoritative is True
        assert [turn.id for turn in older.turns] == ["user-1"]
        assert any(params["cursor"] == "opaque-older" for _, params in calls)
        assert all(method == "thread/turns/list" for method, _ in calls)

    asyncio.run(run())


def test_additive_alias_revisions_keep_continuity_and_rollout_cursors(
    monkeypatch, tmp_path,
):
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("alias-refresh", "alias-refresh")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text('{"type":"session_meta"}\n')
        initial = machine._activate_codex_rollout_history(
            ctx.key, advance_revision=False)
        reads = []

        async def raw_history(sid, **kwargs):
            reads.append(kwargs)
            return History(session_id=sid, revision=machine._history_revision(sid),
                           detail="summary", before=kwargs["before"])

        async def official(*_args, **_kwargs):
            pytest.fail("alias learning changed the selected page family")

        monkeypatch.setattr(machine, "_build_history", raw_history)
        monkeypatch.setattr(machine, "_build_official_codex_history", official)
        for index in range(2):
            assert await machine._remember_codex_client_message_id(
                ctx, f"native-{index}", f"browser-{index}", segment_index=0,
                source_path=str(rollout))
            assert machine._codex_rollout_history_active(ctx.key)
            page = await machine._build_requested_history(
                ctx.key, before="old-rollout-cursor", limit=4, cwd=None,
                detail="summary", _background=True)
            assert page.revision != initial
            assert page.continuity_revision == initial
        assert all(read["before"] == "old-rollout-cursor" for read in reads)
        assert all(read["allow_stale"] is False for read in reads)
        # Duplicate exact evidence must not invalidate the page again.
        revision = machine._history_revision(ctx.key)
        assert not await machine._remember_codex_client_message_id(
            ctx, "native-1", "browser-1", segment_index=0,
            source_path=str(rollout))
        assert machine._history_revision(ctx.key) == revision

    asyncio.run(run())


@pytest.mark.parametrize("invalidation", ["rollback", "provider"])
def test_destructive_change_breaks_alias_continuity(monkeypatch, invalidation):
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("continuity", "continuity")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        initial = machine._history_revision(ctx.key)
        machine._bump_codex_projection_revision(ctx.key, aliases_only=True)
        assert machine._history_continuity_revisions[ctx.key] == initial
        assert "another-profile@continuity" not in machine._history_continuity_revisions
        if invalidation == "rollback":
            machine._invalidate_session_history(ctx, ctx.key)
        else:
            machine._activate_codex_rollout_history(ctx.key)

        async def history(sid, **_kwargs):
            return History(session_id=sid, revision=machine._history_revision(sid))

        monkeypatch.setattr(machine, "_build_history", history)
        monkeypatch.setattr(machine, "_build_official_codex_history", history)
        page = await machine._build_requested_history(
            ctx.key, before=None, limit=4, cwd=None, detail="summary")
        assert page.continuity_revision == page.revision != initial
        # Subsequent aliases belong to the new lineage, never the rolled-back one.
        machine._bump_codex_projection_revision(ctx.key, aliases_only=True)
        next_page = await machine._build_requested_history(
            ctx.key, before=None, limit=4, cwd=None, detail="summary")
        assert next_page.continuity_revision == page.revision

    asyncio.run(run())


def test_obsolete_read_cannot_claim_current_continuity(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("obsolete", "obsolete")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx

        async def history(sid, **_kwargs):
            revision = machine._history_revision(sid)
            machine._bump_history_revision(sid)
            return History(session_id=sid, revision=revision, authoritative=False)

        monkeypatch.setattr(machine, "_build_official_codex_history", history)
        page = await machine._build_requested_history(
            ctx.key, before=None, limit=4, cwd=None, detail="summary",
            _background=True)
        assert page.continuity_revision is None
        assert not machine._history_refresh_tasks

    asyncio.run(run())


def test_official_refresh_coalesces_generic_repairs_without_broadcasting_errors(
    monkeypatch,
):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("coalesced", "coalesced")
        ctx.engine = "codex"
        machine.sessions[ctx.key] = ctx
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def history(sid, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return History(session_id=sid, revision=machine._history_revision(sid),
                           authoritative=False, error="offline")

        monkeypatch.setattr(machine, "_build_official_codex_history", history)
        machine._schedule_official_codex_history_refresh(ctx.key, limit=4, cwd=None)
        await asyncio.wait_for(entered.wait(), timeout=1)
        machine._schedule_history_refresh(
            ctx.key, before=None, limit=4, cwd="/other-hint", detail="summary")
        assert len(machine._history_refresh_tasks) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(
            *list(machine._history_refresh_tasks.values())), timeout=2)
        assert calls == 2  # one coalesced repair, no retry loop for an error
        assert not any(isinstance(frame, History) for frame in transport.sent)

    asyncio.run(run())
