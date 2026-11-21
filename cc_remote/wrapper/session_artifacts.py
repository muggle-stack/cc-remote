"""Session-owned attachment catalog. No directory crawl or model/engine calls.

Uploads are immutable private copies; delivered files remain references to the
user's working files. Historical discovery consumes bounded summary pages.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

from cc_remote.attachments import MAX_IMAGE_PIXELS, decode_attachment, validate_attachments


# Extra durable copies, not the user's working files or native transcripts.
# Reject new copies at the limit; never silently evict an existing attachment.
MAX_SESSION_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_SESSION_UPLOAD_FILES = 512
MAX_TOTAL_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_UPLOAD_FILES = 4096
MIN_FREE_UPLOAD_BYTES = 256 * 1024 * 1024


class ArtifactStorageFull(OSError):
    """An optional durable copy would exceed its storage budget."""


def _upload_copy_name(name: str, digest: str) -> str:
    """Fit one filesystem component, keeping the extension used by previews."""
    safe = re.sub(r"[^\w. -]", "_", Path(name).name).strip(". ") or "attachment"
    budget = 255 - len(digest) - 1  # SHA-256 hex plus the separating dash.
    if len(safe.encode("utf-8")) > budget:
        suffix = Path(safe).suffix
        if len(suffix.encode("utf-8")) >= budget:
            suffix = ""  # An oversized extension cannot be preserved intact.
        stem = safe[:-len(suffix)] if suffix else safe
        stem_budget = budget - len(suffix.encode("utf-8"))
        stem = stem.encode("utf-8")[:stem_budget].decode("utf-8", errors="ignore")
        safe = (stem.rstrip(". ") or "_") + suffix
    return f"{digest}-{safe}"


def image_thumbnail(data: bytes) -> bytes:
    """A static, metadata-free list thumbnail; never send original image bytes."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as source:
        if source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("图片尺寸过大")
        # Use only the first frame of animated images, and honor phone orientation.
        source.thumbnail((160, 160), Image.Resampling.LANCZOS)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        output = io.BytesIO()
        image.save(output, format="WEBP", quality=75, method=4)
        result = output.getvalue()
        if len(result) > 64 * 1024:
            raise ValueError("缩略图尺寸过大")
        return result


def _table_cells(line: str) -> list[str]:
    """Split a Markdown row with optional outer pipes and escaped cell pipes."""
    cells = []
    start, escaped = 0, False
    for index in range(len(line)):
        char = line[index]
        if char == "|" and not escaped:
            cells.append(line[start:index].strip())
            start = index + 1
        escaped = char == "\\" and not escaped
    if start < len(line) or not cells:
        cells.append(line[start:].strip())
    if line.startswith("|"):
        cells.pop(0)
    return cells


def _table_separator(cells: list[str], width: int) -> bool:
    return bool(cells) and len(cells) == width and all(
        re.fullmatch(r":?-+:?", cell) for cell in cells)


_FILE_TABLE_HEADERS = frozenset({
    "file", "files", "filename", "filenames", "path", "paths", "artifact", "artifacts",
    "文件", "文件名", "文件路径", "路径", "产物", "输出文件", "交付文件",
})


def _inline_local_paths(text: str, cwd: str):
    """Recognize quoted paths and directory-led file lists, without filesystem I/O.

    A bare filename needs a directory explicitly introduced immediately before
    its list/table. Do not carry that directory into another paragraph, heading,
    code example or assistant message, or infer paths from shell commands.
    """
    directory = None
    fence = None
    table_started, table_ready = False, False
    table_column, table_width = None, 0
    lines = text.splitlines()
    for line_number, line in enumerate(lines):
        stripped = line.strip()
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        in_bullet = bool(re.match(r"^\s*(?:[-+*]\s+|\d+[.)]\s+)", line))
        block_start = marker or in_bullet or re.match(
            r"^(?:#{1,6}(?:\s|$)|>|<[!/?A-Za-z]|(?:[-*_]\s*){3,}$)", stripped)
        cells = _table_cells(stripped)
        # A pipe in prose is not enough: a new table needs its delimiter row.
        # Once open, a short body row can omit cells (and therefore all pipes).
        table_header = (directory and line_number + 1 < len(lines)
                        and (len(cells) > 1 or stripped.startswith("|") or stripped.endswith("|"))
                        and _table_separator(
                            _table_cells(lines[line_number + 1].strip()), len(cells)))
        delimiter = table_started and not table_ready and _table_separator(cells, table_width)
        is_table = bool(stripped and (delimiter or not block_start and (table_started or table_header)))
        if not is_table:
            if table_started:
                directory = None
            table_started, table_ready = False, False
            table_column = None
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker[1]
            directory = None
            continue
        if not stripped:
            continue
        in_list = in_bullet or is_table
        if not in_list:
            directory = None
        if directory and is_table:
            if not table_started:
                columns = [index for index, cell in enumerate(cells)
                           if re.sub(r"\s+", "", cell.strip("*_` ")).lower()
                           in _FILE_TABLE_HEADERS]
                table_column = columns[0] if len(columns) == 1 else None
                table_width, table_started = len(cells), True
                continue
            if table_column is None:
                continue
            if not table_ready:
                if not _table_separator(cells, table_width):
                    table_column = None
                else:
                    table_ready = True
                continue
            if table_column >= len(cells):
                continue
            # Descriptions may contain source filenames or code identifiers.
            # Only the explicitly named file column participates in discovery.
            line = cells[table_column]
        values = []
        for match in re.finditer(r"(?<!`)(`{1,2})([^`\n]+)\1(?!`)", line):
            value = match[2].strip()
            if (not value or len(value) > 4096
                    or any(ord(char) < 32 for char in value)
                    or re.search(r"[<>|*?;$={}\\]", value)
                    or value.startswith(("//", "#"))
                    or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value)):
                continue
            # Relative path prefixes must look like directories, not commands.
            if "/" in value and not value.startswith(("/", "~/", "./", "../")):
                if not re.fullmatch(r"[\w.-]+", value.split("/", 1)[0]):
                    continue
            values.append(value)
        directories = [value for value in values if value.endswith("/")] if not is_table else []
        if (not directories and len(values) == 1 and not in_list
                and stripped.endswith((":", "："))):
            # A path-shaped, extensionless introduction is unambiguous.
            # Bare/dotted directory names need an explicit location cue.
            value = values[0]
            location = re.search(
                r"(?:\b(?:in|under|inside|directory|folder)\b|目录|文件夹|位于|在|保存到|输出到)"
                r"\s*[:：]?\s*(`{1,2})[^`\n]+\1\s*[:：]$", stripped, re.I)
            if ("/" in value and not Path(value).suffix) or location:
                directories = [value]
        if directories:
            directory = (os.path.abspath(os.path.join(cwd, os.path.expanduser(directories[0])))
                         if len(directories) == 1 and not in_list
                         and stripped.endswith((":", "：")) else None)
        for index, value in enumerate(values):
            if value.endswith("/") or (directory and value in directories):
                continue
            value = re.sub(r":\d+(?::\d+)?$", "", value)
            if not re.fullmatch(r"\.[a-zA-Z0-9]{1,16}", Path(value).suffix):
                continue
            if "/" in value:
                yield os.path.abspath(os.path.join(cwd, os.path.expanduser(value)))
            elif directory and in_list and (index == 0 or is_table):
                # Only the file label in a bullet, not identifiers in its description.
                yield os.path.abspath(os.path.join(directory, value))


def local_links(text: str, cwd: str) -> list[str]:
    """Explicit delivered file references, not tool reads or directory scans."""
    # Link labels can themselves contain code spans; their destination, not the
    # display label, is the file identity.
    inline_text = re.sub(r"!?\[[^\]\n]*\]\([^\n]*?\)", "", text)
    text = re.sub(r"```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~", "", text, flags=re.S)
    candidates = []
    for match in re.finditer(r"\]\(\s*(?:<([^>\n]+)>|([^\s]+?))(?:\s+\"[^\"]*\")?\s*\)", text):
        candidates.append(match.group(1) or match.group(2))
    for match in re.finditer(r'visualize(\{[^\n]*?\})', text):
        try:
            candidates.append(json.loads(match.group(1)).get("path"))
        except (ValueError, TypeError):
            pass
    paths = []
    for value in candidates[:128]:
        if not isinstance(value, str) or len(value) > 4096:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme not in ("", "file") or parsed.netloc or value.startswith("#"):
            continue
        path = re.sub(r":\d+(?::\d+)?$", "", unquote(parsed.path))
        if not path or not Path(path).suffix:
            continue
        path = os.path.abspath(os.path.join(cwd, os.path.expanduser(path)))
        if path not in paths:
            paths.append(path)
    for path in _inline_local_paths(inline_text, cwd):
        if len(paths) >= 128:
            break
        if path not in paths:
            paths.append(path)
    return paths


def artifact_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".heic", ".heif", ".tif", ".tiff", ".bmp"}:
        return "image"
    if ext in {".md", ".txt", ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".csv", ".tsv"}:
        return "document"
    return "file"


class SessionArtifactStore:
    def __init__(self, state_dir: Path):
        self.root = Path(state_dir) / "session-artifacts"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError("unsafe artifact directory")
        os.chmod(self.root, 0o700)
        self.path = self.root / "index.sqlite3"
        if self.path.is_symlink():
            raise OSError("unsafe artifact database")
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                    id TEXT NOT NULL, identity TEXT NOT NULL, name TEXT NOT NULL,
                    path TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
                    turn_id TEXT, image_id TEXT, UNIQUE(scope, identity), UNIQUE(scope, id)
                );
                CREATE TABLE IF NOT EXISTS scope_aliases (old TEXT PRIMARY KEY, current TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS artifact_page ON artifacts(scope, created_at DESC, seq DESC);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def canonical_scope(db, scope):
        for _ in range(32):
            row = db.execute("SELECT current FROM scope_aliases WHERE old=?", (scope,)).fetchone()
            if row is None:
                return scope
            scope = row[0]
        raise ValueError("artifact scope alias cycle")

    def add(self, scope, identity, name, path, source, *, created_at=None,
            turn_id=None, image_id=None):
        with self.lock, self.connect() as db:
            scope = self.canonical_scope(db, scope)
            self._add(db, scope, identity, name, path, source,
                      created_at=created_at, turn_id=turn_id, image_id=image_id)

    @staticmethod
    def _add(db, scope, identity, name, path, source, *, created_at=None,
             turn_id=None, image_id=None):
        identifier = hashlib.sha256(f"{scope}\0{identity}".encode()).hexdigest()
        db.execute("""INSERT INTO artifacts
                (scope,id,identity,name,path,source,created_at,turn_id,image_id)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,identity) DO UPDATE SET
                image_id=COALESCE(excluded.image_id, artifacts.image_id),
                turn_id=COALESCE(excluded.turn_id, artifacts.turn_id),
                path=CASE WHEN excluded.image_id IS NOT NULL THEN excluded.path ELSE artifacts.path END""",
                (scope, identifier, identity, name[:1024], path, source,
                 created_at if created_at is not None else time.time(), turn_id, image_id))

    def _check_upload_budget(self, db, scope, incoming, new_files):
        """Check physical bytes under the SQLite writer lock, before any copy.

        Count legacy and crash-orphan files too; database-only accounting would
        forget those bytes on restart. Scans stop as soon as a limit is exceeded.
        """
        session_files = dict(incoming)
        for row in db.execute("""SELECT path FROM artifacts WHERE scope=?
                AND source='upload' AND image_id IS NULL""", (scope,)):
            path = Path(row[0])
            if path.parent != self.root:
                continue
            try:
                session_files[path] = path.lstat().st_size
            except FileNotFoundError:
                continue
            if len(session_files) > MAX_SESSION_UPLOAD_FILES:
                raise ArtifactStorageFull("session attachment count limit")
        if (len(session_files) > MAX_SESSION_UPLOAD_FILES
                or sum(session_files.values()) > MAX_SESSION_UPLOAD_BYTES):
            raise ArtifactStorageFull("session attachment storage limit")
        if not new_files:
            return  # Idempotent replay consumes no additional disk space.
        total_count, total_bytes = len(new_files), sum(new_files.values())
        database_files = {"index.sqlite3", "index.sqlite3-journal",
                          "index.sqlite3-wal", "index.sqlite3-shm"}
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.name in database_files:
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("unsafe artifact storage entry")
                total_count += 1
                total_bytes += info.st_size
                if total_count > MAX_TOTAL_UPLOAD_FILES or total_bytes > MAX_TOTAL_UPLOAD_BYTES:
                    raise ArtifactStorageFull("total attachment storage limit")
        if total_count > MAX_TOTAL_UPLOAD_FILES or total_bytes > MAX_TOTAL_UPLOAD_BYTES:
            raise ArtifactStorageFull("total attachment storage limit")
        if shutil.disk_usage(self.root).free - sum(new_files.values()) < MIN_FREE_UPLOAD_BYTES:
            raise ArtifactStorageFull("attachment disk reserve")

    def uploads(self, scope, msg_id, files=None, images=None):
        if validate_attachments(images, files):
            raise ValueError("invalid artifact upload")
        items = [(f.get("filename") or "attachment", f["data"]) for f in files or []]
        extensions = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}
        items.extend((f"image-{i:02d}" + extensions.get(f["media_type"], ".png"), f["data"])
                     for i, f in enumerate(images or []))
        if not items:
            return
        with self.lock, self.connect() as db:
            # Serializes quota check, file creation and metadata across store
            # instances/processes, including deletion and session re-keying.
            db.execute("BEGIN IMMEDIATE")
            scope = self.canonical_scope(db, scope)
            pending, new_files = [], {}
            for i, (name, encoded) in enumerate(items):
                identity = f"upload:{msg_id}:{i}:{name}"
                data = decode_attachment(encoded)
                previous = db.execute("SELECT path FROM artifacts WHERE scope=? AND identity=?",
                                      (scope, identity)).fetchone()
                digest = hashlib.sha256(f"{scope}\0{identity}".encode()).hexdigest()
                path = Path(previous[0]) if previous else self.root / _upload_copy_name(name, digest)
                if path.parent != self.root:
                    raise OSError("unsafe artifact upload path")
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    new_files[path] = len(data)
                else:
                    with os.fdopen(fd, "rb") as existing:
                        info = os.fstat(existing.fileno())
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_uid != os.geteuid()):
                            raise OSError("unsafe artifact upload")
                        if existing.read(len(data) + 1) != data:
                            raise OSError("artifact upload content changed")
                pending.append((identity, name, path, data))
            self._check_upload_budget(db, scope,
                {path: len(data) for _, _, path, data in pending}, new_files)
            created = []
            try:
                for identity, name, path, data in pending:
                    if path in new_files:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        created.append(path)
                        with os.fdopen(fd, "wb") as out:
                            out.write(data)
                            out.flush()
                            os.fsync(out.fileno())
                    self._add(db, scope, identity, name, str(path), "upload", turn_id=msg_id)
                db.commit()
            except BaseException:
                for path in created:
                    path.unlink(missing_ok=True)
                raise

    def text(self, scope, text, cwd, *, created_at=None):
        for path in local_links(text, cwd):
            self.add(scope, "path:" + path, Path(path).name, path, "delivered", created_at=created_at)

    def native_outputs(self, scope, files, cwd):
        """Replace legacy DSH prose references with verified output provenance.

        Native metadata is bounded: retain previously verified outputs. Remove
        only legacy path references, never uploads, other scopes or files.
        """
        paths = []
        for file in files[:256]:
            path = file.get("path")
            if not isinstance(path, str) or not path or len(path) > 4096:
                raise ValueError("invalid native artifact path")
            paths.append(os.path.abspath(os.path.join(cwd, os.path.expanduser(path))))
        with self.lock, self.connect() as db:
            scope = self.canonical_scope(db, scope)
            for path in paths:
                old = db.execute("SELECT created_at FROM artifacts WHERE scope=? AND identity=?",
                                 (scope, "path:" + path)).fetchone()
                self._add(db, scope, "dsh-output:" + path, Path(path).name, path,
                          "delivered", created_at=old[0] if old else None)
            db.execute("""DELETE FROM artifacts WHERE scope=? AND source='delivered'
                       AND identity LIKE 'path:%' AND image_id IS NULL""", (scope,))

    def history(self, scope, history, cwd, *, discover_text=True):
        for turn in history.turns:
            stamp = turn.ts / 1000 if turn.ts else None
            for block in turn.blocks:
                if block.get("kind") == "text" and discover_text:
                    self.text(scope, block.get("text", ""), cwd, created_at=stamp)
                elif block.get("kind") == "process" and block.get("tool") == "image_generation" and block.get("status") == "succeeded":
                    item = block.get("input") or {}
                    ref = item.get("history_image") or {}
                    path = item.get("file_path")
                    if isinstance(ref.get("image_id"), str):
                        image_id = ref["image_id"]
                        self.add(scope, "path:" + os.path.abspath(os.path.join(cwd, path)) if path else f"generated:{turn.id}:{image_id}",
                                 Path(path).name if path else "generated.png",
                                 f"artifacts/{image_id}.png", "delivered", created_at=stamp,
                                 turn_id=turn.id, image_id=image_id)
                    elif isinstance(path, str):
                        self.text(scope, f"[image](<{path}>)", cwd, created_at=stamp)
            uploaded = any(self.has_uploads(scope, key) for key in (turn.id, turn.clientMsgId) if key)
            if not uploaded:
                for i, file in enumerate(turn.files or []):
                    name = file.get("filename", "attachment")
                    identity = f"missing:{turn.id}:{i}:{name}"
                    self.add(scope, identity, name, f"artifacts/unavailable/{i}/{name}",
                             "upload", created_at=stamp, turn_id=turn.id)
            for i, ref in enumerate([] if uploaded else turn.imageRefs or []):
                data = ref.model_dump() if hasattr(ref, "model_dump") else ref
                image_id = data.get("image_id")
                if not image_id:
                    continue
                identity = f"history:{turn.id}:{image_id}"
                self.add(scope, identity, f"image-{i:02d}.png", f"artifacts/{image_id}.png",
                         "upload", created_at=stamp, turn_id=turn.id, image_id=image_id)

    def page(self, scope, before=None, limit=40):
        with self.lock, self.connect() as db:
            scope = self.canonical_scope(db, scope)
            anchor = db.execute("SELECT created_at, seq FROM artifacts WHERE scope=? AND seq=?",
                                (scope, before)).fetchone() if before else None
            if before and anchor is None:
                raise ValueError("invalid catalog cursor")
            rows = db.execute("""SELECT * FROM artifacts WHERE scope=? AND
                (? IS NULL OR created_at < ? OR (created_at = ? AND seq < ?))
                ORDER BY created_at DESC,seq DESC LIMIT ?""",
                (scope, before, anchor[0] if anchor else None, anchor[0] if anchor else None,
                 before, limit + 1)).fetchall()
        page = rows[:limit]
        return [dict(row) for row in page], page[-1]["seq"] if page else before, len(rows) > limit

    def get(self, scope, identifier):
        with self.lock, self.connect() as db:
            scope = self.canonical_scope(db, scope)
            row = db.execute("SELECT * FROM artifacts WHERE scope=? AND id=?",
                             (scope, identifier)).fetchone()
        return dict(row) if row else None

    def has_uploads(self, scope, msg_id):
        with self.lock, self.connect() as db:
            scope = self.canonical_scope(db, scope)
            return bool(db.execute("SELECT 1 FROM artifacts WHERE scope=? AND turn_id=? AND image_id IS NULL AND identity NOT LIKE 'missing:%' LIMIT 1",
                                   (scope, msg_id)).fetchone())

    def drop_session(self, machine_id, engine, sid):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            scopes = [row[0] for row in db.execute("SELECT DISTINCT scope FROM artifacts")]
            for scope in scopes:
                parts = json.loads(scope)
                if parts[0] != machine_id or parts[1] != engine or parts[3] != sid:
                    continue
                for row in db.execute("SELECT path FROM artifacts WHERE scope=? AND source='upload' AND image_id IS NULL", (scope,)):
                    path = Path(row[0])
                    if path.parent == self.root:
                        path.unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE scope=?", (scope,))

    def move(self, old_scope, new_scope):
        with self.lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_scope = self.canonical_scope(db, old_scope)
            new_scope = self.canonical_scope(db, new_scope)
            if old_scope == new_scope:
                return
            db.execute("INSERT OR REPLACE INTO scope_aliases VALUES (?,?)", (old_scope, new_scope))
            db.execute("UPDATE OR IGNORE artifacts SET scope=? WHERE scope=?", (new_scope, old_scope))


def public_entry(row):
    size = 0
    available = bool(row["image_id"])
    if not available and not row["identity"].startswith("missing:"):
        try:
            info = os.lstat(row["path"])
            available = stat.S_ISREG(info.st_mode)
            size = info.st_size if available else 0
        except OSError:
            pass
    return {key: row[key] for key in ("id", "name", "path", "source", "created_at")} | {
        "kind": artifact_kind(row["name"]), "available": available, "size": size,
    }
