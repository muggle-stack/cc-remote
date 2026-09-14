"""Work-only context growth layered over authoritative engine totals."""
from __future__ import annotations

import json
import os
from typing import Any

from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER
from cc_remote.wrapper.claude_compaction import compact_context_usage
from cc_remote.wrapper.codex_sessions import codex_rollout_path
from cc_remote.wrapper.stream import _bounded_jsonl_lines, transcript_path


_BASELINE_HISTORY_RECORD_LIMIT = 256
_CONTEXT_TAIL_SCAN_BYTES = 4 * 1024 * 1024
_CONTEXT_RECORD_MAX_BYTES = 1024 * 1024


def _nonnegative_int(value: object) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, int) or value < 0
            or value > MAX_SAFE_WIRE_INTEGER):
        return None
    return value


def _bounded_context_tail(path: str) -> list[bytes] | None:
    """Read one source-stable JSONL tail used by context recovery.

    Appends after the captured size are harmless: the returned records describe
    a slightly older, internally complete snapshot.  Replacement or truncation
    invalidates the sample because those bytes no longer identify the same
    native conversation.
    """
    try:
        with open(path, "rb") as history:
            before = os.fstat(history.fileno())
            size = before.st_size
            start = max(0, size - _CONTEXT_TAIL_SCAN_BYTES)
            read_start = max(0, start - 1)
            history.seek(read_start)
            data = history.read(size - read_start)
            after = os.fstat(history.fileno())
        current = os.stat(path)
    except OSError:
        return None

    if (before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or after.st_size < size
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or current.st_size < size):
        return None

    starts_at_record_boundary = start == 0
    if start > 0:
        starts_at_record_boundary = data[:1] == b"\n"
        data = data[1:]
    lines = data.splitlines()
    if not starts_at_record_boundary and lines:
        lines = lines[1:]
    return lines


def claude_recent_context_usage(
    usage: object,
) -> dict[str, Any] | None:
    """Project one top-level Claude assistant response into current depth.

    Anthropic prompt-cache counters partition the input depth.  The completed
    assistant output will join that depth on the next model call, so include it
    exactly once.  This is intentionally a labelled recent-turn fallback, not
    a replacement for the richer native ``get_context_usage`` breakdown.
    """
    if not isinstance(usage, dict):
        return None
    total = 0
    observed = False
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        if key not in usage:
            continue
        value = _nonnegative_int(usage.get(key))
        if value is None:
            return None
        observed = True
        total += value
        if total > MAX_SAFE_WIRE_INTEGER:
            return None
    if not observed or total <= 0:
        return None
    # AssistantMessage/transcript model ids can be the proxy's upstream model
    # rather than the Claude alias selected by the user. Model presentation is
    # therefore added later from the session's authoritative control state.
    return {"totalTokens": total}


def recover_claude_context_usage(
    session_id: str,
    *,
    path: str | None = None,
) -> dict[str, Any] | None:
    """Recover the newest main-chain Claude context depth from its JSONL tail."""
    source_path = path or transcript_path(session_id)
    if not source_path:
        return None
    lines = _bounded_context_tail(source_path)
    if lines is None:
        return None
    for raw in reversed(lines):
        if not raw or len(raw) > _CONTEXT_RECORD_MAX_BYTES:
            continue
        try:
            record = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if (isinstance(record, dict)
                and record.get("type") == "system"
                and record.get("subtype") == "compact_boundary"
                and record.get("isSidechain") is not True
                and record.get("parentToolUseID") is None
                and record.get("parent_tool_use_id") is None):
            # Never resurrect a pre-compact assistant count when the latest
            # boundary has no post count (older CLI versions can omit it).
            return compact_context_usage(record)
        if (not isinstance(record, dict)
                or record.get("type") != "assistant"
                or record.get("isSidechain") is True
                or record.get("parentToolUseID") is not None
                or record.get("parent_tool_use_id") is not None):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        recovered = claude_recent_context_usage(message.get("usage"))
        if recovered is not None:
            return recovered
    return None


def recover_codex_context_usage(
    session_id: str,
    *,
    codex_home: str | None = None,
) -> dict[str, Any] | None:
    """Recover the newest persisted Codex context sample from a bounded tail.

    Lightweight ``thread/resume`` does not replay historical tokenUsage
    notifications.  Resolve the rollout inside the selected account namespace
    and inspect only its tail, so even multi-gigabyte sessions remain cheap.
    """
    path = (
        codex_rollout_path(session_id)
        if codex_home is None
        else codex_rollout_path(session_id, codex_home=codex_home)
    )
    if not path:
        return None
    lines = _bounded_context_tail(path)
    if lines is None:
        return None
    for raw in reversed(lines):
        if not raw or len(raw) > _CONTEXT_RECORD_MAX_BYTES:
            continue
        try:
            record = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if (record.get("type") != "event_msg"
                or not isinstance(payload, dict)
                or payload.get("type") != "token_count"):
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        source = info.get("last_token_usage")
        if not isinstance(source, dict):
            source = info.get("last")
        if not isinstance(source, dict):
            continue
        total = _nonnegative_int(source.get("total_tokens"))
        if total is None:
            total = _nonnegative_int(source.get("totalTokens"))
        window = _nonnegative_int(info.get("model_context_window"))
        if window is None:
            window = _nonnegative_int(info.get("modelContextWindow"))
        if total is None or window is None or window <= 0:
            continue
        last: dict[str, int] = {"totalTokens": total}
        for snake, camel in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
        ):
            value = _nonnegative_int(source.get(snake))
            if value is None:
                value = _nonnegative_int(source.get(camel))
            if value is not None:
                last[camel] = value
        return {
            "last": last,
            "modelContextWindow": window,
        }
    return None


def initial_work_context_baseline(engine: str, usage: dict[str, Any]) -> int:
    """Return the fresh Work session's startup zero point.

    Claude is normally sampled before its first query by ``SdkHandle.connect``.
    The fallback is still useful for migrated sessions. Codex app-server emits
    token usage only after a turn, so its first input depth is the closest
    authoritative startup measurement; output stays user context.
    """
    if engine == "codex":
        raw = usage.get("raw") if isinstance(usage.get("raw"), dict) else {}
        last = raw.get("last") if isinstance(raw.get("last"), dict) else {}
        value = _nonnegative_int(last.get("inputTokens"))
        if value is not None:
            return value
        value = _nonnegative_int(usage.get("used_tokens"))
        return value or 0
    value = _nonnegative_int(usage.get("totalTokens"))
    return value or 0


def recover_work_context_baseline(
    engine: str,
    session_id: str,
    *,
    codex_home: str | None = None,
    claude_path: str | None = None,
) -> int | None:
    """Recover a migrated Work session's first authoritative input depth.

    Both native histories record input usage after the first turn. That is the
    same startup zero point used for new Codex Work sessions and avoids treating
    an old conversation's *current* depth as engine overhead after an upgrade.
    """
    if engine == "codex":
        path = (
            codex_rollout_path(session_id)
            if codex_home is None
            else codex_rollout_path(session_id, codex_home=codex_home)
        )
    else:
        path = claude_path or transcript_path(session_id)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as history:
            for index, line in enumerate(_bounded_jsonl_lines(history)):
                if index >= _BASELINE_HISTORY_RECORD_LIMIT:
                    break
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if engine == "codex":
                    payload = (record.get("payload")
                               if record.get("type") == "event_msg" else None)
                    if (not isinstance(payload, dict)
                            or payload.get("type") != "token_count"):
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    last = info.get("last_token_usage")
                    if not isinstance(last, dict):
                        last = info.get("last")
                    value = (_nonnegative_int(last.get("input_tokens"))
                             if isinstance(last, dict) else None)
                    if value is None and isinstance(last, dict):
                        value = _nonnegative_int(last.get("inputTokens"))
                    if value is not None and value > 0:
                        return value
                    continue

                message = (record.get("message")
                           if record.get("type") == "assistant" else None)
                usage = message.get("usage") if isinstance(message, dict) else None
                if not isinstance(usage, dict):
                    continue
                total = sum(
                    _nonnegative_int(usage.get(key)) or 0
                    for key in (
                        "input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
                if total > 0:
                    return total
    except (OSError, UnicodeError):
        return None
    return None


def work_context_metrics(
    engine: str,
    usage: dict[str, Any],
    baseline_tokens: int | None,
) -> tuple[int, int, float, int]:
    """Split Work's raw total into startup baseline and later growth.

    The returned tuple is ``session, fixed, session_percentage, baseline``.
    Raw totals are deliberately not changed: callers still use them for the
    actual remaining context capacity and compaction threshold.
    """
    raw_key = "used_tokens" if engine == "codex" else "totalTokens"
    max_key = "context_window" if engine == "codex" else "maxTokens"
    raw_total = _nonnegative_int(usage.get(raw_key)) or 0
    max_tokens = _nonnegative_int(usage.get(max_key)) or 0
    baseline = _nonnegative_int(baseline_tokens)
    if baseline is None:
        baseline = initial_work_context_baseline(engine, usage)
    fixed = min(raw_total, baseline)
    session = max(0, raw_total - fixed)
    percentage = session / max_tokens * 100.0 if max_tokens else 0.0
    return session, fixed, percentage, baseline
