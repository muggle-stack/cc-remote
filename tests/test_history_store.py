from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    history_source_extends,
    materialize_history_turns,
)


def _page(label: str, *, more: bool = False) -> MaterializedHistoryPage:
    events = ({"type": "user_msg", "msg_id": label, "prompt": label},)
    return MaterializedHistoryPage(
        events=events,
        has_more=more,
        oldest_id=label,
        newest_id=label,
        turns=materialize_history_turns(events),
    )


def test_history_index_roundtrip_is_bound_to_exact_source_snapshot(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")

    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4) is None
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4,
        page=_page("first", more=True),
    ) is True
    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4,
    ) == _page("first", more=True)

    # An append changes the source identity; stale narrative is never returned.
    with source_path.open("a") as stream:
        stream.write('{"type":"second"}\n')
    changed = HistorySourceFingerprint.capture(source_path)
    assert changed.token != source.token
    assert store.get_page(
        "session-1", "codex", changed, before=None, limit=4) is None


def test_history_source_append_validation_rejects_truncate_and_replace(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_bytes(b"first\n")
    original = HistorySourceFingerprint.capture(source_path)

    with source_path.open("ab") as source:
        source.write(b"second\n")
    appended = HistorySourceFingerprint.capture(source_path)
    assert history_source_extends(original, appended)

    source_path.write_bytes(b"first\n")
    truncated = HistorySourceFingerprint.capture(source_path)
    assert not history_source_extends(appended, truncated)

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"first\nsecond\n")
    os.replace(replacement, source_path)
    replaced = HistorySourceFingerprint.capture(source_path)
    assert not history_source_extends(appended, replaced)


def test_history_index_invalidates_obsolete_cached_turn_schema_and_rebuilds(
    tmp_path,
):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    page = MaterializedHistoryPage(
        events=({"type": "user_msg", "msg_id": "message-1", "prompt": "hi"},),
        has_more=False,
        oldest_id="message-1",
        newest_id="message-1",
        turns=({"id": "message-1", "prompt": "hi"},),
    )
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4, page=page)

    # Simulate a retained pre-schema cache row.  ``origin`` is deliberately
    # forbidden by ConversationTurn and must invalidate, rather than leak to
    # the browser or make the current process crash while constructing History.
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM history_pages WHERE session_id=?",
            ("session-1",),
        ).fetchone()
        payload = json.loads(bytes(row[0]).decode("utf-8"))
        payload["turns"][0]["origin"] = "legacy-cache"
        connection.execute(
            "UPDATE history_pages SET payload_json=? WHERE session_id=?",
            (json.dumps(payload).encode("utf-8"), "session-1"),
        )

    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4) is None

    # The engine-backed caller can now replace the discarded projection.
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4, page=page)
    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4) == page


def test_turn_detail_survives_append_but_not_destructive_invalidation(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    events = (
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect"},
        {"type": "tool_use", "tool_use_id": "tool-1", "tool": "Read",
         "input": {"file_path": "/tmp/example"}},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
         "is_error": False},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )
    page = MaterializedHistoryPage(
        events=events, has_more=False,
        oldest_id="message-1", newest_id="message-1",
        turns=materialize_history_turns(events),
    )
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4, page=page)
    assert store.get_turn_detail(
        "session-1", "codex", source, "message-1") == events

    # A later append changes the page fingerprint but cannot change an already
    # completed turn, so detail painted from the previous snapshot remains valid.
    with source_path.open("a") as stream:
        stream.write('{"type":"second"}\n')
    changed = HistorySourceFingerprint.capture(source_path)
    assert store.get_turn_detail(
        "session-1", "codex", changed, "message-1") == events

    store.invalidate_session("session-1")
    assert store.get_turn_detail(
        "session-1", "codex", changed, "message-1") is None


def test_turn_detail_recovers_from_retained_page_after_detail_lru_eviction(
    tmp_path,
):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    events = (
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect"},
        {"type": "tool_use", "tool_use_id": "tool-1", "tool": "Read",
         "input": {"file_path": "/tmp/example"}},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok",
         "is_error": False},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    )
    page = MaterializedHistoryPage(
        events=events, has_more=False,
        oldest_id="message-1", newest_id="message-1",
        turns=materialize_history_turns(events),
    )
    assert store.put_page(
        "session-1", "codex", source, before=None, limit=4, page=page)

    # Detail rows have a tighter independent LRU than retained pages.  A summary
    # must not advertise an expandable turn that can no longer be recovered
    # while the containing canonical page is still present.
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM history_turn_details WHERE session_id=?",
            ("session-1",),
        )

    assert store.get_page(
        "session-1", "codex", source, before=None, limit=4) == page
    assert store.get_turn_detail(
        "session-1", "codex", source, "message-1") == events


def test_history_index_separates_cursor_limit_engine_and_session(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    store.put_page(
        "session-1", "claude", source, before="turn-5", limit=12,
        page=_page("older"),
    )

    assert store.get_page(
        "session-1", "claude", source, before="turn-5", limit=12,
    ) == _page("older")
    assert store.get_page(
        "session-1", "claude", source, before=None, limit=12) is None
    assert store.get_page(
        "session-1", "claude", source, before="turn-5", limit=4) is None
    assert store.get_page(
        "session-1", "codex", source, before="turn-5", limit=12) is None
    assert store.get_page(
        "session-2", "claude", source, before="turn-5", limit=12) is None


def test_history_index_is_bounded_and_invalidatable(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(
        tmp_path / "state", max_entries=2, max_bytes=16 * 1024)

    for index in range(3):
        store.put_page(
            f"session-{index}", "claude", source,
            before=None, limit=4, page=_page(f"page-{index}"),
        )
    hits = [
        store.get_page(
            f"session-{index}", "claude", source, before=None, limit=4)
        for index in range(3)
    ]
    assert sum(page is not None for page in hits) == 2

    store.invalidate_session("session-2")
    assert store.get_page(
        "session-2", "claude", source, before=None, limit=4) is None
    if sys.platform != "win32":
        assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("old_version", [6, 7, 8, 9])
def test_legacy_migration_rebuilds_all_derived_history_rows(
        tmp_path, old_version):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id, engine, source, before=None, limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id, engine, source, session_id, f"{engine}-image",
            "thumbnail", "image/png", 1, 1, engine.encode(),
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version={old_version}")
        for table in (
            "history_pages",
            "history_turn_details",
            "history_image_assets",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 1
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 1

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        for table in (
            "history_pages",
            "history_turn_details",
            "history_image_assets",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert migrated.get_page(
            session_id, engine, source, before=None, limit=4,
        ) is None
        assert migrated.get_turn_detail(
            session_id, engine, source, session_id,
        ) is None
        assert migrated.get_image_asset(
            session_id, engine, source, session_id, f"{engine}-image",
            "thumbnail",
        ) is None


def test_v10_migration_invalidates_only_claude_translation_rows(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id, engine, source, before=None, limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id, engine, source, session_id, f"{engine}-image",
            "thumbnail", "image/png", 1, 1, engine.encode(),
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=10")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        for table in (
            "history_pages", "history_turn_details", "history_image_assets",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 1

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) == _page("codex-session")


def test_v11_migration_preserves_pages_and_adds_compact_index(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)
    assert store.put_page(
        "claude-session", "claude", source, before=None, limit=4,
        page=_page("claude-session"),
    )

    with sqlite3.connect(store.path) as connection:
        for table in (
            "claude_compact_sources",
            "claude_compact_records",
            "claude_compact_queue",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=11")

    migrated = HistoryIndexStore(state_dir)
    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) == _page("claude-session")
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "claude_compact_sources",
        "claude_compact_records",
        "claude_compact_queue",
    } <= tables


def test_history_index_rejects_one_page_larger_than_total_budget(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state", max_bytes=1024)
    page = MaterializedHistoryPage(
        events=({"type": "delta", "text": "x" * 2048},),
        has_more=False,
        oldest_id="turn-1",
        newest_id="turn-1",
    )

    assert store.put_page(
        "session-1", "claude", source, before=None, limit=4, page=page,
    ) is False
    assert store.get_page(
        "session-1", "claude", source, before=None, limit=4) is None


def test_materialized_turn_keeps_final_answer_and_defers_process_detail():
    events = [
        {"type": "model", "model": "gpt-test"},
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect",
         "ts": 10.0},
        {"type": "assistant_msg_start", "message_id": "commentary-1",
         "channel": "commentary", "ts": 11.0},
        {"type": "delta", "message_id": "commentary-1",
         "channel": "commentary", "text": "working"},
        {"type": "tool_use", "message_id": "tool-message",
         "tool_use_id": "tool-1", "tool": "exec_command", "input": {}},
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "x" * 1000,
         "is_error": False},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final"},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": "done"},
        {"type": "assistant_msg_end", "message_id": "final-1",
         "channel": "final"},
        {"type": "turn_end", "turn_id": "turn-1", "ts": 12.0,
         "result": {"subtype": "success", "duration_ms": 2000,
                    "is_error": False}},
    ]

    turns = materialize_history_turns(events)

    assert turns == ({
        "id": "message-1",
        "prompt": "inspect",
        "blocks": [{
            "kind": "text", "message_id": "final-1", "text": "done",
            "done": True, "channel": "final",
        }],
        "done": True,
        "detailEventCount": 2,
        "detailLoaded": False,
        "forkPointId": "turn-1",
        "ts": 10_000,
        "doneTs": 12_000,
        "durationMs": 2000,
    },)


def test_materialized_live_projection_keeps_commentary_tools_and_compaction():
    events = [
        {"type": "user_msg", "msg_id": "first-prompt", "prompt": "first"},
        {"type": "assistant_msg_start", "message_id": "commentary-1",
         "channel": "commentary"},
        {"type": "delta", "message_id": "commentary-1",
         "channel": "commentary", "text": "first progress"},
        {"type": "assistant_msg_end", "message_id": "commentary-1",
         "channel": "commentary"},
        {"type": "tool_use", "message_id": "tool-message-1",
         "tool_use_id": "tool-1", "tool": "exec_command",
         "category": "command", "title": "运行命令",
         "input": {"command": "secret large input"}},
        {"type": "tool_result", "tool_use_id": "tool-1",
         "content": "large output must stay deferred", "is_error": False,
         "status": "succeeded", "summary": "命令完成"},
        {"type": "turn_end",
         "result": {"subtype": "steered", "duration_ms": 0,
                    "is_error": False}},
        {"type": "user_msg", "msg_id": "second-prompt", "prompt": "second"},
        {"type": "assistant_msg_start", "message_id": "commentary-2",
         "channel": "commentary"},
        {"type": "delta", "message_id": "commentary-2",
         "channel": "commentary", "text": "second progress"},
        {"type": "assistant_msg_end", "message_id": "commentary-2",
         "channel": "commentary"},
        {"type": "process", "item_id": "compact-1", "kind": "compaction",
         "phase": "end", "status": "succeeded", "title": "压缩上下文"},
    ]

    turns = materialize_history_turns(events, include_live_detail=True)

    assert [(turn["prompt"], turn["done"]) for turn in turns] == [
        ("first", True), ("second", False),
    ]
    assert turns[0]["blocks"] == [
        {
            "kind": "text", "message_id": "commentary-1",
            "text": "first progress", "done": True,
            "channel": "commentary",
        },
        {
            "kind": "tool", "message_id": "tool-message-1",
            "tool_use_id": "tool-1", "tool": "exec_command",
            "input": {}, "category": "command", "title": "运行命令",
            "parent_id": None, "server": None, "done": True,
            "result": {
                "content": "", "is_error": False,
                "status": "succeeded", "summary": "命令完成",
            },
        },
    ]
    assert turns[1]["blocks"] == [
        {
            "kind": "text", "message_id": "commentary-2",
            "text": "second progress", "done": True,
            "channel": "commentary",
        },
        {
            "kind": "process", "item_id": "compact-1",
            "processKind": "compaction", "phase": "end",
            "status": "succeeded", "turn_id": None,
            "parent_id": None, "title": "压缩上下文",
            "done": True,
        },
    ]
    encoded = str(turns)
    assert "secret large input" not in encoded
    assert "large output must stay deferred" not in encoded


def test_materialized_live_projection_bounds_long_tool_tail_but_keeps_status():
    events = [
        {"type": "user_msg", "msg_id": "prompt", "prompt": "work"},
        {"type": "assistant_msg_start", "message_id": "commentary-first",
         "channel": "commentary"},
        {"type": "delta", "message_id": "commentary-first",
         "channel": "commentary", "text": "starting"},
        {"type": "assistant_msg_end", "message_id": "commentary-first",
         "channel": "commentary"},
    ]
    for index in range(40):
        events.extend([
            {"type": "tool_use", "message_id": f"tool-message-{index}",
             "tool_use_id": f"tool-{index}", "tool": "exec_command",
             "input": {"command": "x" * 1000}},
            {"type": "tool_result", "tool_use_id": f"tool-{index}",
             "content": "y" * 1000, "is_error": False,
             "status": "succeeded"},
        ])
    events.extend([
        {"type": "process", "item_id": "compact-late",
         "kind": "compaction", "phase": "end", "status": "succeeded",
         "title": "压缩上下文"},
        {"type": "assistant_msg_start", "message_id": "commentary-latest",
         "channel": "commentary"},
        {"type": "delta", "message_id": "commentary-latest",
         "channel": "commentary", "text": "continuing after compact"},
        {"type": "assistant_msg_end", "message_id": "commentary-latest",
         "channel": "commentary"},
    ])

    turn = materialize_history_turns(
        events, include_live_detail=True)[0]

    assert len(turn["blocks"]) <= 24
    assert any(block.get("text") == "starting" for block in turn["blocks"])
    assert any(
        block.get("text") == "continuing after compact"
        for block in turn["blocks"]
    )
    assert any(
        block.get("processKind") == "compaction"
        for block in turn["blocks"]
    )


def test_materialized_assistant_only_turn_rejects_replay_time_after_terminal():
    turns = materialize_history_turns([
        # Codex history reconstructs process/text envelopes while parsing. Those
        # synthetic events can carry the parse time rather than the rollout
        # time, but they must never move an already-completed continuation past
        # later user turns.
        {"type": "process", "item_id": "turn-1", "ts": 100.0},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final", "ts": 100.0},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": "done", "ts": 100.0},
        {"type": "turn_end", "turn_id": "turn-1", "ts": 50.0,
         "result": {"subtype": "success", "duration_ms": 2000,
                    "is_error": False}},
    ])

    assert turns[0]["prompt"] == ""
    assert turns[0]["ts"] == 48_000
    assert turns[0]["doneTs"] == 50_000


def test_materialized_turn_bounds_initial_final_text_and_advertises_detail():
    huge = "x" * (300 * 1024)
    turns = materialize_history_turns([
        {"type": "user_msg", "msg_id": "message-1", "prompt": "long"},
        {"type": "assistant_msg_start", "message_id": "final-1",
         "channel": "final"},
        {"type": "delta", "message_id": "final-1", "channel": "final",
         "text": huge},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    ])

    assert len(turns[0]["blocks"][0]["text"]) <= 256 * 1024
    assert "完整内容请展开" in turns[0]["blocks"][0]["text"]
    assert turns[0]["detailEventCount"] == 1


def test_materialized_turn_defers_images_and_bounds_large_prompt():
    turns = materialize_history_turns([
        {"type": "user_msg", "msg_id": "message-1",
         "prompt": "p" * (160 * 1024),
         "images": [{"media_type": "image/png", "data": "x" * 500_000}]},
        {"type": "turn_end", "turn_id": "turn-1",
         "result": {"subtype": "success", "duration_ms": 1,
                    "is_error": False}},
    ])

    assert len(turns[0]["prompt"]) <= 128 * 1024
    assert "完整问题请展开" in turns[0]["prompt"]
    assert "images" not in turns[0]
    assert turns[0]["detailEventCount"] == 2
