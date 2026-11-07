"""Read-only native subagent activity for the per-session background dock.

Tool-call completion is not agent completion. Membership comes from each
child's own native turn markers, independently of the parent's turn.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import stat
import threading

from cc_remote.protocol import BackgroundProcessItem, MAX_BACKGROUND_PROCESS_ITEMS
from cc_remote.wrapper.codex_agents import _ID, _parent
from cc_remote.wrapper.codex_external import parse_turn_markers
from cc_remote.wrapper.codex_sessions import _state_db_path, MAX_META_RECORD_BYTES
from cc_remote.wrapper.rollout_io import is_compressed_rollout, rollout_open, rollout_fstat

_MAX_CHILDREN = 512  # Per resident parent, including completed native catalog rows.
_MAX_RECORD = 16 * 1024 * 1024
_READ_BUDGET = 32 * 1024 * 1024
_MAX_TURNS = 4096


def _catalog(home: Path, parents: set[str]) -> dict[str, tuple[str, str]]:
    """Select only direct native subagents, never ordinary user forks."""
    database = _state_db_path(home)
    if database is None:
        return {}
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    result = {}
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        # Bound work even with an unexpectedly large/corrupt native catalog.
        steps = 0

        def bounded():
            nonlocal steps
            steps += 1
            return int(steps > 2000)

        db.set_progress_handler(bounded, 1000)
        source = "CASE WHEN length(source)<=65536 AND json_valid(source) THEN source ELSE '{}' END"
        for parent in sorted(parents):
            steps = 0
            rows = db.execute(
                f"SELECT id, rollout_path, {source} FROM threads WHERE "
                f"json_extract({source}, '$.subagent.thread_spawn.parent_thread_id') "
                "= ? LIMIT ?", (parent, _MAX_CHILDREN + 1),
            ).fetchall()
            if len(rows) > _MAX_CHILDREN:
                # Do not publish a truncated inventory or block other parents.
                # Missing rows retain this parent's previous agents as unknown.
                continue
            for sid, path, raw_source in rows:
                if (not isinstance(sid, str) or not _ID.fullmatch(sid)
                        or not isinstance(path, str) or len(path) > 16384
                        or _parent({"source": json.loads(raw_source)}) != parent):
                    raise ValueError("invalid native agent catalog")
                result[sid] = (parent, path)
    return result


def _local_path(home: Path, path: str) -> Path:
    resolved = Path(path).resolve(strict=True)
    if not any(resolved.is_relative_to(home.resolve() / name)
               for name in ("sessions", "archived_sessions")):
        raise ValueError("native agent rollout outside account")
    return resolved


@dataclass
class _Cursor:
    identity: tuple[int, int]
    offset: int = 0
    mtime_ns: int = 0
    boundary: bool = False
    turn_id: str | None = None
    status: str = "pending"
    turns: set[str] = field(default_factory=set)
    title: str = "协作代理"
    complete: bool = False
    anchor: bytes = b""
    source_key: tuple = ()


class CodexAgentActivity:
    """Bounded incremental projection, owned by one Wrapper/account."""

    def __init__(self):
        self._cursors: dict[str, _Cursor] = {}
        self._previous: dict[str, dict[str, BackgroundProcessItem]] = {}
        self._lock = threading.Lock()

    def _read(self, home, sid, path, parent, budget):
        path = _local_path(home, path)
        native = path.stat(follow_symlinks=False)
        source_key = (str(path), parent, native.st_dev, native.st_ino,
                      native.st_size, native.st_mtime_ns, native.st_ctime_ns)
        cursor = self._cursors.get(sid)
        # Completed archived descendants remain in the native catalog. Do not
        # repeatedly decompress them when the bounded shared cache evicts them.
        if cursor and cursor.complete and cursor.source_key == source_key:
            return cursor
        with rollout_open(path, "rb", follow_symlinks=False) as stream:
            info = rollout_fstat(stream)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("native agent rollout is not a file")
            head = stream.readline(MAX_META_RECORD_BYTES + 1)
            if len(head) > MAX_META_RECORD_BYTES or not head.endswith(b"\n"):
                raise ValueError("incomplete native agent identity")
            record = json.loads(head)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise ValueError("invalid native agent identity")
            meta = record["payload"]
            if (record.get("type") != "session_meta" or meta.get("id") != sid
                    or (parent is not None and _parent(meta) != parent)):
                raise ValueError("native agent rollout identity mismatch")
            identity = (info.st_dev, info.st_ino)
            anchor_matches = True
            if cursor and cursor.anchor:
                stream.seek(max(0, cursor.offset - len(cursor.anchor)))
                anchor_matches = stream.read(len(cursor.anchor)) == cursor.anchor
            if (cursor is None or cursor.identity != identity or info.st_size < cursor.offset
                    or not anchor_matches
                    or (info.st_size == cursor.offset and info.st_mtime_ns != cursor.mtime_ns)):
                cursor = _Cursor(identity, offset=len(head))
                self._cursors[sid] = cursor
            cursor.title = str(meta.get("agent_path") or "协作代理")[:1024]
            stream.seek(cursor.offset)
            while stream.tell() < info.st_size and budget[0] > 0:
                offset = stream.tell()
                line = stream.readline(min(_MAX_RECORD + 1, info.st_size - offset))
                if len(line) > _MAX_RECORD:
                    raise ValueError("native agent record exceeds activity limit")
                if not line.endswith(b"\n"):
                    break  # Retry the same offset after the native writer flushes.
                budget[0] -= len(line)
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("invalid native agent record")
                payload = row.get("payload")
                if row.get("type") == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") == "thread_settings_applied" and payload.get("thread_id") == sid:
                        # Drop all copied parent lifecycle, including an open turn.
                        if not cursor.boundary:
                            cursor.turn_id, cursor.status = None, "pending"
                            cursor.boundary = True
                    markers = parse_turn_markers(line)
                    for kind, turn in markers.ordered:
                        if kind == "task_started":
                            cursor.turns.add(turn)
                            if len(cursor.turns) > _MAX_TURNS:
                                raise ValueError("native agent turn limit exceeded")
                            cursor.turn_id, cursor.status = turn, "running"
                        elif turn == cursor.turn_id:
                            # An unrelated/stale terminal cannot finish this turn.
                            cursor.status = "terminal"
                cursor.offset = stream.tell()
            stream.seek(max(0, cursor.offset - 256))
            cursor.anchor = stream.read(min(256, cursor.offset))
            cursor.mtime_ns = info.st_mtime_ns
            cursor.complete = cursor.offset == info.st_size
            after = rollout_fstat(stream)
            if is_compressed_rollout(path):
                current = path.stat(follow_symlinks=False)
                if source_key != (str(path), parent, current.st_dev, current.st_ino,
                                  current.st_size, current.st_mtime_ns, current.st_ctime_ns):
                    self._cursors.pop(sid, None)
                    raise ValueError("compressed native agent rollout changed during read")
            if after.st_size < info.st_size or (
                    after.st_size == info.st_size and after.st_mtime_ns != info.st_mtime_ns):
                self._cursors.pop(sid, None)
                raise ValueError("native agent rollout changed during read")
            cursor.source_key = source_key
        return cursor

    def read(self, home: Path, parents: set[str]) -> dict[str, list[BackgroundProcessItem]]:
        # asyncio cancellation does not stop an in-flight to_thread reader.
        with self._lock:
            return self._snapshot(home, parents)

    def _snapshot(self, home, parents):
        parents = {sid for sid in parents if _ID.fullmatch(sid)}
        if not parents:
            self._cursors.clear()
            self._previous.clear()
            return {}
        catalog = _catalog(home, parents)
        budget = [_READ_BUDGET]
        result: dict[str, dict[str, BackgroundProcessItem]] = {sid: {} for sid in parents}
        keep = set(catalog)
        # A catalog/read failure or missing row is not evidence of termination.
        for parent in parents:
            for run_id, previous in self._previous.get(parent, {}).items():
                if run_id.removeprefix("codex-agent:") not in catalog:
                    result[parent][run_id] = previous.model_copy(update={"status": "unknown"})
        for sid, (parent, path) in catalog.items():
            run_id = f"codex-agent:{sid}"
            old = self._previous.get(parent, {}).get(run_id)
            status, title = "unknown", old.title if old else "协作代理"
            child_read = False
            scan_pending = False
            try:
                cursor = self._read(home, sid, path, parent, budget)
                child_read = True
                title = cursor.title
                scan_pending = not cursor.complete
                if cursor.complete:
                    if cursor.boundary:
                        status = cursor.status
                    else:
                        # Older native agents may omit the explicit fork boundary.
                        # Exclude copied parent turns using the same account's source.
                        from cc_remote.wrapper.codex_sessions import codex_rollout_path
                        parent_path = codex_rollout_path(parent, codex_home=home)
                        if parent_path:
                            keep.add(parent)
                            ancestor = self._read(home, parent, parent_path, None, budget)
                            scan_pending = not ancestor.complete
                            if ancestor.complete and cursor.turn_id not in ancestor.turns:
                                status = cursor.status
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                # Keep other agents/tasks live when one rollout is being rewritten.
                self._cursors.pop(sid, None)
                if isinstance(exc, FileNotFoundError) and not child_read and old is None:
                    # Native catalogs can retain deleted/archived child paths for
                    # months. A stale row alone cannot introduce a live task.
                    # Previously observed activity still remains unknown below.
                    continue
            if status == "terminal":
                continue
            if scan_pending and old is None:
                # A budget-limited cold scan is not evidence of a new task.
                # Retain already-observed activity as unknown, but do not flash
                # months-old archived children before their terminals are read.
                continue
            result[parent][run_id] = BackgroundProcessItem(
                item_id=run_id, kind="agent", status=status, title=title,
                summary="状态待确认" if status == "unknown" else "协作代理运行中"
                if status == "running" else "等待协作代理启动",
            )
        self._cursors = {sid: cursor for sid, cursor in self._cursors.items() if sid in keep}
        bounded = {
            parent: sorted(items.values(), key=lambda item: item.status == "unknown")
            [:MAX_BACKGROUND_PROCESS_ITEMS] for parent, items in result.items()
        }
        self._previous = {
            parent: {item.item_id: item for item in items} for parent, items in bounded.items()
        }
        return bounded
