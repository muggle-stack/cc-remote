import json
import asyncio
import difflib
import random

import pytest

from cc_remote.wrapper.turn_changes import (
    TurnChangeArchive, TurnChangeTracker, change_summary, native_claude_diff,
    parse_patches, project_turn_changes,
)


def patch(before, after, path="src/code.py", line=1):
    return f"--- {path}\n+++ {path}\n@@ -{line},1 +{line},1 @@\n-{before}\n+{after}\n"


def edit(key, diff, path="src/code.py", **changes):
    return [
        {"type": "tool_use", "tool_use_id": key, "tool": "fileChange", "input": {"file_paths": [path]}},
        {"type": "tool_result", "tool_use_id": key, "diff": diff, "is_error": False, **changes},
    ]


def test_same_file_is_one_net_diff_and_following_turn_cannot_mutate_it(tmp_path):
    events = edit("one", patch("A", "B")) + edit("two", patch("B", "C"))
    first = project_turn_changes(events)
    file, = first["files"]
    assert file["state"] == "available"
    assert "-A\n+C" in file["diff"]
    assert "B" not in file["diff"]
    assert file["additions"] == file["deletions"] == 1
    store = TurnChangeArchive(tmp_path)
    store.put("primary@session", "turn-1", first, final=True)
    second = project_turn_changes(edit("three", patch("C", "D")))
    store.put("primary@session", "turn-2", second, final=True)
    reopened = TurnChangeArchive(tmp_path)
    assert reopened.get("primary@session", "turn-1", first["revision"]) == first
    assert reopened.get("other@session", "turn-1", first["revision"]) is None
    assert reopened.get("primary@session", "turn-2", first["revision"]) is None
    assert "diff" not in change_summary(first)["files"][0]


def test_nonadjacent_hunks_use_real_positions_and_keep_full_paths_distinct():
    events = edit("one", patch("A", "B", line=200)) + edit("two", patch("X", "Y", line=600))
    events += edit("other", patch("same name", "other", path="tests/code.py"), path="tests/code.py")
    projected = project_turn_changes(events)
    assert len(projected["files"]) == 2
    assert "@@ -200,1 +200,1 @@" in projected["files"][0]["diff"]
    assert "@@ -600,1 +600,1 @@" in projected["files"][0]["diff"]


def test_reverted_change_has_no_net_diff():
    file, = project_turn_changes(edit("a", patch("A", "B")) + edit("b", patch("B", "A")))["files"]
    assert file["state"] == "available"
    assert file["diff"] == ""
    assert file["additions"] == file["deletions"] == 0


def test_native_turn_diff_is_latest_cumulative_snapshot_not_concatenated():
    events = edit("a", patch("A", "B")) + [
        {"type": "turn_diff", "diff": patch("A", "B")},
        {"type": "turn_diff", "diff": patch("A", "C")},
    ]
    file, = project_turn_changes(events)["files"]
    assert "-A\n+C" in file["diff"]
    assert file["diff"].count("diff --git") == 1


def test_live_tracker_uses_updated_native_snapshot_source_order():
    tracker = TurnChangeTracker()
    first = edit("a", None)
    second = edit("b", None)
    for event in first + [{"type": "turn_diff", "diff": patch("A", "B")}] + second:
        tracker.observe(event, "turn")
    _, result = tracker.observe({"type": "turn_diff", "diff": patch("A", "C")}, "turn")
    assert result["files"][0]["state"] == "available"
    assert "-A\n+C" in result["files"][0]["diff"]


def test_failed_edit_clears_pending_files_and_persists_empty_terminal(tmp_path):
    tracker = TurnChangeTracker()
    use, result = edit("failure", None, is_error=True)
    _, pending = tracker.observe(use, "turn")
    assert pending["files"][0]["state"] == "pending"
    _, cleared = tracker.observe(result, "turn")
    assert cleared["files"] == []
    _, terminal = tracker.observe({"type": "turn_end"}, "turn")
    store = TurnChangeArchive(tmp_path)
    store.put("session", "turn", terminal, final=True)
    assert store.latest_final("session", "turn") == terminal


def test_missing_base_and_conflicting_patch_never_fall_back_to_live_file():
    events = edit("a", patch("A", "B")) + edit("b", patch("X", "C")) + edit("c", patch("C", "D"))
    file, = project_turn_changes(events)["files"]
    assert file["state"] == "unavailable"
    assert "diff" not in file
    legacy = edit("old", patch("A", "B"))
    legacy[0]["tool"] = "Edit"
    assert project_turn_changes(legacy)["files"][0]["state"] == "unavailable"
    assert project_turn_changes(edit("fail", patch("A", "B"), is_error=True))["files"] == []


def test_native_claude_structured_patch_not_substring_guess():
    data = {"filePath": "src/code.py", "type": "update", "structuredPatch": [
        {"oldStart": 20, "oldLines": 1, "newStart": 20, "newLines": 1, "lines": ["-A", "+B"]},
    ]}
    raw = native_claude_diff(data)
    assert "@@ -20,1 +20,1 @@" in raw
    assert native_claude_diff({"filePath": "src/code.py", "content": "new"}) is None
    data["structuredPatch"][0]["lines"] = ["-A"]
    assert native_claude_diff(data) is None


def test_native_write_uses_proven_before_image_not_a_creation_guess():
    update = native_claude_diff({"type": "update", "filePath": "src/code.py",
                                "originalFile": "A\n", "content": "B\n", "structuredPatch": []})
    assert "--- src/code.py" in update and "-A\n+B" in update
    created = native_claude_diff({"type": "create", "filePath": "src/new.py",
                                 "originalFile": None, "content": "new\n", "structuredPatch": []})
    assert "--- /dev/null" in created
    assert native_claude_diff({"type": "update", "filePath": "src/code.py",
                               "originalFile": None, "content": "B\n"}) is None


def test_missing_middle_result_and_pending_edits_cannot_restore_a_partial_tail():
    first = edit("one", patch("A", "B"))
    missing = edit("missing", None)
    last = edit("last", patch("C", "D"))
    assert project_turn_changes(first + missing + last)["files"][0]["state"] == "unavailable"
    assert project_turn_changes(first + missing[:1])["files"][0]["state"] == "pending"
    # A source-ordered complete native snapshot can repair missing per-tool
    # evidence, but a later mutation must not be hidden behind it.
    native = {"type": "turn_diff", "diff": patch("A", "C")}
    assert "-A\n+C" in project_turn_changes(first + missing + [native])["files"][0]["diff"]
    assert project_turn_changes(first + [native] + missing)["files"][0]["state"] == "unavailable"
    net_zero = project_turn_changes(first + [{"type": "turn_diff", "diff": ""}])
    assert net_zero["files"][0]["diff"] == ""


def test_relative_paths_are_cwd_scoped_and_renames_do_not_duplicate_files():
    original = edit("one", patch("A", "B", "src/code.py"), path="/repo/src/code.py")
    rename = edit("two", "--- src/code.py\n+++ src/renamed.py\n@@ -1 +1 @@\n-B\n+C\n",
                  path="/repo/src/renamed.py")
    file, = project_turn_changes(original + rename, "/repo")["files"]
    assert file["path"] == "/repo/src/renamed.py"
    assert "-A\n+C" in file["diff"]
    # No suffix matching across distinct absolute directories.
    other = edit("three", patch("X", "Y", "/elsewhere/src/renamed.py"), path="/elsewhere/src/renamed.py")
    assert len(project_turn_changes(original + rename + other, "/repo")["files"]) == 2


def test_random_sequential_insert_delete_replace_composes_to_original_result():
    rng = random.Random(708)
    for _ in range(20):
        before = [f"original-{index}" for index in range(40)]
        current = before[:]
        events = []
        for step in range(12):
            after = current[:]
            start = rng.randrange(len(after))
            after[start:start + rng.randrange(3)] = [f"new-{step}-{index}" for index in range(rng.randrange(3))]
            if after == current:
                continue
            raw = "\n".join(difflib.unified_diff(current, after, fromfile="src/code.py", tofile="src/code.py", lineterm="")) + "\n"
            events += edit(str(step), raw)
            current = after
        file, = project_turn_changes(events)["files"]
        assert file["state"] == "available"
        rebuilt = before[:]
        offset = 0
        for patch_info in parse_patches(file["diff"]):
            for old_start, old_count, _, new_count, old_lines, new_lines in patch_info["hunks"]:
                start = old_start - bool(old_count) + offset
                assert rebuilt[start:start + old_count] == old_lines
                rebuilt[start:start + old_count] = new_lines
                offset += new_count - old_count
        assert rebuilt == current


def test_tracker_overflow_is_bounded_and_cannot_recover_from_a_later_partial_patch(monkeypatch):
    monkeypatch.setattr("cc_remote.wrapper.turn_changes.MAX_EVENTS", 4)
    tracker = TurnChangeTracker()
    for index in range(20):
        for event in edit(str(index), patch(str(index), str(index + 1))):
            tracker.observe(event, "turn")
    _, payload = tracker.observe({"type": "turn_end"}, "turn")
    assert payload["truncated"] is True
    assert payload["files"][0]["state"] == "unavailable"
    assert len(tracker.turns["turn"]) <= 4
    assert "diff" not in json.dumps(tracker.incomplete)


def test_archived_diff_request_is_immutable_scoped_and_never_reads_worktree():
    from cc_remote.protocol import DiffReport, Error, GetDiff, ToolUse, ToolResult, TurnEnd, TurnResult, TurnFileChanges
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("profile@session", "session")
        ctx.engine = "claude"
        ctx.active_msg_id = "logical-turn"
        machine.sessions[ctx.key] = ctx
        for event in edit("one", patch("A", "B")):
            if event["type"] == "tool_use":
                msg = ToolUse(message_id="assistant", **event)
            else:
                msg = ToolResult(content="done", **event)
            await machine._emit(ctx, msg)
        terminal = TurnEnd(turn_id="final-assistant", checkpoint_id="native-user",
                           result=TurnResult(subtype="success", is_error=False, duration_ms=1))
        terminal._changes_turn_id = "logical-turn"
        await machine._emit(ctx, terminal)
        changes = [event for event in transport.sent if isinstance(event, TurnFileChanges)][-1]
        assert transport.sent[0].type == "tool_use"  # owner source frame always precedes the sidecar
        path = ctx.cwd + "/src/code.py"
        command = GetDiff(sid=ctx.key, file=path, turn_id="native-user", revision=changes.changes.revision,
                          engine="claude", cmd_id="read", client_id="browser")
        machine.sessions.clear()  # cold read must not spawn or consult an engine
        report = await machine._handle_get_diff(command)
        assert isinstance(report, DiffReport) and "-A\n+B" in report.diff
        for field, value in (("engine", "codex"), ("sid", "other@session"), ("revision", "missing")):
            reply = await machine._handle_get_diff(command.model_copy(update={field: value}))
            assert isinstance(reply, Error) and reply.to == "browser"
        await machine._drop_preview_session("claude", ctx.key, delete_history=False)
        assert isinstance(await machine._handle_get_diff(command), DiffReport)
        await machine._drop_preview_session("claude", ctx.key)
        assert isinstance(await machine._handle_get_diff(command), Error)
    asyncio.run(run())


def test_storage_failure_retires_the_previous_live_diff(monkeypatch):
    from cc_remote.protocol import ToolUse, ToolResult
    from tests.test_multisession import _mk_ctx, _mk_machine

    machine, _ = _mk_machine()
    ctx = _mk_ctx("storage-session", "storage-session")
    ctx.active_msg_id = "logical-turn"
    machine._archive_turn_change_event(ctx, ToolUse(
        message_id="message", tool_use_id="edit", tool="fileChange", input={"path": "file.py"}))
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(machine._turn_change_archive, "put", fail)
    update = machine._archive_turn_change_event(ctx, ToolResult(
        tool_use_id="edit", content="done", is_error=False, diff=patch("A", "B", "file.py")))
    assert update.changes.files[0].state == "unavailable"
    assert update.changes.revision.startswith("unavailable:")


def test_steer_pins_previous_visible_revision_without_ending_the_native_turn():
    from cc_remote.protocol import TurnDiff, TurnSteered
    from tests.test_multisession import _mk_ctx, _mk_machine

    machine, _ = _mk_machine()
    ctx = _mk_ctx("steer-session", "steer-session")
    ctx.engine = "codex"
    ctx.active_msg_id = "logical-turn"
    first = machine._archive_turn_change_event(ctx, TurnDiff(
        item_id="diff", turn_id="native-turn", diff=patch("A", "B")))
    assert machine._archive_turn_change_event(ctx, TurnSteered(
        turn_id="native-turn", msg_id="steer", prompt="continue")) is None
    for index in range(8):
        machine._archive_turn_change_event(ctx, TurnDiff(
            item_id="diff", turn_id="native-turn", diff=patch("A", str(index))))
    retained = machine._turn_change_archive.get("codex:steer-session", "native-turn", first.changes.revision)
    assert "-A\n+B" in retained["files"][0]["diff"]


@pytest.mark.parametrize("keys", [("turn_id",), ("revision",), ("engine",),
                                  ("turn_id", "revision"), ("turn_id", "engine"), ("revision", "engine")])
def test_historical_request_requires_full_identity(keys):
    from pydantic import ValidationError
    from cc_remote.protocol import GetDiff, serialize, deserialize

    identity = {"turn_id": "native-turn", "revision": "immutable-revision", "engine": "codex"}
    with pytest.raises(ValidationError):
        GetDiff(file="src/file.ts", **{key: identity[key] for key in keys})
    command = GetDiff(file="src/file.ts", **identity)
    assert deserialize(serialize(command)) == command


@pytest.mark.parametrize("bad", ["@@ diff preview truncated @@", "Binary files differ", patch("A", "B")[:-3]])
def test_incomplete_patches_rejected(bad):
    with pytest.raises(ValueError):
        parse_patches(bad)


def test_tracker_keeps_only_file_evidence_and_archive_bounds_running_revisions(tmp_path):
    tracker = TurnChangeTracker()
    store = TurnChangeArchive(tmp_path)
    for index in range(8):
        before, after = str(index), str(index+1)
        for event in edit(str(index), patch(before, after)):
            event["content"] = "DO NOT RETAIN process output"
            observed = tracker.observe(event, "turn")
            if observed is None:
                assert event["type"] == "tool_use"  # unchanged visible snapshot
                continue
            turn_id, data = observed
            store.put("sid", turn_id, data)
    assert "DO NOT RETAIN" not in json.dumps(list(tracker.turns["turn"].values()))
    turn, final = tracker.observe({"type": "turn_end"}, "turn")
    store.put("sid", turn, final, final=True)
    store.rekey("sid", "primary@sid")
    assert store.get("primary@sid", turn, final["revision"]) == final
    assert store.get("sid", turn, final["revision"]) is None
    store.drop("primary@sid")
    assert store.get("primary@sid", turn, final["revision"]) is None
