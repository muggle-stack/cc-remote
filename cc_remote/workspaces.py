"""Durable Work registry layered over native Claude and Codex sessions.

The engines remain the transcript authorities. This registry stores only the
cc-remote product identity and private working directory assigned to a Work
chat, so Work and Code can be listed and deleted independently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, cast
from uuid import uuid4

from cc_remote.claude_paths import claude_config_dir


Engine = Literal["claude", "codex"]

_MAX_WORK_ARTIFACTS = 200
_MAX_WORK_ARTIFACT_SCAN = 4096
_WORK_CONTEXT_MANIFEST = ".cc-remote-context.json"
WORK_UPLOADS_MARKER = ".cc-remote-upload-root"
WORK_UPLOADS_MARKER_PAYLOAD = b"cc-remote Work uploads v1\n"
_WORK_SCHEDULE_MAX_ATTEMPTS = 3
_CODEX_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_WORK_ARTIFACT_PREVIEW_SUFFIXES = frozenset({
    ".c", ".cc", ".conf", ".cpp", ".css", ".csv", ".go", ".h", ".hpp",
    ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsonl", ".log", ".md",
    ".mdown", ".markdown", ".mmd", ".mermaid", ".mjs", ".py", ".rs", ".sh", ".sql", ".svg",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
    ".avif", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".odp", ".ods",
    ".odt", ".pdf", ".png", ".ppt", ".pptx", ".rtf", ".webp", ".xls",
    ".xlsx",
})
_WORK_ARTIFACT_KIND_SUFFIXES = {
    "document": frozenset({".doc", ".docx", ".md", ".mmd", ".mermaid", ".odt", ".rtf", ".txt"}),
    "spreadsheet": frozenset({".csv", ".ods", ".xls", ".xlsx"}),
    "presentation": frozenset({".odp", ".ppt", ".pptx"}),
    "image": frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}),
    "pdf": frozenset({".pdf"}),
}

_REGISTRY_INIT_LOCKS_GUARD = threading.Lock()
_REGISTRY_INIT_LOCKS: dict[str, threading.Lock] = {}


def _registry_init_lock(path: Path) -> threading.Lock:
    key = os.path.realpath(path)
    with _REGISTRY_INIT_LOCKS_GUARD:
        lock = _REGISTRY_INIT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REGISTRY_INIT_LOCKS[key] = lock
        return lock

_MAX_CLAUDE_SETTINGS_BYTES = 1024 * 1024
_MAX_CLAUDE_SETTING_VALUE = 16 * 1024
_CLAUDE_RUNTIME_ENV_KEYS = frozenset({
    # Direct Anthropic and compatible endpoints.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    # Subscription-backed non-interactive Claude Code sessions may pin their
    # current OAuth credential in settings.json instead of the OS keychain.
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
    "ANTHROPIC_BETAS",
    "ANTHROPIC_CUSTOM_HEADERS",
    # Claude Code's supported cloud-provider selectors and credentials.
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_FOUNDRY_API_KEY",
})


def _bounded_setting(value: object, *, limit: int = _MAX_CLAUDE_SETTING_VALUE) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= limit else None


def _claude_runtime_settings(
    config_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Copy provider connectivity only; never import global Work context.

    The explicit Work policy is the sole Claude settings file used by the SDK.
    User hooks, permissions, plugins, skills and CLAUDE.md discovery therefore
    cannot cross from Code into Work, while a compatible endpoint configured in
    the user's settings remains usable.
    """
    root = (
        claude_config_dir()
        if config_dir is None else Path(config_dir).resolve(strict=False)
    )
    path = root / "settings.json"
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_CLAUDE_SETTINGS_BYTES:
            return {}
        raw = path.read_bytes()
    except OSError:
        return {}
    if len(raw) > _MAX_CLAUDE_SETTINGS_BYTES:
        return {}
    try:
        source = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(source, dict):
        return {}

    runtime: dict[str, object] = {}
    model = _bounded_setting(source.get("model"), limit=256)
    if model is not None:
        runtime["model"] = model
    source_env = source.get("env")
    if isinstance(source_env, dict):
        env = {
            key: value
            for key in sorted(_CLAUDE_RUNTIME_ENV_KEYS)
            if (value := _bounded_setting(source_env.get(key))) is not None
        }
        if env:
            runtime["env"] = env
    return runtime


@dataclass(frozen=True)
class WorkSessionRecord:
    work_id: str
    engine: Engine
    cwd: str
    session_id: str | None
    title: str | None
    project_id: str | None
    archived: bool
    context_baseline_tokens: int | None
    created_at: float
    updated_at: float
    # Native UUIDs are account-local. Keep the owning profile beside the id so
    # changing an engine's configured default cannot reclassify Work data.
    claude_profile_id: str | None = None
    codex_profile_id: str | None = None


@dataclass(frozen=True)
class WorkProject:
    project_id: str
    name: str
    description: str
    created_at: float
    updated_at: float


class WorkRegistry:
    """One engine's Work namespace below its provider-owned home directory."""

    def __init__(self, root: Path, engine: Engine):
        self.root = Path(os.path.realpath(os.path.expanduser(str(root))))
        self.engine = engine
        self.chats_root = self.root / "chats"
        self.db_path = self.root / "registry.sqlite3"

    def initialize(self) -> None:
        # CREATE/PRAGMA/ALTER are individually safe, but concurrent first-use of
        # the same legacy database can still race before SQLite's busy timeout is
        # established.  Serialize the complete one-time schema path per DB.
        with _registry_init_lock(self.db_path):
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        self._mkdir_private(self.root)
        self._mkdir_private(self.chats_root)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS work_sessions (
                    work_id TEXT PRIMARY KEY,
                    engine TEXT NOT NULL CHECK (engine IN ('claude', 'codex')),
                    session_id TEXT,
                    cwd TEXT NOT NULL UNIQUE,
                    title TEXT,
                    project_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    context_baseline_tokens INTEGER,
                    claude_profile_id TEXT,
                    codex_profile_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_sessions_updated
                    ON work_sessions(updated_at DESC);
                CREATE INDEX IF NOT EXISTS work_sessions_project
                    ON work_sessions(project_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS work_projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_sources (
                    source_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES work_projects(project_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('file', 'link', 'note')),
                    title TEXT NOT NULL,
                    uri TEXT,
                    stored_path TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_sources_project
                    ON work_sources(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS work_plugins (
                    plugin_id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES work_projects(project_id)
                        ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES work_projects(project_id)
                        ON DELETE SET NULL,
                    claude_profile_id TEXT,
                    codex_profile_id TEXT,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    next_run_at REAL NOT NULL,
                    repeat_seconds INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at REAL,
                    last_session_id TEXT,
                    last_claude_profile_id TEXT,
                    last_codex_profile_id TEXT,
                    last_error TEXT,
                    deleted_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS work_schedules_due
                    ON work_schedules(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS work_schedule_runs (
                    run_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL REFERENCES work_schedules(schedule_id)
                        ON DELETE CASCADE,
                    scheduled_for REAL NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'claimed', 'running', 'succeeded', 'failed')),
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    claude_profile_id TEXT,
                    codex_profile_id TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(schedule_id, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS work_schedule_runs_ready
                    ON work_schedule_runs(status, available_at, scheduled_for);
                CREATE TABLE IF NOT EXISTS work_profile_migrations (
                    revision INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_claude_profile_migrations (
                    revision INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );
                """
            )
            # Existing self-hosted registries predate Work's split between
            # fixed engine overhead and user conversation context. initialize()
            # is called from several request paths and may race during the first
            # process after an upgrade; serialize the inspect-and-alter pair so
            # two threads cannot both decide the column is absent.
            db.execute("BEGIN IMMEDIATE")
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(work_sessions)")
            }
            if "context_baseline_tokens" not in columns:
                db.execute(
                    "ALTER TABLE work_sessions "
                    "ADD COLUMN context_baseline_tokens INTEGER"
                )
            if "codex_profile_id" not in columns:
                db.execute(
                    "ALTER TABLE work_sessions ADD COLUMN codex_profile_id TEXT"
                )
            if "claude_profile_id" not in columns:
                db.execute(
                    "ALTER TABLE work_sessions ADD COLUMN claude_profile_id TEXT"
                )
            table_sql_row = db.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'table' AND name = 'work_sessions'"""
            ).fetchone()
            table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
            if re.search(r"session_id\s+TEXT\s+UNIQUE", table_sql, re.IGNORECASE):
                # The pre-profile schema made a native UUID globally unique.
                # Rebuild once so two CODEX_HOMEs may legitimately own the same
                # native UUID while Claude/null-profile rows stay unique.
                db.execute(
                    """CREATE TABLE work_sessions_profiled (
                        work_id TEXT PRIMARY KEY,
                        engine TEXT NOT NULL CHECK (engine IN ('claude', 'codex')),
                        session_id TEXT,
                        cwd TEXT NOT NULL UNIQUE,
                        title TEXT,
                        project_id TEXT,
                        archived INTEGER NOT NULL DEFAULT 0,
                        context_baseline_tokens INTEGER,
                        claude_profile_id TEXT,
                        codex_profile_id TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )"""
                )
                db.execute(
                    """INSERT INTO work_sessions_profiled (
                        work_id, engine, session_id, cwd, title, project_id,
                        archived, context_baseline_tokens, claude_profile_id,
                        codex_profile_id,
                        created_at, updated_at
                    ) SELECT
                        work_id, engine, session_id, cwd, title, project_id,
                        archived, context_baseline_tokens, claude_profile_id,
                        codex_profile_id,
                        created_at, updated_at
                    FROM work_sessions"""
                )
                db.execute("DROP TABLE work_sessions")
                db.execute(
                    "ALTER TABLE work_sessions_profiled RENAME TO work_sessions")
                db.execute(
                    "CREATE INDEX work_sessions_updated "
                    "ON work_sessions(updated_at DESC)")
                db.execute(
                    "CREATE INDEX work_sessions_project "
                    "ON work_sessions(project_id, updated_at DESC)")
            identity_index = db.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'index' AND name = 'work_sessions_identity'"""
            ).fetchone()
            identity_sql = (
                str(identity_index["sql"] or "")
                if identity_index is not None else ""
            )
            if "claude_profile_id" not in identity_sql.lower():
                db.execute("DROP INDEX IF EXISTS work_sessions_identity")
                db.execute(
                    """CREATE UNIQUE INDEX work_sessions_identity
                       ON work_sessions(
                           engine, IFNULL(claude_profile_id, ''),
                           IFNULL(codex_profile_id, ''), session_id
                       ) WHERE session_id IS NOT NULL
                        """
                )
            schedule_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(work_schedules)")
            }
            if "deleted_at" not in schedule_columns:
                db.execute(
                    "ALTER TABLE work_schedules ADD COLUMN deleted_at REAL"
                )
            if "codex_profile_id" not in schedule_columns:
                db.execute(
                    "ALTER TABLE work_schedules "
                    "ADD COLUMN codex_profile_id TEXT"
                )
            if "claude_profile_id" not in schedule_columns:
                db.execute(
                    "ALTER TABLE work_schedules "
                    "ADD COLUMN claude_profile_id TEXT"
                )
            if "last_codex_profile_id" not in schedule_columns:
                db.execute(
                    "ALTER TABLE work_schedules "
                    "ADD COLUMN last_codex_profile_id TEXT"
                )
            if "last_claude_profile_id" not in schedule_columns:
                db.execute(
                    "ALTER TABLE work_schedules "
                    "ADD COLUMN last_claude_profile_id TEXT"
                )
            run_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(work_schedule_runs)")
            }
            if "codex_profile_id" not in run_columns:
                db.execute(
                    "ALTER TABLE work_schedule_runs "
                    "ADD COLUMN codex_profile_id TEXT"
                )
            if "claude_profile_id" not in run_columns:
                db.execute(
                    "ALTER TABLE work_schedule_runs "
                    "ADD COLUMN claude_profile_id TEXT"
                )
        self._chmod_file(self.db_path)

    def create_session(
        self,
        project_id: str | None = None,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> WorkSessionRecord:
        self.initialize()
        claude_profile_id = self._profile_id(claude_profile_id)
        codex_profile_id = self._profile_id(codex_profile_id)
        if self.engine != "claude" and claude_profile_id is not None:
            raise ValueError("Codex Work cannot carry a Claude profile")
        if self.engine != "codex" and codex_profile_id is not None:
            raise ValueError("Claude Work cannot carry a Codex profile")
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        now = time.time()
        work_id = f"work-{uuid4().hex}"
        chat_root = self.chats_root / work_id
        workspace = chat_root / "workspace"
        for directory in (
            chat_root, workspace, workspace / "uploads",
        ):
            self._mkdir_private(directory)
        self._atomic_write_private(
            workspace / "uploads" / WORK_UPLOADS_MARKER,
            WORK_UPLOADS_MARKER_PAYLOAD,
        )
        cwd = str(workspace)
        with self._connect() as db:
            db.execute(
                """INSERT INTO work_sessions
                   (work_id, engine, session_id, cwd, project_id,
                    claude_profile_id, codex_profile_id, created_at, updated_at)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                (work_id, self.engine, cwd, project_id,
                 claude_profile_id, codex_profile_id, now, now),
            )
        record = WorkSessionRecord(
            work_id=work_id, engine=self.engine, cwd=cwd, session_id=None,
            title=None, project_id=project_id, archived=False,
            context_baseline_tokens=None,
            created_at=now, updated_at=now,
            claude_profile_id=claude_profile_id,
            codex_profile_id=codex_profile_id,
        )
        try:
            self._materialize_project(record)
        except Exception:
            self.abandon(work_id)
            raise
        return record

    def dashboard(self) -> dict[str, list[dict[str, object]]]:
        self.initialize()
        with self._connect() as db:
            projects = [dict(row) for row in db.execute(
                "SELECT * FROM work_projects ORDER BY updated_at DESC").fetchall()]
            sources = [dict(row) for row in db.execute(
                "SELECT source_id, project_id, kind, title, uri, created_at "
                "FROM work_sources ORDER BY created_at DESC").fetchall()]
            plugins = [dict(row) for row in db.execute(
                "SELECT * FROM work_plugins ORDER BY updated_at DESC").fetchall()]
            schedules = [dict(row) for row in db.execute(
                "SELECT * FROM work_schedules WHERE deleted_at IS NULL "
                "ORDER BY next_run_at ASC").fetchall()]
            latest_runs = {
                row["schedule_id"]: dict(row)
                for row in db.execute(
                    """SELECT r.* FROM work_schedule_runs r
                       JOIN (
                         SELECT schedule_id, MAX(created_at) AS created_at
                         FROM work_schedule_runs GROUP BY schedule_id
                       ) latest ON latest.schedule_id = r.schedule_id
                         AND latest.created_at = r.created_at"""
                ).fetchall()
            }
        for plugin in plugins:
            plugin["enabled"] = bool(plugin["enabled"])
        for schedule in schedules:
            schedule["enabled"] = bool(schedule["enabled"])
            latest = latest_runs.get(schedule["schedule_id"])
            schedule["last_run_id"] = latest["run_id"] if latest else None
            schedule["last_run_status"] = latest["status"] if latest else None
            schedule["last_run_attempt"] = latest["attempt"] if latest else None
        return {
            "projects": projects, "sources": sources,
            "plugins": plugins, "schedules": schedules,
        }

    def create_project(self, name: str, description: str = "") -> str:
        self.initialize()
        project_id = f"project-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO work_projects VALUES (?, ?, ?, ?, ?)",
                (project_id, name, description, now, now),
            )
        return project_id

    def get_project(self, project_id: str) -> WorkProject | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM work_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkProject(
            project_id=row["project_id"], name=row["name"],
            description=row["description"], created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def artifacts(
        self,
        session_id: str,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> list[dict[str, object]]:
        """List bounded, user-visible deliverables from one private Work cwd.

        Project context and copied source material are inputs, not artifacts.
        Symlinks and hidden paths are ignored so enumeration never escapes or
        exposes implementation state from the wrapper-owned workspace.
        """
        record = self.get_by_session(
            session_id,
            claude_profile_id=claude_profile_id,
            codex_profile_id=codex_profile_id)
        if record is None or not self.contains_cwd(record.cwd):
            raise LookupError(f"unknown Work session: {session_id}")
        root = os.path.realpath(record.cwd)
        managed_uploads = self._is_managed_upload_root(
            Path(root) / "uploads")
        artifacts: list[dict[str, object]] = []
        scanned = 0
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            relative_dir = os.path.relpath(current, root)
            if relative_dir == ".":
                dirs[:] = [name for name in dirs
                           if not name.startswith(".")
                           and name != "资料库"
                           and not (name == "uploads" and managed_uploads)]
            else:
                dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in files:
                scanned += 1
                if scanned > _MAX_WORK_ARTIFACT_SCAN:
                    break
                if name.startswith("."):
                    continue
                path = Path(current) / name
                relative = os.path.relpath(path, root)
                if relative == "WORK.md" or relative.startswith(f"资料库{os.sep}"):
                    continue
                try:
                    info = path.lstat()
                    resolved = os.path.realpath(path)
                    if (not stat.S_ISREG(info.st_mode)
                            or os.path.commonpath((root, resolved)) != root):
                        continue
                except (OSError, ValueError):
                    continue
                suffix = path.suffix.lower()
                kind = next((candidate for candidate, suffixes
                             in _WORK_ARTIFACT_KIND_SUFFIXES.items()
                             if suffix in suffixes), "file")
                artifacts.append({
                    "path": relative.replace(os.sep, "/"),
                    "size": info.st_size,
                    "modified_at": info.st_mtime,
                    "kind": kind,
                    "previewable": suffix in _WORK_ARTIFACT_PREVIEW_SUFFIXES,
                })
            if scanned > _MAX_WORK_ARTIFACT_SCAN:
                break
        artifacts.sort(key=lambda item: (-float(item["modified_at"]), str(item["path"])))
        return artifacts[:_MAX_WORK_ARTIFACTS]

    def delete_project(self, project_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            work_ids = [row["work_id"] for row in db.execute(
                "SELECT work_id FROM work_sessions WHERE project_id = ?",
                (project_id,),
            ).fetchall()]
            paths = [row["stored_path"] for row in db.execute(
                "SELECT stored_path FROM work_sources WHERE project_id = ? "
                "AND stored_path IS NOT NULL", (project_id,)).fetchall()]
            db.execute(
                "UPDATE work_sessions SET project_id = NULL WHERE project_id = ?",
                (project_id,),
            )
            changed = db.execute(
                "DELETE FROM work_projects WHERE project_id = ?", (project_id,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work project: {project_id}")
        for work_id in work_ids:
            record = self.get_by_work_id(work_id)
            if record is not None:
                self._materialize_project(record)
        for stored_path in paths:
            self._remove_owned_library_file(stored_path)

    def add_source(self, project_id: str, kind: str, title: str,
                   uri: str | None = None, filename: str | None = None,
                   content: bytes | None = None) -> str:
        self.initialize()
        if self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        if kind not in {"file", "link", "note"}:
            raise ValueError("unsupported Work source kind")
        source_id = f"source-{uuid4().hex}"
        stored_path = None
        if kind == "file" or (kind == "link" and content is not None):
            if content is None or not filename:
                raise ValueError("stored source requires content and filename")
            safe_name = Path(filename).name.strip()
            if not safe_name or safe_name in {".", ".."}:
                raise ValueError("invalid source filename")
            directory = self.root / "library" / source_id
            self._mkdir_private(directory)
            path = directory / safe_name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            stored_path = str(path)
        now = time.time()
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO work_sources
                       (source_id, project_id, kind, title, uri, stored_path, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source_id, project_id, kind, title, uri, stored_path, now),
                )
                db.execute(
                    "UPDATE work_projects SET updated_at = ? WHERE project_id = ?",
                    (now, project_id),
                )
        except Exception:
            if stored_path:
                self._remove_owned_library_file(stored_path)
            raise
        self.sync_project_sessions(project_id)
        return source_id

    def delete_source(self, source_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT project_id, stored_path FROM work_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            changed = db.execute(
                "DELETE FROM work_sources WHERE source_id = ?", (source_id,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work source: {source_id}")
        assert row is not None
        self.sync_project_sessions(row["project_id"])
        if row and row["stored_path"]:
            self._remove_owned_library_file(row["stored_path"])

    def create_plugin(self, name: str, instructions: str,
                      project_id: str | None = None) -> str:
        self.initialize()
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        plugin_id = f"plugin-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT INTO work_plugins VALUES (?, ?, ?, ?, 1, ?, ?)",
                (plugin_id, project_id, name, instructions, now, now),
            )
        if project_id is None:
            self.sync_all_sessions()
        else:
            self.sync_project_sessions(project_id)
        return plugin_id

    def delete_plugin(self, plugin_id: str) -> None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT project_id FROM work_plugins WHERE plugin_id = ?",
                (plugin_id,),
            ).fetchone()
            changed = db.execute(
                "DELETE FROM work_plugins WHERE plugin_id = ?", (plugin_id,),
            ).rowcount
        if changed != 1 or row is None:
            raise LookupError(f"unknown Work item: {plugin_id}")
        if row["project_id"] is None:
            self.sync_all_sessions()
        else:
            self.sync_project_sessions(row["project_id"])

    def create_schedule(self, title: str, prompt: str, next_run_at: float,
                        repeat_seconds: int | None = None,
                        project_id: str | None = None, *,
                        claude_profile_id: str | None = None,
                        codex_profile_id: str | None = None) -> str:
        self.initialize()
        claude_profile_id = self._profile_id(claude_profile_id)
        codex_profile_id = self._profile_id(codex_profile_id)
        if self.engine != "claude" and claude_profile_id is not None:
            raise ValueError("Codex Work cannot carry a Claude profile")
        if self.engine == "codex" and codex_profile_id is None:
            raise ValueError("Codex Work schedule profile is required")
        if self.engine != "codex" and codex_profile_id is not None:
            raise ValueError("Claude Work cannot carry a Codex profile")
        if project_id and self.get_project(project_id) is None:
            raise LookupError(f"unknown Work project: {project_id}")
        schedule_id = f"schedule-{uuid4().hex}"
        now = time.time()
        with self._connect() as db:
            db.execute(
                """INSERT INTO work_schedules
                   (schedule_id, project_id, claude_profile_id,
                    codex_profile_id,
                    title, prompt, next_run_at,
                    repeat_seconds, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (schedule_id, project_id, claude_profile_id, codex_profile_id,
                 title, prompt, next_run_at,
                 repeat_seconds, now, now),
            )
        return schedule_id

    def delete_schedule(self, schedule_id: str) -> None:
        """Delete an idle schedule or tombstone one whose run is executing.

        Running workers must retain their parent row until they record their
        terminal result because the run table intentionally has a cascading
        foreign key. Queued/claimed work has not begun and can be cancelled
        immediately. The terminal writer removes the tombstone atomically.
        """
        self.initialize()
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT 1 FROM work_schedules WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown Work item: {schedule_id}")
            db.execute(
                "UPDATE work_schedules SET enabled = 0, deleted_at = ?, "
                "updated_at = ? WHERE schedule_id = ?",
                (now, now, schedule_id),
            )
            db.execute(
                "DELETE FROM work_schedule_runs WHERE schedule_id = ? "
                "AND status IN ('queued', 'claimed')",
                (schedule_id,),
            )
            running = db.execute(
                "SELECT 1 FROM work_schedule_runs WHERE schedule_id = ? "
                "AND status = 'running' LIMIT 1",
                (schedule_id,),
            ).fetchone()
            if running is None:
                db.execute(
                    "DELETE FROM work_schedules WHERE schedule_id = ?",
                    (schedule_id,),
                )

    def claim_due_schedules(
        self, now: float, limit: int = 8, lease_seconds: float = 90.0,
    ) -> list[dict[str, object]]:
        """Persist due occurrences, recover expired leases, then claim work.

        Advancing ``next_run_at`` is safe only after a unique run row exists.
        Crashes therefore leave a recoverable queued/leased occurrence instead
        of silently losing a one-shot task.
        """
        self.initialize()
        claimed: list[dict[str, object]] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE work_schedule_runs
                   SET status = 'failed', lease_until = NULL,
                       last_error = COALESCE(last_error, '执行进程多次中断'),
                       updated_at = ?
                   WHERE status IN ('claimed', 'running')
                     AND lease_until <= ? AND attempt >= ?""",
                (now, now, _WORK_SCHEDULE_MAX_ATTEMPTS),
            )
            db.execute(
                """UPDATE work_schedule_runs
                   SET status = 'queued', lease_until = NULL,
                       available_at = ?, updated_at = ?
                   WHERE status IN ('claimed', 'running')
                     AND lease_until <= ? AND attempt < ?""",
                (now, now, now, _WORK_SCHEDULE_MAX_ATTEMPTS),
            )
            rows = db.execute(
                """SELECT * FROM work_schedules
                   WHERE enabled = 1 AND deleted_at IS NULL AND next_run_at <= ?
                   ORDER BY next_run_at ASC LIMIT ?""", (now, limit),
            ).fetchall()
            for row in rows:
                active = db.execute(
                    """SELECT 1 FROM work_schedule_runs
                       WHERE schedule_id = ?
                         AND status IN ('queued', 'claimed', 'running') LIMIT 1""",
                    (row["schedule_id"],),
                ).fetchone()
                if active is not None:
                    continue
                scheduled_for = float(row["next_run_at"])
                run_id = f"run-{uuid4().hex}"
                db.execute(
                    """INSERT OR IGNORE INTO work_schedule_runs
                       (run_id, schedule_id, scheduled_for, status, available_at,
                        attempt, claude_profile_id, codex_profile_id,
                        created_at, updated_at)
                       VALUES (?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)""",
                    (run_id, row["schedule_id"], scheduled_for, now,
                     row["claude_profile_id"], row["codex_profile_id"],
                     now, now),
                )
                repeat = row["repeat_seconds"]
                enabled = 1 if repeat else 0
                if repeat:
                    repeat_value = int(repeat)
                    skipped = max(1, int((now - scheduled_for) // repeat_value) + 1)
                    next_run = scheduled_for + skipped * repeat_value
                else:
                    next_run = scheduled_for
                db.execute(
                    """UPDATE work_schedules SET enabled = ?, next_run_at = ?,
                       last_error = NULL, updated_at = ?
                       WHERE schedule_id = ?""",
                    (enabled, next_run, now, row["schedule_id"]),
                )
            ready = db.execute(
                """SELECT r.*, s.project_id, s.title, s.prompt, s.repeat_seconds
                   FROM work_schedule_runs r
                   JOIN work_schedules s ON s.schedule_id = r.schedule_id
                   WHERE r.status = 'queued' AND r.available_at <= ?
                     AND s.deleted_at IS NULL
                   ORDER BY r.scheduled_for ASC LIMIT ?""",
                (now, limit),
            ).fetchall()
            for row in ready:
                changed = db.execute(
                    """UPDATE work_schedule_runs
                       SET status = 'claimed', lease_until = ?, attempt = attempt + 1,
                           updated_at = ?
                       WHERE run_id = ? AND status = 'queued'""",
                    (now + lease_seconds, now, row["run_id"]),
                ).rowcount
                if changed == 1:
                    item = dict(row)
                    item["attempt"] = int(item["attempt"]) + 1
                    item["lease_until"] = now + lease_seconds
                    claimed.append(item)
        return claimed

    def mark_schedule_running(
        self, run_id: str, now: float, lease_seconds: float = 90.0,
    ) -> bool:
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_schedule_runs SET status = 'running',
                   lease_until = ?, updated_at = ?
                   WHERE run_id = ? AND status = 'claimed'""",
                (now + lease_seconds, now, run_id),
            ).rowcount
        return changed == 1

    def renew_schedule_run(
        self, run_id: str, now: float, lease_seconds: float = 90.0,
    ) -> bool:
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_schedule_runs SET lease_until = ?, updated_at = ?
                   WHERE run_id = ? AND status IN ('claimed', 'running')""",
                (now + lease_seconds, now, run_id),
            ).rowcount
        return changed == 1

    def complete_schedule(
        self, run_id: str, session_id: str | None, error: str | None,
        now: float | None = None,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
        retryable: bool = True,
    ) -> str:
        """Finish a run or place it back on the durable retry queue."""
        self.initialize()
        completed_at = time.time() if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT r.schedule_id, r.attempt, r.claude_profile_id,
                          r.codex_profile_id,
                          s.deleted_at
                   FROM work_schedule_runs r
                   JOIN work_schedules s ON s.schedule_id = r.schedule_id
                   WHERE r.run_id = ?""",
                (run_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown Work schedule run: {run_id}")
            requested_profile_id = self._profile_id(
                claude_profile_id
                if self.engine == "claude" else codex_profile_id)
            stored_profile_id = self._profile_id(
                row["claude_profile_id"]
                if self.engine == "claude" else row["codex_profile_id"])
            if (
                requested_profile_id is not None
                and stored_profile_id is not None
                and requested_profile_id != stored_profile_id
            ):
                raise ValueError("Work schedule run profile cannot change")
            effective_profile_id = stored_profile_id or requested_profile_id
            deleting = row["deleted_at"] is not None
            if error is None:
                status = "succeeded"
                available_at = completed_at
            elif (retryable and not deleting
                  and int(row["attempt"]) < _WORK_SCHEDULE_MAX_ATTEMPTS):
                status = "queued"
                available_at = completed_at + min(
                    300, 15 * (2 ** max(0, int(row["attempt"]) - 1)))
            else:
                status = "failed"
                available_at = completed_at
            db.execute(
                """UPDATE work_schedule_runs SET status = ?, available_at = ?,
                   lease_until = NULL, session_id = ?,
                   claude_profile_id = ?, codex_profile_id = ?,
                   last_error = ?, updated_at = ?
                   WHERE run_id = ?""",
                (status, available_at, session_id,
                 effective_profile_id if self.engine == "claude" else None,
                 effective_profile_id if self.engine == "codex" else None,
                 error, completed_at, run_id),
            )
            db.execute(
                """UPDATE work_schedules SET last_run_at = ?, last_session_id = ?,
                   last_claude_profile_id = ?, last_codex_profile_id = ?,
                   last_error = ?, updated_at = ?
                   WHERE schedule_id = ?""",
                (completed_at, session_id,
                 effective_profile_id if self.engine == "claude" else None,
                 effective_profile_id if self.engine == "codex" else None,
                 error,
                 completed_at, row["schedule_id"]),
            )
            if deleting:
                db.execute(
                    "DELETE FROM work_schedules WHERE schedule_id = ?",
                    (row["schedule_id"],),
                )
        return status

    def _delete_row(self, table: str, key: str, value: str) -> None:
        if (table, key) not in {
            ("work_plugins", "plugin_id"),
            ("work_schedules", "schedule_id"),
        }:
            raise ValueError("unsupported Work table")
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                f"DELETE FROM {table} WHERE {key} = ?", (value,),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work item: {value}")

    def sync_session(self, session_id: str) -> None:
        record = self.get_by_session(session_id)
        if record is None:
            raise LookupError(f"unknown Work session: {session_id}")
        self._materialize_project(record)

    def sync_work_id(self, work_id: str) -> None:
        record = self.get_by_work_id(work_id)
        if record is None:
            raise LookupError(f"unknown Work session: {work_id}")
        self._materialize_project(record)

    def sync_project_sessions(self, project_id: str) -> None:
        """Refresh wrapper-owned context for every live chat in one project."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM work_sessions WHERE engine = ? AND project_id = ?",
                (self.engine, project_id),
            ).fetchall()
        for row in rows:
            record = self._record(row)
            if record is not None:
                self._materialize_project(record)

    def sync_all_sessions(self) -> None:
        """Refresh global templates without crossing the provider namespace."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM work_sessions WHERE engine = ?", (self.engine,),
            ).fetchall()
        for row in rows:
            record = self._record(row)
            if record is not None:
                self._materialize_project(record)

    def _materialize_project(self, record: WorkSessionRecord) -> None:
        project = (self.get_project(record.project_id)
                   if record.project_id else None)
        if record.project_id and project is None:
            raise LookupError(f"unknown Work project: {record.project_id}")
        with self._connect() as db:
            sources = (db.execute(
                "SELECT * FROM work_sources WHERE project_id = ? ORDER BY created_at",
                (record.project_id,),
            ).fetchall() if record.project_id else [])
            plugins = (db.execute(
                """SELECT * FROM work_plugins
                   WHERE enabled = 1 AND (project_id IS NULL OR project_id = ?)
                   ORDER BY created_at""", (record.project_id,),
            ).fetchall() if record.project_id else db.execute(
                """SELECT * FROM work_plugins
                   WHERE enabled = 1 AND project_id IS NULL ORDER BY created_at"""
            ).fetchall())
        workspace = Path(record.cwd)
        library = workspace / "资料库"
        manifest_path = workspace / _WORK_CONTEXT_MANIFEST
        previous_files = self._read_context_manifest(manifest_path, workspace)
        managed_paths = set(previous_files.values())
        lines = ([f"# {project.name}", "", project.description.strip(), ""]
                 if project else ["# Work 工作上下文", ""])
        if sources:
            lines.extend(["## 资料库", ""])
        used_names: set[str] = set()
        current_files: dict[str, str] = {}
        for source in sources:
            if source["stored_path"] and source["kind"] in {"file", "link"}:
                self._mkdir_private(library)
                base = Path(source["stored_path"]).name
                previous = previous_files.get(source["source_id"])
                candidate = (Path(previous).name if previous else base)
                index = 2
                while candidate in used_names or self._context_name_conflicts(
                    library / candidate, workspace, managed_paths,
                    Path(source["stored_path"]),
                ):
                    candidate = f"{Path(base).stem}-{index}{Path(base).suffix}"
                    index += 1
                used_names.add(candidate)
                target = library / candidate
                self._replace_private_file(Path(source["stored_path"]), target)
                relative = str(target.relative_to(workspace)).replace(os.sep, "/")
                current_files[source["source_id"]] = relative
                if source["kind"] == "link":
                    lines.append(
                        f"- {source['title']}: `资料库/{candidate}`（原链接：{source['uri'] or ''}）")
                else:
                    lines.append(f"- {source['title']}: `资料库/{candidate}`")
            elif source["kind"] == "link":
                lines.append(f"- {source['title']}: {source['uri'] or ''}")
            else:
                lines.append(f"- {source['title']}: {source['uri'] or ''}")
        if plugins:
            lines.extend(["", "## 已启用工作模板", ""])
            for plugin in plugins:
                lines.extend([f"### {plugin['name']}", plugin["instructions"], ""])
        context = workspace / "WORK.md"
        if project is not None or plugins:
            self._atomic_write_private(
                context, ("\n".join(lines).strip() + "\n").encode("utf-8"))
        else:
            try:
                context.unlink()
            except FileNotFoundError:
                pass

        for relative in managed_paths - set(current_files.values()):
            stale = self._managed_context_path(workspace, relative)
            if stale is not None:
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass
        if current_files:
            self._atomic_write_private(
                manifest_path,
                json.dumps({"version": 1, "files": current_files},
                           ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            )
        else:
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
        try:
            library.rmdir()
        except (FileNotFoundError, OSError):
            pass

    @staticmethod
    def _managed_context_path(workspace: Path, relative: str) -> Path | None:
        path = workspace / relative
        try:
            resolved_workspace = os.path.realpath(workspace)
            resolved = os.path.realpath(path)
            if (os.path.commonpath((resolved_workspace, resolved)) != resolved_workspace
                    or not relative.replace("\\", "/").startswith("资料库/")):
                return None
        except (OSError, ValueError):
            return None
        return path

    def _read_context_manifest(
        self, path: Path, workspace: Path,
    ) -> dict[str, str]:
        try:
            raw = path.read_bytes()
            if len(raw) > 256 * 1024:
                return {}
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        files = value.get("files") if isinstance(value, dict) else None
        if not isinstance(files, dict):
            return {}
        valid: dict[str, str] = {}
        for source_id, relative in files.items():
            if (isinstance(source_id, str) and isinstance(relative, str)
                    and self._managed_context_path(workspace, relative) is not None):
                valid[source_id] = relative
        return valid

    @staticmethod
    def _context_name_conflicts(
        target: Path, workspace: Path, managed_paths: set[str], source: Path,
    ) -> bool:
        if not target.exists():
            return False
        relative = str(target.relative_to(workspace)).replace(os.sep, "/")
        if relative in managed_paths:
            return False
        try:
            return target.read_bytes() != source.read_bytes()
        except OSError:
            return True

    @staticmethod
    def _replace_private_file(source: Path, target: Path) -> None:
        tmp = target.with_name(f".{target.name}.sync-{uuid4().hex}")
        try:
            shutil.copyfile(source, tmp)
            tmp.chmod(0o600)
            os.replace(tmp, target)
            target.chmod(0o600)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_write_private(path: Path, payload: bytes) -> None:
        tmp = path.with_name(f".{path.name}.sync-{uuid4().hex}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            path.chmod(0o600)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _remove_owned_library_file(self, stored_path: str) -> None:
        library = self.root / "library"
        path = Path(stored_path)
        try:
            if os.path.commonpath((os.path.realpath(path), os.path.realpath(library))) \
                    != os.path.realpath(library):
                raise RuntimeError("refusing to remove a source outside Work library")
        except ValueError as exc:
            raise RuntimeError("invalid Work source path") from exc
        directory = path.parent
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass

    def bind_session(
        self,
        work_id: str,
        session_id: str,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> None:
        claude_profile_id = self._profile_id(claude_profile_id)
        codex_profile_id = self._profile_id(codex_profile_id)
        if self.engine != "claude" and claude_profile_id is not None:
            raise ValueError("Codex Work cannot carry a Claude profile")
        if self.engine != "codex" and codex_profile_id is not None:
            raise ValueError("Claude Work cannot carry a Codex profile")
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_sessions SET session_id = ?,
                   claude_profile_id = COALESCE(claude_profile_id, ?),
                   codex_profile_id = COALESCE(codex_profile_id, ?), updated_at = ?
                   WHERE work_id = ? AND engine = ?
                     AND (claude_profile_id IS NULL OR claude_profile_id IS ?)
                     AND (codex_profile_id IS NULL OR codex_profile_id IS ?)""",
                (session_id, claude_profile_id, codex_profile_id, time.time(),
                 work_id, self.engine, claude_profile_id, codex_profile_id),
            ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work session: {work_id}")

    def set_context_baseline(self, work_id: str, tokens: int) -> int:
        """Persist the fresh session's startup context measurement once."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("invalid Work context baseline")
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_sessions
                   SET context_baseline_tokens = COALESCE(context_baseline_tokens, ?)
                   WHERE work_id = ? AND engine = ?""",
                (tokens, work_id, self.engine),
            ).rowcount
            if changed != 1:
                raise LookupError(f"unknown Work session: {work_id}")
            row = db.execute(
                """SELECT context_baseline_tokens FROM work_sessions
                   WHERE work_id = ? AND engine = ?""",
                (work_id, self.engine),
            ).fetchone()
        value = row["context_baseline_tokens"] if row is not None else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("invalid persisted Work context baseline")
        return value

    def ensure_claude_policy(
        self,
        record: WorkSessionRecord,
        *,
        claude_config_dir: str | os.PathLike[str] | None = None,
    ) -> str:
        """Write the wrapper-owned fail-closed Claude sandbox configuration."""
        if self.engine != "claude" or not self.contains_cwd(record.cwd):
            raise ValueError("Claude Work policy requested for an invalid workspace")
        policy_dir = self.root / "policies"
        self._mkdir_private(policy_dir)
        path = policy_dir / f"{record.work_id}.json"
        payload = {
            **_claude_runtime_settings(claude_config_dir),
            "permissions": {"defaultMode": "acceptEdits"},
            "sandbox": {
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "failIfUnavailable": True,
                "filesystem": {
                    "denyRead": ["~/"],
                    "allowRead": [record.cwd],
                    "denyWrite": ["~/"],
                    "allowWrite": [record.cwd],
                },
            },
        }
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            self._chmod_file(path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return str(path)

    def assign_legacy_claude_profile(self, profile_id: str) -> int:
        """Freeze pre-profile Claude Work ownership to the legacy account."""
        if self.engine != "claude":
            return 0
        profile_id = self._profile_id(profile_id)
        if profile_id is None:
            raise ValueError("Claude Work profile is required")
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_sessions SET claude_profile_id = ?, updated_at = ?
                   WHERE engine = 'claude' AND claude_profile_id IS NULL""",
                (profile_id, time.time()),
            ).rowcount
            db.execute(
                """UPDATE work_schedules SET claude_profile_id = ?
                   WHERE claude_profile_id IS NULL""",
                (profile_id,),
            )
            db.execute(
                """UPDATE work_schedules SET last_claude_profile_id = ?
                   WHERE last_session_id IS NOT NULL
                     AND last_claude_profile_id IS NULL""",
                (profile_id,),
            )
            db.execute(
                """UPDATE work_schedule_runs
                   SET claude_profile_id = COALESCE(
                       (SELECT s.claude_profile_id FROM work_schedules s
                        WHERE s.schedule_id = work_schedule_runs.schedule_id),
                       ?)
                   WHERE claude_profile_id IS NULL""",
                (profile_id,),
            )
        return changed

    def migrate_claude_profiles(
        self,
        remaps: dict[str, str],
        *,
        legacy_profile_id: str,
        profile_revision: int,
    ) -> int:
        """Atomically apply one Claude profile topology revision to Work."""
        if self.engine != "claude":
            return 0
        legacy_profile_id = self._profile_id(legacy_profile_id)
        if legacy_profile_id is None:
            raise ValueError("Claude Work profile is required")
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ValueError("Claude Work profile revision is invalid")
        normalized = {
            self._profile_id(old): self._profile_id(new)
            for old, new in remaps.items()
        }
        self.initialize()
        changed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            applied = db.execute(
                "SELECT 1 FROM work_claude_profile_migrations "
                "WHERE revision = ?",
                (profile_revision,),
            ).fetchone()
            if applied is not None:
                return 0
            staged: list[tuple[str, str]] = []
            occupied = set(normalized) | set(normalized.values())
            for index, (old_id, new_id) in enumerate(normalized.items()):
                if old_id is None or new_id is None or old_id == new_id:
                    continue
                temporary = (
                    f"ccremote_claude_profile_migration_"
                    f"{profile_revision}_{index}"
                )
                while temporary in occupied:
                    temporary += "_"
                occupied.add(temporary)
                changed += db.execute(
                    """UPDATE work_sessions
                       SET claude_profile_id = ?, updated_at = ?
                       WHERE engine = 'claude' AND claude_profile_id = ?""",
                    (temporary, time.time(), old_id),
                ).rowcount
                db.execute(
                    """UPDATE work_schedules SET claude_profile_id = ?
                       WHERE claude_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedules SET last_claude_profile_id = ?
                       WHERE last_claude_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET claude_profile_id = ?
                       WHERE claude_profile_id = ?""",
                    (temporary, old_id),
                )
                staged.append((temporary, new_id))
            for temporary, new_id in staged:
                db.execute(
                    """UPDATE work_sessions
                       SET claude_profile_id = ?, updated_at = ?
                       WHERE engine = 'claude' AND claude_profile_id = ?""",
                    (new_id, time.time(), temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET claude_profile_id = ?
                       WHERE claude_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET last_claude_profile_id = ?
                       WHERE last_claude_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET claude_profile_id = ?
                       WHERE claude_profile_id = ?""",
                    (new_id, temporary),
                )
            changed += db.execute(
                """UPDATE work_sessions
                   SET claude_profile_id = ?, updated_at = ?
                   WHERE engine = 'claude' AND claude_profile_id IS NULL""",
                (legacy_profile_id, time.time()),
            ).rowcount
            db.execute(
                """UPDATE work_schedules SET claude_profile_id = ?
                   WHERE claude_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                """UPDATE work_schedules SET last_claude_profile_id = ?
                   WHERE last_session_id IS NOT NULL
                     AND last_claude_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                """UPDATE work_schedule_runs
                   SET claude_profile_id = COALESCE(
                       (SELECT s.claude_profile_id FROM work_schedules s
                        WHERE s.schedule_id = work_schedule_runs.schedule_id),
                       ?)
                   WHERE claude_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                "INSERT INTO work_claude_profile_migrations"
                "(revision, applied_at) VALUES (?, ?)",
                (profile_revision, time.time()),
            )
        return changed

    def assign_legacy_codex_profile(self, profile_id: str) -> int:
        """Bind legacy Codex Work state to the current default account once.

        Schedule ownership was dynamic before profiles were selectable: every
        occurrence ran on whichever account was default at launch.  The first
        profile-aware wrapper freezes all still-unowned schedules and runs to
        its current default.  This repair is deliberately independent of the
        topology revision because that revision may predate the schedule
        ownership column.
        """
        if self.engine != "codex":
            return 0
        profile_id = self._profile_id(profile_id)
        if profile_id is None:
            raise ValueError("Codex Work profile is required")
        self.initialize()
        with self._connect() as db:
            changed = db.execute(
                """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                   WHERE engine = 'codex' AND codex_profile_id IS NULL""",
                (profile_id, time.time()),
            ).rowcount
            db.execute(
                """UPDATE work_schedules SET codex_profile_id = ?
                   WHERE codex_profile_id IS NULL""",
                (profile_id,),
            )
            db.execute(
                """UPDATE work_schedules SET last_codex_profile_id = ?
                   WHERE last_session_id IS NOT NULL
                     AND last_codex_profile_id IS NULL""",
                (profile_id,),
            )
            db.execute(
                """UPDATE work_schedule_runs
                   SET codex_profile_id = COALESCE(
                       (SELECT s.codex_profile_id FROM work_schedules s
                        WHERE s.schedule_id = work_schedule_runs.schedule_id),
                       ?)
                   WHERE codex_profile_id IS NULL""",
                (profile_id,),
            )
        return changed

    def remap_codex_profiles(self, remaps: dict[str, str]) -> int:
        """Keep Work ownership attached to CODEX_HOME across profile id renames."""
        if self.engine != "codex" or not remaps:
            return 0
        normalized = {
            self._profile_id(old): self._profile_id(new)
            for old, new in remaps.items()
        }
        self.initialize()
        changed = 0
        with self._connect() as db:
            staged: list[tuple[str, str]] = []
            occupied = set(normalized) | set(normalized.values())
            for index, (old_id, new_id) in enumerate(normalized.items()):
                if old_id is None or new_id is None or old_id == new_id:
                    continue
                temporary = f"ccremote_profile_migration_{index}"
                while temporary in occupied:
                    temporary += "_"
                occupied.add(temporary)
                changed += db.execute(
                    """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                       WHERE engine = 'codex' AND codex_profile_id = ?""",
                    (temporary, time.time(), old_id),
                ).rowcount
                db.execute(
                    """UPDATE work_schedules SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedules SET last_codex_profile_id = ?
                       WHERE last_codex_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (temporary, old_id),
                )
                staged.append((temporary, new_id))
            for temporary, new_id in staged:
                db.execute(
                    """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                       WHERE engine = 'codex' AND codex_profile_id = ?""",
                    (new_id, time.time(), temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET last_codex_profile_id = ?
                       WHERE last_codex_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (new_id, temporary),
                )
        return changed

    def migrate_codex_profiles(
        self,
        remaps: dict[str, str],
        *,
        legacy_profile_id: str,
        profile_revision: int,
    ) -> int:
        """Atomically apply one profile topology revision to all Work rows."""
        if self.engine != "codex":
            return 0
        legacy_profile_id = self._profile_id(legacy_profile_id)
        if legacy_profile_id is None:
            raise ValueError("Codex Work profile is required")
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ValueError("Codex Work profile revision is invalid")
        normalized = {
            self._profile_id(old): self._profile_id(new)
            for old, new in remaps.items()
        }
        self.initialize()
        changed = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            applied = db.execute(
                "SELECT 1 FROM work_profile_migrations WHERE revision = ?",
                (profile_revision,),
            ).fetchone()
            if applied is not None:
                return 0
            staged: list[tuple[str, str]] = []
            occupied = set(normalized) | set(normalized.values())
            for index, (old_id, new_id) in enumerate(normalized.items()):
                if old_id is None or new_id is None or old_id == new_id:
                    continue
                temporary = f"ccremote_profile_migration_{profile_revision}_{index}"
                while temporary in occupied:
                    temporary += "_"
                occupied.add(temporary)
                changed += db.execute(
                    """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                       WHERE engine = 'codex' AND codex_profile_id = ?""",
                    (temporary, time.time(), old_id),
                ).rowcount
                db.execute(
                    """UPDATE work_schedules SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedules SET last_codex_profile_id = ?
                       WHERE last_codex_profile_id = ?""",
                    (temporary, old_id),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (temporary, old_id),
                )
                staged.append((temporary, new_id))
            for temporary, new_id in staged:
                db.execute(
                    """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                       WHERE engine = 'codex' AND codex_profile_id = ?""",
                    (new_id, time.time(), temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedules SET last_codex_profile_id = ?
                       WHERE last_codex_profile_id = ?""",
                    (new_id, temporary),
                )
                db.execute(
                    """UPDATE work_schedule_runs SET codex_profile_id = ?
                       WHERE codex_profile_id = ?""",
                    (new_id, temporary),
                )
            changed += db.execute(
                """UPDATE work_sessions SET codex_profile_id = ?, updated_at = ?
                   WHERE engine = 'codex' AND codex_profile_id IS NULL""",
                (legacy_profile_id, time.time()),
            ).rowcount
            db.execute(
                """UPDATE work_schedules SET codex_profile_id = ?
                   WHERE codex_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                """UPDATE work_schedules SET last_codex_profile_id = ?
                   WHERE last_session_id IS NOT NULL
                     AND last_codex_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                """UPDATE work_schedule_runs
                   SET codex_profile_id = COALESCE(
                       (SELECT s.codex_profile_id FROM work_schedules s
                        WHERE s.schedule_id = work_schedule_runs.schedule_id),
                       ?)
                   WHERE codex_profile_id IS NULL""",
                (legacy_profile_id,),
            )
            db.execute(
                "INSERT INTO work_profile_migrations(revision, applied_at) "
                "VALUES (?, ?)",
                (profile_revision, time.time()),
            )
        return changed

    def get_by_session(
        self,
        session_id: str,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> WorkSessionRecord | None:
        self.initialize()
        claude_profile_id = self._profile_id(claude_profile_id)
        codex_profile_id = self._profile_id(codex_profile_id)
        selected_profile_id = (
            claude_profile_id if self.engine == "claude"
            else codex_profile_id)
        profile_column = (
            "claude_profile_id" if self.engine == "claude"
            else "codex_profile_id")
        with self._connect() as db:
            if selected_profile_id is None:
                rows = db.execute(
                    "SELECT * FROM work_sessions "
                    "WHERE session_id = ? AND engine = ? LIMIT 2",
                    (session_id, self.engine),
                ).fetchall()
                if len(rows) > 1:
                    raise ValueError(
                        "ambiguous Work session; profile is required"
                    )
                row = rows[0] if rows else None
            else:
                row = db.execute(
                    "SELECT * FROM work_sessions WHERE session_id = ? "
                    f"AND engine = ? AND {profile_column} = ?",
                    (session_id, self.engine, selected_profile_id),
                ).fetchone()
        return self._record(row)

    def get_by_work_id(self, work_id: str) -> WorkSessionRecord | None:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM work_sessions WHERE work_id = ? AND engine = ?",
                (work_id, self.engine),
            ).fetchone()
        return self._record(row)

    def records_by_session(self) -> dict[str, WorkSessionRecord]:
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM work_sessions
                   WHERE session_id IS NOT NULL AND engine = ?""",
                (self.engine,),
            ).fetchall()
        result: dict[str, WorkSessionRecord] = {}
        for row in rows:
            record = self._record(row)
            if record is None or record.session_id is None:
                continue
            if record.session_id in result:
                raise ValueError(
                    f"ambiguous {self.engine.title()} Work sessions; "
                    "use account-aware records"
                )
            result[record.session_id] = record
        return result

    def records_by_profile_session(
        self,
    ) -> dict[tuple[str | None, str], WorkSessionRecord]:
        """Return account-aware keys for either engine's catalog."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM work_sessions
                   WHERE session_id IS NOT NULL AND engine = ?""",
                (self.engine,),
            ).fetchall()
        records = (self._record(row) for row in rows)
        return {
            (
                record.claude_profile_id
                if self.engine == "claude" else record.codex_profile_id,
                record.session_id,
            ): record
            for record in records
            if record is not None and record.session_id is not None
        }

    def unbound_records_by_cwd(self) -> dict[str, WorkSessionRecord]:
        """Crash-recovery candidates created before a native session id landed."""
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM work_sessions
                   WHERE session_id IS NULL AND engine = ?""",
                (self.engine,),
            ).fetchall()
        records = (self._record(row) for row in rows)
        return {
            os.path.realpath(record.cwd): record
            for record in records
            if record is not None
        }

    def contains_cwd(self, cwd: str | None) -> bool:
        if not cwd:
            return False
        try:
            target = os.path.realpath(os.path.expanduser(cwd))
            common = os.path.commonpath((target, str(self.chats_root)))
        except (OSError, ValueError):
            return False
        return common == str(self.chats_root)

    def update_title(
        self, session_id: str, title: str, *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> None:
        self._update(
            session_id, "title", title,
            claude_profile_id=claude_profile_id,
            codex_profile_id=codex_profile_id)

    def update_archived(
        self,
        session_id: str,
        archived: bool,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> None:
        self._update(
            session_id, "archived", 1 if archived else 0,
            claude_profile_id=claude_profile_id,
            codex_profile_id=codex_profile_id)

    def delete(
        self, session_id: str, *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> WorkSessionRecord:
        record = self.get_by_session(
            session_id,
            claude_profile_id=claude_profile_id,
            codex_profile_id=codex_profile_id)
        if record is None:
            raise LookupError(f"unknown Work session: {session_id}")
        chat_root = Path(record.cwd).parent
        self._require_owned_chat_root(chat_root, record.work_id)
        with self._connect() as db:
            effective_profile_id = (
                record.claude_profile_id if self.engine == "claude"
                else record.codex_profile_id)
            profile_column = (
                "claude_profile_id" if self.engine == "claude"
                else "codex_profile_id")
            if effective_profile_id is None:
                changed = db.execute(
                    "DELETE FROM work_sessions "
                    "WHERE session_id = ? AND engine = ?",
                    (session_id, self.engine),
                ).rowcount
            else:
                changed = db.execute(
                    "DELETE FROM work_sessions WHERE session_id = ? "
                    f"AND engine = ? AND {profile_column} = ?",
                    (session_id, self.engine, effective_profile_id),
                ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work session: {session_id}")
        if chat_root.exists():
            shutil.rmtree(chat_root)
        policy = self.root / "policies" / f"{record.work_id}.json"
        try:
            policy.unlink()
        except FileNotFoundError:
            pass
        return record

    def abandon(self, work_id: str) -> None:
        """Remove a never-bound Work directory after native session spawn fails."""
        record = self.get_by_work_id(work_id)
        if record is None or record.session_id is not None:
            return
        chat_root = Path(record.cwd).parent
        self._require_owned_chat_root(chat_root, work_id)
        with self._connect() as db:
            db.execute(
                "DELETE FROM work_sessions WHERE work_id = ? AND session_id IS NULL",
                (work_id,),
            )
        if chat_root.exists():
            shutil.rmtree(chat_root)
        policy = self.root / "policies" / f"{work_id}.json"
        try:
            policy.unlink()
        except FileNotFoundError:
            pass

    def _update(
        self,
        session_id: str,
        column: str,
        value: object,
        *,
        claude_profile_id: str | None = None,
        codex_profile_id: str | None = None,
    ) -> None:
        if column not in {"title", "archived"}:
            raise ValueError("unsupported Work metadata column")
        selected_profile_id = self._profile_id(
            claude_profile_id if self.engine == "claude"
            else codex_profile_id)
        profile_column = (
            "claude_profile_id" if self.engine == "claude"
            else "codex_profile_id")
        with self._connect() as db:
            if selected_profile_id is None:
                matches = db.execute(
                    "SELECT COUNT(*) FROM work_sessions "
                    "WHERE session_id = ? AND engine = ?",
                    (session_id, self.engine),
                ).fetchone()[0]
                if matches > 1:
                    raise ValueError(
                        "ambiguous Work session; profile is required"
                    )
                changed = db.execute(
                    f"UPDATE work_sessions SET {column} = ?, updated_at = ? "
                    "WHERE session_id = ? AND engine = ?",
                    (value, time.time(), session_id, self.engine),
                ).rowcount
            else:
                changed = db.execute(
                    f"UPDATE work_sessions SET {column} = ?, updated_at = ? "
                    "WHERE session_id = ? AND engine = ? "
                    f"AND {profile_column} = ?",
                    (value, time.time(), session_id, self.engine,
                     selected_profile_id),
                ).rowcount
        if changed != 1:
            raise LookupError(f"unknown Work session: {session_id}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _record(row: sqlite3.Row | None) -> WorkSessionRecord | None:
        if row is None:
            return None
        return WorkSessionRecord(
            work_id=row["work_id"], engine=row["engine"], cwd=row["cwd"],
            session_id=row["session_id"], title=row["title"],
            project_id=row["project_id"], archived=bool(row["archived"]),
            context_baseline_tokens=(
                int(row["context_baseline_tokens"])
                if row["context_baseline_tokens"] is not None else None
            ),
            created_at=float(row["created_at"]), updated_at=float(row["updated_at"]),
            claude_profile_id=row["claude_profile_id"],
            codex_profile_id=row["codex_profile_id"],
        )

    def _profile_id(self, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not _CODEX_PROFILE_ID.fullmatch(value)
        ):
            raise ValueError(f"invalid {self.engine.title()} Work profile id")
        return value

    @staticmethod
    def _mkdir_private(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)

    @staticmethod
    def _chmod_file(path: Path) -> None:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISREG(mode):
            path.chmod(0o600)

    @staticmethod
    def _is_managed_upload_root(path: Path) -> bool:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            return False
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_fd = marker_fd = -1
        try:
            directory_fd = os.open(path, flags)
            directory = os.fstat(directory_fd)
            marker_fd = os.open(
                WORK_UPLOADS_MARKER,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
            marker = os.fstat(marker_fd)
            with os.fdopen(marker_fd, "rb", closefd=False) as stream:
                payload = stream.read(len(WORK_UPLOADS_MARKER_PAYLOAD) + 1)
        except OSError:
            return False
        finally:
            for descriptor in (marker_fd, directory_fd):
                if descriptor >= 0:
                    os.close(descriptor)
        return (stat.S_ISDIR(directory.st_mode)
                and stat.S_ISREG(marker.st_mode)
                and marker.st_uid == os.geteuid()
                and marker.st_nlink == 1
                and payload == WORK_UPLOADS_MARKER_PAYLOAD)

    def _require_owned_chat_root(self, path: Path, work_id: str) -> None:
        expected = self.chats_root / work_id
        if path != expected or not self.contains_cwd(str(path / "workspace")):
            raise RuntimeError("refusing to delete a path outside the Work registry")


class WorkStores:
    def __init__(self, claude_root: Path, codex_root: Path):
        self._stores: dict[Engine, WorkRegistry] = {
            "claude": WorkRegistry(claude_root, "claude"),
            "codex": WorkRegistry(codex_root, "codex"),
        }

    def for_engine(self, engine: str) -> WorkRegistry:
        if engine not in self._stores:
            raise ValueError(f"unsupported Work engine: {engine}")
        return self._stores[cast(Engine, engine)]

    def initialize(self) -> None:
        for store in self._stores.values():
            store.initialize()

    def classify(
        self,
        engine: str,
        session_id: str | None,
        cwd: str | None,
        *,
        codex_profile_id: str | None = None,
    ) -> str:
        store = self.for_engine(engine)
        if session_id and store.get_by_session(
            session_id,
            codex_profile_id=(
                codex_profile_id if engine == "codex" else None
            ),
        ) is not None:
            return "work"
        return "work" if store.contains_cwd(cwd) else "code"

    def roots(self) -> Iterable[Path]:
        return (store.root for store in self._stores.values())
