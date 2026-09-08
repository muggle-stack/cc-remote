"""History/file-list regressions: all I/O is private fixtures, no engine calls."""
import asyncio
import json
import sqlite3

import pytest

from cc_remote.protocol import GetDiff, GetTurnFileChanges, TurnEnd, TurnResult, UserMsg
from cc_remote.wrapper import machine as mm
from cc_remote.wrapper.codex_history import CodexHistoryPage
from cc_remote.wrapper.codex_stream import (
    codex_history_file_changes, codex_history_native_witness,
    codex_history_process_append, codex_history_process_witnesses,
)
from cc_remote.wrapper.history_store import (
    HistoryIndexStore, HistorySourceFingerprint, materialize_history_turns,
)
from cc_remote.wrapper.turn_changes import TurnChangeArchive, project_turn_changes
from tests.test_multisession import _mk_ctx, _mk_machine
from tests.test_codex_turn_change_bounds import _changes


def _record(kind, **payload):
    return {"timestamp": "2026-09-08T01:00:00Z", "type": "event_msg",
            "payload": {"type": kind, **payload}}


def _patch(call, before, after, path="src/code.py", success=True):
    return _record("patch_apply_end", call_id=call, success=success, changes={
        path: {"type": "update", "unified_diff":
               f"--- {path}\n+++ {path}\n@@ -1,1 +1,1 @@\n-{before}\n+{after}\n"},
    })


def _rows():
    return [
        {"type": "session_meta", "payload": {"id": "history-files", "cwd": "/repo"}},
        _record("task_started", turn_id="native-one"),
        _record("user_message", message="first"),
        _patch("first-edit", "A", "B"),
        _record("task_complete", turn_id="native-one"),
        _record("task_started", turn_id="native-two"),
        _record("user_message", message="second"),
        _patch("second-edit", "B", "C"),
        _record("task_complete", turn_id="native-two"),
    ]


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _native_items(rows):
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "patch_apply_end":
            row["payload"] = {"type": "item_completed", "item": {
                "type": "FileChange", "id": payload["call_id"],
                "changes": payload["changes"], "status": "completed",
                "stdout": "", "stderr": "",
            }}
    return rows


def _official_page():
    events = []
    for suffix in ("one", "two"):
        events.extend((
            UserMsg(msg_id=f"official-{suffix}", prompt=suffix).model_dump(mode="json"),
            TurnEnd(turn_id=f"native-{suffix}", result=TurnResult(
                subtype="success", duration_ms=1000, is_error=False)).model_dump(mode="json"),
        ))
    return CodexHistoryPage(
        events=tuple(events), turns=materialize_history_turns(events),
        has_more=False, oldest_id="official-one", newest_id="official-two",
        native_turn_ids=("native-two", "native-one"),
        native_segment_by_visible_id={
            "official-one": ("native-one", 0), "official-two": ("native-two", 0),
        },
    )


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.parametrize("native_format", ["legacy", "item_completed"])
@pytest.mark.parametrize("count", [1, 130])
def test_official_idle_history_recovers_file_lists_without_expanding_tools(tmp_path, monkeypatch, older, native_format, count):
    path = tmp_path / "rollout.jsonl"
    rows = _rows()
    rows[3]["payload"]["changes"].update(_changes(count - 1, "map"))
    if native_format == "item_completed":
        _native_items(rows)
    _write(path, rows)
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))
    monkeypatch.setattr(mm, "codex_translate_history", lambda *_a, **_k: pytest.fail("full history hydration"))

    class Official:
        async def summary_page(self, *_args, **_kwargs):
            return _official_page()

    async def run():
        machine, transport = _mk_machine()
        machine._codex_history = Official()
        ctx = _mk_ctx("history-files", "history-files")
        ctx.engine, ctx.cwd = "codex", "/repo"
        machine.sessions[ctx.key] = ctx
        history = await machine._build_requested_history(
            ctx.key, before="older-page" if older else None,
            limit=4, cwd=ctx.cwd, detail="summary",
        )
        first, second = history.turns
        assert all(not turn.blocks for turn in history.turns)
        assert first.fileChanges.files[0].path == "/repo/src/code.py"
        assert first.fileChanges.files[0].state == "available"
        assert first.fileChanges.total_files == count
        assert len(first.fileChanges.files) == min(count, 64)
        assert second.fileChanges.files[0].state == "available"
        assert first.fileChanges.revision != second.fileChanges.revision
        await machine._handle_get_diff(GetDiff(
            sid=ctx.key, engine="codex", turn_id=first.id,
            revision=first.fileChanges.revision, file="/repo/src/code.py",
        ))
        assert "-A\n+B" in transport.sent[-1].diff
        assert "+C" not in transport.sent[-1].diff
        # Reopening a cold session has no live events to rescue its file list.
        machine.sessions.clear()
        machine._watch[ctx.key]["cwd"] = "/repo"
        again = await machine._build_requested_history(
            ctx.key, before="older-page" if older else None,
            limit=4, cwd=ctx.cwd, detail="summary",
        )
        assert again.turns[0].fileChanges == first.fileChanges
        if count > 64:
            page = await machine._handle_get_turn_file_changes(GetTurnFileChanges(
                sid=ctx.key, engine="codex", turn_id=first.id,
                revision=first.fileChanges.revision, offset=128))
            assert len(page.files) == 2 and page.next_offset is None
    asyncio.run(run())


@pytest.mark.parametrize("native_format", ["legacy", "item_completed"])
@pytest.mark.parametrize("count", [1, 130])
def test_full_history_cache_keeps_changes_in_later_summary_reads(tmp_path, monkeypatch, native_format, count):
    path = tmp_path / "rollout.jsonl"
    rows = _rows()
    rows[3]["payload"]["changes"].update(_changes(count - 1, "map"))
    _write(path, _native_items(rows) if native_format == "item_completed" else rows)
    monkeypatch.setattr(mm, "codex_rollout_path", lambda _sid: str(path))

    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("history-files", "history-files")
        ctx.engine, ctx.cwd = "codex", "/repo"
        machine.sessions[ctx.key] = ctx
        full = await machine._build_history(ctx.key, limit=4, detail="full")
        assert any(e["type"] == "tool_use" for e in full.events)
        monkeypatch.setattr(mm, "codex_translate_history", lambda *_a, **_k: pytest.fail("cache was not used"))
        summary = await machine._build_history(ctx.key, limit=4, detail="summary")
        assert len(summary.turns) == 2
        assert all(turn.fileChanges.files[0].state == "available" for turn in summary.turns)
        assert summary.turns[0].fileChanges.total_files == count
        assert "_file_diffs" not in full.model_dump_json()
        assert not any(e["type"] == "tool_use" for e in summary.events)
    asyncio.run(run())


def test_mutation_witness_ignores_file_types_inside_other_payloads(tmp_path):
    path = tmp_path / "not-an-edit.jsonl"
    rows = _rows()[:3] + [
        {"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "read-only",
            "output": {"type": "FileChange", "id": "fake", "changes": {}},
        }},
        _record("item_completed", item={
            "type": "CommandExecution", "output": {"type": "FileChange"},
        }),
        _record("agent_reasoning", text=json.dumps(_patch("fake", "A", "B"))),
        _record("task_complete", turn_id="native-one"),
    ]
    _write(path, rows)
    witness = codex_history_native_witness(str(path), max_turns=1)
    assert all(not proof.file_change_offsets
               for proof in witness.process_by_native_segment.values())


def test_mutation_offsets_remain_bound_to_steer_segment_and_append(tmp_path):
    path = tmp_path / "steered.jsonl"
    rows = _rows()[:4]
    _write(path, rows)
    first = codex_history_process_witnesses(
        str(path), before="head", max_turns=1, native_turn_ids=("native-one",),
    )
    old_size = path.stat().st_size
    _write(path, [*rows, _patch("edit-again", "B", "C")])
    appended = codex_history_process_append(
        str(path), previous=first, start_offset=old_size, end_offset=path.stat().st_size,
    )
    assert appended is not None
    assert len(appended.process_by_native_segment[("native-one", 0)].file_change_offsets) == 2
    _write(path, [*rows, _record("user_message", message="steered"),
                  _patch("steer-edit", "B", "C"), _record("task_complete", turn_id="native-one")])
    witness = codex_history_native_witness(str(path), max_turns=1)
    sources, incomplete = codex_history_file_changes(
        str(path), {str(segment): proof.file_change_offsets
                    for (_native, segment), proof in witness.process_by_native_segment.items()},
        end_offset=path.stat().st_size,
    )
    assert not incomplete
    assert "-A\n+B" in project_turn_changes(sources["0"])["files"][0]["diff"]
    assert "-B\n+C" in project_turn_changes(sources["1"])["files"][0]["diff"]


def test_history_copies_exact_live_alias_but_not_shared_native_task(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    events = [{"type": "tool_use", "tool_use_id": "edit", "tool": "fileChange",
               "input": {"path": "/repo/README.md"}}]
    payload = project_turn_changes(events)
    archive.put("codex:session", "client-message", payload, final=True)
    summary = archive.history_summary("codex:session", {
        "id": "official-user", "clientMsgId": "client-message", "done": True,
    }, [], "/repo")
    assert summary["files"][0]["path"] == "/repo/README.md"
    assert archive.get("codex:session", "official-user", summary["revision"]) == payload
    assert not archive.history_summary("codex:other", {
        "id": "official-user", "clientMsgId": "client-message", "done": True,
    }, [], "/repo")["files"]
    assert not archive.history_summary("codex:session", {
        "id": "another-steer", "forkPointId": "client-message", "done": True,
    }, [], "/repo")["files"]


def test_complete_client_capture_upgrades_a_weaker_official_alias(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    events = [{"type": "tool_use", "tool_use_id": "edit", "tool": "fileChange",
               "input": {"path": "/repo/code.py"}}]
    archive.put("codex:session", "official-user", project_turn_changes(events), final=True)
    captured = project_turn_changes([*events, {
        "type": "tool_result", "tool_use_id": "edit", "is_error": False,
        "diff": "--- /repo/code.py\n+++ /repo/code.py\n@@ -1,1 +1,1 @@\n-A\n+B\n",
    }])
    archive.put("codex:session", "client-message", captured, final=True)
    summary = archive.history_summary("codex:session", {
        "id": "official-user", "clientMsgId": "client-message", "done": True,
    }, [], "/repo")
    assert summary["files"][0]["state"] == "available"
    assert archive.get("codex:session", "official-user", summary["revision"]) == captured


def test_v31_migration_rebuilds_codex_details_but_preserves_images_and_archived_diffs(tmp_path):
    source_path = tmp_path / "source.jsonl"
    _write(source_path, _rows())
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path)
    from cc_remote.wrapper.history_store import MaterializedHistoryPage
    page = _official_page()
    store.put_page("session", "codex", source, before=None, limit=4, page=MaterializedHistoryPage(
        events=page.events, turns=page.turns, has_more=False,
        oldest_id=page.oldest_id, newest_id=page.newest_id,
    ))
    store.put_image_asset("session", "codex", source, "official-one", "image", "thumbnail", "image/png", 1, 1, b"image")
    archive = TurnChangeArchive(tmp_path)
    payload = project_turn_changes([])
    archive.put("codex:session", "official-one", payload, final=True)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM history_turn_details").fetchone()[0] > 0
        db.execute("PRAGMA user_version=31")
    HistoryIndexStore(tmp_path)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM history_pages").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM history_turn_details").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM history_image_assets").fetchone()[0] == 1
    assert archive.get("codex:session", "official-one", payload["revision"]) == payload


def test_failed_edits_and_partial_records_never_become_available_history(tmp_path):
    path = tmp_path / "partial.jsonl"
    _write(path, _rows())
    witness = codex_history_native_witness(str(path), max_turns=2)
    offsets = witness.process_by_native_segment[("native-one", 0)].file_change_offsets
    sources, incomplete = codex_history_file_changes(
        str(path), {"one": offsets}, end_offset=offsets[0] + 10,
    )
    assert incomplete == {"one"}
    assert not sources["one"]
    _write(path, [_patch("failed", "A", "B", success=False)])
    sources, incomplete = codex_history_file_changes(str(path), {"one": (0,)}, end_offset=path.stat().st_size)
    assert not incomplete
    assert not project_turn_changes(sources["one"])["files"]


def test_missing_native_diff_keeps_markdown_list_and_storage_failure_keeps_paths(tmp_path, monkeypatch):
    events = [{"type": "tool_use", "tool_use_id": "edit", "tool": "Edit",
               "input": {"file_path": "/repo/README.md"}},
              {"type": "tool_result", "tool_use_id": "edit", "is_error": False}]
    machine, _ = _mk_machine()
    turn = {"id": "claude-old", "done": True}
    summary = machine._history_file_changes("session", "claude", [turn], {turn["id"]: events}, cwd="/repo")
    assert summary[turn["id"]]["files"][0]["path"] == "/repo/README.md"
    assert summary[turn["id"]]["files"][0]["state"] == "unavailable"
    monkeypatch.setattr(machine._turn_change_archive, "put", lambda *_a, **_k: (_ for _ in ()).throw(OSError("full")))
    summary = machine._history_file_changes("session", "claude", [turn], {turn["id"]: events}, cwd="/repo")
    assert summary[turn["id"]]["files"][0]["path"] == "/repo/README.md"
    assert summary[turn["id"]]["files"][0]["state"] == "unavailable"
