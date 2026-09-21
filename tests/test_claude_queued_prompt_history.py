"""Consumed human inputs survive native catalog, compaction and cached pages."""
import json
import sqlite3

import pytest
from claude_agent_sdk._internal.sessions import (
    _entries_to_session_messages,
    _parse_transcript_entries,
)

from cc_remote.protocol import Delta, TurnEnd, UserMsg
from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    materialize_history_turns,
)
from cc_remote.wrapper.stream import (
    recover_claude_native_metadata,
    transcript_compact_history_page,
    transcript_compact_snapshot,
    transcript_timestamps,
    translate_history,
)
from tests.test_history_store import _page

SID = "11111111-1111-4111-8111-111111111111"


def record(uid, parent, role, content, second, **extra):
    return {"uuid": uid, "parentUuid": parent, "type": role, "sessionId": SID,
            "timestamp": f"2026-09-19T13:26:{second:02d}Z", "isSidechain": False,
            "message": {"role": role, "content": content}, **extra}


def queued(uid, parent, public_id, prompt, second, **extra):
    row = record(uid, parent, "attachment", None, second)
    row.pop("message")
    row["attachment"] = {"type": "queued_command", "source_uuid": public_id,
                         "commandMode": "prompt", "prompt": prompt, **extra}
    return row


def response(uid, parent, text, second, stop="tool_use"):
    row = record(uid, parent, "assistant", [{"type": "text", "text": text}], second)
    row["message"]["stop_reason"] = stop
    return row


def transcript(compact, rich=False):
    prefix = [record("old", None, "user", "earlier", 0),
              response("old-answer", "old", "earlier answer", 1, "end_turn")]
    parent = "old-answer"
    if compact:
        prefix.append({"type": "system", "subtype": "compact_boundary", "uuid": "compact",
                       "parentUuid": None, "logicalParentUuid": parent,
                       "timestamp": "2026-09-19T13:26:02Z"})
        parent = "compact"
    # Same text with different native UUIDs is two distinct human messages.
    prompt = "additional direction"
    if rich:
        prompt = [{"type": "text", "text": "additional"},
                  {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                               "data": "aGVsbG8="}},
                  {"type": "text", "text": "direction"}]
    return [*prefix, record("human", parent, "user", "original question", 3),
            response("working", "human", "initial work", 4),
            queued("attachment-one", "working", "steer-one", prompt, 5),
            # Consecutive consumed inputs also preserve their order.
            queued("attachment-two", "attachment-one", "steer-two", prompt, 6,
                   origin={"kind": "human"}),
            response("continued", "attachment-two", "continued work", 7),
            queued("task", "continued", "task-source", "background result", 8,
                   commandMode="task-notification"),
            response("answer", "task", "final answer", 9, "end_turn")]


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def read(path, mode, store=None):
    if mode == "catalog":
        messages = _entries_to_session_messages(
            _parse_transcript_entries(path.read_text()), None, 0)
        timestamps = transcript_timestamps(SID, path=str(path))
        internal = {}
    else:
        messages, timestamps, internal = transcript_compact_snapshot(
            SID, path=str(path), index_store=store)
    messages = recover_claude_native_metadata(
        SID, messages, path=str(path), timestamps=timestamps,
        internal_events=internal, index_store=store, include_queued_prompts=mode == "catalog")
    # Repeating the recovery cannot duplicate already projected inputs.
    messages = recover_claude_native_metadata(
        SID, messages, path=str(path), timestamps=timestamps,
        internal_events=internal, index_store=store, include_queued_prompts=mode == "catalog")
    return translate_history(messages, 4096, timestamps, internal,
                             client_message_ids={"steer-two": "browser-two"})


@pytest.mark.parametrize("mode", ["catalog", "compact", "indexed"])
@pytest.mark.parametrize("rich", [False, True])
def test_human_attachments_keep_order_identity_and_reply_owner(tmp_path, mode, rich):
    path = tmp_path / f"{SID}.jsonl"
    write(path, transcript(mode != "catalog", rich))
    original = path.read_bytes()
    store = HistoryIndexStore(tmp_path / "index") if mode == "indexed" else None
    for _ in range(2):
        events = read(path, mode, store)
        users = [e for e in events if isinstance(e, UserMsg)]
        assert [e.msg_id for e in users] == ["old", "human", "steer-one", "steer-two"]
        assert users[-1].client_msg_id == "browser-two"
        assert users[-1].ts - users[-2].ts == 1
        assert users[-1].prompt == ("additional\ndirection" if rich else "additional direction")
        assert bool(users[-1].images) == rich
        ends = [e for e in events if isinstance(e, TurnEnd)]
        assert [e.result.subtype for e in ends] == ["success", "steered", "steered", "success"]
        turns = materialize_history_turns([e.model_dump() for e in events])
        assert [t["id"] for t in turns] == [e.msg_id for e in users]
        assert all(t["done"] for t in turns)
        assert not any(b.get("channel") == "final" for t in turns[1:-1] for b in t["blocks"])
        final = [b for b in turns[-1]["blocks"] if b.get("channel") == "final"]
        assert len(final) == 1 and final[0]["text"] == "final answer"
        assert not final[0].get("background")
        assert [e.text for e in events if isinstance(e, Delta)].count("final answer") == 1
    assert path.read_bytes() == original


def test_compact_pages_use_echo_uuid_and_count_attachment_payload(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    rows = transcript(True)
    rows[-2]["attachment"]["prompt"] = "task output"
    rows[5]["attachment"]["prompt"] = "x" * 4096
    write(path, rows)
    store = HistoryIndexStore(tmp_path / "index")
    before = None
    cursors = []
    for _ in range(4):
        page = transcript_compact_history_page(
            SID, path=str(path), limit=1, before=before, index_store=store)
        assert page is not None
        page.messages[:] = recover_claude_native_metadata(
            SID, page.messages, path=str(path), timestamps=page.timestamps,
            internal_events=page.internal_events, index_store=store,
            include_queued_prompts=False)
        events = translate_history(page.messages, 4096, page.timestamps, page.internal_events)
        user, = [e for e in events if isinstance(e, UserMsg)]
        assert page.oldest_cursor == user.msg_id
        cursors.append(user.msg_id)
        before = page.oldest_cursor
    assert cursors == ["steer-two", "steer-one", "human", "old"]
    assert not page.has_more
    assert transcript_compact_history_page(
        SID, path=str(path), limit=1, before="steer-two", index_store=store,
        max_payload_bytes=1024) is None


@pytest.mark.parametrize("compact", [False, True])
def test_only_accepted_humans_on_active_ancestry_are_restored(tmp_path, compact):
    rows = transcript(compact)
    answer = rows.pop()
    rows.extend([
        queued("abandoned", "human", "abandoned-input", "rewound input", 10),
        queued("sidechain", "human", "child-input", "child input", 11),
        {"type": "queue-operation", "operation": "enqueue", "content": "not consumed yet"},
    ])
    rows[-2]["isSidechain"] = True
    parent = "task"
    for index, extra in enumerate([
        {"origin": {"kind": "task-notification"}},
        {"origin": {"kind": "agent"}},
        {"commandMode": "other"},
        {"source_uuid": None},
    ]):
        uid = f"not-human-{index}"
        rows.append(queued(uid, parent, uid + "-source", "hidden input", 12 + index, **extra))
        parent = uid
    answer["parentUuid"] = parent
    rows.append(answer)
    path = tmp_path / f"{SID}.jsonl"
    write(path, rows)
    events = read(path, "compact" if compact else "catalog")
    assert [e.msg_id for e in events if isinstance(e, UserMsg)] == [
        "old", "human", "steer-one", "steer-two"]


def test_v43_rebuilds_claude_projection_preserving_other_engines_and_assets(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    write(path, transcript(True))
    store = HistoryIndexStore(tmp_path / "index")
    read(path, "indexed", store)
    fingerprint = HistorySourceFingerprint.capture(path)
    for engine in ("claude", "codex", "dsh"):
        store.put_page(SID, engine, fingerprint, before=None, limit=4, page=_page(engine))
        store.put_image_asset(SID, engine, fingerprint, engine, "image",
                              "thumbnail", "image/png", 1, 1, b"image")
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE claude_compact_records SET visible_user=0 WHERE row_type='attachment'")
        db.execute("PRAGMA user_version=43")
    reopened = HistoryIndexStore(tmp_path / "index")
    assert reopened.get_page(SID, "claude", fingerprint, before=None, limit=4) is None
    for engine in ("codex", "dsh"):
        assert reopened.get_page(SID, engine, fingerprint, before=None, limit=4) is not None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM claude_compact_records").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM history_image_assets").fetchone()[0] == 3
    events = read(path, "indexed", reopened)
    assert [e.msg_id for e in events if isinstance(e, UserMsg)][-2:] == ["steer-one", "steer-two"]
