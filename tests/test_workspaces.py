import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cc_remote.workspaces import WorkRegistry, WorkStores


class WorkRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "provider" / "work"
        self.store = WorkRegistry(self.root, "claude")

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_registry_reads_close_every_sqlite_connection(self):
        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch(
            "cc_remote.workspaces.sqlite3.connect", side_effect=tracked_connect,
        ):
            for _ in range(10):
                self.store.records_by_session()
                self.store.dashboard()

        self.assertGreater(len(opened), 0)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_legacy_schema_migration_is_safe_under_concurrent_initialize(self):
        self.root.mkdir(parents=True)
        with sqlite3.connect(self.store.db_path) as db:
            db.execute(
                """CREATE TABLE work_sessions (
                    work_id TEXT PRIMARY KEY,
                    engine TEXT NOT NULL,
                    session_id TEXT UNIQUE,
                    cwd TEXT NOT NULL UNIQUE,
                    title TEXT,
                    project_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )

        workers = 8
        barrier = threading.Barrier(workers)

        def initialize() -> None:
            barrier.wait(timeout=5)
            self.store.initialize()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda _index: initialize(), range(workers)))

        with sqlite3.connect(self.store.db_path) as db:
            columns = [row[1] for row in db.execute(
                "PRAGMA table_info(work_sessions)"
            )]
        self.assertEqual(columns.count("context_baseline_tokens"), 1)

    def test_project_sources_plugins_are_materialized_into_private_session(self):
        project_id = self.store.create_project("季度复盘", "整理业务结果")
        self.store.add_source(
            project_id, "file", "原始数据", filename="report.csv",
            content=b"name,value\nA,1\n",
        )
        self.store.add_source(
            project_id, "link", "说明文档", uri="https://example.com/spec",
        )
        self.store.create_plugin("输出规范", "结论先行并附数据来源", project_id)

        record = self.store.create_session(project_id)
        workspace = Path(record.cwd)
        context = (workspace / "WORK.md").read_text(encoding="utf-8")
        self.assertIn("季度复盘", context)
        self.assertIn("资料库/report.csv", context)
        self.assertIn("https://example.com/spec", context)
        self.assertIn("结论先行", context)
        self.assertEqual((workspace / "资料库" / "report.csv").read_bytes(),
                         b"name,value\nA,1\n")
        if sys.platform != "win32":
            self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
            self.assertEqual((workspace / "WORK.md").stat().st_mode & 0o777, 0o600)

    def test_existing_work_session_tracks_later_project_context_changes(self):
        project_id = self.store.create_project("持续项目", "初始说明")
        record = self.store.create_session(project_id)
        self.store.bind_session(record.work_id, "session-1")
        workspace = Path(record.cwd)

        source_id = self.store.add_source(
            project_id, "file", "第一版", filename="brief.txt",
            content=b"version one",
        )
        self.store.create_plugin("交付格式", "必须带结论", project_id)

        self.assertEqual(
            (workspace / "资料库" / "brief.txt").read_bytes(), b"version one")
        context = (workspace / "WORK.md").read_text(encoding="utf-8")
        self.assertIn("第一版", context)
        self.assertIn("已启用工作模板", context)
        self.assertIn("必须带结论", context)

        self.store.delete_source(source_id)
        self.assertFalse((workspace / "资料库" / "brief.txt").exists())
        self.assertNotIn(
            "第一版", (workspace / "WORK.md").read_text(encoding="utf-8"))

    def test_context_sync_never_overwrites_user_created_library_file(self):
        project_id = self.store.create_project("资料冲突")
        record = self.store.create_session(project_id)
        workspace = Path(record.cwd)
        library = workspace / "资料库"
        library.mkdir()
        user_file = library / "brief.txt"
        user_file.write_bytes(b"user owned")

        self.store.add_source(
            project_id, "file", "资料", filename="brief.txt",
            content=b"managed copy",
        )

        self.assertEqual(user_file.read_bytes(), b"user owned")
        self.assertEqual((library / "brief-2.txt").read_bytes(), b"managed copy")
        self.assertIn(
            "资料库/brief-2.txt",
            (workspace / "WORK.md").read_text(encoding="utf-8"),
        )

    def test_deleting_project_removes_only_managed_context(self):
        project_id = self.store.create_project("可删除项目")
        self.store.add_source(
            project_id, "file", "资料", filename="source.txt", content=b"source")
        record = self.store.create_session(project_id)
        workspace = Path(record.cwd)
        user_file = workspace / "keep.txt"
        user_file.write_text("keep", encoding="utf-8")

        self.store.delete_project(project_id)

        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertFalse((workspace / "WORK.md").exists())
        self.assertFalse((workspace / "资料库" / "source.txt").exists())

    def test_dashboard_never_exposes_provider_storage_paths(self):
        project_id = self.store.create_project("知识库")
        self.store.add_source(
            project_id, "file", "secret", filename="secret.txt",
            content=b"safe copy only",
        )
        dashboard = self.store.dashboard()
        self.assertEqual(len(dashboard["sources"]), 1)
        self.assertNotIn("stored_path", dashboard["sources"][0])
        self.assertNotIn(str(self.root), repr(dashboard))

    def test_schedule_claim_is_atomic_and_records_result(self):
        schedule_id = self.store.create_schedule(
            "日报", "生成日报", time.time() - 1, repeat_seconds=3600)
        first = self.store.claim_due_schedules(time.time())
        second = self.store.claim_due_schedules(time.time())
        self.assertEqual([row["schedule_id"] for row in first], [schedule_id])
        self.assertEqual(second, [])
        self.store.complete_schedule(first[0]["run_id"], "session-1", None)
        schedule = self.store.dashboard()["schedules"][0]
        self.assertEqual(schedule["last_session_id"], "session-1")
        self.assertEqual(schedule["last_run_status"], "succeeded")
        self.assertTrue(schedule["enabled"])

    def test_one_shot_schedule_recovers_same_run_after_lease_expiry(self):
        now = time.time()
        schedule_id = self.store.create_schedule("一次任务", "生成报告", now - 1)

        first = self.store.claim_due_schedules(now, lease_seconds=10)
        self.assertEqual(first[0]["schedule_id"], schedule_id)
        run_id = first[0]["run_id"]
        self.assertEqual(self.store.claim_due_schedules(now + 5), [])

        recovered = self.store.claim_due_schedules(now + 11, lease_seconds=10)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["run_id"], run_id)
        self.assertEqual(recovered[0]["attempt"], 2)
        self.assertFalse(self.store.dashboard()["schedules"][0]["enabled"])

    def test_failed_schedule_run_retries_with_backoff_then_succeeds(self):
        now = time.time()
        self.store.create_schedule("重试任务", "生成报告", now - 1)
        first = self.store.claim_due_schedules(now)[0]

        status = self.store.complete_schedule(
            first["run_id"], None, "temporary failure", now=now)
        self.assertEqual(status, "queued")
        self.assertEqual(self.store.claim_due_schedules(now + 14), [])
        second = self.store.claim_due_schedules(now + 15)[0]
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["attempt"], 2)

        self.assertEqual(
            self.store.complete_schedule(
                second["run_id"], "session-ok", None, now=now + 16),
            "succeeded",
        )
        schedule = self.store.dashboard()["schedules"][0]
        self.assertEqual(schedule["last_run_status"], "succeeded")
        self.assertEqual(schedule["last_session_id"], "session-ok")

    def test_deleting_running_schedule_defers_cleanup_until_completion(self):
        now = time.time()
        schedule_id = self.store.create_schedule(
            "可删除任务", "生成报告", now - 1)
        run = self.store.claim_due_schedules(now)[0]
        self.assertTrue(self.store.mark_schedule_running(run["run_id"], now))

        self.store.delete_schedule(schedule_id)

        self.assertEqual(self.store.dashboard()["schedules"], [])
        self.assertEqual(
            self.store.complete_schedule(
                run["run_id"], "session-finished", None, now=now + 1),
            "succeeded",
        )
        with sqlite3.connect(self.store.db_path) as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM work_schedules WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone())
            self.assertIsNone(db.execute(
                "SELECT 1 FROM work_schedule_runs WHERE run_id = ?",
                (run["run_id"],),
            ).fetchone())

    def test_delete_session_removes_only_registry_owned_random_directory(self):
        record = self.store.create_session()
        self.store.bind_session(record.work_id, "session-1")
        outside = Path(self.tmp.name) / "keep.txt"
        outside.write_text("keep", encoding="utf-8")
        self.store.delete("session-1")
        self.assertFalse(Path(record.cwd).parent.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_engine_stores_are_strictly_separate(self):
        stores = WorkStores(self.root / "claude", self.root / "codex")
        claude = stores.for_engine("claude").create_session()
        stores.for_engine("claude").bind_session(claude.work_id, "same-id")
        self.assertEqual(stores.classify("claude", "same-id", claude.cwd), "work")
        self.assertEqual(stores.classify("codex", "same-id", claude.cwd), "code")
        with self.assertRaises(ValueError):
            stores.for_engine("other")

    def test_unbound_records_are_indexed_by_canonical_private_cwd(self):
        unbound = self.store.create_session()
        bound = self.store.create_session()
        self.store.bind_session(bound.work_id, "session-bound")

        records = self.store.unbound_records_by_cwd()

        self.assertEqual(list(records), [os.path.realpath(unbound.cwd)])
        self.assertEqual(records[os.path.realpath(unbound.cwd)].work_id,
                         unbound.work_id)

    def test_artifacts_list_only_user_deliverables_inside_private_workspace(self):
        record = self.store.create_session()
        self.store.bind_session(record.work_id, "session-1")
        workspace = Path(record.cwd)
        (workspace / "report.md").write_text("# result", encoding="utf-8")
        slides = workspace / "output" / "deck.pptx"
        slides.parent.mkdir()
        slides.write_bytes(b"presentation")
        (workspace / ".private.txt").write_text("hidden", encoding="utf-8")
        (workspace / "资料库").mkdir(exist_ok=True)
        (workspace / "资料库" / "source.csv").write_text("input", encoding="utf-8")

        artifacts = self.store.artifacts("session-1")

        self.assertEqual({item["path"] for item in artifacts}, {
            "report.md", "output/deck.pptx",
        })
        by_path = {item["path"]: item for item in artifacts}
        self.assertTrue(by_path["report.md"]["previewable"])
        self.assertEqual(by_path["report.md"]["kind"], "document")
        self.assertTrue(by_path["output/deck.pptx"]["previewable"])
        self.assertEqual(by_path["output/deck.pptx"]["kind"], "presentation")

        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            (workspace / "linked.txt").symlink_to(outside)
        except OSError as exc:
            if sys.platform == "win32":
                self.skipTest(f"Windows symlink privilege is unavailable: {exc}")
            raise

        artifacts_with_symlink = self.store.artifacts("session-1")
        self.assertEqual({item["path"] for item in artifacts_with_symlink}, {
            "report.md", "output/deck.pptx",
        })

    def test_claude_policy_copies_only_runtime_provider_settings(self):
        config_dir = Path(self.tmp.name) / "claude-config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(json.dumps({
            "model": "claude-sonnet-4-5",
            "env": {
                "ANTHROPIC_BASE_URL": "https://provider.example/v1",
                "ANTHROPIC_AUTH_TOKEN": "provider-token",
                "ANTHROPIC_MODEL": "provider-model",
                "CLAUDE_CODE_OAUTH_TOKEN": "subscription-oauth-token",
                "UNRELATED_SECRET": "must-not-cross",
            },
            "hooks": {"UserPromptSubmit": [{"hooks": [{"command": "inject-memory"}]}]},
            "permissions": {"allow": ["Read(~/private/**)"]},
            "enabledPlugins": {"global-memory": True},
            "theme": "dark",
        }), encoding="utf-8")
        record = self.store.create_session()

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            policy = Path(self.store.ensure_claude_policy(record))

        payload = json.loads(policy.read_text(encoding="utf-8"))
        self.assertEqual(payload["model"], "claude-sonnet-4-5")
        self.assertEqual(payload["env"], {
            "ANTHROPIC_BASE_URL": "https://provider.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "provider-token",
            "ANTHROPIC_MODEL": "provider-model",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-oauth-token",
        })
        self.assertNotIn("hooks", payload)
        self.assertNotIn("enabledPlugins", payload)
        self.assertNotIn("UNRELATED_SECRET", repr(payload))
        self.assertEqual(payload["sandbox"]["filesystem"]["allowRead"],
                         [record.cwd])
        self.assertEqual(payload["sandbox"]["filesystem"]["allowWrite"],
                         [record.cwd])
        if sys.platform != "win32":
            self.assertEqual(policy.stat().st_mode & 0o777, 0o600)

    def test_claude_policy_ignores_invalid_or_oversized_user_settings(self):
        config_dir = Path(self.tmp.name) / "claude-config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text("{" + "x" * 1_100_000,
                                                   encoding="utf-8")
        record = self.store.create_session()

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            policy = Path(self.store.ensure_claude_policy(record))

        payload = json.loads(policy.read_text(encoding="utf-8"))
        self.assertNotIn("model", payload)
        self.assertNotIn("env", payload)

    def test_claude_policy_refreshes_a_rotated_oauth_token(self):
        config_dir = Path(self.tmp.name) / "claude-config"
        config_dir.mkdir()
        settings = config_dir / "settings.json"
        settings.write_text(json.dumps({
            "env": {"CLAUDE_CODE_OAUTH_TOKEN": "old-token"},
        }), encoding="utf-8")
        record = self.store.create_session()

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            policy = Path(self.store.ensure_claude_policy(record))
            self.assertEqual(
                json.loads(policy.read_text(encoding="utf-8"))["env"][
                    "CLAUDE_CODE_OAUTH_TOKEN"
                ],
                "old-token",
            )
            settings.write_text(json.dumps({
                "env": {"CLAUDE_CODE_OAUTH_TOKEN": "rotated-token"},
            }), encoding="utf-8")
            self.store.ensure_claude_policy(record)

        payload = json.loads(policy.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"],
            "rotated-token",
        )
        if sys.platform != "win32":
            self.assertEqual(policy.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
