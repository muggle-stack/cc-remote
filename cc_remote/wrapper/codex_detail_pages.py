"""Bounded, read-only intra-turn pages for oversized Codex rollout segments.

Conversation cursors still navigate between human inputs. These private cursors
walk source windows *inside* one input, then reuse the ordinary display-group
pager. No source path or caller-supplied byte offset crosses the wire.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import os
import threading
from typing import Callable
from uuid import uuid4

from cc_remote.protocol import TurnBinding, UserMsg
from cc_remote.wrapper.codex_stream import (
    _history_boundary_records,
    _next_jsonl_offset,
    _previous_jsonl_record_offset,
    codex_history_boundary_user,
    codex_next_user_boundary_ts,
    codex_translate_history,
)
from cc_remote.wrapper.history_store import (
    HistorySourceFingerprint,
    history_source_extends,
)

PREFIX = "cd1."
_MAX_SNAPSHOTS = 32
_MAX_PAGES = 8192
_Position = tuple[int, str | None]
_Page = tuple[list[dict], bool, str | None, bool, str | None]


class CodexDetailCursorExpired(ValueError):
    """The exact source/page chain is no longer available; restart explicitly."""


@dataclass
class _Snapshot:
    sid: str
    turn_id: str
    revision: str
    source: HistorySourceFingerprint
    start: int
    end: int
    native_turn_id: str | None
    user: UserMsg
    window_bytes: int
    limit: int
    max_bytes: int
    tool_result_max: int
    segment_end_ts: float | None = None
    # Every accepted coordinate was issued by this pager, never supplied by
    # the browser. Parent links keep reverse navigation exact across windows.
    pages: dict[_Position, _Position | None] = field(default_factory=dict)


class CodexDetailPages:
    def __init__(self) -> None:
        self._snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _cursor(key: str, position: _Position) -> str:
        end, before = position
        return f"{PREFIX}{key}.{end:x}.{before if before is not None else 'n'}"

    def read(
        self, path: str, sid: str, turn_id: str, revision: str,
        *, before: str | None, limit: int, window_bytes: int,
        max_bytes: int, tool_result_max: int, paginate: Callable[..., _Page],
    ) -> _Page | None:
        """Return None for ordinary turns; reject stale private cursors."""
        with self._lock:
            return self._read(
                path, sid, turn_id, revision, before=before, limit=limit,
                window_bytes=max(1024 * 1024, window_bytes),
                max_bytes=max_bytes, tool_result_max=tool_result_max,
                paginate=paginate,
            )

    def _read(
        self, path: str, sid: str, turn_id: str, revision: str,
        *, before: str | None, limit: int, window_bytes: int,
        max_bytes: int, tool_result_max: int, paginate: Callable[..., _Page],
    ) -> _Page | None:
        if before is not None:
            if not before.startswith(PREFIX):
                return None
            try:
                _, key, encoded_end, encoded_before = before.split(".")
                position = (int(encoded_end, 16),
                            None if encoded_before == "n" else encoded_before)
                snapshot = self._snapshots[key]
            except (ValueError, KeyError):
                raise CodexDetailCursorExpired("expired Codex detail cursor") from None
            if (snapshot.sid != sid or snapshot.turn_id != turn_id
                    or snapshot.revision != revision or snapshot.limit != limit
                    or position not in snapshot.pages
                    or self._cursor(key, position) != before):
                raise CodexDetailCursorExpired("Codex detail cursor scope changed")
        else:
            if os.path.getsize(path) <= window_bytes:
                return None
            source = HistorySourceFingerprint.capture(path)
            end = source.size
            # Never freeze a half-written JSONL record into an immutable page.
            with open(path, "rb") as stream:
                stream.seek(end - 1)
                if stream.read(1) != b"\n":
                    end = _previous_jsonl_record_offset(path, end)
            found = None
            for boundary in _history_boundary_records(
                path, use_turns=True, end_offset=end,
            ):
                if turn_id in (boundary.cursor, boundary.compatibility_cursor):
                    found = boundary
                    break
                end = boundary.offset
            if found is None or end - found.offset <= window_bytes:
                return None
            user = codex_history_boundary_user(path, found.offset, found.cursor)
            if user is None:
                # An assistant-only native task has a proven boundary too.
                user = UserMsg(msg_id=found.cursor, prompt="", ts=0)
            user = user.model_copy(update={"msg_id": turn_id})
            snapshot = _Snapshot(
                sid, turn_id, revision, source, found.offset, end,
                found.native_turn_id, user, window_bytes, limit, max_bytes,
                tool_result_max,
                segment_end_ts=(codex_next_user_boundary_ts(
                                    path, end, found.native_turn_id, end_offset=source.size)
                                if end < source.size else None),
            )
            key = uuid4().hex
            position = (end, None)
            snapshot.pages[position] = None
            self._snapshots[key] = snapshot
        self._snapshots.move_to_end(key)
        while len(self._snapshots) > _MAX_SNAPSHOTS:
            self._snapshots.popitem(last=False)

        current = HistorySourceFingerprint.capture(path)
        if not history_source_extends(snapshot.source, current):
            self._snapshots.pop(key, None)
            raise CodexDetailCursorExpired("Codex rollout was replaced or truncated")
        end, group_before = position
        start = snapshot.start
        if end - start > snapshot.window_bytes:
            start = _next_jsonl_offset(path, end - snapshot.window_bytes, end)
            if start == end:
                # A single oversized record is skipped by the bounded reader;
                # still advance past it instead of issuing the same page forever.
                start = max(snapshot.start, _previous_jsonl_record_offset(path, end))
        events, _ = codex_translate_history(
            path, snapshot.tool_result_max, start_offset=start, end_offset=end,
            source_continuation="authoritative_page",
            source_turn_id=snapshot.native_turn_id,
            segment_end_ts=snapshot.segment_end_ts if end == snapshot.end else None,
            snapshot_in_progress=True,
        )
        # The source window may omit its user/start. Always repeat the proven
        # envelope; never project the chunk as a new question or a new task.
        if start > snapshot.start:
            events.insert(0, snapshot.user)
        if snapshot.native_turn_id:
            events.insert(1, TurnBinding(
                msg_id=turn_id, turn_id=snapshot.native_turn_id,
                ts=snapshot.user.ts,
            ))
        rows = [event.model_dump(mode="json") for event in events]
        for row in rows:
            row["sid"] = sid
            if row.get("type") == "user_msg":
                row["msg_id"] = turn_id
        page, has_more, older, _has_newer, _newer = paginate(
            rows, before=group_before, limit=snapshot.limit,
            max_bytes=snapshot.max_bytes,
        )
        # Validate again after parsing, so a concurrent rollback cannot publish
        # an apparently authoritative page from a different source generation.
        if not history_source_extends(
            snapshot.source, HistorySourceFingerprint.capture(path),
        ):
            self._snapshots.pop(key, None)
            raise CodexDetailCursorExpired("Codex rollout changed during detail read")
        older_position = ((end, older) if has_more else
                          (start, None) if start > snapshot.start else None)
        if older_position is not None:
            if len(snapshot.pages) >= _MAX_PAGES and older_position not in snapshot.pages:
                raise CodexDetailCursorExpired("Codex detail page chain expired")
            snapshot.pages.setdefault(older_position, position)
        newer_position = snapshot.pages[position]
        return (
            page, older_position is not None,
            self._cursor(key, older_position) if older_position is not None else None,
            newer_position is not None,
            self._cursor(key, newer_position) if newer_position is not None else None,
        )
