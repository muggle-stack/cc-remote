"""Native change limits stay explicit through live and historical archives."""
import json

import pytest

from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_history_file_changes, codex_translate_history,
)
from cc_remote.wrapper.turn_changes import (
    TurnChangeArchive, TurnChangeTracker, project_turn_changes, turn_change_event_data,
)


def _patch(path, after="B"):
    return f"--- {path}\n+++ {path}\n@@ -1 +1 @@\n-A\n+{after}\n"


def _changes(count, shape, *, large=False):
    entries = []
    for index in range(count):
        path = f"src/file-{index}.py"
        raw = _patch(path)
        if large:
            # Clipping inside the final added line still leaves a syntactically
            # valid hunk. Its rendered net diff is smaller than the wire cap.
            raw = (f"--- {path}\n+++ {path}\n@@ -1,2 +1,2 @@\n "
                   + "c" * (1024 * 1024) + "\n-A\n+"
                   + "n" * (2 * 1024 * 1024) + "\n")
        entries.append({"path": path, "kind": "update", "diff": raw})
    if shape == "list":
        return entries
    return {entry["path"]: {"type": "update", "unified_diff": entry["diff"]}
            for entry in entries}


def _events(tmp_path, changes, source):
    item = {"type": "fileChange", "id": "edit", "status": "completed",
            "changes": changes}
    if source == "live":
        translator = CodexStreamTranslator(64 * 1024)
        return [turn_change_event_data(event) for event in translator.feed({
            "method": "item/completed", "params": {
                "threadId": "session", "turnId": "turn", "item": item,
            },
        })]
    payload = ({"type": "patch_apply_end", "call_id": "edit", "success": True,
                "changes": changes} if source.endswith("legacy") else
               {"type": "item_completed", "item": {**item, "type": "FileChange"}})
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps({"type": "event_msg", "payload": payload}) + "\n")
    if source.startswith("offset"):
        rows, _incomplete = codex_history_file_changes(
            str(path), {"turn": (0,)}, end_offset=path.stat().st_size)
        return rows["turn"]
    events, _model = codex_translate_history(str(path), 64 * 1024)
    return [turn_change_event_data(event) for event in events]


SOURCES = ["live", "rollout_legacy", "rollout_item", "offset_legacy", "offset_item"]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("shape", ["list", "map"])
def test_clipped_native_diff_never_becomes_an_available_archive(tmp_path, source, shape):
    events = _events(tmp_path, _changes(1, shape, large=True), source)
    result, = [event for event in events if event["type"] == "tool_result"]
    assert result["diff_truncated"] is True
    for payload in (project_turn_changes(events), _tracked(events)):
        file, = payload["files"]
        assert file["state"] == "unavailable"
        assert "diff" not in file
        archive = TurnChangeArchive(tmp_path)
        archive.put("codex:session", "turn", payload, final=True)
        assert archive.get("codex:session", "turn", payload["revision"]) == payload


def _tracked(events):
    tracker = TurnChangeTracker()
    for event in events:
        tracker.observe(event, "turn")
    observed = tracker.observe({"type": "turn_end"}, "turn")
    assert observed is not None
    return observed[1]


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("shape", ["list", "map"])
@pytest.mark.parametrize("count", [64, 65, 321])
def test_card_limit_does_not_truncate_the_native_file_archive(tmp_path, source, shape, count):
    events = _events(tmp_path, _changes(count, shape), source)
    result, = [event for event in events if event["type"] == "tool_result"]
    assert result["diff_truncated"] is (count > 64)
    for payload in (project_turn_changes(events), _tracked(events)):
        assert len(payload["files"]) == count
        assert not payload.get("truncated")
        assert all(file["state"] == "available" for file in payload["files"])


@pytest.mark.parametrize("tool", ["apply_patch", "Edit"])
@pytest.mark.parametrize("specific", [None, False, True])
def test_legacy_combined_truncation_and_explicit_output_only_truncation(tool, specific):
    events = [
        {"type": "tool_use", "tool_use_id": "edit", "tool": tool,
         "input": {"file_path": "src/file.py"}},
        {"type": "tool_result", "tool_use_id": "edit", "is_error": False,
         "diff": _patch("src/file.py"), "diff_source": "native", "truncated": True,
         **({"diff_truncated": specific} if specific is not None else {})},
    ]
    for payload in (project_turn_changes(events), _tracked(events)):
        file, = payload["files"]
        assert file["state"] == ("available" if specific is False else "unavailable")
        if specific is False:
            assert "-A\n+B" in file["diff"]
        else:
            assert "diff" not in file


def test_old_unverified_codex_tool_archive_cannot_override_new_truncation(tmp_path):
    archive = TurnChangeArchive(tmp_path)
    events = [
        {"type": "tool_use", "tool_use_id": "edit", "tool": "apply_patch",
         "input": {"file_path": "src/file.py"}},
        {"type": "tool_result", "tool_use_id": "edit", "is_error": False,
         "diff": _patch("src/file.py")},
    ]
    legacy = project_turn_changes(events)
    legacy.pop("version", None)
    legacy["revision"] = "legacy-unverified"
    archive.put("codex:session", "turn", legacy, final=True)
    assert archive.get("codex:session", "turn", legacy["revision"]) is None
    assert archive.latest_final("codex:session", "turn") is None
    events[-1]["diff_truncated"] = True
    summary = archive.history_summary("codex:session", {"id": "turn", "done": True}, events, None)
    assert summary["files"][0]["state"] == "unavailable"
    assert summary["revision"] != legacy["revision"]
    # Existing immutable rows remain stored; unrelated Claude captures and
    # native turn-only snapshots did not pass through the buggy per-tool path.
    archive.put("claude:session", "turn", legacy, final=True)
    assert archive.get("claude:session", "turn", legacy["revision"]) == legacy
    native_only = {**legacy, "revision": "native-only"}
    native_only.pop("tool_ids")
    archive.put("codex:session", "native", native_only, final=True)
    assert archive.get("codex:session", "native", native_only["revision"]) == native_only
