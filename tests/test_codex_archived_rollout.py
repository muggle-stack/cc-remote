from __future__ import annotations

import json
import sqlite3

import pytest

from cc_remote.wrapper import codex_sessions


def _write_catalog_rollout(
    home,
    thread_id: str,
    *,
    parent_id: str | None = None,
    session_id: str | None = None,
    archived: bool = False,
):
    root = home / ("archived_sessions" if archived else "sessions")
    root.mkdir(parents=True, exist_ok=True)
    rollout = root / f"rollout-{thread_id}.jsonl"
    payload = {
        "id": thread_id,
        "session_id": session_id or thread_id,
    }
    if parent_id is not None:
        payload["forked_from_id"] = parent_id
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )
    return rollout


def test_archived_rollout_still_supports_engine_and_cwd_lookup(tmp_path, monkeypatch):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    session_id = "019f555d-archive-test"
    rollout = archived / f"rollout-2026-07-12-{session_id}.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": "/repo/archived"},
    }) + "\n")
    monkeypatch.setattr(codex_sessions, "_ROOT", str(active))
    monkeypatch.setattr(codex_sessions, "_ARCHIVE_ROOT", str(archived))

    assert codex_sessions.codex_rollout_path(session_id) == str(rollout)
    assert codex_sessions.codex_session_cwd(session_id) == "/repo/archived"


def test_active_rollout_wins_if_both_stores_contain_same_id(tmp_path, monkeypatch):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active.mkdir()
    archived.mkdir()
    session_id = "019f555d-duplicate-test"
    active_rollout = active / f"rollout-active-{session_id}.jsonl"
    archived_rollout = archived / f"rollout-archived-{session_id}.jsonl"
    for path, cwd in ((active_rollout, "/repo/active"),
                      (archived_rollout, "/repo/archived")):
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        }) + "\n")
    monkeypatch.setattr(codex_sessions, "_ROOT", str(active))
    monkeypatch.setattr(codex_sessions, "_ARCHIVE_ROOT", str(archived))

    assert codex_sessions.codex_rollout_path(session_id) == str(active_rollout)
    assert codex_sessions.codex_session_cwd(session_id) == "/repo/active"


@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("exact_first", [False, True])
def test_rollout_lookup_requires_exact_thread_identity_regardless_of_order(
    tmp_path, monkeypatch, archived, exact_first,
):
    exact = _write_catalog_rollout(tmp_path, "child", archived=archived)
    _write_catalog_rollout(tmp_path, "grandchild")
    # A copied session_id is not the native thread id of a fork.
    _write_catalog_rollout(tmp_path, "child-copy", session_id="child")
    (tmp_path / "sessions/rollout-empty-child.jsonl").write_text("")
    (tmp_path / "sessions/rollout-broken-child.jsonl").write_text("{invalid}\n")
    iglob = codex_sessions.glob.iglob

    def ordered_matches(pattern, *, recursive):
        # Directory order differs between filesystems. Exercise both orders
        # without relying on this machine's native directory enumeration.
        return iter(sorted(
            iglob(pattern, recursive=recursive),
            key=lambda path: path == str(exact),
            reverse=exact_first,
        ))

    monkeypatch.setattr(codex_sessions.glob, "iglob", ordered_matches)

    assert codex_sessions.codex_rollout_path("child", codex_home=tmp_path) == str(exact)
    exact.unlink()
    assert codex_sessions.codex_rollout_path("child", codex_home=tmp_path) is None


def test_codex_session_presence_uses_exact_state_db_and_preserves_uncertainty(
    tmp_path,
):
    home = tmp_path / ".codex"
    home.mkdir()
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO threads(id) VALUES (?)", ("native-id",))

    assert codex_sessions.codex_session_presence(
        "native-id", codex_home=home) is True
    assert codex_sessions.codex_session_presence(
        "missing-id", codex_home=home) is False

    db.write_bytes(b"not sqlite")
    assert codex_sessions.codex_session_presence(
        "native-id", codex_home=home) is None


def test_archive_states_ignore_current_provider_inside_exact_home(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "current-provider"\n', encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                archived INTEGER NOT NULL,
                model_provider TEXT NOT NULL
            )"""
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?)",
            [
                ("historical-active", 0, "old-provider"),
                ("historical-archived", 1, "old-provider"),
            ],
        )

    assert codex_sessions.codex_thread_archive_states(
        ["historical-active", "historical-archived", "missing"],
        codex_home=home,
    ) == {
        "historical-active": False,
        "historical-archived": True,
    }

    db.write_bytes(b"not sqlite")
    assert codex_sessions.codex_thread_archive_states(
        ["historical-active"], codex_home=home) is None


def test_cross_home_rollout_record_is_read_only_even_with_local_duplicate(
    tmp_path,
):
    session_id = "019fdb22-cross-home-read-only"
    home = tmp_path / "codex-stack"
    local_dir = home / "sessions" / "2026" / "08" / "07"
    external_dir = tmp_path / "old-codex" / "sessions" / "2026" / "08" / "07"
    local_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    name = f"rollout-2026-08-07-{session_id}.jsonl"
    local_rollout = local_dir / name
    external_rollout = external_dir / name
    payload = (
        json.dumps({
            "type": "session_meta",
            "payload": {"id": session_id, "session_id": session_id},
        })
        + "\n"
        + json.dumps({"type": "event_msg", "payload": {"value": "same"}})
        + "\n"
    )
    local_rollout.write_text(payload, encoding="utf-8")
    external_rollout.write_text(payload, encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, 0)",
            (session_id, str(external_rollout)),
        )

    assert codex_sessions.codex_thread_rollout_record(
        session_id,
        codex_home=home,
    ) == (str(external_rollout), False)

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT rollout_path, archived FROM threads WHERE id=?",
            (session_id,),
        ).fetchone() == (str(external_rollout), 0)
    assert local_rollout.read_text(encoding="utf-8") == payload
    assert external_rollout.read_text(encoding="utf-8") == payload
    assert not hasattr(codex_sessions, "repair_codex_active_rollout_path")


def test_local_catalog_path_is_a_noop_even_with_duplicate_candidates(tmp_path):
    session_id = "019fdb22-local-authoritative"
    home = tmp_path / "codex-stack"
    first_dir = home / "sessions" / "2026" / "08" / "07"
    second_dir = home / "sessions" / "2026" / "08" / "08"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    payload = json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id},
    }) + "\n"
    first = first_dir / f"rollout-first-{session_id}.jsonl"
    second = second_dir / f"rollout-second-{session_id}.jsonl"
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, 0)",
            (session_id, str(first)),
        )

    assert codex_sessions.codex_thread_rollout_record(
        session_id,
        codex_home=home,
    ) == (str(first), False)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT rollout_path FROM threads WHERE id=?",
            (session_id,),
        ).fetchone() == (str(first),)


def test_cross_home_rollout_record_does_not_compare_or_rewrite_duplicates(
    tmp_path,
):
    session_id = "019fdb22-cross-home-mismatch"
    home = tmp_path / "codex-stack"
    local_dir = home / "sessions"
    external_dir = tmp_path / "old-codex" / "sessions"
    local_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    name = f"rollout-{session_id}.jsonl"
    meta = json.dumps({
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id},
    }) + "\n"
    local_rollout = local_dir / name
    external_rollout = external_dir / name
    local_rollout.write_text(meta + "local\n", encoding="utf-8")
    external_rollout.write_text(meta + "external\n", encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
            "archived INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, 0)",
            (session_id, str(external_rollout)),
        )

    assert codex_sessions.codex_thread_rollout_record(
        session_id,
        codex_home=home,
    ) == (str(external_rollout), False)

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT rollout_path FROM threads WHERE id=?",
            (session_id,),
        ).fetchone() == (str(external_rollout),)


def test_archive_states_reject_oversized_or_invalid_exact_sets(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, archived INTEGER)"
        )

    oversized = [
        f"thread-{index}"
        for index in range(codex_sessions.CODEX_EXACT_CATALOG_MAX_IDS + 1)
    ]
    assert codex_sessions.codex_thread_archive_states(
        oversized, codex_home=home,
    ) is None
    assert codex_sessions.codex_thread_archive_states(
        ["valid", "not a thread id"], codex_home=home,
    ) is None


def test_spawn_parent_map_is_bounded_and_provider_neutral(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        rows = []
        for thread_id, source in [
            ("root", "vscode"),
            (
                "hidden-child",
                json.dumps({"subagent": {"thread_spawn": {
                    "parent_thread_id": "root",
                }}}),
            ),
            (
                "hidden-grandchild",
                json.dumps({"subagent": {"thread_spawn": {
                    "parent_thread_id": "hidden-child",
                }}}),
            ),
            ("guardian", json.dumps({
                "subagent": {"other": "guardian"},
            })),
        ]:
            rows.append((
                thread_id,
                source,
                str(_write_catalog_rollout(home, thread_id)),
                0,
            ))
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            rows,
        )

    assert codex_sessions.codex_thread_spawn_parent_map(
        codex_home=home,
    ) == {
        "hidden-child": "root",
        "hidden-grandchild": "hidden-child",
    }


def test_parent_maps_read_three_layer_ordinary_fork_from_rollout_meta(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    rows = []
    for thread_id, parent_id in (
        ("root", None),
        ("fork-child", "root"),
        ("fork-grandchild", "fork-child"),
    ):
        rows.append((
            thread_id,
            "vscode",
            str(_write_catalog_rollout(
                home,
                thread_id,
                parent_id=parent_id,
                session_id="root",
            )),
            0,
        ))
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?)",
            rows,
        )

    expected = {
        "fork-child": "root",
        "fork-grandchild": "fork-child",
    }
    assert codex_sessions.codex_thread_parent_maps(
        codex_home=home,
    ) == (expected, expected)


def test_source_backed_subagent_is_not_misclassified_as_ordinary_fork(
    tmp_path,
):
    home = tmp_path / ".codex"
    home.mkdir()
    root_rollout = _write_catalog_rollout(home, "root")
    child_rollout = _write_catalog_rollout(
        home,
        "hidden-child",
        parent_id="root",
        session_id="root",
    )
    source = json.dumps({"subagent": {"thread_spawn": {
        "parent_thread_id": "root",
    }}})
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, 0)",
            [
                ("root", "vscode", str(root_rollout)),
                ("hidden-child", source, str(child_rollout)),
            ],
        )

    assert codex_sessions.codex_thread_parent_maps(
        codex_home=home,
    ) == ({"hidden-child": "root"}, {})


def test_parent_maps_fail_closed_when_source_and_rollout_disagree(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    rollout = _write_catalog_rollout(
        home,
        "child",
        parent_id="rollout-parent",
    )
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, 0)",
            (
                "child",
                json.dumps({"subagent": {"thread_spawn": {
                    "parent_thread_id": "source-parent",
                }}}),
                str(rollout),
            ),
        )

    assert codex_sessions.codex_thread_parent_maps(
        codex_home=home,
    ) is None


def test_spawn_parent_map_preserves_uncertainty(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, "
            "rollout_path TEXT, archived INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, 0)",
            (
                "hidden",
                "{not-json",
                str(_write_catalog_rollout(home, "hidden")),
            ),
        )

    assert codex_sessions.codex_thread_spawn_parent_map(
        codex_home=home,
    ) is None

    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM threads")
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, 0)",
            [
                (
                    f"thread-{index}",
                    "vscode",
                    str(_write_catalog_rollout(home, f"thread-{index}")),
                )
                for index in range(3)
            ],
        )
    monkeypatch.setattr(
        codex_sessions,
        "CODEX_THREAD_PARENT_SCAN_MAX_ROWS",
        2,
    )
    assert codex_sessions.codex_thread_spawn_parent_map(
        codex_home=home,
    ) is None


def test_exact_catalog_rows_restore_empty_preview_without_crossing_provider(
    tmp_path,
):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "openai"\n', encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                name TEXT,
                preview TEXT,
                first_user_message TEXT,
                title TEXT,
                recency_at INTEGER,
                updated_at INTEGER,
                created_at INTEGER,
                git_branch TEXT,
                archived INTEGER,
                model_provider TEXT
            )"""
        )
        connection.executemany(
            """INSERT INTO threads VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                (
                    "empty-preview-child", "/repo/stack", None, "", "", "",
                    20, 21, 19, "fork-fix", 0, "openai",
                ),
                (
                    "other-provider", "/repo/other", None, "secret", "secret",
                    "secret", 30, 30, 30, None, 0, "different-provider",
                ),
            ],
        )

    rows = codex_sessions.codex_exact_catalog_rows(
        ["empty-preview-child", "other-provider"], codex_home=home)

    assert rows == [{
        "session_id": "empty-preview-child",
        "summary": None,
        "first_prompt": None,
        "cwd": "/repo/stack",
        "last_modified": "21",
        "git_branch": "fork-fix",
        "forked_from_id": None,
        "status": None,
        "tag": None,
    }]


def test_exact_catalog_rows_bound_optional_text_on_minimal_schema(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, preview TEXT)"
        )
        connection.execute(
            "INSERT INTO threads(id, preview) VALUES (?, ?)",
            ("bounded-preview", "x" * 10_000),
        )

    rows = codex_sessions.codex_exact_catalog_rows(
        ["bounded-preview"], codex_home=home)

    assert rows is not None and len(rows) == 1
    assert rows[0]["session_id"] == "bounded-preview"
    assert rows[0]["first_prompt"] == "x" * 2000
    assert rows[0]["cwd"] is None


def test_exact_catalog_old_schema_with_provider_preserves_uncertainty(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model_provider = "openai"\n', encoding="utf-8")
    db = home / "state_5.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, preview TEXT)"
        )
        connection.execute(
            "INSERT INTO threads(id, preview) VALUES (?, ?)",
            ("old-schema-child", "hidden fork"),
        )

    assert codex_sessions.codex_exact_catalog_rows(
        ["old-schema-child"], codex_home=home) is None
