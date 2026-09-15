"""Native manual compaction must not manufacture a human conversation turn."""
import json
import sqlite3

import pytest

from cc_remote.protocol import ProcessEvent, TurnEnd, UserMsg
from cc_remote.wrapper.history_store import HistoryIndexStore, HistorySourceFingerprint
from tests.test_history_store import _page
from cc_remote.wrapper.stream import (
    transcript_compact_history_page,
    transcript_compact_snapshot,
    translate_history,
)

SID = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("blocks", [False, True])
def test_manual_compact_replay_preserves_answer_and_pagination(tmp_path, blocks):
    def row(uid, role, content, timestamp, parent=None, **extra):
        return {"uuid": uid, "type": role, "parentUuid": parent,
                "timestamp": timestamp, "message": {"role": role, "content": content},
                **extra}

    caveat = "<local-command-caveat>Internal command disclaimer</local-command-caveat>"
    if blocks:
        caveat = [{"type": "text", "text": caveat}]
    records = [
        row("human", "user", "inspect the code", "2026-09-15T02:38:00Z"),
        row("answer", "assistant", [{"type": "text", "text": "Inspection complete."}],
            "2026-09-15T02:38:05Z", "human"),
        {"uuid": "boundary", "type": "system", "subtype": "compact_boundary",
         "parentUuid": None, "logicalParentUuid": "answer",
         "timestamp": "2026-09-15T02:46:05Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 600_000}},
        row("summary", "user", "This session is being continued from a previous conversation.",
            "2026-09-15T02:46:05Z", "boundary", isCompactSummary=True),
        row("caveat", "user", caveat, "2026-09-15T02:43:34Z", "summary", isMeta=True),
        row("command", "user", "<command-name>/compact</command-name>",
            "2026-09-15T02:43:34Z", "caveat"),
        row("stdout", "user", "<local-command-stdout>Compacted</local-command-stdout>",
            "2026-09-15T02:46:07Z", "command"),
    ]
    source = tmp_path / f"{SID}.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in records))
    store = HistoryIndexStore(tmp_path / "index")
    for iteration in range(3):  # Initial graph, unchanged cache, and v38 upgrade.
        messages, timestamps, internal = transcript_compact_snapshot(
            SID, path=str(source), index_store=store)
        events = translate_history(messages, 4096, timestamps, internal)
        assert [e.prompt for e in events if isinstance(e, UserMsg)] == ["inspect the code"]
        ends = [e for e in events if isinstance(e, TurnEnd)]
        assert len(ends) == 1
        assert ends[0].ts == timestamps["answer"]
        assert ends[0].result.duration_ms == 5_000
        compact = [e for e in events if isinstance(e, ProcessEvent) and e.kind == "compaction"]
        assert len(compact) == 1
        assert compact[0].ts == timestamps["boundary"]
        page = transcript_compact_history_page(SID, path=str(source), index_store=store, limit=1)
        assert page is not None
        assert not page.has_more
        assert page.oldest_cursor == "human"
        if iteration == 1:
            fingerprint = HistorySourceFingerprint.capture(source)
            for engine in ("claude", "codex"):
                store.put_page(SID, engine, fingerprint, before=None, limit=4, page=_page(engine))
                store.put_image_asset(SID, engine, fingerprint, engine, "image",
                                      "thumbnail", "image/png", 1, 1, b"image")
            with sqlite3.connect(store.path) as db:
                db.execute("UPDATE claude_compact_records SET visible_user=1 WHERE uuid='caveat'")
                db.execute("PRAGMA user_version=38")
            store = HistoryIndexStore(tmp_path / "index")
            assert store.get_page(SID, "claude", fingerprint, before=None, limit=4) is None
            assert store.get_page(SID, "codex", fingerprint, before=None, limit=4) == _page("codex")
            with sqlite3.connect(store.path) as db:
                assert db.execute("SELECT count(*) FROM claude_compact_records").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM history_image_assets").fetchone()[0] == 2


def test_compact_controls_survive_reload_in_the_same_account(tmp_path):
    import asyncio
    from copy import copy

    from cc_remote.wrapper.claude_controls import ClaudeControlStore
    from tests.test_claude_autocompact import _AutoCompactSdk, _machine_with_sdk

    async def run():
        sdk = _AutoCompactSdk()
        sdk.set_auto_compact("custom", 500_000)
        machine, _, ctx = _machine_with_sdk(sdk)
        machine._claude_controls = ClaudeControlStore(tmp_path)
        machine.sessions.clear()
        ctx.key = f"primary@{ctx.session_id}"
        machine.sessions[ctx.key] = ctx
        sibling = copy(ctx)
        sibling.key = f"stack@{ctx.session_id}"
        sibling.sdk = _AutoCompactSdk()
        sibling.sdk.set_auto_compact("custom", 700_000)
        machine.sessions[sibling.key] = sibling
        await machine._persist_claude_session_controls(ctx)
        await machine._persist_claude_session_controls(sibling)
        machine._claude_controls = ClaudeControlStore(tmp_path)
        saved = await machine._load_claude_session_controls(ctx.key)
        assert saved.auto_compact_threshold_tokens == 500_000
        assert saved.applied_auto_compact_mode == "inherit"
        assert (await machine._load_claude_session_controls(
            sibling.key)).auto_compact_threshold_tokens == 700_000
        assert machine._claude_controls.get(ctx.session_id).auto_compact_mode == "inherit"

    asyncio.run(run())


def test_manual_compact_resyncs_profile_scoped_watch(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_claude_autocompact import _machine_with_sdk
    from tests.test_claude_compaction_flow import CompactClient, boundary, result, status

    async def run():
        path = tmp_path / "native.jsonl"
        path.write_text("before")
        handle = SdkHandle(SimpleNamespace(turn_reader_queue_cap=4))
        handle.client = CompactClient([status(), boundary(), result()])
        original = handle.client.query

        async def compact(prompt):
            path.write_text("before plus native compact records")
            await original(prompt)

        handle.client.query = compact
        handle._start_message_pump()
        machine, _, ctx = _machine_with_sdk(handle)
        machine.sessions.clear()
        ctx.key = f"primary@{ctx.session_id}"
        machine.sessions[ctx.key] = ctx
        watch = {"path": str(path), "size": len("before")}
        machine._watch[ctx.key] = watch
        try:
            await machine._compact_managed_claude_context(ctx, reason="test")
            assert watch["size"] == path.stat().st_size
            assert not ctx.claude_write_active
        finally:
            await handle._stop_message_pump()

    asyncio.run(run())
