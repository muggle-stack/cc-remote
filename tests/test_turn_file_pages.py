"""File-index pagination reads immutable native evidence, never the worktree."""
import asyncio
import json
import sqlite3

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    DiffReport, Error, GetDiff, GetTurnFileChanges, TurnFileChanges,
    TurnFileChangesPage, ToolUse, ToolResult, TurnEnd, TurnResult,
    deserialize, is_downstream, serialize,
)
from cc_remote.wrapper.codex_stream import CodexStreamTranslator
from cc_remote.wrapper.turn_changes import (
    TurnChangeArchive, change_summary, project_turn_changes, turn_change_event_data,
)
from tests.test_codex_turn_change_bounds import _changes, _patch
from tests.test_multisession import _mk_ctx, _mk_machine


def native_events(count, after=None):
    changes = _changes(count, "list")
    if after:
        for entry in changes:
            entry["diff"] = _patch(entry["path"], after)
    translator = CodexStreamTranslator(64 * 1024)
    item = {"type": "fileChange", "id": "edit", "status": "completed", "changes": changes}
    return [translator._tool_update(item), translator._tool_result(item)]


def capture(count, after=None):
    return project_turn_changes([turn_change_event_data(event) for event in native_events(count, after)])


def test_all_321_files_are_paged_with_one_file_diff_read(tmp_path, monkeypatch):
    archive = TurnChangeArchive(tmp_path)
    payload = capture(321)
    summary = change_summary(payload)
    assert (len(summary["files"]), summary["total_files"], summary["next_offset"]) == (64, 321, 64)
    assert summary["total_additions"] == summary["total_deletions"] == 321
    assert not summary.get("truncated")
    scope = ("codex:profile@session", "turn", payload["revision"])
    archive.put(*scope[:2], payload, final=True)
    statements = []
    connect = archive._connect
    def traced():
        db = connect()
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(archive, "_connect", traced)
    files, offset = [], 0
    while offset is not None:
        page = archive.page(*scope, offset)
        assert "diff" not in json.dumps(page)
        files.extend(page["files"])
        offset = page["next_offset"]
    assert len(files) == len({row["path"] for row in files}) == 321
    assert not any("SELECT payload FROM turn_change_files" in sql for sql in statements)
    statements.clear()
    row = archive.file(*scope, files[-1]["path"])
    assert "-A\n+B" in row["diff"]
    assert len([sql for sql in statements if "SELECT payload FROM turn_change_files" in sql]) == 1
    assert not any("ORDER BY position" in sql for sql in statements)


def test_file_pages_and_diffs_are_scoped_and_immutable_after_later_edits(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    original = capture(130)
    archive.put("codex:one@session", "first", original, final=True)
    archive.put("codex:one@session", "second", capture(130, "later"), final=True)
    reopened = TurnChangeArchive(tmp_path)
    identity = ("codex:one@session", "first", original["revision"])
    assert reopened.page(*identity, 128)["next_offset"] is None
    assert "+later" not in reopened.file(*identity, "src/file-129.py")["diff"]
    for scope in (("claude:one@session", *identity[1:]),
                  ("codex:two@session", *identity[1:]),
                  (identity[0], "second", identity[2]),
                  (identity[0], identity[1], "missing")):
        assert reopened.page(*scope, 64) is None
        assert reopened.file(*scope, "src/file-129.py") is None
    assert reopened.file(*identity, "../../secrets") is None
    for offset, limit in ((-1, 64), (True, 64), (131, 64), (0, 0), (0, 65)):
        with pytest.raises(ValueError):
            reopened.page(*identity, offset, limit)
    assert reopened.page(*identity, 130)["files"] == []


def test_pre_normalization_archives_still_support_pages_and_rekey_cleanup(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    payload = capture(130)
    payload["version"] = 2
    with sqlite3.connect(archive.path) as db:
        db.execute("INSERT INTO turn_changes VALUES (?,?,?,?,1)",
                   ("codex:old", "turn", payload["revision"], json.dumps(payload)))
    identity = ("codex:old", "turn", payload["revision"])
    assert len(archive.page(*identity, 64)["files"]) == 64
    assert archive.file(*identity, "src/file-129.py")["state"] == "available"
    current = capture(321)
    archive.put("codex:old", "new-turn", current, final=True)
    archive.rekey("codex:old", "codex:renamed")
    assert archive.page(*identity) is None
    assert archive.page("codex:renamed", "new-turn", current["revision"], 320)["total_files"] == 321
    archive.drop("codex:renamed")
    with sqlite3.connect(archive.path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_change_files").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM turn_changes").fetchone()[0] == 0


def test_running_revision_pruning_removes_only_its_own_file_index(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    original = capture(65)
    archive.put("codex:session", "turn", original, final=True)
    for index in range(7):
        archive.put("codex:session", "turn", capture(65, str(index)))
    assert archive.page("codex:session", "turn", original["revision"], 64) is not None
    with sqlite3.connect(archive.path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_changes").fetchone()[0] == 5
        assert db.execute("SELECT COUNT(*) FROM turn_change_files").fetchone()[0] == 5 * 65


def test_native_evidence_is_not_serialized_or_retained_by_the_ring_and_cold_reads_work(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("profile@session", "session")
        ctx.engine, ctx.active_msg_id = "codex", "turn"
        machine.sessions[ctx.key] = ctx
        for event in native_events(130):
            assert event._turn_change_source
            assert "_file_diffs" not in serialize(event)
            assert "file-129.py" not in serialize(event)
            await machine._emit(ctx, event)
            assert event._turn_change_source is None
        await machine._emit(ctx, TurnEnd(turn_id="turn", result=TurnResult(
            subtype="success", is_error=False, duration_ms=1)))
        summary = [event.changes for event in transport.sent if isinstance(event, TurnFileChanges)][-1]
        assert summary.total_files == 130
        assert isinstance(transport.sent[0], ToolUse)
        assert all(not event._turn_change_source for event in transport.sent if isinstance(event, (ToolUse, ToolResult)))
        machine.sessions.clear()
        def no_hydrate(*_args):
            pytest.fail("paged reads must not hydrate the full archive")
        monkeypatch.setattr(machine._turn_change_archive, "get", no_hydrate)
        command = GetTurnFileChanges(sid=ctx.key, engine="codex", turn_id="turn",
                                    revision=summary.revision, offset=128, cmd_id="read", client_id="browser")
        reply = await machine._handle_get_turn_file_changes(command)
        assert isinstance(reply, TurnFileChangesPage)
        assert len(reply.files) == 2 and reply.to == "browser" and reply.request_id == "read"
        assert reply.sid == ctx.key and not is_downstream(reply)
        assert isinstance(deserialize(serialize(command)), GetTurnFileChanges)
        diff = await machine._handle_get_diff(GetDiff(sid=ctx.key, engine="codex", turn_id="turn",
            revision=summary.revision, file=reply.files[-1].path, client_id="browser"))
        assert isinstance(diff, DiffReport) and "-A\n+B" in diff.diff
        for key, value in (("sid", "other@session"), ("engine", "claude"), ("revision", "wrong"), ("offset", 400)):
            error = await machine._handle_get_turn_file_changes(command.model_copy(update={key: value}))
            assert isinstance(error, Error) and error.to == "browser"
        assert "get_turn_file_changes" in machine.SAFE_RETRY_COMMANDS
        assert "get_turn_file_changes" in machine.BTW_SID_COMMANDS
    asyncio.run(run())


@pytest.mark.parametrize("extra", [{"limit": 65}, {"offset": -1}, {"offset": True}, {"sid": None}])
def test_page_protocol_is_bounded_and_requires_an_explicit_session(extra):
    with pytest.raises(ValidationError):
        GetTurnFileChanges(**{"sid": "session", "engine": "codex", "turn_id": "turn",
                             "revision": "revision", **extra})


def test_native_per_file_evidence_survives_aggregate_card_clipping():
    changes = _changes(2, "list")
    for entry in changes:
        entry["diff"] = _patch(entry["path"], "N" * (1200 * 1024))
    translator = CodexStreamTranslator(64 * 1024)
    item = {"type": "fileChange", "id": "edit", "status": "completed", "changes": changes}
    use, result = translator._tool_update(item), translator._tool_result(item)
    assert result.diff_truncated is True
    payload = project_turn_changes([turn_change_event_data(use), turn_change_event_data(result)])
    assert len(payload["files"]) == 2
    assert all(row["state"] == "available" for row in payload["files"])
    assert all("N" * (1200 * 1024) in row["diff"] for row in payload["files"])
