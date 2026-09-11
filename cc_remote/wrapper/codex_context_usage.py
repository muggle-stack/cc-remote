"""Read Codex's compaction estimate, bound to a persisted usage notification.

The app-server's tokenUsage.last is model billing usage, not its compaction
counter. Native diagnostic rows expose the latter, but an arbitrary latest row
can predate a compaction, rollback or account switch. Accept a row only when it
matches the newest rollout usage sample, native turn and effective window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER
from cc_remote.wrapper.codex_sessions import codex_rollout_path
from cc_remote.wrapper.work_context import _bounded_context_tail

_MARKER = "post sampling token usage "
_FIELDS = re.compile(r"(?:^|\s)([a-z_]+)=([^\s]+)")
_USAGE_KEYS = (
    ("total_tokens", "totalTokens"), ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"), ("cached_input_tokens", "cachedInputTokens"),
    ("reasoning_output_tokens", "reasoningOutputTokens"),
)


@dataclass(frozen=True)
class CodexContextEstimate:
    used_tokens: int
    threshold_tokens: int | None


def _number(value: str | None) -> int | None:
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 16:
        return None
    number = int(value)
    return number if number <= MAX_SAFE_WIRE_INTEGER else None


def _optional_number(value: str | None) -> int | None:
    return _number(value[5:-1]) if value and value.startswith("Some(") and value.endswith(")") else None


def _sample(
    lines: list[bytes], last: dict, window: int, turn_id: str | None,
) -> tuple[float, str] | None:
    sample = None
    for raw in reversed(lines):
        if len(raw) > 1024 * 1024:
            if sample is None:
                # An oversized newer record can itself be a compaction. Never
                # skip it and resurrect the old counter beneath that boundary.
                return None
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if sample is None:
            if record.get("type") == "compacted":
                return None
            if record.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if (not isinstance(usage, dict)
                    or info.get("model_context_window") != window
                    or type(last.get("totalTokens")) is not int
                    or any(usage.get(snake) != last[camel]
                           for snake, camel in _USAGE_KEYS if camel in last)):
                return None
            try:
                timestamp = record.get("timestamp")
                if not isinstance(timestamp, str):
                    return None
                stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    return None
                sample = stamp.timestamp()
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            if turn_id:
                return sample, turn_id
        elif (record.get("type") == "turn_context"
                or record.get("type") == "event_msg" and payload.get("type") == "task_started"):
            owner = payload.get("turn_id")
            return (sample, owner) if isinstance(owner, str) and owner else None
    return None


def read_codex_context_estimate(
    session_id: str, *, codex_home: str | None, last: dict,
    window: int, turn_id: str | None = None,
) -> CodexContextEstimate | None:
    """Bounded, account-scoped read; unavailable/ambiguous evidence stays unknown.

    No engine RPCs, database writes, full-log scans or private log text cross the
    control link. The timestamp comes from the source, never browser/WS latency.
    """
    path = codex_rollout_path(session_id, codex_home=codex_home)
    if not path or window <= 0:
        return None
    try:
        source = os.stat(path)
        lines = _bounded_context_tail(path)
        sample = _sample(lines, last, window, turn_id) if lines is not None else None
        if sample is None:
            return None
        observed_at, owner = sample
        home = Path(codex_home or os.environ.get("CODEX_HOME") or "~/.codex").expanduser()
        db = sqlite3.connect((home / "logs_2.sqlite").resolve().as_uri() + "?mode=ro",
                             uri=True, timeout=0.05)
        try:
            deadline = time.monotonic() + 0.1
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            db.execute("PRAGMA query_only=ON")
            # thread_id/ts is a native index. Bound time, row count and body
            # size too; missing indexes or a changed schema fail within 100ms.
            rows = db.execute(
                "SELECT ts, ts_nanos, substr(feedback_log_body, "
                "instr(feedback_log_body, ?), 2048) FROM logs "
                "WHERE thread_id=? AND ts BETWEEN ? AND ? "
                "AND target='codex_core::session::turn' "
                "AND instr(feedback_log_body, ?) > 0 "
                "ORDER BY ts DESC, ts_nanos DESC LIMIT 3",
                (_MARKER, session_id, int(observed_at), int(observed_at) + 1, _MARKER),
            ).fetchall()
        finally:
            db.close()
        # Concurrent append/rewrite invalidates the snapshot. The next bounded
        # read retries rather than retaining a potentially pre-compaction value.
        current = os.stat(path)
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns)):
            return None
        matches = [(body, dict(_FIELDS.findall(body[len(_MARKER):])))
                   for seconds, nanos, body in rows
                   if isinstance(body, str) and type(seconds) is int and type(nanos) is int
                   and 0 <= seconds + nanos / 1e9 - observed_at <= 1]
        if len(matches) != 1:
            return None
        _, fields = matches[0]
        used = _number(fields.get("total_usage_tokens"))
        if (fields.get("turn_id") != owner or used is None
                or _optional_number(fields.get("full_context_window_limit")) != window
                or fields.get("auto_compact_limit_scope") != "Total"
                or _number(fields.get("auto_compact_scope_tokens")) != used):
            return None
        threshold = _optional_number(fields.get("auto_compact_scope_limit"))
        if threshold is not None and threshold > window:
            return None
        return CodexContextEstimate(used, threshold)
    except (OSError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return None
