from __future__ import annotations

import json
import os
import sqlite3

import pytest

from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    MaterializedTurnDetail,
    history_source_extends,
    history_turn_snapshot_hash,
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


def test_turn_detail_snapshot_lookup_is_immutable_and_scope_bound(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text('{"type":"first"}\n')
    original = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    original_events = (
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect"},
        {"type": "process", "item_id": "old-step", "phase": "end"},
        {"type": "turn_end", "turn_id": "native-1", "result": {
            "subtype": "success", "duration_ms": 1, "is_error": False,
        }},
    )
    store.put_turn_details(
        "session-1", "codex", original, original_events)

    first = store.get_turn_detail_snapshot(
        "session-1", "codex", original, "message-1")
    assert first == MaterializedTurnDetail(
        events=original_events,
        turn_id="message-1",
        source_token=original.token,
    )

    with source_path.open("a") as stream:
        stream.write('{"type":"second"}\n')
    appended = HistorySourceFingerprint.capture(source_path)
    appended_events = (
        {"type": "user_msg", "msg_id": "message-1", "prompt": "inspect"},
        {"type": "process", "item_id": "new-step", "phase": "end"},
        {"type": "turn_end", "turn_id": "native-1", "result": {
            "subtype": "success", "duration_ms": 2, "is_error": False,
        }},
    )
    store.put_turn_details(
        "session-1", "codex", appended, appended_events)

    turn_hash = history_turn_snapshot_hash("message-1")
    assert store.get_turn_detail_by_snapshot(
        "session-1", "codex", original.token, turn_hash,
    ) == first
    assert store.get_turn_detail_by_snapshot(
        "session-1", "codex", appended.token, turn_hash,
    ) == MaterializedTurnDetail(
        events=appended_events,
        turn_id="message-1",
        source_token=appended.token,
    )
    assert store.get_turn_detail_by_snapshot(
        "session-2", "codex", original.token, turn_hash,
    ) is None
    assert store.get_turn_detail_by_snapshot(
        "session-1", "claude", original.token, turn_hash,
    ) is None
    assert store.get_turn_detail_by_snapshot(
        "session-1", "codex", original.token,
        history_turn_snapshot_hash("message-2"),
    ) is None

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM history_turn_details "
            "WHERE session_id=? AND engine=? AND source_token=?",
            ("session-1", "codex", original.token),
        )
    assert store.get_turn_detail_by_snapshot(
        "session-1", "codex", original.token, turn_hash,
    ) is None


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


def test_summary_cache_read_skips_full_events_without_losing_detail(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text('{"type":"first"}\n')
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    events = (
        {"type": "model", "model": "claude-test"},
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
        events=events,
        has_more=False,
        oldest_id="message-1",
        newest_id="message-1",
        turns=materialize_history_turns(events),
    )
    assert store.put_page(
        "session-1", "claude", source,
        before=None, limit=4, page=page,
    )

    summary = store.get_page(
        "session-1", "claude", source,
        before=None, limit=4, summary_only=True,
    )
    assert summary is not None
    assert summary.events == ({"type": "model", "model": "claude-test"},)
    assert summary.turns == page.turns

    # The source-complete page remains available to compatibility full-history
    # callers and to TurnDetail's retained-page fallback.
    assert store.get_page(
        "session-1", "claude", source,
        before=None, limit=4,
    ) == page
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM history_turn_details WHERE session_id=?",
            ("session-1",),
        )
    assert store.get_turn_detail(
        "session-1", "claude", source, "message-1",
    ) == events[1:]


@pytest.mark.parametrize("summary_only", [False, True])
def test_page_read_does_not_retry_unrelated_sqlite_failures(summary_only):
    class BrokenConnection:
        def __init__(self):
            self.calls = 0

        def execute(self, _sql, _parameters):
            self.calls += 1
            raise sqlite3.OperationalError("database is locked")

    store = object.__new__(HistoryIndexStore)
    store._summary_json_sql_available = None
    connection = BrokenConnection()

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store._select_page_row(
            connection,
            summary_only=summary_only,
            projected_sql="SELECT projected",
            raw_sql="SELECT raw",
            parameters=(),
        )

    assert connection.calls == 1


def test_missing_sqlite_json_projection_falls_back_only_once():
    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return self.value

    class JsonlessConnection:
        def __init__(self):
            self.calls = []

        def execute(self, sql, _parameters):
            self.calls.append(sql)
            if sql == "SELECT projected":
                raise sqlite3.OperationalError(
                    "no such function: json_set")
            return Cursor("raw-row")

    store = object.__new__(HistoryIndexStore)
    store._summary_json_sql_available = None
    connection = JsonlessConnection()
    kwargs = {
        "summary_only": True,
        "projected_sql": "SELECT projected",
        "raw_sql": "SELECT raw",
        "parameters": (),
    }

    assert store._select_page_row(connection, **kwargs) == "raw-row"
    assert store._summary_json_sql_available is False
    assert store._select_page_row(connection, **kwargs) == "raw-row"
    assert connection.calls == [
        "SELECT projected", "SELECT raw", "SELECT raw",
    ]


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
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_agent_detail_cache_is_source_bound_and_invalidatable(tmp_path):
    source_path = tmp_path / "agent.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    store = HistoryIndexStore(tmp_path / "state")
    events = ({"type": "delta", "text": "agent output"},)

    store.put_agent_detail("session-1", source, "agent-run", events)
    assert store.get_agent_detail(
        "session-1", source, "agent-run") == events

    source_path.write_text("{}\n{}\n")
    changed = HistorySourceFingerprint.capture(source_path)
    assert store.get_agent_detail(
        "session-1", changed, "agent-run") is None

    store.invalidate_session("session-1")
    assert store.get_agent_detail(
        "session-1", source, "agent-run") is None


def test_v19_migration_rebuilds_history_and_adds_agent_details(tmp_path):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)
    assert store.put_page(
        "session-1", "claude", source, before=None, limit=4,
        page=_page("session-1"),
    )

    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE history_agent_details")
        connection.execute("PRAGMA user_version=19")

    migrated = HistoryIndexStore(state_dir)
    assert migrated.get_page(
        "session-1", "claude", source, before=None, limit=4) is None
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        assert connection.execute(
            "SELECT COUNT(*) FROM history_agent_details").fetchone()[0] == 0


def test_v20_migration_rebuilds_codex_and_claude_identity_projections(tmp_path):
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
        connection.execute("PRAGMA user_version=20")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None
    assert migrated.get_image_asset(
        "codex-session", "codex", source,
        "codex-session", "codex-image", "thumbnail",
    ) == ("image/png", 1, 1, b"codex")


def test_v21_migration_rebuilds_claude_alias_and_codex_process_projections(
    tmp_path,
):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id,
            engine,
            source,
            before=None,
            limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
            "image/png",
            1,
            1,
            engine.encode(),
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=21")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None


def test_v22_migration_applies_codex_and_claude_projection_repairs(
    tmp_path,
):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id,
            engine,
            source,
            before=None,
            limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
            "image/png",
            1,
            1,
            engine.encode(),
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=22")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None


@pytest.mark.parametrize("old_version", [23, 24])
def test_recent_migration_applies_codex_and_claude_projection_repairs(
    tmp_path,
    old_version,
):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id,
            engine,
            source,
            before=None,
            limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
            "image/png",
            1,
            1,
            engine.encode(),
        )
    agent_events = ({"type": "delta", "text": "agent output"},)
    store.put_agent_detail(
        "claude-session", source, "agent-run", agent_events)

    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version={old_version}")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM history_agent_details"
        ).fetchone()[0] == 1

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None
    assert migrated.get_image_asset(
        "codex-session", "codex", source,
        "codex-session", "codex-image", "thumbnail",
    ) == ("image/png", 1, 1, b"codex")
    assert migrated.get_agent_detail(
        "claude-session", source, "agent-run",
    ) == agent_events


@pytest.mark.parametrize("old_version", [25, 26, 27, 28, 29, 30, 31])
def test_async_question_migration_preserves_assets_and_unchanged_claude_history(
    tmp_path, old_version,
):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id,
            engine,
            source,
            before=None,
            limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
            "image/png",
            1,
            1,
            engine.encode(),
        )
    agent_events = ({"type": "delta", "text": "agent output"},)
    store.put_agent_detail(
        "claude-session", source, "agent-run", agent_events)

    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version={old_version}")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == (1 if old_version >= 27 else 0)
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM history_agent_details"
        ).fetchone()[0] == 1

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) == (_page("claude-session") if old_version >= 27 else None)
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None
    assert migrated.get_image_asset(
        "claude-session", "claude", source,
        "claude-session", "claude-image", "thumbnail",
    ) == ("image/png", 1, 1, b"claude")
    assert migrated.get_agent_detail(
        "claude-session", source, "agent-run",
    ) == agent_events


def test_async_summary_migration_rebuilds_once_without_source_changes(tmp_path):
    source_path = tmp_path / "rollout.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)
    events = (
        {"type": "user_msg", "msg_id": "user", "prompt": "test"},
        {"type": "delta", "message_id": "question", "channel": "commentary",
         "text": "Question?"},
        {"type": "assistant_msg_end", "message_id": "question",
         "delivery": "async", "questions": [{"title": "Question?"}]},
        {"type": "delta", "message_id": "answer", "channel": "unknown",
         "text": "Finished."},
        {"type": "turn_end", "reason": "success"},
    )
    repaired = materialize_history_turns(events)
    stale = dict(repaired[0], blocks=[repaired[0]["blocks"][0]])
    page = MaterializedHistoryPage(
        events=events, turns=(stale,), has_more=False,
        oldest_id="user", newest_id="user",
    )
    assert store.put_page("session", "codex", source, before=None, limit=4, page=page)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA user_version=28")

    migrated = HistoryIndexStore(state_dir)
    assert HistorySourceFingerprint.capture(source_path).token == source.token
    assert migrated.get_page("session", "codex", source, before=None, limit=4) is None
    detail = migrated.get_turn_detail("session", "codex", source, "user")
    assert detail is None  # v30 also rebuilds generated-image references.
    rebuilt = MaterializedHistoryPage(
        events=events, turns=repaired, has_more=False,
        oldest_id="user", newest_id="user",
    )
    assert migrated.put_page(
        "session", "codex", source, before=None, limit=4, page=rebuilt)
    reopened = HistoryIndexStore(state_dir)
    restored = reopened.get_page("session", "codex", source, before=None, limit=4)
    assert restored == rebuilt
    assert [b["text"] for b in restored.turns[0]["blocks"]] == ["Question?", "Finished."]


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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
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


def test_v10_migration_invalidates_changed_projection_rows(tmp_path):
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in (
            "history_pages", "history_turn_details", "history_image_assets",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == (
                1 if table == "history_image_assets" else 0)

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None
    assert migrated.get_image_asset(
        "codex-session", "codex", source,
        "codex-session", "codex-image", "thumbnail",
    ) == ("image/png", 1, 1, b"codex")


def test_v11_migration_invalidates_claude_pages_and_adds_compact_index(
    tmp_path,
):
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
    ) is None
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
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


@pytest.mark.parametrize("old_version", [12, 13, 14])
def test_recent_migration_invalidates_changed_projection_rows(
    tmp_path, old_version,
):
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
        connection.execute(
            "INSERT INTO claude_compact_sources ("
            "source_path, source_device, source_inode, indexed_size, "
            "source_head_sha256, source_tail_sha256, record_count, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(source_path), 1, 2, 3, "head", "tail", 1, 1.0),
        )
        connection.execute(f"PRAGMA user_version={old_version}")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in (
            "history_pages", "history_turn_details", "history_image_assets",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == (
                1 if table == "history_image_assets" else 0)
        assert connection.execute(
            "SELECT COUNT(*) FROM claude_compact_sources"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("old_version", [15, 16])
def test_owner_and_interrupt_alias_migration_invalidates_both_projections(
    tmp_path, old_version,
):
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

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        for table in ("history_pages", "history_turn_details"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='claude'"
            ).fetchone()[0] == 0
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE engine='codex'"
            ).fetchone()[0] == 0
        for engine in ("claude", "codex"):
            assert connection.execute(
                "SELECT COUNT(*) FROM history_image_assets WHERE engine=?",
                (engine,),
            ).fetchone()[0] == 1

    assert migrated.get_page(
        "claude-session", "claude", source, before=None, limit=4,
    ) is None
    assert migrated.get_page(
        "codex-session", "codex", source, before=None, limit=4,
    ) is None
    assert migrated.get_image_asset(
        "codex-session", "codex", source,
        "codex-session", "codex-image", "thumbnail",
    ) == ("image/png", 1, 1, b"codex")


@pytest.mark.parametrize("old_version", [17, 18])
def test_recent_summary_migration_rebuilds_pages_but_preserves_source_assets(
    tmp_path, old_version,
):
    source_path = tmp_path / "transcript.jsonl"
    source_path.write_text("{}\n")
    source = HistorySourceFingerprint.capture(source_path)
    state_dir = tmp_path / "state"
    store = HistoryIndexStore(state_dir)

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert store.put_page(
            session_id,
            engine,
            source,
            before=None,
            limit=4,
            page=_page(session_id),
        )
        store.put_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
            "image/png",
            1,
            1,
            engine.encode(),
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version={old_version}")

    migrated = HistoryIndexStore(state_dir)
    with sqlite3.connect(migrated.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 32
        assert connection.execute(
            "SELECT COUNT(*) FROM history_pages"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_turn_details WHERE engine='claude'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_turn_details WHERE engine='codex'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM history_image_assets"
        ).fetchone()[0] == 2

    for engine in ("claude", "codex"):
        session_id = f"{engine}-session"
        assert migrated.get_page(
            session_id, engine, source, before=None, limit=4,
        ) is None
        assert migrated.get_turn_detail(
            session_id, engine, source, session_id,
        ) is None
        assert migrated.get_image_asset(
            session_id,
            engine,
            source,
            session_id,
            f"{engine}-image",
            "thumbnail",
        ) == ("image/png", 1, 1, engine.encode())


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
        "processDetailState": "present",
        "detailReasons": ["process"],
        "detailEventCount": 2,
        "detailLoaded": False,
        "forkPointId": "turn-1",
        "ts": 10_000,
        "doneTs": 12_000,
        "durationMs": 2000,
    },)


def test_materialized_turn_separates_visible_process_from_private_detail():
    direct = materialize_history_turns([
        {"type": "user_msg", "msg_id": "direct-user", "prompt": "hello",
         "ts": 10.0},
        {"type": "process", "item_id": "reasoning-private",
         "kind": "reasoning", "phase": "end", "status": "succeeded",
         "ts": 11.0},
        {"type": "process", "item_id": "hook-success",
         "kind": "hook", "phase": "end", "status": "succeeded",
         "ts": 12.0},
        {"type": "assistant_msg_start", "message_id": "legacy-unknown",
         "channel": "unknown", "ts": 12.5},
        {"type": "delta", "message_id": "legacy-unknown",
         "channel": "unknown", "text": "compatibility envelope",
         "ts": 12.5},
        {"type": "assistant_msg_start", "message_id": "direct-final",
         "channel": "final", "ts": 13.0},
        {"type": "delta", "message_id": "direct-final",
         "channel": "final", "text": "hi", "ts": 13.0},
        {"type": "turn_end", "turn_id": "direct-turn", "ts": 14.0,
         "result": {"subtype": "success", "duration_ms": 4000,
                    "is_error": False}},
    ])[0]

    assert direct["detailEventCount"] == 3
    assert direct["processDetailState"] == "none"
    assert direct["detailReasons"] == []
    assert "processStartedTs" not in direct
    assert "processDoneTs" not in direct

    processed = materialize_history_turns([
        {"type": "user_msg", "msg_id": "process-user", "prompt": "work",
         "ts": 10.0},
        {"type": "assistant_msg_start", "message_id": "commentary",
         "channel": "commentary", "ts": 20.0},
        {"type": "delta", "message_id": "commentary",
         "channel": "commentary", "text": "checking", "ts": 21.0},
        {"type": "assistant_msg_end", "message_id": "commentary",
         "channel": "commentary", "ts": 22.0},
        {"type": "tool_use", "message_id": "tool-message",
         "tool_use_id": "tool-visible", "tool": "exec_command", "ts": 23.0},
        {"type": "tool_result", "tool_use_id": "tool-visible",
         "content": "ok", "is_error": False, "ts": 24.0},
        {"type": "assistant_msg_start", "message_id": "process-final",
         "channel": "final", "ts": 29.0},
        {"type": "delta", "message_id": "process-final",
         "channel": "final", "text": "done", "ts": 29.0},
        {"type": "turn_end", "turn_id": "process-turn", "ts": 30.0,
         "result": {"subtype": "success", "duration_ms": 20_000,
                    "is_error": False}},
    ])[0]

    assert processed["processDetailState"] == "present"
    assert processed["detailReasons"] == ["process"]
    assert processed["processStartedTs"] == 20_000
    assert processed["processDoneTs"] == 24_000

    commentary_only = materialize_history_turns([
        {"type": "user_msg", "msg_id": "commentary-user",
         "prompt": "explain", "ts": 10.0},
        {"type": "assistant_msg_start", "message_id": "commentary-only",
         "channel": "commentary", "ts": 20.0},
        {"type": "delta", "message_id": "commentary-only",
         "channel": "commentary", "text": "checking", "ts": 21.0},
        {"type": "assistant_msg_end", "message_id": "commentary-only",
         "channel": "commentary", "ts": 22.0},
        {"type": "assistant_msg_start", "message_id": "commentary-final",
         "channel": "final", "ts": 29.0},
        {"type": "delta", "message_id": "commentary-final",
         "channel": "final", "text": "done", "ts": 29.0},
        {"type": "turn_end", "turn_id": "commentary-turn", "ts": 30.0,
         "result": {"subtype": "success", "duration_ms": 20_000,
                    "is_error": False}},
    ])[0]

    assert commentary_only["processStartedTs"] == 20_000
    assert commentary_only["processDoneTs"] == 22_000

    failed_hook = materialize_history_turns([
        {"type": "user_msg", "msg_id": "hook-user", "prompt": "work",
         "ts": 10.0},
        {"type": "process", "item_id": "hook-actionable",
         "kind": "hook", "phase": "start", "status": "running",
         "ts": 11.0},
        {"type": "process", "item_id": "hook-actionable",
         "kind": "hook", "phase": "end", "status": "failed",
         "ts": 15.0},
        {"type": "turn_end", "turn_id": "hook-turn", "ts": 16.0,
         "result": {"subtype": "error", "duration_ms": 6000,
                    "is_error": True}},
    ])[0]

    assert failed_hook["processDetailState"] == "present"
    assert failed_hook["processStartedTs"] == 15_000
    assert failed_hook["processDoneTs"] == 15_000

    missing_tool_terminal_stamp = materialize_history_turns([
        {"type": "user_msg", "msg_id": "tool-user", "prompt": "work",
         "ts": 10.0},
        {"type": "tool_use", "message_id": "tool-envelope",
         "tool_use_id": "tool-no-terminal-ts", "tool": "exec_command",
         "input": {}, "ts": 20.0},
        {"type": "tool_result", "tool_use_id": "tool-no-terminal-ts",
         "content": "ok", "is_error": False},
        {"type": "turn_end", "turn_id": "tool-turn", "ts": 30.0,
         "result": {"subtype": "success", "duration_ms": 20_000,
                    "is_error": False}},
    ])[0]

    assert missing_tool_terminal_stamp["processStartedTs"] == 20_000
    assert missing_tool_terminal_stamp["processDoneTs"] == 30_000


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
    assert turns[0]["processDetailState"] == "none"
    assert turns[0]["detailReasons"] == ["answer_truncated"]


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
    assert turns[0]["processDetailState"] == "none"
    assert turns[0]["detailReasons"] == [
        "prompt_truncated", "image_deferred"]
