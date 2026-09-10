"""Translate Codex app-server notifications into the remote rich-event model.

Only app-server fields that are explicitly part of its public client protocol are
forwarded.  In particular, reasoning *summary* is visible, while raw/encrypted
reasoning and terminal stdin are deliberately hidden.  A terminal-interaction
marker is still forwarded so the remote timeline does not silently omit the step.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import islice
from typing import Callable

from pydantic import ValidationError

from cc_remote.attachments import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_SINGLE_ATTACHMENT_BYTES,
    decode_attachment,
    image_dimensions,
)
from cc_remote.protocol import (
    AssistantMsgStart, Delta, ToolUse, ToolDelta, ToolResult, AssistantMsgEnd,
    AsyncQuestionSpec,
    ProcessEvent, TurnPlan, TurnDiff, TurnEnd, TurnResult, UserMsg, Error,
    StateEvent, ERR_CC_CRASH,
)
from cc_remote.wrapper.codex_external import (
    codex_rollout_user_message,
    codex_user_item_text,
    is_codex_account_switch_message,
    visible_codex_user_message,
)
from cc_remote.wrapper.sanitize import bounded_text, bounded_tool_input

_TOOL_TYPES = {
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "webSearch",
}
_PROCESS_ITEM_TYPES = {
    "plan", "reasoning", "collabAgentToolCall", "subAgentActivity",
    "contextCompaction", "imageView", "sleep", "imageGeneration",
    "enteredReviewMode", "exitedReviewMode",
}
_MAX_HISTORY_RECORD_CHARS = 16 * 1024 * 1024
_SAFE_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_CREDENTIAL_EXACT_KEYS = frozenset({"env", "environment"})
_CREDENTIAL_KEY_FRAGMENTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "authorization",
    "credential",
    "cookie",
    "accesskey",
    "privatekey",
    "apikey",
)
_REDACTED = "[REDACTED]"
_REDACTION_BUDGET_EXCEEDED = "<redaction budget exceeded>"
_REDACTION_REMAINDER_KEY = "<remaining omitted>"
_MAX_REDACTION_DEPTH = 6
_MAX_REDACTION_NODES = 2048
_MAX_REDACTION_DICT_ITEMS = 64
_MAX_REDACTION_SEQUENCE_ITEMS = 32
_EMPTY_COMPLETED_MESSAGE = (
    "Codex 回合已结束，但没有返回任何内容；上游服务可能暂时不可用，请重试。"
)
_MAX_DELTA_STREAMS = 2048
_MAX_DELTA_EVENTS_PER_STREAM = 1024
_MAX_FINISHED_DELTA_ITEMS = 4096
_LIVE_DELTA_FLUSH_SECONDS = 0.05
_MAX_LIVE_ITEMS = 4096
_LIVE_ITEMS_OMITTED_ID = "cc-remote-live-items-omitted"
_DELTA_TRUNCATION_NOTICE = "\n…（后续输出已截断）"
_MODEL_NAME_MAX_CHARS = 256
_MODEL_ENUM_MAX_CHARS = 256
_MODEL_LIST_MAX_ITEMS = 32
_MODEL_DETAIL_MAX_CHARS = 16 * 1024
_DEFAULT_HISTORY_WINDOW_MAX_BYTES = 32 * 1024 * 1024
_REVERSE_HISTORY_CHUNK_BYTES = 1024 * 1024
_MAX_HISTORY_REVERSE_RECORD_BYTES = 1024 * 1024
_MAX_HISTORY_BOUNDARY_RECORD_BYTES = 1024 * 1024
_MAX_HISTORY_BOUNDARY_FORWARD_BYTES = 64 * 1024 * 1024
_MAX_OFFICIAL_AUTOMATIC_USER_SCAN_BYTES = 64 * 1024 * 1024
_MAX_STREAM_BINDING_SCAN_BYTES = 64 * 1024 * 1024
_MAX_STREAM_BINDING_MESSAGE_IDS = 64
MIN_PROCESS_DURATION_MS = 500


def _async_message_fields(item: dict) -> dict:
    """Project only native async metadata, never infer a question from prose.

    Keep oversized/unrecognized payloads readable as text instead of failing an
    entire stream. Do not partially truncate a question or its selectable labels.
    """
    if item.get("delivery") != "async":
        return {}
    fields: dict = {"delivery": "async"}
    questions = item.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 16:
        return fields
    if len(json.dumps(questions, ensure_ascii=False)) > 16 * 1024:
        return fields
    try:
        parsed = [AsyncQuestionSpec.model_validate(q) for q in questions]
        if len(json.dumps([q.model_dump() for q in parsed],
                          ensure_ascii=False)) <= 16 * 1024:
            fields["questions"] = parsed
    except ValidationError:
        pass
    return fields


@dataclass(frozen=True)
class CodexLiveUserMessage:
    """A visible user boundary carried by the app-server live protocol."""

    message_id: str
    turn_id: str
    prompt: str
    client_id: str | None = None


def codex_live_user_message(message: object) -> CodexLiveUserMessage | None:
    """Normalize one live ``userMessage`` item without accepting foreign shapes."""
    if not isinstance(message, dict) or message.get("method") not in {
        "item/started", "item/completed",
    }:
        return None
    params = message.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict) or item.get("type") != "userMessage":
        return None
    message_id = item.get("id")
    turn_id = params.get("turnId")
    if (
        not isinstance(message_id, str)
        or not _SAFE_WIRE_ID.fullmatch(message_id)
        or not isinstance(turn_id, str)
        or not _SAFE_WIRE_ID.fullmatch(turn_id)
    ):
        return None
    prompt = visible_codex_user_message(codex_user_item_text(item))
    if not prompt:
        return None
    client_id = item.get("clientId")
    if not isinstance(client_id, str) or not _SAFE_WIRE_ID.fullmatch(client_id):
        client_id = None
    return CodexLiveUserMessage(
        message_id=message_id,
        turn_id=turn_id,
        prompt=prompt,
        client_id=client_id,
    )


def codex_rollout_task_bindings(
    path: str,
    native_message_ids: set[str] | frozenset[str] | tuple[str, ...],
    *,
    max_scan_bytes: int = _MAX_STREAM_BINDING_SCAN_BYTES,
) -> dict[str, str]:
    """Resolve official user-item ids to exact rollout task ids.

    This is deliberately an identity lookup, not a history heuristic.  It only
    accepts a ``response_item`` user message whose exact native item id carries
    Codex's own ``internal_chat_message_metadata_passthrough.turn_id``. Prompt
    text, timestamps, ordering and file size never participate in attribution.
    """
    targets = {
        value
        for value in native_message_ids
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value)
    }
    if (
        not targets
        or len(targets) > _MAX_STREAM_BINDING_MESSAGE_IDS
        or isinstance(max_scan_bytes, bool)
        or not isinstance(max_scan_bytes, int)
        or max_scan_bytes <= 0
    ):
        return {}
    bindings: dict[str, str] = {}
    try:
        records = _reverse_jsonl_records(
            path, max_scan_bytes=max_scan_bytes)
        for _offset, line in records:
            # Avoid decoding unrelated large tool/result records.
            if b'"response_item"' not in line or b'"message"' not in line:
                continue
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(row, dict) or row.get("type") != "response_item":
                continue
            payload = row.get("payload")
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "message"
                or payload.get("role") != "user"
            ):
                continue
            native_message_id = payload.get("id")
            if native_message_id not in targets:
                continue
            metadata = payload.get(
                "internal_chat_message_metadata_passthrough")
            task_id = (
                metadata.get("turn_id")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not isinstance(task_id, str)
                or not _SAFE_WIRE_ID.fullmatch(task_id)
            ):
                continue
            bindings[native_message_id] = task_id
            if len(bindings) == len(targets):
                break
    except OSError:
        return {}
    return bindings
_GOAL_TURN_CORRELATION_SECONDS = 5.0
_MAX_PENDING_HISTORY_COMPACTIONS = 32
_MAX_HISTORY_IMAGE_VIEWS_PER_SEGMENT = 128
_MAX_HISTORY_IMAGE_BYTES_PER_SEGMENT = 64 * 1024 * 1024
_HISTORY_TURN_SEARCH_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_HISTORY_TURN_MATCHES = 16 * 1024


@dataclass(frozen=True)
class CodexHistoryImageView:
    """One source-bound image-view activity recovered from a rollout turn."""

    call_id: str
    event: ProcessEvent
    previous_item_id: str | None = None
    next_item_id: str | None = None
    media_type: str | None = None
    width: int | None = None
    height: int | None = None
    data: bytes | None = None
    source_complete: bool = False


@dataclass(frozen=True)
class CodexAutomaticUserRecovery:
    """Goal prompts plus proof of which native task boundaries were inspected."""

    users: dict[str, UserMsg] = field(default_factory=dict)
    seen_turn_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CodexHistoryProcessWitness:
    """Lightweight proof that one visible rollout segment had public work.

    Only small lifecycle records participate.  Large tool inputs/results and
    private reasoning are never decoded merely to decide whether the collapsed
    ``已处理`` row exists.  Equal/missing timestamps still prove presence,
    but callers must not turn them into a synthetic ``0s`` duration.
    """

    started_ms: int | None = None
    done_ms: int | None = None
    generated_images: bool = False


@dataclass(frozen=True)
class CodexHistoryNativeWitness:
    """Bounded rollout evidence for the visible native-turn projection.

    ``turn_ids`` are newest first and de-duplicated so a native turn containing
    multiple steered user segments is compared with exactly one official
    ``thread/turns/list`` row. ``scanned_to_start`` is true only when the byte
    budget covered the complete frozen rollout; ``has_more_turns`` records a
    native boundary beyond the bounded id tuple without retaining every id.
    """

    turn_ids: tuple[str, ...] = ()
    scanned_to_start: bool = False
    has_more_turns: bool = False
    process_by_visible_id: dict[str, CodexHistoryProcessWitness] = field(
        default_factory=dict,
    )
    offset_by_visible_id: dict[str, int] = field(default_factory=dict)
    process_by_native_segment: dict[
        tuple[str, int], CodexHistoryProcessWitness
    ] = field(default_factory=dict)
    offset_by_native_segment: dict[tuple[str, int], int] = field(
        default_factory=dict,
    )


@dataclass(frozen=True)
class CodexHistoryProcessPageWitness:
    """Process proof and reusable source offsets for one older native page."""

    process_by_visible_id: dict[str, CodexHistoryProcessWitness] = field(
        default_factory=dict,
    )
    offset_by_visible_id: dict[str, int] = field(default_factory=dict)
    process_by_native_segment: dict[
        tuple[str, int], CodexHistoryProcessWitness
    ] = field(default_factory=dict)
    offset_by_native_segment: dict[tuple[str, int], int] = field(
        default_factory=dict,
    )
    scanned_to_start: bool = False
    # Only an exact newest native task/user segment can absorb an ordinary
    # append. Goal continuations and pages behind the physical head cannot.
    append_segment: tuple[str, int] | None = None


@dataclass(frozen=True)
class _CodexHistoryBoundary:
    offset: int
    cursor: str
    native_turn_id: str | None
    compatibility_cursor: str | None = None
    process: CodexHistoryProcessWitness | None = None
    # Stable within one native task: the oldest user segment is zero and the
    # newest steer is ``segment_count - 1``. Official history exposes different
    # visible item ids, so this private coordinate is the cross-source join key.
    segment_index: int = 0
    segment_count: int = 1
    appendable: bool = False


@dataclass(frozen=True)
class CodexHistoryWindow:
    """One bounded rollout page plus exact metadata from its boundary scan.

    The legacy five-tuple exposed by :func:`codex_history_window` deliberately
    omits native ownership.  Callers which need to persist source-bound facts
    must use this richer result rather than reopening one boundary record and
    guessing its enclosing task: a later steer boundary can be a paired
    ``response_item`` which does not carry ``turn_id`` itself.
    """

    start_offset: int
    end_offset: int
    has_older: bool
    forced_oldest_cursor: str | None = None
    forced_boundary_offset: int | None = None
    forced_native_turn_id: str | None = None
    forced_segment_index: int | None = None
    newest_boundary_offset: int | None = None
    newest_cursor: str | None = None
    newest_native_turn_id: str | None = None
    newest_segment_index: int | None = None

    def legacy(self) -> tuple[int, int, bool, str | None, int | None]:
        return (
            self.start_offset,
            self.end_offset,
            self.has_older,
            self.forced_oldest_cursor,
            self.forced_boundary_offset,
        )


@dataclass
class _HistoryProcessAccumulator:
    """Mutable reverse-scan accumulator for one visible user segment."""

    present: bool = False
    started_ms: int | None = None
    done_ms: int | None = None
    generated_images: bool = False

    def observe(self, stamp_ms: int | None) -> None:
        self.present = True
        if stamp_ms is None:
            return
        self.started_ms = (
            stamp_ms if self.started_ms is None
            else min(self.started_ms, stamp_ms)
        )
        self.done_ms = (
            stamp_ms if self.done_ms is None
            else max(self.done_ms, stamp_ms)
        )

    def merge(self, witness: CodexHistoryProcessWitness | None) -> None:
        if witness is None:
            return
        self.present = True
        self.generated_images |= witness.generated_images
        for stamp_ms in (witness.started_ms, witness.done_ms):
            if stamp_ms is not None:
                self.observe(stamp_ms)

    def take(self) -> CodexHistoryProcessWitness | None:
        witness = self.snapshot()
        self.present = False
        self.started_ms = None
        self.done_ms = None
        self.generated_images = False
        return witness

    def snapshot(self) -> CodexHistoryProcessWitness | None:
        if not self.present:
            return None
        return CodexHistoryProcessWitness(
            started_ms=self.started_ms,
            done_ms=self.done_ms,
            generated_images=self.generated_images,
        )


def _bounded_jsonl_records(file, *, end_offset: int | None = None):
    """Yield bounded complete records with stable absolute byte offsets.

    ``end_offset`` freezes a growing rollout at the snapshot selected by the
    history pager.  Byte offsets, unlike window-local line numbers, remain
    stable when a large file is read through different pages.
    """
    while True:
        record_offset = file.tell()
        if end_offset is not None and record_offset >= end_offset:
            return
        read_limit = _MAX_HISTORY_RECORD_CHARS + 1
        if end_offset is not None:
            read_limit = min(read_limit, end_offset - record_offset)
        line = file.readline(read_limit)
        if not line:
            return
        complete = (
            line.endswith(b"\n")
            or len(line) < _MAX_HISTORY_RECORD_CHARS + 1
            or (end_offset is not None and file.tell() >= end_offset)
        )
        if complete:
            yield record_offset, line.decode("utf-8", "replace")
            continue
        while line and not line.endswith(b"\n"):
            if end_offset is not None and file.tell() >= end_offset:
                return
            read_limit = _MAX_HISTORY_RECORD_CHARS + 1
            if end_offset is not None:
                read_limit = min(read_limit, end_offset - file.tell())
            line = file.readline(read_limit)


def _reverse_jsonl_records(
    path: str,
    *,
    max_scan_bytes: int | None = None,
    end_offset: int | None = None,
    max_record_bytes: int = _MAX_HISTORY_REVERSE_RECORD_BYTES,
):
    """Yield ``(byte_offset, line)`` from newest to oldest without buffering.

    Fixed-size reverse reads avoid mmap implementations faulting a large part
    of a multi-gigabyte rollout into RSS. Individual pathological records and
    the cross-chunk carry remain bounded.
    """
    with open(path, "rb") as source:
        source_size = os.fstat(source.fileno()).st_size
        size = source_size
        if end_offset is not None:
            size = min(source_size, max(0, int(end_offset)))
        if size <= 0:
            return
        floor = 0
        if max_scan_bytes is not None:
            floor = max(0, size - max(0, int(max_scan_bytes)))
        position = size
        carry = b""
        dropping_oversized = False
        while position > floor:
            read_size = min(
                _REVERSE_HISTORY_CHUNK_BYTES,
                position - floor,
            )
            position -= read_size
            source.seek(position)
            chunk = source.read(read_size)
            data = chunk if dropping_oversized else chunk + carry
            parts = data.split(b"\n")
            if len(parts) == 1:
                if (dropping_oversized
                        or len(data) > max_record_bytes):
                    carry = b""
                    dropping_oversized = True
                else:
                    carry = data
                continue

            starts = [position]
            for part in parts[:-1]:
                starts.append(starts[-1] + len(part) + 1)
            for index in range(len(parts) - 1, 0, -1):
                # The newest fragment still belongs to a record whose newer
                # suffix was discarded after it exceeded the per-record cap.
                if dropping_oversized and index == len(parts) - 1:
                    continue
                line = parts[index]
                if line and len(line) <= max_record_bytes:
                    yield starts[index], line
            carry = parts[0]
            dropping_oversized = len(carry) > max_record_bytes
            if dropping_oversized:
                carry = b""

        if (floor == 0 and not dropping_oversized and carry
                and len(carry) <= max_record_bytes):
            yield 0, carry


def _next_jsonl_offset(path: str, offset: int, end_offset: int) -> int:
    """Move a byte budget boundary to the next complete JSONL record."""
    if offset <= 0:
        return 0
    with open(path, "rb") as source:
        source.seek(offset - 1)
        if source.read(1) == b"\n":
            return offset
        source.seek(offset)
        while source.tell() < end_offset:
            chunk = source.readline(
                min(_MAX_HISTORY_RECORD_CHARS + 1,
                    end_offset - source.tell()))
            if not chunk:
                return end_offset
            if chunk.endswith(b"\n"):
                return source.tell()
        return end_offset


def _previous_jsonl_record_offset(path: str, offset: int) -> int:
    """Return the start of the complete record immediately before ``offset``.

    A persisted user ``response_item`` can contain a large inline image and is
    therefore intentionally skipped by the bounded reverse JSON decoder.  The
    following small ``event_msg/user_message`` remains a useful page boundary,
    but its page must start one record earlier so history replay still sees the
    image.  Locate that record by newlines without buffering or decoding it.
    """
    if offset <= 0:
        return 0
    with open(path, "rb") as source:
        position = offset - 1  # exclude the newline immediately before offset
        while position > 0:
            start = max(0, position - _REVERSE_HISTORY_CHUNK_BYTES)
            source.seek(start)
            chunk = source.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            position = start
    return 0


def _legacy_response_user_item_id(payload: object) -> str | None:
    """Return the native id from one legacy persisted user response item.

    Older Codex rollouts persist a visible user input twice: first as a
    ``response_item/message`` carrying the app-server item id, then immediately
    as an ``event_msg/user_message`` carrying the clean prompt.  The latter is
    the row history translates, but the former is the identity emitted by the
    live app-server stream.  Keep this helper deliberately structural; callers
    must additionally prove that the two records are adjacent.
    """
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "message"
        or payload.get("role") != "user"
    ):
        return None
    item_id = payload.get("id")
    if isinstance(item_id, str) and _SAFE_WIRE_ID.fullmatch(item_id):
        return item_id
    return None


def _legacy_user_item_before(
    path: str,
    offset: int,
) -> tuple[int, str | None]:
    """Return the preceding record offset and its legacy native user id."""
    if offset <= 0:
        return 0, None
    try:
        previous = _previous_jsonl_record_offset(path, offset)
        record_bytes = offset - previous
        if record_bytes > _MAX_HISTORY_REVERSE_RECORD_BYTES + 2:
            return previous, None
        with open(path, "rb") as source:
            source.seek(previous)
            raw = source.read(record_bytes).rstrip(b"\r\n")
    except OSError:
        # A disappearing/replaced source must not turn a mid-file boundary into
        # offset zero and accidentally request the entire rollout.
        return offset, None
    if not raw or len(raw) > _MAX_HISTORY_REVERSE_RECORD_BYTES:
        return previous, None
    try:
        row = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return previous, None
    if not isinstance(row, dict) or row.get("type") != "response_item":
        return previous, None
    return previous, _legacy_response_user_item_id(row.get("payload"))


def _fallback_history_id(path: str, kind: str, offset: int, raw_ts: str,
                         identity: str) -> str:
    seed = "\0".join((path, kind, identity, str(offset), raw_ts))
    return hashlib.sha256(seed.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _history_user_cursors(
    path: str,
    offset: int,
    line: bytes,
) -> tuple[str, str, str | None, int] | None:
    """Return paging identities and source boundary for one visible user row."""
    if (len(line) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES
            or (
                b'"user_message"' not in line
                and b'"UserMessage"' not in line
            )):
        return None
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    payload = row.get("payload")
    if row.get("type") != "event_msg":
        return None
    user = codex_rollout_user_message(payload)
    if user is None or not user.prompt:
        return None
    native_cursor = None
    if isinstance(user.message_id, str) and _SAFE_WIRE_ID.fullmatch(
            user.message_id):
        native_cursor = user.message_id
    previous_offset, paired_cursor = _legacy_user_item_before(path, offset)
    if native_cursor is None:
        native_cursor = paired_cursor
    turn_id = user.turn_id
    raw_ts = row.get("timestamp", "")
    fallback_cursor = _fallback_history_id(
        path, "user", offset, str(raw_ts), type(turn_id).__name__)
    per_message_cursor = native_cursor or fallback_cursor
    preferred_cursor = (
        native_cursor
        or (
            turn_id
            if isinstance(turn_id, str)
            and _SAFE_WIRE_ID.fullmatch(turn_id)
            else None
        )
        or fallback_cursor
    )
    return (
        preferred_cursor,
        per_message_cursor,
        native_cursor,
        previous_offset,
    )


def _history_user_cursor(
    path: str,
    offset: int,
    line: bytes,
    *,
    prefer_turn_id: bool = True,
) -> str | None:
    cursors = _history_user_cursors(path, offset, line)
    if cursors is None:
        return None
    return cursors[0] if prefer_turn_id else cursors[1]


def _history_turn_cursor(line: bytes) -> str | None:
    if (len(line) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES
            or (b'"type":"task_started"' not in line
                and b'"type": "task_started"' not in line)):
        return None
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if (row.get("type") != "event_msg" or not isinstance(payload, dict)
            or payload.get("type") != "task_started"):
        return None
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and _SAFE_WIRE_ID.fullmatch(turn_id):
        return turn_id
    return None


_HISTORY_TERMINAL_TYPES = frozenset({
    "task_complete",
    "turn_aborted",
    "task_failed",
    "turn_failed",
    "task_error",
    "task_cancelled",
})


def _history_terminal_marker(line: bytes) -> bool:
    if (len(line) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES
            or not any(marker.encode() in line
                       for marker in _HISTORY_TERMINAL_TYPES)):
        return False
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    payload = row.get("payload") if isinstance(row, dict) else None
    return bool(
        row.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") in _HISTORY_TERMINAL_TYPES
    )


_HISTORY_VISIBLE_PROCESS_RESPONSE_TYPES = (
    frozenset({
        "function_call",
        "custom_tool_call",
        "function_call_output",
        "custom_tool_call_output",
    })
    | _TOOL_TYPES
    | (_PROCESS_ITEM_TYPES - {"reasoning"})
)
_HISTORY_VISIBLE_PROCESS_EVENT_TYPES = frozenset({
    "image_generation_end",
    "exec_command_end",
    "mcp_tool_call_end",
    "patch_apply_end",
    "web_search_end",
    "sub_agent_activity",
    "context_compacted",
})
_HISTORY_VISIBLE_PROCESS_MARKERS = tuple(
    marker.encode()
    for marker in (
        *_HISTORY_VISIBLE_PROCESS_RESPONSE_TYPES,
        *_HISTORY_VISIBLE_PROCESS_EVENT_TYPES,
        "agent_message",
        "item_completed",
        "commentary",
        '"plan"',
    )
)


def _history_generated_image_record(line: bytes) -> bool:
    """Cheap positive hint; the exact image reader validates the full record.

    Native image payloads exceed the ordinary process-record budget. Inspect
    only their envelope, never decode the base64 while counting process rows.
    """
    header = line[:1024]
    return bool(
        re.search(rb'"type"\s*:\s*"image_generation_end"', header)
        or (
            re.search(rb'"type"\s*:\s*"item_completed"', header)
            and re.search(rb'"type"\s*:\s*"Extension"', header)
            and re.search(rb'"kind"\s*:\s*"image_gen\.generation"', header)
        )
    )


def _history_visible_process_stamp(line: bytes) -> tuple[bool, int | None]:
    """Return bounded public-process evidence from one persisted record.

    This intentionally mirrors the history translator's public surface rather
    than treating every hidden item as work.  Reasoning, final answers, token
    counts and ordinary successful plumbing therefore cannot recreate the old
    empty ``已处理`` disclosure for direct replies.
    """
    if (
        len(line) > _MAX_HISTORY_REVERSE_RECORD_BYTES
        or not any(marker in line for marker in _HISTORY_VISIBLE_PROCESS_MARKERS)
    ):
        return False, None
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False, None
    if not isinstance(row, dict):
        return False, None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return False, None
    row_type = row.get("type")
    payload_type = payload.get("type")
    visible = False
    if row_type == "response_item":
        visible = payload_type in _HISTORY_VISIBLE_PROCESS_RESPONSE_TYPES
        if (
            not visible
            and payload_type == "message"
            and payload.get("role") == "assistant"
            and _assistant_channel(payload.get("phase")) == "commentary"
        ):
            visible = any(
                isinstance(part, dict)
                and part.get("type") in {"output_text", "text"}
                and isinstance(part.get("text"), str)
                and bool(part.get("text"))
                for part in payload.get("content") or []
            )
    elif row_type == "event_msg":
        visible = payload_type in _HISTORY_VISIBLE_PROCESS_EVENT_TYPES
        if payload_type == "agent_message":
            visible = bool(
                _assistant_channel(payload.get("phase")) == "commentary"
                and isinstance(payload.get("message"), str)
                and payload.get("message")
            )
        elif payload_type == "item_completed":
            item = payload.get("item")
            visible = bool(
                isinstance(item, dict)
                and (
                    item.get("type") in _TOOL_TYPES
                    or item.get("type") in (
                        _PROCESS_ITEM_TYPES - {"reasoning"}
                    )
                )
            )
    if not visible:
        return False, None
    raw_ts = row.get("timestamp")
    if not isinstance(raw_ts, str):
        return True, None
    try:
        stamp = datetime.fromisoformat(
            raw_ts.replace("Z", "+00:00"),
        ).timestamp()
    except (TypeError, ValueError):
        return True, None
    return True, max(0, int(round(stamp * 1000)))


def _history_account_switch_marker(line: bytes) -> bool:
    if (
        len(line) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES
        or b"cc_remote_account_switch" not in line
    ):
        return False
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    payload = row.get("payload") if isinstance(row, dict) else None
    user = codex_rollout_user_message(payload)
    return bool(
        row.get("type") == "event_msg"
        and user is not None
        and is_codex_account_switch_message(user.raw_text)
    )


def _history_goal_record(
    line: bytes,
) -> tuple[
    str, str | None, float | None, float | None, str | None,
] | None:
    """Return the public Goal state carried by one bounded rollout record."""
    if (
        len(line) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES
        or b"thread_goal_" not in line
    ):
        return None
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if row.get("type") != "event_msg" or not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")
    if payload_type == "thread_goal_cleared":
        return ("cleared", None, None, None, None)
    if payload_type != "thread_goal_updated":
        return None
    goal = payload.get("goal")
    if not isinstance(goal, dict):
        return None
    objective = goal.get("objective")
    if not isinstance(objective, str) or not objective:
        return None
    created_at = goal.get("createdAt")
    updated_at = goal.get("updatedAt")
    status = goal.get("status")
    return (
        "updated",
        objective,
        float(created_at)
        if isinstance(created_at, (int, float))
        and not isinstance(created_at, bool) else None,
        float(updated_at)
        if isinstance(updated_at, (int, float))
        and not isinstance(updated_at, bool) else None,
        status if isinstance(status, str) else None,
    )


def _history_record_timestamp(line: bytes) -> float | None:
    try:
        row = json.loads(line)
        return datetime.fromisoformat(
            str(row.get("timestamp", "")).replace("Z", "+00:00")
        ).timestamp()
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _goal_turn_correlated(
    goal_timestamp: float | None,
    turn_timestamp: float | None,
) -> bool:
    return bool(
        goal_timestamp is not None
        and turn_timestamp is not None
        and 0 <= turn_timestamp - goal_timestamp
        <= _GOAL_TURN_CORRELATION_SECONDS
    )


def _history_goal_prompt_before_boundary(
    path: str,
    boundary_offset: int,
    *,
    max_scan_bytes: int = 4 * 1024 * 1024,
) -> tuple[str, float | None] | None:
    """Find a changed Goal objective immediately preceding one task start.

    The lookup is bounded and reads backwards from the already-authoritative
    native task boundary. A status-only Goal update repeats the previous
    objective and therefore remains an assistant-only continuation.
    """
    if boundary_offset < 0:
        return None
    start = max(0, boundary_offset - max_scan_bytes)
    try:
        with open(path, "rb") as source:
            source.seek(start)
            data = source.read(boundary_offset - start)
            source.seek(boundary_offset)
            boundary_line = source.readline(
                _MAX_HISTORY_BOUNDARY_RECORD_BYTES + 1,
            )
    except OSError:
        return None
    boundary_timestamp = _history_record_timestamp(
        boundary_line.rstrip(b"\r\n"))
    candidate: tuple[
        str, float | None, float | None, float | None,
    ] | None = None
    boundary_record = _history_goal_record(boundary_line.rstrip(b"\r\n"))
    if (
        boundary_record is not None
        and boundary_record[0] == "updated"
        and boundary_record[4] == "active"
    ):
        _kind, objective, created_at, updated_at, _status = boundary_record
        if objective is not None:
            timestamp = boundary_timestamp
            candidate = (objective, created_at, updated_at, timestamp)
    for raw in reversed(data.splitlines()):
        if len(raw) > _MAX_HISTORY_BOUNDARY_RECORD_BYTES:
            continue
        record = _history_goal_record(raw)
        if record is not None:
            kind, objective, created_at, updated_at, status = record
            if candidate is None:
                if kind == "cleared" or objective is None or status != "active":
                    return None
                timestamp = _history_record_timestamp(raw)
                candidate = (
                    objective, created_at, updated_at, timestamp)
                continue
            previous_objective = objective if kind == "updated" else None
            return (
                (candidate[0], candidate[3])
                if candidate[0] != previous_objective
                and _goal_turn_correlated(
                    candidate[3], boundary_timestamp)
                else None
            )
        if candidate is None:
            if _history_turn_cursor(raw) is not None:
                return None
            user_cursor = _history_user_cursor(path, 0, raw)
            if user_cursor is not None:
                return None
    if candidate is None:
        return None
    # At the beginning of a rollout, creation time equality is the only
    # authoritative proof that this is a new objective rather than a resumed
    # pre-existing Goal whose earlier state lies outside the bounded window.
    return (
        (candidate[0], candidate[3])
        if candidate[1] is not None and candidate[1] == candidate[2]
        and _goal_turn_correlated(candidate[3], boundary_timestamp)
        else None
    )


def _history_goal_prompt_after_boundary(
    path: str,
    boundary_offset: int,
    *,
    max_scan_bytes: int = 4 * 1024 * 1024,
) -> tuple[tuple[str, float | None] | None, bool]:
    """Find a new Goal written just after one native task boundary.

    The app-server may persist ``task_started`` before the correlated
    ``thread_goal_updated`` record.  Scan only this task's bounded forward
    segment and report whether a terminal/next boundary proves an empty result
    is stable.  Reaching a growing rollout's EOF is deliberately not proof: a
    later append must be allowed to repair the missing prompt.
    """
    if boundary_offset < 0:
        return (None, False)
    baseline_known, baseline_objective = (
        _history_goal_objective_before_offset(path, boundary_offset)
    )
    try:
        size = os.path.getsize(path)
        source = open(path, "rb")
    except (OSError, TypeError, ValueError):
        return (None, False)
    end_offset = min(size, boundary_offset + max_scan_bytes)
    with source:
        source.seek(boundary_offset)
        boundary_line = source.readline(
            _MAX_HISTORY_BOUNDARY_RECORD_BYTES + 1,
        )
        boundary_raw = boundary_line.rstrip(b"\r\n")
        if _history_turn_cursor(boundary_raw) is None:
            return (None, False)
        boundary_timestamp = _history_record_timestamp(boundary_raw)
        for offset, line in _bounded_jsonl_records(
            source, end_offset=end_offset,
        ):
            raw = line.encode("utf-8", "replace").rstrip(b"\r\n")
            if _history_turn_cursor(raw) is not None:
                return (None, True)
            if _history_user_cursor(path, offset, raw) is not None:
                return (None, True)
            record = _history_goal_record(raw)
            if record is not None:
                kind, objective, created_at, updated_at, status = record
                if kind == "cleared" or objective is None:
                    baseline_known = True
                    baseline_objective = None
                    continue
                changed = bool(
                    objective != baseline_objective
                    and (
                        baseline_known
                        or (
                            created_at is not None
                            and created_at == updated_at
                        )
                    )
                )
                baseline_known = True
                baseline_objective = objective
                goal_timestamp = _history_record_timestamp(raw)
                if (
                    changed
                    and status == "active"
                    and goal_timestamp is not None
                    and boundary_timestamp is not None
                    and 0 <= goal_timestamp - boundary_timestamp
                    <= _GOAL_TURN_CORRELATION_SECONDS
                ):
                    return ((objective, goal_timestamp), True)
                continue
            if _history_terminal_marker(raw):
                return (None, True)
    return (None, False)


def _history_goal_prompt_for_boundary(
    path: str,
    boundary_offset: int,
) -> tuple[tuple[str, float | None] | None, bool]:
    prompt = _history_goal_prompt_before_boundary(path, boundary_offset)
    if prompt is not None:
        return (prompt, True)
    return _history_goal_prompt_after_boundary(path, boundary_offset)


def _history_goal_objective_before_offset(
    path: str,
    offset: int,
    *,
    max_scan_bytes: int = 4 * 1024 * 1024,
) -> tuple[bool, str | None]:
    """Return the nearest bounded Goal baseline before a history page."""
    if offset <= 0:
        return (False, None)
    start = max(0, offset - max_scan_bytes)
    try:
        with open(path, "rb") as source:
            source.seek(start)
            data = source.read(offset - start)
    except OSError:
        return (False, None)
    for raw in reversed(data.splitlines()):
        record = _history_goal_record(raw)
        if record is None:
            continue
        kind, objective, _created_at, _updated_at, _status = record
        return (True, objective if kind == "updated" else None)
    return (False, None)


def _history_boundary_records(
    path: str,
    *,
    use_turns: bool,
    max_scan_bytes: int | None = None,
    end_offset: int | None = None,
    include_process: bool = False,
):
    if not use_turns:
        for offset, line in _reverse_jsonl_records(
            path,
            max_scan_bytes=max_scan_bytes,
            end_offset=end_offset,
        ):
            cursor = _history_user_cursor(path, offset, line)
            if cursor is not None:
                yield _CodexHistoryBoundary(offset, cursor, None)
        return

    # A task_started without a user_message is ambiguous. It is an independent
    # goal/background turn only when the preceding visible turn had already
    # reached a terminal record; otherwise it is an automatic continuation of
    # the same visible reply. Resolve that relationship while walking backwards
    # so pagination counts visible chat turns, not internal app-server turns.
    # Multiple user messages may be steered into one still-running Codex task.
    # A message with a native app-server item id uses it as the visible cursor,
    # matching live replay. Older rows without that identity retain the task
    # cursor for the oldest message; every newer steer gets its own fallback id.
    # Delaying emission until task_started preserves reverse chronological order
    # and keeps the separate native task id available for ownership evidence.
    segment_users: list[
        tuple[
            int,
            str,
            str,
            str | None,
            CodexHistoryProcessWitness | None,
        ]
    ] = []
    segment_account_switch = False
    pending_assistant_only: tuple[
        int,
        str,
        CodexHistoryProcessWitness | None,
    ] | None = None
    # A private account-switch query resumes the preceding visible prompt under
    # a new native task.  The forward history translator deliberately hides
    # that internal user row and merges its output into the preceding turn, so
    # the lightweight reverse witness must carry its public process evidence
    # across the interrupted terminal boundary as well.
    pending_account_switch = _HistoryProcessAccumulator()
    process = _HistoryProcessAccumulator()
    saw_native_start = False
    for offset, line in _reverse_jsonl_records(
        path,
        max_scan_bytes=max_scan_bytes,
        end_offset=end_offset,
        # Inspect only the short event header below. The image payload is never
        # JSON-decoded here; existing total scan and per-record bounds remain.
        max_record_bytes=(12 * 1024 * 1024 if include_process
                          else _MAX_HISTORY_REVERSE_RECORD_BYTES),
    ):
        if include_process:
            if _history_generated_image_record(line):
                process.observe(None)
                process.generated_images = True
            visible_process, process_stamp = _history_visible_process_stamp(line)
            if visible_process:
                process.observe(process_stamp)
        if _history_account_switch_marker(line):
            segment_account_switch = True
            continue
        user_cursors = _history_user_cursors(path, offset, line)
        if user_cursors is not None:
            (
                user_cursor,
                fallback_cursor,
                native_cursor,
                previous_offset,
            ) = user_cursors
            segment_users.append((
                previous_offset,
                user_cursor,
                fallback_cursor,
                native_cursor,
                process.take(),
            ))
            continue

        turn_cursor = _history_turn_cursor(line)
        if turn_cursor is not None:
            is_native_head = not saw_native_start
            saw_native_start = True
            # An unresolved newer no-user start had no terminal boundary
            # between it and this older start, so it merely continued this turn.
            if pending_assistant_only is not None:
                # The newer assistant-only task had no separating terminal, so
                # its public work belongs to the newest visible user segment of
                # this task. This is the persisted automatic-continuation shape.
                _offset, _turn, continuation = pending_assistant_only
                if segment_users:
                    current = _HistoryProcessAccumulator()
                    current.merge(segment_users[0][4])
                    current.merge(continuation)
                    segment_users[0] = (
                        *segment_users[0][:4], current.snapshot(),
                    )
                else:
                    process.merge(continuation)
                pending_assistant_only = None
            account_switch_continuation = (
                pending_account_switch.take()
            )
            if account_switch_continuation is not None:
                if segment_users:
                    current = _HistoryProcessAccumulator()
                    current.merge(segment_users[0][4])
                    current.merge(account_switch_continuation)
                    segment_users[0] = (
                        *segment_users[0][:4], current.snapshot(),
                    )
                else:
                    process.merge(account_switch_continuation)
            if segment_users:
                # A compact/process marker between task_started and the oldest
                # user row is flushed onto that row by the forward translator.
                process.merge(segment_users[-1][4])
                segment_users[-1] = (
                    *segment_users[-1][:4], process.take(),
                )
                # Reverse scan order is newest -> oldest. Extra steered messages
                # need stable per-record cursors. The oldest message also uses
                # its native item id when available, while the task id remains
                # an accepted compatibility cursor for rolling upgrades.
                segment_count = len(segment_users)
                for reverse_index, (
                    boundary, _cursor, extra_cursor, _native_cursor,
                    process_witness,
                ) in enumerate(segment_users[:-1]):
                    yield _CodexHistoryBoundary(
                        boundary, extra_cursor, turn_cursor,
                        process=process_witness,
                        segment_index=segment_count - reverse_index - 1,
                        segment_count=segment_count,
                        appendable=is_native_head and reverse_index == 0,
                    )
                oldest_native_cursor = segment_users[-1][3]
                yield _CodexHistoryBoundary(
                    offset,
                    oldest_native_cursor or turn_cursor,
                    turn_cursor,
                    (
                        turn_cursor
                        if oldest_native_cursor is not None
                        else None
                    ),
                    process=segment_users[-1][4],
                    segment_index=0,
                    segment_count=segment_count,
                    appendable=is_native_head and segment_count == 1,
                )
            elif not segment_account_switch:
                pending_assistant_only = (
                    offset, turn_cursor, process.take(),
                )
            else:
                pending_account_switch.merge(process.take())
            segment_users = []
            segment_account_switch = False
            continue

        if (pending_assistant_only is not None
                and _history_terminal_marker(line)):
            offset, turn_cursor, process_witness = pending_assistant_only
            yield _CodexHistoryBoundary(
                offset, turn_cursor, turn_cursor,
                process=process_witness,
            )
            pending_assistant_only = None
    if (
        pending_assistant_only is not None
        and _history_goal_prompt_for_boundary(
            path, pending_assistant_only[0],
        )[0] is not None
    ):
        offset, turn_cursor, process_witness = pending_assistant_only
        yield _CodexHistoryBoundary(
            offset, turn_cursor, turn_cursor,
            process=process_witness,
        )


def _history_boundaries(
    path: str,
    *,
    use_turns: bool,
    max_scan_bytes: int | None = None,
):
    """Compatibility iterator exposing only the stable paging cursor pair."""
    for boundary in _history_boundary_records(
        path,
        use_turns=use_turns,
        max_scan_bytes=max_scan_bytes,
    ):
        yield boundary.offset, boundary.cursor


def codex_history_native_witness(
    path: str,
    *,
    max_turns: int,
    max_scan_bytes: int | None = _DEFAULT_HISTORY_WINDOW_MAX_BYTES,
    required_turn_ids: tuple[str, ...] = (),
) -> CodexHistoryNativeWitness:
    """Return bounded, native-turn evidence without translating the rollout.

    Boundary records are deliberately the only decoded payloads. Large tool,
    token-count, reasoning, or malformed records therefore cannot recreate the
    upstream app-server projection failure this witness is meant to detect.
    """
    bounded_turns = max(1, int(max_turns))
    byte_budget = (
        None if max_scan_bytes is None
        else max(1024 * 1024, int(max_scan_bytes))
    )
    required = {
        turn_id for turn_id in required_turn_ids
        if isinstance(turn_id, str) and _SAFE_WIRE_ID.fullmatch(turn_id)
    }
    source_size = os.path.getsize(path)
    turn_ids: list[str] = []
    seen: set[str] = set()
    retained: set[str] = set()
    process_by_visible_id: dict[str, CodexHistoryProcessWitness] = {}
    offset_by_visible_id: dict[str, int] = {}
    process_by_native_segment: dict[
        tuple[str, int], CodexHistoryProcessWitness
    ] = {}
    offset_by_native_segment: dict[tuple[str, int], int] = {}
    has_more_turns = False
    stopped_early = False
    completed_required_group: str | None = None
    for boundary in _history_boundary_records(
        path,
        use_turns=True,
        max_scan_bytes=byte_budget,
        include_process=True,
    ):
        turn_id = boundary.native_turn_id
        if (
            turn_id is None
        ):
            continue
        if turn_id not in seen:
            if (
                completed_required_group is not None
                and turn_id != completed_required_group
            ):
                has_more_turns = True
                stopped_early = True
                break
            seen.add(turn_id)
            if len(turn_ids) >= bounded_turns:
                has_more_turns = True
            else:
                retained.add(turn_id)
                turn_ids.append(turn_id)
        if turn_id in retained and boundary.process is not None:
            process_by_visible_id[boundary.cursor] = boundary.process
            process_by_native_segment[
                (turn_id, boundary.segment_index)
            ] = boundary.process
        if turn_id in retained:
            offset_by_visible_id[boundary.cursor] = boundary.offset
            offset_by_native_segment[
                (turn_id, boundary.segment_index)
            ] = boundary.offset
            if boundary.compatibility_cursor is not None:
                offset_by_visible_id[
                    boundary.compatibility_cursor
                ] = boundary.offset
        if required and required.issubset(seen):
            completed_required_group = turn_id
    return CodexHistoryNativeWitness(
        turn_ids=tuple(turn_ids),
        scanned_to_start=(
            not stopped_early
            and (
                byte_budget is None
                or source_size <= byte_budget
            )
        ),
        has_more_turns=has_more_turns,
        process_by_visible_id=process_by_visible_id,
        offset_by_visible_id=offset_by_visible_id,
        process_by_native_segment=process_by_native_segment,
        offset_by_native_segment=offset_by_native_segment,
    )


def codex_history_process_witnesses(
    path: str,
    *,
    before: str,
    max_turns: int,
    before_offset: int | None = None,
    native_turn_ids: tuple[str, ...] = (),
    source_end_offset: int | None = None,
) -> CodexHistoryProcessPageWitness:
    """Return positive process proof for one requested official-history page.

    Official item ids need not occur anywhere in the rollout. Prefer the exact
    native ids from the requested official page; a cached source offset only
    accelerates that lookup. Legacy callers without native coordinates may
    still use a source-visible cursor. Retain every steer segment belonging to
    the requested native rows. The bounded head witness remains the completeness
    check; this metadata-only read fills page rows outside its tail budget
    without translating tools or expanding turn details.
    """
    if (
        not isinstance(before, str)
        or not _SAFE_WIRE_ID.fullmatch(before)
        or isinstance(max_turns, bool)
        or not isinstance(max_turns, int)
        or max_turns <= 0
    ):
        return CodexHistoryProcessPageWitness()
    if before_offset is not None and (
        isinstance(before_offset, bool)
        or not isinstance(before_offset, int)
        or before_offset < 0
    ):
        return CodexHistoryProcessPageWitness()
    if source_end_offset is not None and (
        isinstance(source_end_offset, bool)
        or not isinstance(source_end_offset, int)
        or source_end_offset < 0
    ):
        return CodexHistoryProcessPageWitness()
    if any(
        not isinstance(turn_id, str) or not _SAFE_WIRE_ID.fullmatch(turn_id)
        for turn_id in native_turn_ids
    ) or len(set(native_turn_ids)) > max_turns:
        return CodexHistoryProcessPageWitness()
    requested = set(native_turn_ids)
    target_found = before_offset is not None
    retained: set[str] = set()
    process_by_visible_id: dict[str, CodexHistoryProcessWitness] = {}
    offset_by_visible_id: dict[str, int] = {}
    process_by_native_segment: dict[
        tuple[str, int], CodexHistoryProcessWitness
    ] = {}
    offset_by_native_segment: dict[tuple[str, int], int] = {}
    stopped_early = False
    append_segment = None
    scan_end = source_end_offset
    if before_offset is not None:
        scan_end = before_offset if scan_end is None else min(before_offset, scan_end)
    for boundary in _history_boundary_records(
        path,
        use_turns=True,
        end_offset=scan_end,
        include_process=True,
    ):
        native_turn_id = boundary.native_turn_id
        if requested:
            if native_turn_id not in requested:
                if requested.issubset(retained):
                    stopped_early = True
                    break
                continue
            target_found = True
        elif not target_found:
            if (
                boundary.cursor == before
                or boundary.compatibility_cursor == before
            ):
                target_found = True
            continue
        if native_turn_id is None:
            continue
        if boundary.appendable and before_offset is None:
            append_segment = (native_turn_id, boundary.segment_index)
        if native_turn_id not in retained:
            if len(retained) >= max_turns:
                stopped_early = True
                break
            retained.add(native_turn_id)
        offset_by_visible_id[boundary.cursor] = boundary.offset
        offset_by_native_segment[
            (native_turn_id, boundary.segment_index)
        ] = boundary.offset
        if boundary.compatibility_cursor is not None:
            offset_by_visible_id[
                boundary.compatibility_cursor
            ] = boundary.offset
        if boundary.process is not None:
            process_by_visible_id[boundary.cursor] = boundary.process
            process_by_native_segment[
                (native_turn_id, boundary.segment_index)
            ] = boundary.process
        if requested and requested.issubset(retained) and boundary.segment_index == 0:
            # Every steer of the oldest requested task has now been emitted.
            # Do not walk an unrelated (possibly huge) older task just to find
            # its next boundary and discover that the page is complete.
            stopped_early = boundary.offset > 0
            break
    return CodexHistoryProcessPageWitness(
        process_by_visible_id=process_by_visible_id,
        offset_by_visible_id=offset_by_visible_id,
        process_by_native_segment=process_by_native_segment,
        offset_by_native_segment=offset_by_native_segment,
        scanned_to_start=target_found and not stopped_early,
        append_segment=append_segment,
    )


def codex_history_process_append(
    path: str,
    *,
    previous: CodexHistoryProcessPageWitness,
    start_offset: int,
    end_offset: int,
) -> CodexHistoryProcessPageWitness | None:
    """Extend positive metadata by reading only a source-validated append.

    The caller verifies the old file prefix. New user/task boundaries require
    the full ownership parser, as do partial JSONL records; never guess which
    steer/automatic continuation should own their process evidence.
    """
    segment = previous.append_segment
    if segment is None or start_offset <= 0 or end_offset <= start_offset:
        return None
    with open(path, "rb") as source:
        source.seek(start_offset - 1)
        if source.read(1) != b"\n":
            return None
        source.seek(end_offset - 1)
        if source.read(1) != b"\n":
            return None
    process = _HistoryProcessAccumulator()
    process.merge(previous.process_by_native_segment.get(segment))
    for _offset, line in _reverse_jsonl_records(
        # Include the preceding newline so the reverse reader can emit the
        # first appended record rather than dropping it as a partial carry.
        path, max_scan_bytes=end_offset - start_offset + 1,
        end_offset=end_offset, max_record_bytes=12 * 1024 * 1024,
    ):
        if (any(marker in line for marker in (
            b'"task_started"', b'"user_message"', b'"session_meta"',
            b'"thread_goal_updated"', b'"thread_goal_cleared"',
        )) or re.search(rb'"usermessage"|"role"\s*:\s*"user"', line, re.I)):
            return None
        if _history_generated_image_record(line):
            process.observe(None)
            process.generated_images = True
        visible, stamp = _history_visible_process_stamp(line)
        if visible:
            process.observe(stamp)
    updated = process.snapshot()
    if updated is None:
        return previous
    native = {**previous.process_by_native_segment, segment: updated}
    visible = dict(previous.process_by_visible_id)
    segment_offset = previous.offset_by_native_segment[segment]
    for cursor, offset in previous.offset_by_visible_id.items():
        if offset == segment_offset:
            visible[cursor] = updated
    return replace(previous, process_by_native_segment=native,
                   process_by_visible_id=visible)


def codex_history_window_info(
    path: str, *, before: str | None, limit: int | None,
    max_bytes: int = _DEFAULT_HISTORY_WINDOW_MAX_BYTES,
) -> CodexHistoryWindow:
    """Select a bounded Codex history page and retain exact boundary identity.

    The rollout can be many gigabytes: only boundary records are decoded while
    locating the latest page, and the forward translator sees at most the
    configured source window.  ``forced_oldest_cursor`` preserves pagination
    when one visible turn alone is larger than the byte budget and its prefix
    must be omitted. ``forced_boundary_offset`` lets the caller recover the
    omitted user boundary without parsing or retaining that entire turn.
    """
    size = os.path.getsize(path)
    if size <= 0 or not isinstance(limit, int) or limit <= 0:
        return CodexHistoryWindow(0, size, False)
    byte_budget = max(1024 * 1024, int(max_bytes))

    # Current app-server rollouts have an authoritative task_started boundary
    # for user and assistant-only continuation turns.  Fall back to historical
    # user_message boundaries only for older rollout shapes.
    for use_turns in (True, False):
        end_offset = size
        target_found = before is None
        boundaries: list[_CodexHistoryBoundary] = []
        newest_boundary: _CodexHistoryBoundary | None = None
        saw_boundary = False
        for boundary in _history_boundary_records(path, use_turns=use_turns):
            offset = boundary.offset
            cursor = boundary.cursor
            saw_boundary = True
            if not target_found:
                if (
                    cursor == before
                    or boundary.compatibility_cursor == before
                ):
                    target_found = True
                    end_offset = offset
                continue
            boundaries.append(boundary)
            if newest_boundary is None:
                newest_boundary = boundary
            if end_offset - offset > byte_budget:
                if len(boundaries) > 1:
                    return CodexHistoryWindow(
                        start_offset=boundaries[-2].offset,
                        end_offset=end_offset,
                        has_older=True,
                        newest_boundary_offset=newest_boundary.offset,
                        newest_cursor=newest_boundary.cursor,
                        newest_native_turn_id=newest_boundary.native_turn_id,
                        newest_segment_index=newest_boundary.segment_index,
                    )
                # Preserve the recent tail of a pathological single turn. Its
                # visible boundary cursor remains available for older history.
                start_offset = _next_jsonl_offset(
                    path, max(0, end_offset - byte_budget), end_offset)
                return CodexHistoryWindow(
                    start_offset=start_offset,
                    end_offset=end_offset,
                    has_older=True,
                    forced_oldest_cursor=cursor,
                    forced_boundary_offset=offset,
                    forced_native_turn_id=boundary.native_turn_id,
                    forced_segment_index=boundary.segment_index,
                    newest_boundary_offset=newest_boundary.offset,
                    newest_cursor=newest_boundary.cursor,
                    newest_native_turn_id=newest_boundary.native_turn_id,
                    newest_segment_index=newest_boundary.segment_index,
                )
            if len(boundaries) > limit:
                return CodexHistoryWindow(
                    start_offset=boundaries[limit - 1].offset,
                    end_offset=end_offset,
                    has_older=True,
                    newest_boundary_offset=newest_boundary.offset,
                    newest_cursor=newest_boundary.cursor,
                    newest_native_turn_id=newest_boundary.native_turn_id,
                    newest_segment_index=newest_boundary.segment_index,
                )
        if saw_boundary:
            if before is not None and not target_found:
                return CodexHistoryWindow(0, 0, False)
            return CodexHistoryWindow(
                start_offset=0,
                end_offset=end_offset,
                has_older=False,
                newest_boundary_offset=(
                    newest_boundary.offset if newest_boundary is not None
                    else None
                ),
                newest_cursor=(
                    newest_boundary.cursor if newest_boundary is not None
                    else None
                ),
                newest_native_turn_id=(
                    newest_boundary.native_turn_id
                    if newest_boundary is not None else None
                ),
                newest_segment_index=(
                    newest_boundary.segment_index
                    if newest_boundary is not None else None
                ),
            )
    if size > byte_budget:
        start_offset = _next_jsonl_offset(
            path, size - byte_budget, size)
        return CodexHistoryWindow(start_offset, size, True)
    return CodexHistoryWindow(0, size, False)


def codex_history_window(
    path: str, *, before: str | None, limit: int | None,
    max_bytes: int = _DEFAULT_HISTORY_WINDOW_MAX_BYTES,
) -> tuple[int, int, bool, str | None, int | None]:
    """Compatibility five-tuple for callers which need only page offsets."""
    return codex_history_window_info(
        path,
        before=before,
        limit=limit,
        max_bytes=max_bytes,
    ).legacy()


def codex_history_boundary_process_start(
    path: str,
    boundary_offset: int,
    *,
    max_scan_bytes: int = _MAX_HISTORY_BOUNDARY_FORWARD_BYTES,
) -> int | None:
    """Recover the first public-process timestamp omitted by a tail window.

    ``codex_history_window`` can retain only the recent tail of one enormous
    native turn. The tail may begin at a late compaction record even though a
    commentary/tool event was persisted near the original user boundary. Scan
    forward from that already-proven boundary and stop at the first visible
    process event belonging to that exact user segment. A direct final answer,
    private reasoning, and ordinary lifecycle plumbing remain non-evidence, so
    this helper cannot recreate an empty ``已处理`` disclosure.

    The scan is byte-bounded and oversized JSONL records are skipped by
    ``_bounded_jsonl_records``. A timestamp is returned only after the segment's
    real visible user row has also been observed; assistant-only boundaries are
    therefore never attached to an unrelated prompt.
    """
    if (
        isinstance(boundary_offset, bool)
        or not isinstance(boundary_offset, int)
        or boundary_offset < 0
        or isinstance(max_scan_bytes, bool)
        or not isinstance(max_scan_bytes, int)
        or max_scan_bytes <= 0
    ):
        return None
    try:
        size = os.path.getsize(path)
        end_offset = min(
            size,
            boundary_offset + max(1024 * 1024, max_scan_bytes),
        )
        source = open(path, "rb")
    except (OSError, TypeError, ValueError):
        return None

    saw_user = False
    process_before_user: int | None = None
    with source:
        source.seek(boundary_offset)
        for _offset, line in _bounded_jsonl_records(
            source, end_offset=end_offset,
        ):
            if len(line) <= _MAX_HISTORY_REVERSE_RECORD_BYTES:
                visible_process, stamp_ms = _history_visible_process_stamp(
                    line.encode("utf-8"),
                )
                if visible_process and stamp_ms is not None:
                    if saw_user:
                        return stamp_ms
                    if process_before_user is None:
                        process_before_user = stamp_ms

            # Avoid decoding ordinary output, token and large compact rows just
            # to discover the one visible user boundary.
            if "user_message" not in line and "item_completed" not in line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            user = codex_rollout_user_message(payload)
            if user is None or not user.prompt:
                continue
            if saw_user:
                # Any later public work belongs to this newer steer segment.
                return None
            saw_user = True
            if process_before_user is not None:
                return process_before_user
    return None


def codex_history_boundary_user(
    path: str,
    boundary_offset: int,
    cursor: str,
    *,
    user_index: int = 0,
    max_scan_bytes: int = _MAX_HISTORY_BOUNDARY_FORWARD_BYTES,
) -> UserMsg | None:
    """Recover one user row omitted from a bounded single-turn tail.

    A single Codex turn can grow far beyond the history byte window, especially
    after one or more ``compacted`` records.  Reading only its recent tail keeps
    memory bounded but otherwise leaves the browser with tool/assistant events
    that have no prompt.  Scan forward from the already-discovered turn boundary
    only until the requested visible user record. Reuse its adjacent native
    item id when present, otherwise retain the paging cursor as the stable id.
    ``user_index`` also lets the official-history adapter recover images from
    later steer messages without translating the whole rollout.
    Oversized JSONL records are skipped by ``_bounded_jsonl_records`` rather than
    materialized.
    """
    if (not isinstance(boundary_offset, int) or boundary_offset < 0
            or not isinstance(cursor, str)
            or not _SAFE_WIRE_ID.fullmatch(cursor)
            or isinstance(user_index, bool)
            or not isinstance(user_index, int)
            or user_index < 0):
        return None
    try:
        size = os.path.getsize(path)
        end_offset = min(
            size,
            boundary_offset + max(
                1024 * 1024, int(max_scan_bytes)),
        )
        source = open(path, "rb")
    except (OSError, TypeError, ValueError):
        return None

    saw_task_start = False
    pending_images: list = []
    pending_legacy_user_item_id: str | None = None
    with source:
        source.seek(boundary_offset)
        for _offset, line in _bounded_jsonl_records(
                source, end_offset=end_offset):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                pending_legacy_user_item_id = None
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            row_type = row.get("type") if isinstance(row, dict) else None
            payload_type = (
                payload.get("type") if isinstance(payload, dict) else None
            )
            paired_legacy_user_item_id = None
            if pending_legacy_user_item_id is not None:
                if row_type == "event_msg" and payload_type == "user_message":
                    paired_legacy_user_item_id = pending_legacy_user_item_id
                # Pairing is valid for exactly one adjacent persisted record.
                pending_legacy_user_item_id = None
            if not isinstance(payload, dict):
                continue
            if row_type == "event_msg" and payload_type == "task_started":
                if saw_task_start:
                    break
                saw_task_start = True
                continue
            if (row_type == "response_item"
                    and payload_type == "message"
                    and payload.get("role") == "user"):
                pending_legacy_user_item_id = (
                    _legacy_response_user_item_id(payload)
                )
                for item in payload.get("content") or []:
                    if (isinstance(item, dict)
                            and item.get("type") == "input_image"):
                        image = _data_uri_to_img(item.get("image_url"))
                        if image:
                            pending_images.append(image)
                continue
            if row_type != "event_msg":
                continue
            user = codex_rollout_user_message(payload)
            if user is None:
                continue
            if not user.prompt:
                pending_images = []
                continue
            if user_index:
                user_index -= 1
                pending_images = []
                continue
            message_id = (
                user.message_id
                if isinstance(user.message_id, str)
                and _SAFE_WIRE_ID.fullmatch(user.message_id)
                else paired_legacy_user_item_id or cursor
            )
            client_id = (
                user.client_id
                if isinstance(user.client_id, str)
                and _SAFE_WIRE_ID.fullmatch(user.client_id)
                else None
            )
            event = UserMsg(
                msg_id=message_id,
                client_msg_id=client_id,
                prompt=user.prompt,
            )
            if pending_images:
                event.images = pending_images
            raw_ts = row.get("timestamp")
            if isinstance(raw_ts, str):
                try:
                    event.ts = datetime.fromisoformat(
                        raw_ts.replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    pass
            return event
    goal_prompt, _stable = _history_goal_prompt_for_boundary(
        path, boundary_offset)
    if goal_prompt is None or user_index != 0:
        return None
    prompt, timestamp = goal_prompt
    event = UserMsg(msg_id=cursor, prompt=prompt)
    if timestamp is not None:
        event.ts = timestamp
    return event


def codex_history_turn_user(
    path: str,
    turn_id: str,
    cursor: str,
    user_index: int = 0,
    *,
    max_reverse_scan_bytes: int | None = None,
) -> UserMsg | None:
    """Recover one visible user row for one native Codex turn.

    Official summary items retain expired ``localImage`` paths rather than the
    inline image bytes persisted in the rollout. Locate only the requested
    native turn boundary, then reuse the bounded forward reader above so image
    thumbnails remain available without translating the whole rollout. Live
    recovery may bound the reverse search to the recent tail; history detail
    reads retain the existing unbounded exact-turn lookup by default.
    """
    if (
        not isinstance(turn_id, str)
        or not _SAFE_WIRE_ID.fullmatch(turn_id)
        or not isinstance(cursor, str)
        or not _SAFE_WIRE_ID.fullmatch(cursor)
        or isinstance(user_index, bool)
        or not isinstance(user_index, int)
        or user_index < 0
    ):
        return None
    try:
        for offset, line in _reverse_jsonl_records(
            path,
            max_scan_bytes=max_reverse_scan_bytes,
        ):
            if _history_turn_cursor(line) == turn_id:
                return codex_history_boundary_user(
                    path, offset, cursor, user_index=user_index)
    except OSError:
        return None
    return None


def codex_history_turn_users(
    path: str,
    turn_ids: tuple[str, ...],
    *,
    max_scan_bytes: int = _MAX_OFFICIAL_AUTOMATIC_USER_SCAN_BYTES,
) -> CodexAutomaticUserRecovery:
    """Recover a bounded set of assistant-only Goal prompts in one reverse pass.

    Official summary pages must never walk a multi-gigabyte rollout once per
    row.  The live projection fills misses authoritatively; this compatibility
    lookup inspects only a bounded recent tail for sessions created before that
    live overlay existed.
    """
    targets = {
        turn_id for turn_id in turn_ids
        if isinstance(turn_id, str) and _SAFE_WIRE_ID.fullmatch(turn_id)
    }
    if not targets:
        return CodexAutomaticUserRecovery()
    recovered: dict[str, UserMsg] = {}
    seen: set[str] = set()
    try:
        for offset, line in _reverse_jsonl_records(
            path, max_scan_bytes=max_scan_bytes,
        ):
            boundary = _history_turn_cursor(line)
            if boundary not in targets:
                continue
            goal_prompt, stable = _history_goal_prompt_for_boundary(
                path, offset)
            if stable:
                seen.add(boundary)
            if goal_prompt is not None:
                prompt, timestamp = goal_prompt
                user = UserMsg(msg_id=boundary, prompt=prompt)
                if timestamp is not None:
                    user.ts = timestamp
                recovered[boundary] = user
            targets.remove(boundary)
            if not targets:
                break
    except OSError:
        pass
    return CodexAutomaticUserRecovery(
        users=recovered,
        seen_turn_ids=frozenset(seen),
    )


def codex_native_rollback_turns(path: str, logical_turns: int) -> int:
    """Translate visible rollback turns to Codex's native user-boundary count.

    The private account-switch continuation is persisted by ``turn/start`` as a
    native user message but merged into the preceding logical browser turn.
    Count those exact envelopes with their preceding visible prompt so
    ``thread/rollback`` removes the whole logical turn. Other internal context
    rows are not real rollback boundaries and remain ignored.
    """
    if (
        not isinstance(logical_turns, int)
        or isinstance(logical_turns, bool)
        or logical_turns < 1
    ):
        raise ValueError("logical_turns must be a positive integer")
    groups: list[int] = []
    try:
        size = os.path.getsize(path)
        source = open(path, "rb")
    except (OSError, TypeError, ValueError):
        return logical_turns
    with source:
        for _offset, line in _bounded_jsonl_records(
            source, end_offset=size,
        ):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if row.get("type") != "event_msg":
                continue
            user = codex_rollout_user_message(payload)
            if user is None:
                continue
            if is_codex_account_switch_message(user.raw_text):
                if groups:
                    groups[-1] += 1
                continue
            if user.prompt:
                groups.append(1)
                if len(groups) > logical_turns:
                    del groups[0]
    native_turns = sum(groups)
    return native_turns if native_turns > 0 else logical_turns


_CODEX_APPEND_NOTIFICATION_FIELDS = {
    "item/agentMessage/delta": "delta",
    "item/reasoning/summaryTextDelta": "delta",
    "item/plan/delta": "delta",
    "item/commandExecution/outputDelta": "delta",
    "item/fileChange/outputDelta": "delta",
    "item/mcpToolCall/progress": "message",
}
_NO_CODEX_NOTIFICATION = object()


def _codex_append_notification_key(message: object) -> tuple | None:
    """Return the exact append-only field eligible for live burst merging."""
    if not isinstance(message, dict):
        return None
    method = message.get("method")
    field = _CODEX_APPEND_NOTIFICATION_FIELDS.get(method)
    params = message.get("params")
    if field is None or not isinstance(params, dict):
        return None
    item_id = params.get("itemId")
    value = params.get(field)
    if not isinstance(item_id, str) or not item_id or not isinstance(value, str):
        return None
    # Preserve turn/item/stream/index boundaries even if a future app-server
    # interleaves two append channels under the same item id.
    return (
        method,
        params.get("threadId"),
        params.get("turnId"),
        item_id,
        params.get("stream"),
        params.get("summaryIndex"),
        params.get("contentIndex"),
    )


def _merge_codex_append_notifications(
    previous: dict, incoming: dict,
) -> dict | None:
    key = _codex_append_notification_key(previous)
    if key is None or key != _codex_append_notification_key(incoming):
        return None
    method = previous.get("method")
    field = _CODEX_APPEND_NOTIFICATION_FIELDS.get(method)
    previous_params = previous.get("params")
    incoming_params = incoming.get("params")
    if (field is None or not isinstance(previous_params, dict)
            or not isinstance(incoming_params, dict)):
        return None
    previous_text = previous_params.get(field)
    incoming_text = incoming_params.get(field)
    if not isinstance(previous_text, str) or not isinstance(incoming_text, str):
        return None
    merged = dict(previous)
    merged["params"] = {
        **previous_params,
        field: previous_text + incoming_text,
    }
    return merged


async def coalesce_codex_live_notifications(
    source, *, flush_seconds: float = _LIVE_DELTA_FLUSH_SECONDS,
):
    """Bound live append bursts before translation and replay seq allocation.

    The first append for a field is immediate. A following run for that exact
    field is merged only until the first append's deadline, so a quiet provider
    cannot strand the final chunk waiting for another notification. Structural,
    unknown, turn, item and stream boundaries remain strict ordering barriers.
    """
    iterator = source.__aiter__()
    loop = asyncio.get_running_loop()
    next_task: asyncio.Task | None = None
    buffered: object = _NO_CODEX_NOTIFICATION
    source_done = False
    pending_error: Exception | None = None
    last_key: tuple | None = None
    last_emitted_at = float("-inf")

    async def take_next():
        nonlocal next_task
        if next_task is None:
            next_task = asyncio.create_task(anext(iterator))
        task = next_task
        try:
            return await task
        finally:
            if task.done():
                next_task = None

    try:
        while not source_done or buffered is not _NO_CODEX_NOTIFICATION:
            if buffered is not _NO_CODEX_NOTIFICATION:
                message = buffered
                buffered = _NO_CODEX_NOTIFICATION
            else:
                try:
                    message = await take_next()
                except StopAsyncIteration:
                    break

            key = _codex_append_notification_key(message)
            now = loop.time()
            if (flush_seconds <= 0 or key is None or key != last_key
                    or now - last_emitted_at >= flush_seconds):
                last_key = key
                last_emitted_at = now
                yield message
                continue

            merged = message
            deadline = last_emitted_at + flush_seconds
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                if next_task is None:
                    next_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    {next_task}, timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                task = next_task
                next_task = None
                try:
                    incoming = task.result()
                except StopAsyncIteration:
                    source_done = True
                    break
                except Exception as exc:
                    # The provider may fail after delivering part of an
                    # append burst.  Flush those confirmed bytes before the
                    # original stream error reaches the turn lifecycle.
                    source_done = True
                    pending_error = exc
                    break
                combined = _merge_codex_append_notifications(merged, incoming)
                if combined is None:
                    buffered = incoming
                    break
                merged = combined

            last_key = key
            last_emitted_at = loop.time()
            yield merged
            if pending_error is not None:
                raise pending_error
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            await asyncio.gather(next_task, return_exceptions=True)


class CodexStreamTranslator:
    def __init__(self, tool_result_max: int):
        self.tool_result_max = tool_result_max
        self._started: set[str] = set()
        self._text_seen: set[str] = set()
        self._message_channels: dict[str, str] = {}
        self._async_messages: set[str] = set()
        self._tools_started: set[str] = set()
        self._tool_message_ids: dict[str, str] = {}
        self._reasoning_started: set[str] = set()
        self._file_diffs: dict[str, str] = {}
        self._file_diff_stream_aligned: set[str] = set()
        self._open_msg: str | None = None
        self._open_channel = "unknown"
        self._visible_output = False
        self._final_output = False
        self._completed_plan: tuple[str, str] | None = None
        self._terminal_error = False
        self._delta_chars: dict[tuple[str, str], int] = {}
        self._delta_events: dict[tuple[str, str], int] = {}
        self._truncated_delta_streams: set[tuple[str, str]] = set()
        self._finished_delta_items: set[str] = set()
        # One translator owns one turn. Keep a fixed admission set instead of
        # allowing every distinct provider id to grow several parallel maps.
        # Rejected ids are not tombstoned individually: once full, *all* new ids
        # stay rejected, so a later completed event cannot resurrect them.
        self._live_items: set[str] = set()
        self._live_items_truncated = False
        self._turn_closed = False

    def feed(
        self,
        msg: dict,
        *,
        authoritative_terminal: bool = True,
    ) -> list:
        """Translate one app-server notification.

        ``authoritative_terminal=False`` is reserved for the few callers which
        feed a locally synthesized ``turn/completed`` only to close translator
        blocks.  Its TurnEnd remains useful on the live wire, but cannot be
        persisted as an engine-owned lifecycle fact.
        """
        method = msg.get("method")
        p = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        out: list = []

        if method == "item/agentMessage/delta":
            iid = _live_id(p.get("itemId"), "agent-message")
            if not self._admit_live_item(iid, out):
                return out
            channel = self._message_channels.get(iid, "unknown")
            if iid not in self._started:
                if self._open_msg is not None and self._open_msg != iid:
                    self._close_open(out)
                self._started.add(iid)
                self._open_msg = iid
                self._open_channel = channel
                out.append(AssistantMsgStart(message_id=iid, channel=channel))
            delta = p.get("delta")
            if isinstance(delta, str) and delta:
                self._text_seen.add(iid)
                self._visible_output = True
                if channel == "final" and iid not in self._async_messages:
                    self._final_output = True
                out.append(Delta(message_id=iid, text=delta, channel=channel))

        elif method == "item/started":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            item_type = item.get("type")
            if item_type == "agentMessage":
                iid = _live_id(item.get("id"), "agent-message")
                if not self._admit_live_item(iid, out):
                    return out
                channel = _assistant_channel(item.get("phase"))
                self._message_channels[iid] = channel
                if item.get("delivery") == "async":
                    self._async_messages.add(iid)
                if iid not in self._started:
                    if self._open_msg is not None and self._open_msg != iid:
                        self._close_open(out)
                    self._started.add(iid)
                    self._open_msg = iid
                    self._open_channel = channel
                    out.append(AssistantMsgStart(
                        message_id=iid, channel=channel))
            elif item_type in _TOOL_TYPES:
                self._visible_output = True
                out.extend(self._tool_use(item))
            elif item_type in _PROCESS_ITEM_TYPES:
                iid = _live_id(item.get("id"), str(item_type or "process"))
                if not self._admit_live_item(iid, out):
                    return out
                event = self._process_item(item, p, completed=False)
                if event is not None:
                    self._visible_output = True
                    out.append(event)

        elif method == "item/completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            t = item.get("type")
            if t == "agentMessage":
                text = item.get("text") if isinstance(item.get("text"), str) else ""
                iid = _live_id(item.get("id") or text, "agent-message")
                if not self._admit_live_item(iid, out):
                    return out
                channel = _assistant_channel(item.get("phase"))
                if channel == "unknown":
                    channel = self._message_channels.get(iid, "unknown")
                else:
                    self._message_channels[iid] = channel
                # Some providers send only item/completed with the final text and
                # no delta notification. Preserve that answer instead of turning
                # it into a false empty-completed error.
                if text and iid not in self._text_seen:
                    if iid not in self._started:
                        if self._open_msg is not None and self._open_msg != iid:
                            self._close_open(out)
                        self._started.add(iid)
                        self._open_msg = iid
                        self._open_channel = channel
                        out.append(AssistantMsgStart(
                            message_id=iid, channel=channel))
                    self._text_seen.add(iid)
                    self._visible_output = True
                    out.append(Delta(
                        message_id=iid, text=text, channel=channel))
                if item.get("delivery") == "async":
                    self._async_messages.add(iid)
                    self._final_output = any(
                        mid not in self._async_messages
                        and self._message_channels.get(mid) == "final"
                        for mid in self._text_seen)
                elif text and channel == "final":
                    self._final_output = True
                if iid in self._started:
                    out.append(AssistantMsgEnd(
                        message_id=iid, channel=channel,
                        **_async_message_fields(item)))
                    if self._open_msg == iid:
                        self._open_msg = None
                        self._open_channel = "unknown"
            elif t in _TOOL_TYPES:
                self._visible_output = True
                iid = _live_id(item.get("id"), f"{t}-tool")
                if not self._admit_live_item(iid, out):
                    return out
                if iid not in self._tools_started:
                    out.extend(self._tool_use(item))
                elif t == "fileChange":
                    out.append(self._tool_update(item))
                out.append(self._tool_result(item))
            elif t in _PROCESS_ITEM_TYPES:
                iid = _live_id(item.get("id"), str(t or "process"))
                if t == "plan":
                    self._remember_completed_plan(item, iid)
                if not self._admit_live_item(iid, out):
                    return out
                event = self._process_item(item, p, completed=True)
                if event is not None:
                    self._visible_output = True
                    out.append(event)
            if t in _TOOL_TYPES | _PROCESS_ITEM_TYPES:
                fallback = {
                    "commandExecution": "command-tool",
                    "fileChange": "fileChange-tool",
                    "mcpToolCall": "mcpToolCall-tool",
                    "dynamicToolCall": "dynamicToolCall-tool",
                    "webSearch": "webSearch-tool",
                }.get(t, str(t or "process"))
                finished_id = _live_id(item.get("id"), fallback)
                self._finish_delta_item(finished_id)
                self._file_diffs.pop(finished_id, None)
                self._file_diff_stream_aligned.discard(finished_id)

        elif method == "item/reasoning/summaryPartAdded":
            iid = _live_id(p.get("itemId"), "reasoning")
            if not self._admit_live_item(iid, out):
                return out
            event = self._ensure_reasoning(iid, p)
            if event is not None:
                self._visible_output = True
                out.append(event)

        elif method == "item/reasoning/summaryTextDelta":
            iid = _live_id(p.get("itemId"), "reasoning")
            if not self._admit_live_item(iid, out):
                return out
            event = self._ensure_reasoning(iid, p)
            if event is not None:
                out.append(event)
            delta = self._bounded_live_delta(
                iid, "reasoning-summary", p.get("delta"), 512 * 1024)
            if delta:
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="reasoning",
                    phase="update",
                    status="running",
                    turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                    title="思考",
                    append_to="summary",
                    delta=delta,
                ))

        elif method == "item/plan/delta":
            iid = _live_id(p.get("itemId"), "plan")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "plan-detail", p.get("delta"), 512 * 1024)
            if delta:
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="plan",
                    phase="update",
                    status="running",
                    turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                    title="计划",
                    append_to="detail",
                    delta=delta,
                ))

        elif method == "turn/plan/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"plan:{turn_id or p.get('turnId') or 'current'}", "plan")
            if not self._admit_live_item(iid, out):
                return out
            plan = []
            for entry in (p.get("plan") or [])[:128]:
                if not isinstance(entry, dict):
                    continue
                step, _ = bounded_text(entry.get("step"), 16 * 1024)
                if not step:
                    continue
                plan.append({
                    "step": step,
                    "status": _plan_status(entry.get("status")),
                })
            explanation, _ = bounded_text(p.get("explanation"), 64 * 1024)
            self._visible_output = True
            out.append(TurnPlan(
                item_id=iid,
                turn_id=turn_id,
                explanation=explanation or None,
                plan=plan,
            ))

        elif method == "item/commandExecution/outputDelta":
            iid = _live_id(p.get("itemId"), "command-tool")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "output", p.get("delta"),
                min(self.tool_result_max, 512 * 1024))
            if delta:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="output",
                    delta=delta,
                ))

        elif method == "item/fileChange/outputDelta":
            # Kept for old app-server builds; 0.144.1 marks it deprecated.
            iid = _live_id(p.get("itemId"), "fileChange-tool")
            if not self._admit_live_item(iid, out):
                return out
            delta = self._bounded_live_delta(
                iid, "output", p.get("delta"),
                min(self.tool_result_max, 512 * 1024))
            if delta:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="output",
                    delta=delta,
                ))

        elif method == "item/fileChange/patchUpdated":
            iid = _live_id(p.get("itemId"), "fileChange-tool")
            if not self._admit_live_item(iid, out):
                return out
            if iid in self._tools_started:
                out.append(self._tool_update({
                    "type": "fileChange",
                    "id": p.get("itemId"),
                    "status": "inProgress",
                    "changes": p.get("changes"),
                }))
            latest, _ = bounded_text(_changes_diff(p.get("changes")), 2 * 1024 * 1024)
            seen_snapshot = iid in self._file_diffs
            previous = self._file_diffs.get(iid, "")
            self._file_diffs[iid] = latest
            # patchUpdated is a snapshot. ToolDelta is append-only, so forward
            # only a genuinely-new suffix while the snapshots remain aligned.
            # Once the server rewrites an earlier hunk, no future suffix can be
            # appended safely to the browser's old projection; completion still
            # replaces it authoritatively through ToolResult.diff.
            if not seen_snapshot:
                self._file_diff_stream_aligned.add(iid)
            elif not latest.startswith(previous):
                self._file_diff_stream_aligned.discard(iid)
            if (latest and iid in self._file_diff_stream_aligned
                    and latest.startswith(previous)):
                delta = self._bounded_live_delta(
                    iid, "diff", latest[len(previous):], 512 * 1024)
                if delta:
                    out.append(ToolDelta(
                        tool_use_id=iid, stream="diff", delta=delta))

        elif method == "turn/diff/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"diff:{turn_id or p.get('turnId') or 'current'}", "diff")
            if not self._admit_live_item(iid, out):
                return out
            diff, truncated = bounded_text(p.get("diff"), 2 * 1024 * 1024)
            self._visible_output = True
            out.append(TurnDiff(
                item_id=iid,
                turn_id=turn_id,
                diff=diff,
                truncated=True if truncated else None,
            ))

        elif method == "item/mcpToolCall/progress":
            iid = _live_id(p.get("itemId"), "mcpToolCall-tool")
            if not self._admit_live_item(iid, out):
                return out
            progress = self._bounded_live_delta(
                iid, "progress", p.get("message"), 64 * 1024)
            if progress:
                out.append(ToolDelta(
                    tool_use_id=iid,
                    stream="progress",
                    delta=progress,
                ))

        elif method == "item/commandExecution/terminalInteraction":
            # The official payload's only interaction body is `stdin`.  It may
            # contain a password, token, or an answer to a secret prompt, so never
            # copy it to the wire.  Preserve a visible, sanitized timeline marker
            # instead of making the interaction look like a stalled command.
            command_id = _live_id(p.get("itemId"), "command-tool")
            iid = _live_id(f"{command_id}:terminal", "terminal")
            if not self._admit_live_item(iid, out):
                return out
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=iid,
                kind="terminal",
                phase="snapshot",
                status="succeeded",
                turn_id=_optional_wire_id(p.get("turnId"), "turn"),
                parent_id=command_id,
                title="终端交互",
                summary="已向运行中的终端进程写入输入（内容已隐藏）",
            ))

        elif method == "model/rerouted":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            from_model = _bounded_model_field(
                p.get("fromModel"), _MODEL_NAME_MAX_CHARS)
            to_model = _bounded_model_field(
                p.get("toModel"), _MODEL_NAME_MAX_CHARS)
            reason = _bounded_model_field(
                p.get("reason"), _MODEL_ENUM_MAX_CHARS)
            if turn_id and from_model and to_model and reason:
                iid = _live_id(
                    f"reroute:{turn_id}:{from_model}:{to_model}:{reason}",
                    "model-reroute",
                )
                if not self._admit_live_item(iid, out):
                    return out
                summary, _ = bounded_text(
                    f"{from_model} → {to_model}", 1024)
                detail, _ = bounded_text(
                    f"原因：{reason}", _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="model",
                    phase="snapshot",
                    status="succeeded",
                    turn_id=turn_id,
                    title="模型已重路由",
                    summary=summary,
                    detail=detail,
                ))

        elif method == "model/safetyBuffering/updated":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            model = _bounded_model_field(
                p.get("model"), _MODEL_NAME_MAX_CHARS)
            showing = p.get("showBufferingUi")
            if turn_id and model and isinstance(showing, bool):
                # One card follows the lifecycle of one turn/model pair. Repeated
                # updates therefore merge instead of filling the timeline.
                iid = _live_id(
                    f"safety-buffering:{turn_id}:{model}",
                    "model-safety-buffering",
                )
                if not self._admit_live_item(iid, out):
                    return out
                reasons = _bounded_model_list(p.get("reasons"))
                use_cases = _bounded_model_list(p.get("useCases"))
                faster_model = _bounded_model_field(
                    p.get("fasterModel"), _MODEL_NAME_MAX_CHARS)
                detail_parts = []
                if reasons:
                    detail_parts.append("原因：" + "、".join(reasons))
                if use_cases:
                    detail_parts.append("使用场景：" + "、".join(use_cases))
                if faster_model:
                    detail_parts.append(f"可用的更快模型：{faster_model}")
                detail, _ = bounded_text(
                    "\n".join(detail_parts), _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="safety",
                    phase="start" if showing else "end",
                    status="running" if showing else "succeeded",
                    turn_id=turn_id,
                    title="模型安全缓冲",
                    summary=f"模型：{model}",
                    detail=detail or None,
                ))

        elif method == "model/verification":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            verifications = _bounded_model_list(p.get("verifications"))
            if turn_id and verifications:
                iid = _live_id(
                    f"model-verification:{turn_id}", "model-verification")
                if not self._admit_live_item(iid, out):
                    return out
                summary, _ = bounded_text(
                    "、".join(verifications), _MODEL_DETAIL_MAX_CHARS)
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="safety",
                    phase="snapshot",
                    status="succeeded",
                    turn_id=turn_id,
                    title="模型验证",
                    summary=summary,
                ))

        elif method in {
            "item/autoApprovalReview/started",
            "item/autoApprovalReview/completed",
        }:
            event = _auto_approval_review_event(
                p, completed=method.endswith("/completed"))
            if event is not None and self._admit_live_item(event.item_id, out):
                self._visible_output = True
                out.append(event)

        elif method == "turn/moderationMetadata":
            # ``metadata`` is deliberately untyped in the public schema and may
            # contain provider-internal data. Preserve the lifecycle marker but
            # never forward the opaque payload across the remote boundary.
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            if turn_id and p.get("metadata") is not None:
                iid = _live_id(f"moderation:{turn_id}", "moderation")
                if not self._admit_live_item(iid, out):
                    return out
                self._visible_output = True
                out.append(ProcessEvent(
                    item_id=iid,
                    kind="safety",
                    phase="snapshot",
                    status="succeeded",
                    turn_id=turn_id,
                    title="内容安全检查",
                    summary="已完成（详细元数据未在远程端展示）",
                ))

        elif method in {"hook/started", "hook/completed"}:
            event = _hook_event(p, completed=(method == "hook/completed"))
            if event is not None and self._admit_live_item(event.item_id, out):
                self._visible_output = True
                out.append(event)

        elif method == "thread/compacted":
            turn_id = _optional_wire_id(p.get("turnId"), "turn")
            iid = _live_id(
                f"compaction:{turn_id or p.get('turnId') or 'current'}",
                "compaction")
            if not self._admit_live_item(iid, out):
                return out
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=iid,
                kind="compaction",
                phase="end",
                status="succeeded",
                turn_id=turn_id,
                title="压缩上下文",
            ))

        # Raw reasoning text is intentionally ignored. Only the public summary
        # notifications above and the summary array on a completed item cross the
        # remote boundary.

        elif method == "error":
            # Retrying provider failures are progress, not terminal errors. Emit a
            # running StateEvent so old clients remain compatible while new clients
            # can replace the generic spinner with a useful status.
            err = p.get("error") if isinstance(p.get("error"), dict) else {}
            if p.get("willRetry"):
                out.append(StateEvent(
                    state="running",
                    phase="retrying",
                    detail=_retry_detail(err),
                ))
            else:
                self._terminal_error = True
                out.append(Error(
                    code=ERR_CC_CRASH,
                    message=_provider_failure_message(err),
                ))

        elif method == "turn/completed":
            self._close_open(out)
            turn = p.get("turn") or {}
            st = turn.get("status") or "completed"
            if st == "completed" and turn.get("error") is not None:
                st = "failed"
            # A failed turn may carry provider diagnostics in turn.error. Keep
            # those on the local engine boundary and emit only stable product
            # copy (the error notification above may not fire for every mode).
            if st == "failed":
                if not self._terminal_error:
                    raw_error = (
                        turn.get("error")
                        if isinstance(turn.get("error"), dict)
                        else {}
                    )
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message=_provider_failure_message(raw_error),
                    ))
                    self._terminal_error = True
            if st == "completed" and not self._terminal_error:
                self._append_completed_plan_answer(out)
            # Codex 0.144.1 can record an upstream 503 as completed/error=null with
            # only the userMessage item. Treat that impossible "empty success" as
            # a terminal failure, while allowing tool-only turns as visible output.
            if st == "completed" and not self._visible_output:
                if not self._terminal_error:
                    out.append(Error(
                        code=ERR_CC_CRASH,
                        message=_EMPTY_COMPLETED_MESSAGE,
                    ))
                    self._terminal_error = True
                st = "failed"
            elif st == "completed" and self._terminal_error:
                st = "failed"
            # Map codex TurnStatus (completed|interrupted|failed) onto cc's wire
            # subtype vocabulary so the engine-agnostic reducer treats them right:
            # "interrupted" -> "error_during_execution" is the token the client keys
            # on to render the "— 已打断 —" note (verified: turn/interrupt yields
            # turn/completed{status:"interrupted"}).
            subtype = ("success" if st == "completed"
                       else "error_during_execution" if st == "interrupted"
                       else "error")
            completed_turn_id = turn.get("id")
            terminal = TurnEnd(result=TurnResult(
                subtype=subtype,
                duration_ms=int(turn.get("durationMs") or 0),
                is_error=(st != "completed"),
            ), turn_id=(completed_turn_id
                        if isinstance(completed_turn_id, str) else None))
            terminal._codex_authoritative_terminal = authoritative_terminal
            out.append(terminal)
            self._clear_all_delta_budgets()
            self._completed_plan = None
            self._turn_closed = True

        # everything else (raw reasoning, userMessage, mcpServer/startupStatus,
        # thread/status, account/rateLimits, tokenUsage, remoteControl…) -> skip.
        return out

    # ---- helpers ----
    def _admit_live_item(self, item_id: str, out: list) -> bool:
        if self._turn_closed:
            return False
        if item_id in self._live_items:
            return True
        if len(self._live_items) < _MAX_LIVE_ITEMS:
            self._live_items.add(item_id)
            return True
        if not self._live_items_truncated:
            self._live_items_truncated = True
            self._visible_output = True
            out.append(ProcessEvent(
                item_id=_LIVE_ITEMS_OMITTED_ID,
                kind="compaction",
                phase="snapshot",
                status="succeeded",
                title="较早过程已省略",
                summary="此回合的处理项目过多，后续新增项目未实时展示。",
            ))
        return False

    def _bounded_live_delta(
        self, item_id: str, stream: str, value, single_event_cap: int,
    ) -> str:
        """Bound cumulative append-only payload and append count per UI field."""
        key = (item_id, stream)
        if (item_id in self._finished_delta_items
                or key in self._truncated_delta_streams):
            return ""
        if key not in self._delta_chars and len(self._delta_chars) >= _MAX_DELTA_STREAMS:
            return ""
        budget = max(1, self.tool_result_max)
        used = self._delta_chars.get(key, 0)
        count = self._delta_events.get(key, 0)
        remaining = budget - used
        # Reserve the final allowed append for an explicit truncation marker.
        if remaining <= 0 or count >= _MAX_DELTA_EVENTS_PER_STREAM - 1:
            self._truncated_delta_streams.add(key)
            if remaining <= 0:
                return ""
            notice = _DELTA_TRUNCATION_NOTICE[-remaining:]
            self._delta_chars[key] = used + len(notice)
            self._delta_events[key] = count + 1
            return notice

        text, truncated = bounded_text(
            value, min(max(1, single_event_cap), remaining))
        if not text and not truncated:
            return ""
        if truncated:
            self._truncated_delta_streams.add(key)
            notice = _DELTA_TRUNCATION_NOTICE
            if len(notice) >= remaining:
                text = notice[-remaining:]
            else:
                text = text[:remaining - len(notice)] + notice
        self._delta_chars[key] = used + len(text)
        self._delta_events[key] = count + 1
        return text

    def _finish_delta_item(self, item_id: str) -> None:
        if len(self._finished_delta_items) < _MAX_FINISHED_DELTA_ITEMS:
            self._finished_delta_items.add(item_id)
        for key in [key for key in self._delta_chars if key[0] == item_id]:
            self._delta_chars.pop(key, None)
            self._delta_events.pop(key, None)
            self._truncated_delta_streams.discard(key)

    def _clear_all_delta_budgets(self) -> None:
        self._delta_chars.clear()
        self._delta_events.clear()
        self._truncated_delta_streams.clear()
        self._finished_delta_items.clear()

    def _close_open(self, out: list) -> None:
        if self._open_msg is None:
            return
        out.append(AssistantMsgEnd(
            message_id=self._open_msg,
            channel=self._open_channel,
        ))
        self._open_msg = None
        self._open_channel = "unknown"

    def _remember_completed_plan(self, item: dict, item_id: str) -> None:
        """Keep the last authoritative completed Plan as a bounded fallback."""
        text, _ = bounded_text(item.get("text"), 256 * 1024)
        self._completed_plan = (item_id, text) if text else None

    def _append_completed_plan_answer(self, out: list) -> None:
        """Expose a plan-only turn as the final answer without duplicating one."""
        if self._final_output or self._completed_plan is None:
            return
        item_id, text = self._completed_plan
        message_id = _live_id(f"{item_id}:final", "plan-answer")
        out.extend([
            AssistantMsgStart(message_id=message_id, channel="final"),
            Delta(message_id=message_id, text=text, channel="final"),
            AssistantMsgEnd(message_id=message_id, channel="final"),
        ])
        self._visible_output = True
        self._final_output = True

    def _ensure_block(self, mid: str, out: list) -> None:
        """A tool card needs an assistant message block to hang under (the reducer
        keys tool cards by message_id); open one lazily if none is active."""
        if self._open_msg is None:
            self._open_msg = mid
            self._open_channel = "commentary"
            self._started.add(mid)
            out.append(AssistantMsgStart(
                message_id=mid, channel="commentary"))

    def _tool_use(self, item: dict) -> list:
        out: list = []
        item_type = str(item.get("type") or "tool")
        iid = _live_id(item.get("id"), f"{item_type}-tool")
        if not self._admit_live_item(iid, out):
            return out
        if iid in self._tools_started:
            return out
        self._tools_started.add(iid)
        mid = self._open_msg or iid
        self._ensure_block(mid, out)
        self._tool_message_ids[iid] = self._open_msg or mid
        inp = _tool_input(item)
        tool, category, title, server = _tool_presentation(item)
        out.append(ToolUse(
            message_id=self._open_msg or "",
            tool_use_id=iid,
            tool=tool,
            input=bounded_tool_input(inp, self.tool_result_max),
            category=category,
            title=title,
            server=server,
        ))
        return out

    def _tool_update(self, item: dict) -> ToolUse:
        item_type = str(item.get("type") or "tool")
        iid = _live_id(item.get("id"), f"{item_type}-tool")
        tool, category, title, server = _tool_presentation(item)
        return ToolUse(
            message_id=self._tool_message_ids.get(iid, iid),
            tool_use_id=iid,
            tool=tool,
            input=bounded_tool_input(
                _tool_input(item), self.tool_result_max),
            category=category,
            title=title,
            server=server,
        )

    def _tool_result(self, item: dict) -> ToolResult:
        item_type = item.get("type")
        status = _process_status(item.get("status"))
        code = _nonnegative_or_signed_int(item.get("exitCode"))
        diff = None
        summary = None
        raw_content = item.get("aggregatedOutput") or item.get("output") or ""
        if item_type == "fileChange":
            diff, diff_truncated = bounded_text(
                _changes_diff(item.get("changes")), 2 * 1024 * 1024)
            paths = _change_paths(item.get("changes"))
            summary = _file_summary(paths, status)
            raw_content = summary
        elif item_type == "mcpToolCall":
            raw_content = _mcp_result_content(item)
            error = item.get("error") if isinstance(item.get("error"), dict) else {}
            summary, _ = bounded_text(error.get("message"), 64 * 1024)
            if summary:
                status = "failed"
        elif item_type == "dynamicToolCall":
            raw_content = _redact_credentials(item.get("contentItems") or "")
            success = item.get("success")
            if success is False:
                status = "failed"
            elif success is True:
                status = "succeeded"
        elif item_type == "webSearch":
            raw_content = {
                "query": item.get("query"),
                "action": item.get("action"),
            }
            status = "succeeded"
        text, was_truncated = bounded_text(raw_content, self.tool_result_max)
        truncated = True if was_truncated else None
        if item_type == "fileChange" and diff_truncated:
            truncated = True
        is_error = (
            status in {"failed", "declined", "cancelled", "interrupted"}
            or (code is not None and code != 0)
        )
        return ToolResult(
            tool_use_id=_live_id(item.get("id"), f"{item_type}-tool"),
            content=text,
            is_error=is_error,
            truncated=truncated,
            status=status,
            summary=summary or None,
            diff=diff or None,
            exit_code=code,
            duration_ms=_duration_ms(item.get("durationMs")),
        )

    def _ensure_reasoning(self, iid: str, params: dict):
        if iid in self._reasoning_started:
            return None
        self._reasoning_started.add(iid)
        return ProcessEvent(
            item_id=iid,
            kind="reasoning",
            phase="start",
            status="running",
            turn_id=_optional_wire_id(params.get("turnId"), "turn"),
            title="思考",
        )

    def _process_item(self, item: dict, params: dict, *, completed: bool):
        item_type = item.get("type")
        iid = _live_id(item.get("id"), str(item_type or "process"))
        turn_id = _optional_wire_id(params.get("turnId"), "turn")
        phase = "end" if completed else "start"
        status = "succeeded" if completed else "running"
        if item_type == "reasoning":
            summary = _reasoning_summary(item)
            if not summary:
                # Never substitute content/encryptedContent for a missing public
                # summary.
                return None
            self._reasoning_started.add(iid)
            return ProcessEvent(
                item_id=iid,
                kind="reasoning",
                phase=phase,
                status=status,
                turn_id=turn_id,
                title="思考",
                summary=summary,
            )
        if item_type == "plan":
            detail, _ = bounded_text(item.get("text"), 256 * 1024)
            return ProcessEvent(
                item_id=iid, kind="plan", phase=phase, status=status,
                turn_id=turn_id, title="计划", detail=detail or None)
        if item_type == "collabAgentToolCall":
            return _collab_event(item, turn_id, completed)
        if item_type == "subAgentActivity":
            return _subagent_event(item, turn_id, completed)
        if item_type == "contextCompaction":
            return ProcessEvent(
                item_id=iid, kind="compaction", phase=phase, status=status,
                turn_id=turn_id, title="压缩上下文")
        if item_type == "imageView":
            path, _ = bounded_text(item.get("path"), 16 * 1024)
            image_status = _process_status(item.get("status"))
            if completed and image_status in {"unknown", "running", "pending"}:
                image_status = "succeeded"
            return ProcessEvent(
                item_id=iid, kind="server_tool", phase=phase,
                status=image_status if completed else status,
                turn_id=turn_id, title="查看图片",
                summary=path or None,
                input={"file_path": path} if path else None,
                tool="view_image",
            )
        if item_type == "sleep":
            duration = _duration_ms(item.get("durationMs"))
            return ProcessEvent(
                item_id=iid, kind="task", phase=phase, status=status,
                turn_id=turn_id, title="等待",
                summary=_human_duration(duration) if duration is not None else None,
                duration_ms=duration,
            )
        if item_type == "imageGeneration":
            generated_status = _process_status(item.get("status"))
            if completed and generated_status in {"unknown", "running", "pending"}:
                generated_status = "succeeded"
            prompt, prompt_truncated = bounded_text(
                item.get("revisedPrompt"), 64 * 1024)
            path, _ = bounded_text(item.get("savedPath"), 16 * 1024)
            image = (_generated_image_payload(item.get("result"))
                     if completed and generated_status == "succeeded" else None)
            image_input: dict = {"file_path": path} if path else {}
            if image is not None and turn_id:
                image_input["history_image"] = codex_generated_image_ref(turn_id, image)
            # `result` may be a full base64 image. The saved file is previewable
            # through the existing authenticated artifact route; never duplicate
            # the binary payload into replay history or relay buffers.
            return ProcessEvent(
                item_id=iid, kind="server_tool", phase=phase,
                status=generated_status, turn_id=turn_id, title="生成图片",
                summary=prompt or (path if path else None),
                input=image_input or None, tool="image_generation",
                truncated=True if prompt_truncated else None,
            )
        if item_type in {"enteredReviewMode", "exitedReviewMode"}:
            review, review_truncated = bounded_text(
                item.get("review"), 256 * 1024)
            return ProcessEvent(
                item_id=iid, kind="safety", phase=phase, status=status,
                turn_id=turn_id,
                title=("进入 Review" if item_type == "enteredReviewMode"
                       else "退出 Review"),
                detail=review or None,
                truncated=True if review_truncated else None,
            )
        return None


def _human_duration(duration_ms: int) -> str:
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:g} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:g} 分钟"
    return f"{minutes / 60:g} 小时"


def _auto_approval_review_event(params: dict, *, completed: bool):
    review_id = params.get("reviewId")
    if not isinstance(review_id, str) or not review_id:
        return None
    review = params.get("review") if isinstance(params.get("review"), dict) else {}
    action = params.get("action") if isinstance(params.get("action"), dict) else {}
    action_type = str(action.get("type") or "")
    action_labels = {
        "command": "命令",
        "execve": "程序执行",
        "applyPatch": "文件修改",
        "networkAccess": "网络访问",
        "mcpToolCall": "MCP 工具",
        "requestPermissions": "权限请求",
    }
    review_status = _process_status(review.get("status"))
    if not completed:
        review_status = "running"
    elif review_status in {"unknown", "running", "pending"}:
        review_status = "succeeded"
    risk = str(review.get("riskLevel") or "")
    authorization = str(review.get("userAuthorization") or "")
    summary_parts = [action_labels.get(action_type, "工具操作")]
    if risk in {"low", "medium", "high", "critical"}:
        summary_parts.append(f"风险 {risk}")
    if authorization in {"unknown", "low", "medium", "high"}:
        summary_parts.append(f"用户授权 {authorization}")
    rationale, rationale_truncated = bounded_text(
        review.get("rationale"), 64 * 1024)
    started_at = _nonnegative_or_signed_int(params.get("startedAtMs"))
    completed_at = _nonnegative_or_signed_int(params.get("completedAtMs"))
    duration = None
    if started_at is not None and completed_at is not None and completed_at >= started_at:
        duration = completed_at - started_at
    return ProcessEvent(
        item_id=_live_id(review_id, "auto-approval-review"),
        kind="safety",
        phase="end" if completed else "start",
        status=review_status,
        turn_id=_optional_wire_id(params.get("turnId"), "turn"),
        parent_id=_optional_wire_id(params.get("targetItemId"), "item"),
        title="自动审批审查",
        summary=" · ".join(summary_parts),
        detail=rationale or None,
        duration_ms=duration,
        truncated=True if rationale_truncated else None,
    )


def _retry_detail(error: dict) -> str:
    """Return a bounded, credential-free retry status for the client."""
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    details = (error.get("additionalDetails")
               if isinstance(error.get("additionalDetails"), str) else "")
    combined = message + " " + details
    status_match = re.search(r"\b([45]\d\d)\b", combined)
    status = status_match.group(1) if status_match else _structured_http_status(error)
    attempt = re.search(r"\b(\d+\s*/\s*\d+)\b", combined)
    if status:
        text = f"上游服务返回 HTTP {status}，Codex 正在重试"
    else:
        text = "Codex 上游请求暂时失败，正在重试"
    if attempt:
        text += f"（{attempt.group(1).replace(' ', '')}）"
    return text + "…"


_GENERIC_TURN_FAILURE = "Codex 本次回复未完成，请重试。"
_AUTH_TURN_FAILURE = (
    "模型服务认证已失效或当前账号无权限，"
    "请检查当前服务的凭据或账号权限后重试。"
)
_RATE_LIMIT_TURN_FAILURE = "请求过于频繁或当前额度受限，请稍后重试。"
_TIMEOUT_TURN_FAILURE = "请求超时，请重新尝试。"
_UPSTREAM_TURN_FAILURE = "Codex 上游服务暂时不可用，请稍后重试。"
_NETWORK_TURN_FAILURE = "网络连接异常，请检查网络后重试。"
_POLICY_TURN_FAILURE = (
    "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    "若属误判请向服务提供方反馈。"
)


def _provider_failure_message(error: object) -> str:
    """Map one provider terminal to bounded, user-actionable product copy."""
    if not isinstance(error, dict):
        return _GENERIC_TURN_FAILURE
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    details = (
        error.get("additionalDetails")
        if isinstance(error.get("additionalDetails"), str)
        else ""
    )
    combined = f"{message} {details}"[:8192].lower()
    info = error.get("codexErrorInfo", error.get("codex_error_info"))
    if info in ("cyberPolicy", "cyber_policy") or (
        isinstance(info, dict)
        and ("cyberPolicy" in info or "cyber_policy" in info)
    ):
        return _POLICY_TURN_FAILURE
    status = _structured_http_status(error)
    if status is None:
        status_match = re.search(r"\b([45]\d\d)\b", combined)
        status = status_match.group(1) if status_match else None
    if status in {"401", "403"}:
        return _AUTH_TURN_FAILURE
    if status == "429":
        return _RATE_LIMIT_TURN_FAILURE
    if status == "408":
        return _TIMEOUT_TURN_FAILURE
    if status is not None:
        if status.startswith("5"):
            return _UPSTREAM_TURN_FAILURE
        # A concrete provider-side 4xx must not be relabelled as a local
        # network failure merely because its transport also disconnected.
        return _GENERIC_TURN_FAILURE
    if any(marker in combined for marker in (
        "request timed out",
        "request timeout",
        "timed out",
    )):
        return _TIMEOUT_TURN_FAILURE
    if any(marker in combined for marker in (
        "stream disconnected",
        "connection reset",
        "connection closed",
        "connection refused",
        "error sending request",
        "dns error",
        "tls error",
        "socket error",
    )):
        return _NETWORK_TURN_FAILURE
    return _GENERIC_TURN_FAILURE


def _bounded_model_field(value, max_chars: int) -> str:
    """Copy one declared model-notification string, never arbitrary payloads."""
    if not isinstance(value, str) or not value:
        return ""
    return bounded_text(value, max_chars)[0]


def _bounded_model_list(value) -> list[str]:
    """Bound declared string arrays by item count and per-item length."""
    if not isinstance(value, list):
        return []
    out = []
    for item in islice(value, _MODEL_LIST_MAX_ITEMS):
        text = _bounded_model_field(item, _MODEL_ENUM_MAX_CHARS)
        if text:
            out.append(text)
    return out


def _structured_http_status(error: dict) -> str | None:
    """Find a bounded codexErrorInfo.httpStatusCode without exposing details."""
    stack = [error.get("codexErrorInfo")]
    seen = 0
    while stack and seen < 32:
        value = stack.pop()
        seen += 1
        if not isinstance(value, dict):
            continue
        status = value.get("httpStatusCode")
        if isinstance(status, int) and 400 <= status <= 599:
            return str(status)
        stack.extend(list(value.values())[:16])
    return None


def _live_id(value, kind: str) -> str:
    """Return a protocol-safe, stable identity without trusting provider text."""
    if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
        return value
    if isinstance(value, str):
        identity = value[:4096]
    elif value is None:
        identity = "missing"
    else:
        identity = type(value).__name__
    return hashlib.sha256(
        f"codex\0{kind}\0{identity}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:32]


def _optional_wire_id(value, kind: str) -> str | None:
    return None if value is None else _live_id(value, kind)


def _assistant_channel(value) -> str:
    if value in {"final", "final_answer"}:
        return "final"
    if value == "commentary":
        return "commentary"
    if value == "thinking":
        return "thinking"
    return "unknown"


def _process_status(value) -> str:
    key = str(value or "").replace("_", "").replace("-", "").lower()
    if key in {"pending"}:
        return "pending"
    if key in {"inprogress", "running", "started"}:
        return "running"
    if key in {"completed", "complete", "succeeded", "success", "approved"}:
        return "succeeded"
    if key in {"failed", "failure", "error", "timedout", "timeout"}:
        return "failed"
    if key in {"declined", "denied", "blocked"}:
        return "declined"
    if key in {"cancelled", "canceled", "stopped"}:
        return "cancelled"
    if key in {"interrupted", "aborted"}:
        return "interrupted"
    return "unknown"


def _plan_status(value) -> str:
    status = _process_status(value)
    if status == "running":
        return "inProgress"
    if status == "succeeded":
        return "completed"
    return "pending"


def _duration_ms(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _nonnegative_or_signed_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _is_credential_key(key_text: str) -> bool:
    """Match common credential keys across snake, kebab and camel case."""
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_text)
    tokens = tuple(filter(None, re.split(r"[^a-z0-9]+", separated.lower())))
    compact = "".join(tokens)
    return (
        compact in _CREDENTIAL_EXACT_KEYS
        or any(fragment in compact for fragment in _CREDENTIAL_KEY_FRAGMENTS)
    )


def _redact_credentials(
    value,
    depth: int = 0,
    ancestors=None,
    node_budget=None,
):
    """Copy bounded JSON-like tool data and replace credential-bearing values."""
    if node_budget is None:
        node_budget = [_MAX_REDACTION_NODES]
    if node_budget[0] <= 0:
        return _REDACTION_BUDGET_EXCEEDED
    node_budget[0] -= 1
    if depth >= _MAX_REDACTION_DEPTH:
        return f"<{type(value).__name__} omitted>"
    if isinstance(value, dict):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>"
        ancestors.add(identity)
        try:
            out = {}
            for key, item in islice(
                value.items(), _MAX_REDACTION_DICT_ITEMS
            ):
                if node_budget[0] <= 0:
                    out[_REDACTION_REMAINDER_KEY] = (
                        _REDACTION_BUDGET_EXCEEDED
                    )
                    break
                key_text = key if isinstance(key, str) else f"<{type(key).__name__}>"
                if _is_credential_key(key_text):
                    node_budget[0] -= 1
                    safe_item = _REDACTED
                else:
                    safe_item = _redact_credentials(
                        item, depth + 1, ancestors, node_budget
                    )
                out[key_text[:128]] = safe_item
            return out
        finally:
            ancestors.discard(identity)
    if isinstance(value, (list, tuple)):
        ancestors = ancestors if ancestors is not None else set()
        identity = id(value)
        if identity in ancestors:
            return "<cycle omitted>"
        ancestors.add(identity)
        try:
            out = []
            for item in islice(value, _MAX_REDACTION_SEQUENCE_ITEMS):
                if node_budget[0] <= 0:
                    out.append(_REDACTION_BUDGET_EXCEEDED)
                    break
                out.append(_redact_credentials(
                    item, depth + 1, ancestors, node_budget
                ))
            return out
        finally:
            ancestors.discard(identity)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _tool_input(item: dict) -> dict:
    item_type = item.get("type")
    if item_type == "commandExecution":
        out = {
            "command": item.get("command"),
            "cwd": item.get("cwd"),
            "actions": item.get("commandActions"),
            "source": item.get("source"),
        }
        if item.get("processId") is not None:
            out["process_id"] = item.get("processId")
        return {key: value for key, value in out.items() if value is not None}
    if item_type == "fileChange":
        changes = _change_descriptors(item.get("changes"))
        return {
            "changes": changes,
            "file_paths": _descriptor_paths(changes),
        }
    if item_type == "mcpToolCall":
        arguments = item.get("arguments")
        if isinstance(arguments, dict):
            return _redact_credentials(arguments)
        return {"arguments": _redact_credentials(arguments)}
    if item_type == "dynamicToolCall":
        arguments = item.get("arguments")
        sanitized = _redact_credentials(arguments)
        out = sanitized if isinstance(sanitized, dict) else {"arguments": sanitized}
        if item.get("namespace") is not None:
            out = dict(out)
            out["namespace"] = item.get("namespace")
        return out
    if item_type == "webSearch":
        return {
            key: item.get(key) for key in ("query", "action")
            if item.get(key) is not None
        }
    return {}


def _tool_presentation(item: dict) -> tuple[str, str, str | None, str | None]:
    item_type = item.get("type")
    if item_type == "commandExecution":
        actions = item.get("commandActions") or []
        first = actions[0] if actions and isinstance(actions[0], dict) else {}
        action_type = first.get("type")
        if action_type == "read":
            path = first.get("path") or first.get("name")
            title = f"读取 {path}" if path else "读取文件"
            return "readFile", "command", title, None
        if action_type == "listFiles":
            path = first.get("path")
            title = f"列出 {path}" if path else "列出文件"
            return "listFiles", "command", title, None
        if action_type == "search":
            query = first.get("query")
            title = f"搜索 {query}" if query else "搜索内容"
            return "search", "command", title, None
        return "shell", "command", "运行命令", None
    if item_type == "fileChange":
        paths = _change_paths(item.get("changes"))
        return "apply_patch", "file", _file_summary(paths, "running"), None
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "MCP")[:1024]
        tool = str(item.get("tool") or "mcp")[:1024]
        return tool, "mcp", f"{server} · {tool}"[:1024], server
    if item_type == "dynamicToolCall":
        tool = str(item.get("tool") or "dynamicTool")[:1024]
        namespace = item.get("namespace")
        title = f"{namespace} · {tool}" if namespace else tool
        return tool, "server_tool", title[:1024], None
    if item_type == "webSearch":
        query, _ = bounded_text(item.get("query"), 900)
        return "webSearch", "web_search", (
            f"搜索 {query}" if query else "搜索网页"), None
    return str(item_type or "tool")[:1024], "tool", None, None


def _change_descriptors(changes) -> list[dict]:
    descriptors: list[dict] = []
    if isinstance(changes, list):
        iterable = changes[:64]
        for entry in iterable:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            kind_name = kind.get("type") if isinstance(kind, dict) else kind
            descriptor = {
                "path": str(entry.get("path") or "")[:16 * 1024],
                "kind": str(kind_name or "update")[:128],
            }
            move_path = _change_move_path(entry)
            if isinstance(move_path, str) and move_path:
                descriptor["move_path"] = move_path[:16 * 1024]
            descriptors.append(descriptor)
    elif isinstance(changes, dict):
        for path, change in list(changes.items())[:64]:
            kind = change.get("type") if isinstance(change, dict) else "update"
            descriptor = {
                "path": str(path)[:16 * 1024],
                "kind": str(kind or "update")[:128],
            }
            move_path = (change.get("move_path") if isinstance(change, dict)
                         else None)
            if isinstance(move_path, str) and move_path:
                descriptor["move_path"] = move_path[:16 * 1024]
            descriptors.append(descriptor)
    return descriptors


def _change_paths(changes) -> list[str]:
    return _descriptor_paths(_change_descriptors(changes))


def _descriptor_paths(descriptors: list[dict]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for entry in descriptors:
        for key in ("path", "move_path"):
            path = entry.get(key)
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _changes_diff(changes) -> str:
    """Normalize v2 FileUpdateChange arrays and legacy path->change maps."""
    parts: list[str] = []
    if isinstance(changes, list):
        for entry in changes[:64]:
            if not isinstance(entry, dict):
                continue
            diff = _change_diff(str(entry.get("path") or "file"), entry)
            if isinstance(diff, str) and diff:
                parts.append(diff)
    elif isinstance(changes, dict):
        for path, entry in list(changes.items())[:64]:
            if not isinstance(entry, dict):
                continue
            diff = _change_diff(str(path), entry)
            if isinstance(diff, str) and diff:
                parts.append(diff)
    return "\n".join(parts)


def _change_move_path(entry: dict) -> str | None:
    kind = entry.get("kind")
    nested = kind.get("move_path") if isinstance(kind, dict) else None
    move_path = (entry.get("move_path") or entry.get("destination_path")
                 or entry.get("to") or nested)
    return move_path if isinstance(move_path, str) and move_path else None


def _change_diff(path: str, entry: dict) -> str:
    kind = entry.get("kind") or entry.get("type") or "update"
    if isinstance(kind, dict):
        kind = kind.get("type") or "update"
    normalized_kind = str(kind).lower()
    from_file = "/dev/null" if normalized_kind in {
        "add", "create", "added",
    } else path
    move_path = _change_move_path(entry)
    to_file = "/dev/null" if normalized_kind in {
        "delete", "remove", "deleted",
    } else move_path or path

    explicit = entry.get("unified_diff") or entry.get("diff")
    if isinstance(explicit, str) and explicit:
        # FileUpdateChange.diff is authoritative but current app-server builds
        # may omit file headers. Add only those structural headers so the web
        # parser can render the native hunks; never derive content from the
        # current worktree.
        if ("@@" in explicit
                and not re.search(r"(?m)^(?:diff --git |--- |\+\+\+ )", explicit)):
            return f"--- {from_file}\n+++ {to_file}\n{explicit}"
        return explicit

    old_content = entry.get("old_content")
    new_content = entry.get("content")
    if normalized_kind in {"add", "create", "added"}:
        old_content = ""
    elif normalized_kind in {"delete", "remove", "deleted"}:
        old_content = (entry.get("content") if old_content is None
                       else old_content)
        new_content = ""
    if not isinstance(old_content, str) or not isinstance(new_content, str):
        return ""
    return "".join(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=from_file,
        tofile=to_file,
    ))


def _file_summary(paths: list[str], status: str) -> str:
    prefix = "修改了" if status == "succeeded" else "修改"
    if not paths:
        return f"{prefix}文件"
    if len(paths) == 1:
        return f"{prefix} {paths[0]}"[:64 * 1024]
    return f"{prefix} {len(paths)} 个文件"


def _reasoning_summary(item: dict) -> str:
    values = item.get("summary")
    parts: list[str] = []
    if isinstance(values, list):
        for value in values[:128]:
            if isinstance(value, str):
                text = value
            elif isinstance(value, dict) and value.get("type") == "summary_text":
                text = value.get("text")
            else:
                continue
            if isinstance(text, str) and text:
                parts.append(text)
    text, _ = bounded_text("\n\n".join(parts), 64 * 1024)
    return text


def _mcp_result_content(item: dict):
    error = item.get("error") if isinstance(item.get("error"), dict) else None
    if error is not None:
        return error.get("message") or "MCP tool call failed"
    result = item.get("result")
    if not isinstance(result, dict):
        return result or ""
    # `_meta` is server-private and can contain connector/session data. Never put
    # it in a replayable client ring.
    return _redact_credentials({
        key: result.get(key) for key in ("content", "structuredContent")
        if result.get(key) is not None
    })


def _collab_event(item: dict, turn_id: str | None, completed: bool):
    tool = str(item.get("tool") or "agent")[:1024]
    status = _process_status(item.get("status"))
    if completed and status in {"unknown", "running", "pending"}:
        status = "succeeded"
    labels = {
        "spawnAgent": "启动协作代理",
        "sendInput": "向协作代理发送消息",
        "resumeAgent": "恢复协作代理",
        "wait": "等待协作代理",
        "closeAgent": "关闭协作代理",
    }
    states = item.get("agentsStates")
    safe_states = {}
    if isinstance(states, dict):
        for agent_id, state in list(states.items())[:32]:
            if not isinstance(state, dict):
                continue
            safe_states[str(agent_id)[:128]] = {
                "status": _process_status(state.get("status")),
            }
    input_value = bounded_tool_input({
        "prompt": item.get("prompt"),
        "model": item.get("model"),
        "reasoning_effort": item.get("reasoningEffort"),
        "receivers": item.get("receiverThreadIds"),
        "agents": safe_states,
    }, 64 * 1024)
    return ProcessEvent(
        item_id=_live_id(item.get("id"), "collab-agent"),
        kind="agent",
        phase="end" if completed else "start",
        status=status,
        turn_id=turn_id,
        parent_id=_optional_wire_id(item.get("senderThreadId"), "thread"),
        title=labels.get(tool, "协作代理"),
        input=input_value,
        tool=tool,
    )


def _subagent_event(item: dict, turn_id: str | None, completed: bool):
    kind = str(item.get("kind") or "started")
    status = (
        "interrupted" if kind == "interrupted"
        else "succeeded" if completed
        else "running"
    )
    path, _ = bounded_text(item.get("agentPath"), 16 * 1024)
    return ProcessEvent(
        item_id=_live_id(item.get("id"), "sub-agent"),
        kind="agent",
        phase="end" if completed else "start",
        status=status,
        turn_id=turn_id,
        parent_id=_optional_wire_id(item.get("agentThreadId"), "thread"),
        title={
            "started": "协作代理已启动",
            "interacted": "协作代理有新进展",
            "interrupted": "协作代理已中断",
        }.get(kind, "协作代理"),
        summary=path or None,
    )


def _hook_event(params: dict, *, completed: bool):
    run = params.get("run") if isinstance(params.get("run"), dict) else None
    if run is None:
        return None
    status = _process_status(run.get("status"))
    if completed and status in {"unknown", "running", "pending"}:
        status = "succeeded"
    event_name = str(run.get("eventName") or "hook")[:256]
    handler_type = str(run.get("handlerType") or "")[:128]
    # Hook output/statusMessage can include command output, environment data, or
    # credentials. Only lifecycle metadata crosses the remote boundary.
    return ProcessEvent(
        item_id=_live_id(run.get("id"), "hook"),
        kind="hook",
        phase="end" if completed else "start",
        status=status,
        turn_id=_optional_wire_id(params.get("turnId"), "turn"),
        title=(f"Hook · {event_name}" + (
            f" · {handler_type}" if handler_type else ""))[:1024],
        duration_ms=_duration_ms(run.get("durationMs")),
    )


# ---- helpers the machine loop needs (codex analogs of stream.extract_*) ----

def codex_session_id(msg: dict) -> str | None:
    """Thread id from either current app-server notification shape."""
    p = msg.get("params") or {}
    thread_id = p.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        return thread_id
    th = p.get("thread")
    if isinstance(th, dict):
        return th.get("id") or th.get("sessionId")
    return None


def is_turn_terminal(msg: dict) -> bool:
    """Codex's turn/completed plays the role of Claude's ResultMessage."""
    return msg.get("method") == "turn/completed"


# ---- on-disk Codex rollout -> wire events (session history) ----

def codex_translate_history(
    path: str,
    tool_result_max: int,
    *,
    start_offset: int = 0,
    end_offset: int | None = None,
    source_continuation: str | None = None,
    snapshot_in_progress: bool = False,
    active_task_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
    client_message_ids: dict[str, str] | None = None,
    segment_client_message_ids: dict[tuple[str, int], str] | None = None,
) -> tuple[list, str | None]:
    """Translate a Codex rollout .jsonl into wire events (same vocabulary as the
    live stream) + the model used. Codex analog of stream.translate_history.

    A turn = persisted user boundary -> (function_call/reasoning...) -> agent_message.
    Skips the <environment_context>/<permissions> developer/user envelope messages;
    uses the clean event_msg user_message / agent_message text. Returns
    (events, model)."""
    events: list = []
    model: str | None = None
    turn_open = False
    active_turn_id: str | None = None
    active_msg_id: str | None = None
    pending_turn_id: str | None = None
    turn_visible = False
    turn_text_visible = False
    turn_final_visible = False
    turn_has_user = False
    turn_continuation_reason: str | None = None
    source_continuation_available = (
        source_continuation == "authoritative_page")
    assistant_open = False
    cur_mid: str | None = None
    cur_channel = "unknown"
    last_ts = None
    pending_images: list = []   # input_image blocks seen before the next user_message
    pending_legacy_user_item_id: str | None = None
    pending_compactions: list[
        tuple[str, float | None, str | None]
    ] = []
    pending_agent_message: tuple[dict, int, str] | None = None
    goal_baseline_known, goal_objective = (
        _history_goal_objective_before_offset(path, start_offset)
    )
    pending_goal_prompt: tuple[str, float | None] | None = None
    pending_task_goal_prompt: tuple[
        str, float | None, str, int, str,
    ] | None = None
    pending_task_started: tuple[str, float | None, int, str] | None = None
    completed_plan: tuple[str, str] | None = None
    task_has_user = False
    # Ordinal aliases are safe only after this bounded read has observed the
    # native task/start marker. A window which begins mid-turn must not mistake
    # its first visible steer for segment zero.
    task_segment_index: int | None = None
    known_active_task_ids = {
        value for value in active_task_ids
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value)
    }
    native_client_aliases = client_message_ids or {}
    segment_client_aliases = segment_client_message_ids or {}
    seen_tool_uses: set[str] = set()
    seen_tool_results: set[str] = set()
    seen_authoritative_results: set[str] = set()
    plan_tool_ids: set[str] = set()
    seen_process_items: set[str] = set()
    history_tools: dict[str, tuple[str, str, str | None, str | None, dict]] = {}
    seen_agent_messages: set[tuple[str, str, str]] = set()
    seen_reasoning: set[tuple[str, str]] = set()

    def _ts(iso: str):
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    def _stable_id(kind: str, line_no: int, raw_ts: str = "", identity=None) -> str:
        """Deterministic fallback for rollout records that carry no item id."""
        stable_identity = str(identity or active_turn_id or "")
        return _fallback_history_id(
            path, kind, line_no, raw_ts, stable_identity)

    def _history_id(value, kind: str, line_no: int, raw_ts: str = "") -> str:
        if isinstance(value, str) and _SAFE_WIRE_ID.fullmatch(value):
            return value
        identity = value[:1024] if isinstance(value, str) else type(value).__name__
        return _stable_id(kind, line_no, raw_ts, identity)

    def _duration(payload: dict) -> int:
        try:
            return int(payload.get("duration_ms") or payload.get("durationMs") or 0)
        except (TypeError, ValueError):
            return 0

    def _completed_ts(payload: dict, fallback):
        value = payload.get("completed_at") or payload.get("completedAt")
        if isinstance(value, (int, float)):
            value = float(value)
            return value / 1000 if value > 100_000_000_000 else value
        if isinstance(value, str):
            return _ts(value) or fallback
        return fallback

    def ensure_assistant(
        line_no: int,
        raw_ts: str = "",
        item_id=None,
        channel: str = "commentary",
        *,
        force_new: bool = False,
    ):
        nonlocal assistant_open, cur_mid, cur_channel
        if force_new and assistant_open:
            close_assistant()
        if assistant_open and cur_channel != channel:
            close_assistant()
        if not assistant_open:
            cur_mid = _history_id(item_id, "assistant", line_no, raw_ts)
            assistant_open = True
            cur_channel = channel
            events.append(AssistantMsgStart(
                message_id=cur_mid, channel=channel))

    def close_assistant(**message_fields):
        nonlocal assistant_open, cur_mid, cur_channel
        if assistant_open and cur_mid:
            events.append(AssistantMsgEnd(
                message_id=cur_mid, channel=cur_channel, **message_fields))
        assistant_open = False
        cur_mid = None
        cur_channel = "unknown"

    def append_compaction(
        marker: tuple[str, float | None, str | None],
    ) -> None:
        nonlocal turn_visible
        item_id, stamp, owner = marker
        event = ProcessEvent(
            item_id=item_id,
            kind="compaction",
            phase="end",
            status="succeeded",
            turn_id=owner,
            title="压缩上下文",
        )
        if stamp is not None:
            event.ts = stamp
        events.append(event)
        turn_visible = True

    def flush_pending_compactions(target_owner: str | None) -> None:
        """Materialize only markers compatible with the now-visible owner.

        The marker freezes the task id visible when context_compacted arrived.
        A later task_started must never relabel it onto a new user turn.
        """
        if not pending_compactions:
            return
        normalized_target = _history_optional_turn_id(target_owner)
        markers = list(pending_compactions)
        pending_compactions.clear()
        for marker in markers:
            owner = marker[2]
            if (owner is not None and normalized_target is not None
                    and owner != normalized_target):
                continue
            append_compaction(marker)

    def upsert_tool_use(
        tool_id: str,
        tool: str,
        category: str,
        title: str | None,
        server: str | None,
        tool_input: dict,
        line_no: int,
        raw_ts: str,
    ) -> None:
        nonlocal turn_visible
        history_tools[tool_id] = (
            tool, category, title, server, tool_input)
        for event in reversed(events):
            if isinstance(event, ToolUse) and event.tool_use_id == tool_id:
                event.tool = tool
                event.category = category
                event.title = title
                event.server = server
                event.input = tool_input
                seen_tool_uses.add(tool_id)
                turn_visible = True
                return
        ensure_assistant(line_no, raw_ts)
        events.append(ToolUse(
            message_id=cur_mid or "",
            tool_use_id=tool_id,
            tool=tool,
            input=tool_input,
            category=category,
            title=title,
            server=server,
        ))
        seen_tool_uses.add(tool_id)
        turn_visible = True

    def upsert_tool_result(result: ToolResult) -> None:
        nonlocal turn_visible
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if (isinstance(event, ToolResult)
                    and event.tool_use_id == result.tool_use_id):
                events[index] = result
                seen_tool_results.add(result.tool_use_id)
                turn_visible = True
                return
        events.append(result)
        seen_tool_results.add(result.tool_use_id)
        turn_visible = True

    def materialize_pending_goal_turn() -> bool:
        """Create a Goal user anchor only after its native turn is visible.

        ``thread_goal_updated`` and ``task_started`` are metadata, not proof
        that a model turn ran.  A real assistant/tool/terminal record calls
        this helper; a following ordinary ``user_message`` discards the
        candidate before it can steal that prompt.
        """
        nonlocal pending_task_goal_prompt, turn_open, active_turn_id
        nonlocal active_msg_id, pending_turn_id, turn_visible
        nonlocal turn_text_visible, turn_final_visible, turn_has_user
        nonlocal turn_continuation_reason, source_continuation_available
        nonlocal task_has_user
        candidate = pending_task_goal_prompt
        if candidate is None:
            return False
        pending_task_goal_prompt = None
        prompt, prompt_ts, turn_id, line_no, raw_ts = candidate
        if turn_open:
            close_turn(
                "error", 0, True,
                authoritative_boundary=False,
            )
        active_turn_id = turn_id
        pending_turn_id = turn_id
        uid = _history_id(turn_id, "user", line_no, raw_ts)
        active_msg_id = uid
        user = UserMsg(msg_id=uid, prompt=prompt)
        if prompt_ts is not None:
            user.ts = prompt_ts
        events.append(user)
        turn_open = True
        turn_visible = False
        turn_text_visible = False
        turn_final_visible = False
        turn_has_user = True
        turn_continuation_reason = None
        source_continuation_available = False
        task_has_user = True
        flush_pending_compactions(active_turn_id)
        return True

    def open_assistant_only_turn(
        reason: str | None = None,
        turn_id: str | None = None,
    ):
        """Start a visible continuation that has no user_message record.

        Goal/background continuations can begin with task_started after the
        previous user turn is already complete. The first visible assistant or
        tool item proves this is a separate assistant-only turn.
        """
        nonlocal turn_open, active_turn_id, active_msg_id
        nonlocal turn_visible, turn_text_visible, turn_final_visible
        nonlocal turn_has_user, turn_continuation_reason
        nonlocal source_continuation_available
        materialize_pending_goal_turn()
        if turn_open:
            if (not turn_has_user and reason is not None
                    and turn_continuation_reason is None):
                turn_continuation_reason = reason
            flush_pending_compactions(active_turn_id)
            return
        pending_owner = (
            pending_compactions[0][2] if pending_compactions else None
        )
        turn_open = True
        active_turn_id = turn_id or pending_owner or pending_turn_id
        active_msg_id = None
        turn_visible = False
        turn_text_visible = False
        turn_final_visible = False
        turn_has_user = False
        turn_continuation_reason = (
            reason or ("context_compacted" if pending_compactions else None)
        )
        if (turn_continuation_reason is None
                and source_continuation_available):
            turn_continuation_reason = "authoritative_page"
        source_continuation_available = False
        flush_pending_compactions(active_turn_id)

    def materialize_pending_terminal(value) -> None:
        materialize_pending_goal_turn()
        if not pending_compactions or turn_open:
            return
        terminal_owner = _history_optional_turn_id(value)
        marker_owner = pending_compactions[0][2]
        if (terminal_owner is not None and marker_owner is not None
                and terminal_owner != marker_owner):
            pending_compactions.clear()
            return
        open_assistant_only_turn(
            "context_compacted", marker_owner or terminal_owner)

    def emit_agent_message(
        payload: dict,
        line_no: int,
        raw_ts: str,
        item_id=None,
    ) -> None:
        nonlocal turn_visible, turn_text_visible, turn_final_visible
        open_assistant_only_turn()
        text = payload.get("message") or ""
        channel = _assistant_channel(payload.get("phase"))
        key = (
            str(active_turn_id or pending_turn_id or ""),
            (f"async:{channel}:{item_id or payload.get('id') or line_no}"
             if payload.get("delivery") == "async" else channel),
            text,
        )
        if not text or key in seen_agent_messages:
            return
        seen_agent_messages.add(key)
        close_assistant()
        ensure_assistant(
            line_no,
            raw_ts,
            item_id or payload.get("id") or payload.get("message_id"),
            channel=channel,
        )
        turn_visible = True
        turn_text_visible = True
        if channel == "final" and payload.get("delivery") != "async":
            turn_final_visible = True
        events.append(Delta(
            message_id=cur_mid, text=text, channel=channel))
        close_assistant(**_async_message_fields(payload))

    def emit_completed_plan_answer(
        line_no: int,
        raw_ts: str,
    ) -> None:
        nonlocal turn_visible, turn_text_visible, turn_final_visible
        if turn_final_visible or completed_plan is None:
            return
        item_id, text = completed_plan
        turn_key = str(active_turn_id or pending_turn_id or "")
        close_assistant()
        ensure_assistant(
            line_no,
            raw_ts,
            _live_id(f"{item_id}:final", "plan-answer"),
            channel="final",
            force_new=True,
        )
        events.append(Delta(
            message_id=cur_mid, text=text, channel="final"))
        close_assistant()
        seen_agent_messages.add((turn_key, "final", text))
        turn_visible = True
        turn_text_visible = True
        turn_final_visible = True

    def paired_agent_item_id(
        payload: dict,
        pending: tuple[dict, int, str],
    ) -> str | None:
        pending_payload, _line_no, _raw_ts = pending
        if (payload.get("type") != "message"
                or payload.get("role") != "assistant"
                or _assistant_channel(payload.get("phase"))
                != _assistant_channel(pending_payload.get("phase"))):
            return None
        response_text = "".join(
            item.get("text", "")
            for item in (payload.get("content") or [])
            if (isinstance(item, dict)
                and item.get("type") in {"output_text", "text"}
                and isinstance(item.get("text"), str))
        )
        clean_text = pending_payload.get("message")
        if (not isinstance(clean_text, str) or not clean_text
                or not response_text.startswith(clean_text)):
            return None
        item_id = payload.get("id")
        return (
            item_id
            if isinstance(item_id, str) and _SAFE_WIRE_ID.fullmatch(item_id)
            else None
        )

    def close_turn(
        subtype: str,
        duration_ms: int,
        is_error: bool,
        completed_ts=None,
        completed_turn_id=None,
        authoritative_boundary: bool = True,
    ):
        nonlocal turn_open, active_turn_id, active_msg_id, pending_turn_id
        nonlocal assistant_open, cur_mid, turn_visible, turn_text_visible
        nonlocal turn_final_visible, turn_has_user, turn_continuation_reason
        nonlocal completed_plan, pending_task_goal_prompt
        nonlocal pending_task_started
        if not turn_open:
            return
        close_assistant()
        # Automatic continuations may replace the initially-visible turn id.
        # The message action must fork after the last internal turn that actually
        # completed this visible reply, so prefer the terminal record, then the
        # latest task_started/turn_context id, and only then the first user turn.
        terminal_turn_id = (
            completed_turn_id or pending_turn_id
            if authoritative_boundary else None
        )
        if (not isinstance(terminal_turn_id, str)
                or not _SAFE_WIRE_ID.fullmatch(terminal_turn_id)):
            terminal_turn_id = None
        te = TurnEnd(result=TurnResult(
            subtype=subtype, duration_ms=duration_ms, is_error=is_error),
            turn_id=terminal_turn_id)
        terminal_ts = completed_ts if completed_ts is not None else last_ts
        if terminal_ts is not None:
            te.ts = terminal_ts
        events.append(te)
        turn_open = False
        pending_turn_id = None
        active_turn_id = None
        active_msg_id = None
        turn_visible = False
        turn_text_visible = False
        turn_final_visible = False
        turn_has_user = False
        turn_continuation_reason = None
        completed_plan = None
        pending_task_goal_prompt = None
        pending_task_started = None
        pending_compactions.clear()

    try:
        f = open(path, "rb")
    except Exception:
        return [], None
    with f:
        if start_offset > 0:
            f.seek(start_offset)
        for line_no, line in _bounded_jsonl_records(f, end_offset=end_offset):
            try:
                d = json.loads(line)
            except Exception:
                pending_legacy_user_item_id = None
                if pending_agent_message is not None:
                    payload, pending_line, pending_ts = pending_agent_message
                    emit_agent_message(
                        payload, pending_line, pending_ts)
                    pending_agent_message = None
                continue
            t = d.get("type")
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            raw_ts = d.get("timestamp", "")
            ts = _ts(raw_ts)
            payload_type = p.get("type")
            paired_legacy_user_item_id = None
            if pending_legacy_user_item_id is not None:
                if t == "event_msg" and payload_type == "user_message":
                    paired_legacy_user_item_id = pending_legacy_user_item_id
                # Never carry a native id across an intervening rollout row.
                pending_legacy_user_item_id = None

            consumed_paired_agent = False
            if pending_agent_message is not None:
                paired_id = (
                    paired_agent_item_id(p, pending_agent_message)
                    if t == "response_item" else None
                )
                payload, pending_line, pending_ts = pending_agent_message
                emit_agent_message(
                    payload, pending_line, pending_ts, paired_id)
                pending_agent_message = None
                consumed_paired_agent = paired_id is not None
            if consumed_paired_agent:
                if ts is not None:
                    last_ts = ts
                continue

            if t == "session_meta":
                continue
            elif t == "turn_context":
                if p.get("model"):
                    model = p["model"]
                context_turn_id = p.get("turn_id")
                if context_turn_id:
                    context_turn_id = str(context_turn_id)
                    # Codex can start an automatic continuation with a new turn_id
                    # but no new user_message. It is still the same visible chat
                    # turn, so only a real user_message creates a boundary.
                    pending_turn_id = context_turn_id
            elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                # Legacy Codex writes this native app-server item immediately
                # before the clean event_msg/user_message. Keep its id for that
                # one adjacent record only; clientUserMessageId remains a
                # separate browser alias.
                pending_legacy_user_item_id = (
                    _legacy_response_user_item_id(p)
                )
                for it in (p.get("content") or []):
                    if isinstance(it, dict) and it.get("type") == "input_image":
                        img = _data_uri_to_img(it.get("image_url"))
                        if img:
                            pending_images.append(img)
            elif t == "event_msg" and payload_type == "thread_goal_updated":
                goal = p.get("goal")
                if isinstance(goal, dict):
                    objective = goal.get("objective")
                    created_at = goal.get("createdAt")
                    updated_at = goal.get("updatedAt")
                    status = goal.get("status")
                    if isinstance(objective, str) and objective:
                        objective_changed = bool(
                            objective != goal_objective
                            and (
                                goal_baseline_known
                                or (
                                    isinstance(created_at, (int, float))
                                    and not isinstance(created_at, bool)
                                    and created_at == updated_at
                                )
                            )
                        )
                        goal_objective = objective
                        goal_baseline_known = True
                        if status != "active":
                            pending_goal_prompt = None
                            pending_task_goal_prompt = None
                        if objective_changed and status == "active":
                            pending_goal_prompt = (objective, ts)
                            if (
                                pending_task_started is not None
                                and not task_has_user
                            ):
                                (
                                    task_turn_id,
                                    task_ts,
                                    task_line_no,
                                    task_raw_ts,
                                ) = pending_task_started
                                if (
                                    ts is not None
                                    and task_ts is not None
                                    and 0 <= ts - task_ts
                                    <= _GOAL_TURN_CORRELATION_SECONDS
                                ):
                                    pending_task_goal_prompt = (
                                        objective,
                                        ts,
                                        task_turn_id,
                                        task_line_no,
                                        task_raw_ts,
                                    )
                                    pending_goal_prompt = None
            elif t == "event_msg" and payload_type == "thread_goal_cleared":
                goal_objective = None
                goal_baseline_known = True
                pending_goal_prompt = None
                pending_task_goal_prompt = None
            elif t == "event_msg" and payload_type == "task_started":
                pending_task_goal_prompt = None
                pending_task_started = None
                next_turn_id = p.get("turn_id")
                if next_turn_id:
                    next_turn_id = str(next_turn_id)
                    marker_owner = (
                        pending_compactions[0][2]
                        if pending_compactions else None
                    )
                    if (pending_compactions
                            and marker_owner != _history_optional_turn_id(
                                next_turn_id)):
                        pending_compactions.clear()
                    pending_turn_id = next_turn_id
                task_has_user = False
                task_segment_index = 0
                if pending_turn_id:
                    pending_task_started = (
                        str(pending_turn_id), ts, line_no, raw_ts,
                    )
                if pending_goal_prompt is not None and pending_task_started:
                    prompt, prompt_ts = pending_goal_prompt
                    if _goal_turn_correlated(prompt_ts, ts):
                        pending_task_goal_prompt = (
                            prompt,
                            prompt_ts if prompt_ts is not None else ts,
                            pending_task_started[0],
                            line_no,
                            raw_ts,
                        )
                pending_goal_prompt = None
            elif (
                t == "event_msg"
                and (user_record := codex_rollout_user_message(p)) is not None
            ):
                pending_goal_prompt = None
                pending_task_goal_prompt = None
                pending_task_started = None
                user_client_id = (
                    user_record.client_id
                    if isinstance(user_record.client_id, str)
                    and _SAFE_WIRE_ID.fullmatch(user_record.client_id)
                    else None
                )
                account_switch_continuation = (
                    is_codex_account_switch_message(user_record.raw_text)
                )
                msg = user_record.prompt
                if (
                    account_switch_continuation
                    and events
                    and isinstance(events[-1], TurnEnd)
                    and events[-1].result.is_error
                ):
                    # The old daemon's interrupted terminal and this private
                    # continuation are one logical browser turn. The final
                    # replacement terminal below becomes its sole boundary.
                    events.pop()
                if msg:
                    next_turn_id = user_record.turn_id or pending_turn_id
                    if turn_open:
                        # Codex accepts another user message while the same
                        # app-server task is still running.  That is steering,
                        # not evidence that the preceding visible segment
                        # crashed.  We still need a synthetic boundary because
                        # the Web projection stores one user prompt per turn,
                        # but it must be a neutral non-error boundary.
                        steered_same_task = task_has_user and turn_has_user
                        active_mid_task_steer = bool(
                            snapshot_in_progress
                            and turn_open
                            and not turn_has_user
                            and isinstance(next_turn_id, str)
                            and next_turn_id in known_active_task_ids
                        )
                        # No terminal record proved where the previous visible
                        # reply ended. In particular, pending_turn_id now often
                        # belongs to this NEW user turn; never attach it to the
                        # synthetic boundary. Visible output is not completion
                        # evidence: only an assistant-only continuation carrying
                        # an authoritative compact/page reason may close cleanly.
                        proven_continuation = bool(
                            not turn_has_user
                            and turn_visible
                            and turn_continuation_reason in {
                                "context_compacted",
                                "authoritative_page",
                            }
                        )
                        if steered_same_task or active_mid_task_steer:
                            close_turn(
                                "steered", 0, False,
                                authoritative_boundary=False)
                        else:
                            close_turn(
                                "success" if proven_continuation else "error",
                                0, not proven_continuation,
                                authoritative_boundary=False)
                    active_turn_id = str(next_turn_id) if next_turn_id else None
                    pending_turn_id = active_turn_id
                    source_message_id = (
                        user_record.message_id
                        if isinstance(user_record.message_id, str)
                        and _SAFE_WIRE_ID.fullmatch(user_record.message_id)
                        else paired_legacy_user_item_id
                    )
                    if source_message_id is not None:
                        # 0.147 persists the same authoritative item id returned
                        # by official History and the live app-server stream;
                        # legacy double records now retain that identity too.
                        uid = source_message_id
                    elif task_has_user:
                        # A steered message inside the same app-server task has
                        # no fresh turn id. Reusing active_turn_id would make the
                        # reducer drop it as a duplicate of the first user row.
                        uid = _fallback_history_id(
                            path,
                            "user",
                            line_no,
                            raw_ts,
                            type(p.get("turn_id")).__name__,
                        )
                    else:
                        uid = _history_id(
                            active_turn_id, "user", line_no, raw_ts)
                    if user_client_id is None:
                        user_client_id = native_client_aliases.get(uid)
                    if (
                        user_client_id is None
                        and isinstance(active_turn_id, str)
                        and task_segment_index is not None
                    ):
                        user_client_id = segment_client_aliases.get((
                            active_turn_id,
                            task_segment_index,
                        ))
                    active_msg_id = uid
                    um = UserMsg(
                        msg_id=uid,
                        client_msg_id=user_client_id,
                        prompt=msg,
                    )
                    if pending_images:
                        um.images = pending_images
                    if ts is not None:
                        um.ts = ts
                    events.append(um)
                    turn_open = True
                    turn_has_user = True
                    turn_continuation_reason = None
                    source_continuation_available = False
                    flush_pending_compactions(active_turn_id)
                    task_has_user = True
                    if task_segment_index is not None:
                        task_segment_index += 1
                pending_images = []   # consume (per user turn)
            elif (t == "response_item"
                  and payload_type in {"function_call", "custom_tool_call"}):
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id") or p.get("id"),
                    "tool", line_no, raw_ts)
                arguments = (p.get("arguments") if payload_type == "function_call"
                             else p.get("input"))
                hist_input = _hist_tool_input(arguments, p.get("name"))
                plan_event = _history_plan_event(
                    p.get("name"), hist_input,
                    _history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                    _history_id, tool_id, line_no, raw_ts,
                )
                if plan_event is not None:
                    plan_tool_ids.add(tool_id)
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(plan_event)
                else:
                    ensure_assistant(line_no, raw_ts)
                    tool, category, title, server = _hist_tool_presentation(
                        p.get("name"), hist_input)
                    history_tools[tool_id] = (
                        tool, category, title, server, hist_input)
                    if tool_id not in seen_tool_uses:
                        seen_tool_uses.add(tool_id)
                        turn_visible = True
                        events.append(ToolUse(
                            message_id=cur_mid or "",
                            tool_use_id=tool_id,
                            tool=tool,
                            input=hist_input,
                            category=category,
                            title=title,
                            server=server,
                        ))
            elif (t == "response_item"
                  and payload_type in {
                      "function_call_output", "custom_tool_call_output"}):
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                tool_meta = history_tools.get(
                    tool_id, ("tool", "tool", None, None, {}))
                if tool_id in plan_tool_ids:
                    seen_tool_results.add(tool_id)
                else:
                    if tool_id not in seen_tool_uses:
                        ensure_assistant(line_no, raw_ts)
                        tool, category, title, server, hist_input = tool_meta
                        seen_tool_uses.add(tool_id)
                        events.append(ToolUse(
                            message_id=cur_mid or "", tool_use_id=tool_id,
                            tool=tool, input=hist_input, category=category,
                            title=title, server=server))
                    if tool_id not in seen_tool_results:
                        seen_tool_results.add(tool_id)
                        turn_visible = True
                        category = tool_meta[1]
                        raw_output = p.get("output")
                        structured_error = False
                        if _normalized_history_tool(
                                tool_meta[0]) == "viewimage":
                            has_image = bool(
                                isinstance(raw_output, list)
                                and any(
                                    isinstance(item, dict)
                                    and item.get("type") == "input_image"
                                    for item in raw_output
                                )
                            )
                            raw_output = (
                                "图片已读取"
                                if has_image
                                else "图片读取未返回可预览内容"
                            )
                        if category in {"mcp", "server_tool"}:
                            raw_output, structured_error = (
                                _history_structured_tool_output(raw_output))
                        output, was_truncated = bounded_text(
                            raw_output, tool_result_max)
                        exit_code = _history_exit_code(output)
                        is_error = structured_error or _exit_is_error(output)
                        events.append(ToolResult(
                            tool_use_id=tool_id,
                            content=output,
                            is_error=is_error,
                            truncated=True if was_truncated else None,
                            status="failed" if is_error else "succeeded",
                            exit_code=exit_code,
                        ))
            elif t == "event_msg" and payload_type == "exec_command_end":
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                if tool_id not in seen_authoritative_results:
                    seen_authoritative_results.add(tool_id)
                    command = _legacy_command_text(p.get("command"))
                    command_input = bounded_tool_input({
                        "command": command,
                        "cwd": p.get("cwd"),
                        "actions": p.get("parsed_cmd"),
                        "source": p.get("source"),
                        "process_id": p.get("process_id"),
                    }, 64 * 1024)
                    title = _legacy_command_title(p.get("parsed_cmd"))
                    upsert_tool_use(
                        tool_id, "shell", "command", title, None,
                        command_input, line_no, raw_ts)
                    output, truncated = bounded_text(
                        p.get("aggregated_output")
                        or p.get("formatted_output")
                        or p.get("stdout")
                        or p.get("stderr")
                        or "",
                        tool_result_max,
                    )
                    exit_code = _nonnegative_or_signed_int(p.get("exit_code"))
                    status = _process_status(p.get("status"))
                    if exit_code is not None and exit_code != 0:
                        status = "failed"
                    elif status in {"unknown", "running", "pending"}:
                        status = "succeeded"
                    upsert_tool_result(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=(status in {
                            "failed", "declined", "cancelled", "interrupted"
                        }),
                        truncated=True if truncated else None,
                        status=status,
                        exit_code=exit_code,
                        duration_ms=_legacy_duration_ms(p.get("duration")),
                    ))
            elif t == "event_msg" and payload_type == "mcp_tool_call_end":
                open_assistant_only_turn()
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                if tool_id not in seen_authoritative_results:
                    seen_authoritative_results.add(tool_id)
                    invocation = (p.get("invocation")
                                  if isinstance(p.get("invocation"), dict)
                                  else {})
                    server = str(invocation.get("server") or "MCP")[:1024]
                    tool = str(invocation.get("tool") or "mcp")[:1024]
                    arguments = invocation.get("arguments")
                    tool_input = bounded_tool_input(
                        _redact_credentials(
                            arguments if isinstance(arguments, dict)
                            else {"arguments": arguments}),
                        64 * 1024,
                    )
                    upsert_tool_use(
                        tool_id, tool, "mcp", f"{server} · {tool}"[:1024],
                        server, tool_input, line_no, raw_ts)
                    content, is_error = _legacy_mcp_result(p.get("result"))
                    output, truncated = bounded_text(content, tool_result_max)
                    upsert_tool_result(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=is_error,
                        truncated=True if truncated else None,
                        status="failed" if is_error else "succeeded",
                        duration_ms=_legacy_duration_ms(p.get("duration")),
                    ))
            elif (t == "event_msg"
                  and (generated_item := _rollout_generated_image_item(p)) is not None):
                open_assistant_only_turn()
                native_turn = active_turn_id or pending_turn_id
                if (p.get("turn_id") is not None
                        and p.get("turn_id") != native_turn):
                    continue
                item_id = _history_id(generated_item.get("id"), "image", line_no, raw_ts)
                if item_id not in seen_process_items:
                    seen_process_items.add(item_id)
                    image_event = CodexStreamTranslator(tool_result_max)._process_item(
                        {**generated_item, "id": item_id},
                        {"turnId": _history_optional_turn_id(native_turn)},
                        completed=True,
                    )
                    if image_event is not None:
                        image_event.ts = ts if ts is not None else 0
                        events.append(image_event)
                        turn_visible = True
            elif t == "event_msg" and payload_type == "item_completed":
                item = p.get("item") if isinstance(p.get("item"), dict) else {}
                if str(item.get("type") or "").lower() == "plan":
                    open_assistant_only_turn()
                    item_id = _history_id(
                        item.get("id"), "plan-detail", line_no, raw_ts)
                    detail, truncated = bounded_text(
                        item.get("text"), 256 * 1024)
                    completed_plan = (
                        (item_id, detail) if detail else None
                    )
                    if item_id not in seen_process_items:
                        seen_process_items.add(item_id)
                        events.append(ProcessEvent(
                            item_id=item_id,
                            kind="plan",
                            phase="end",
                            status="succeeded",
                            turn_id=_history_optional_turn_id(
                                p.get("turn_id") or active_turn_id
                                or pending_turn_id),
                            title="计划",
                            detail=detail or None,
                            truncated=True if truncated else None,
                        ))
                        turn_visible = True
            elif t == "response_item" and payload_type == "reasoning":
                summary = _reasoning_summary(p)
                key = (str(active_turn_id or pending_turn_id or ""), summary)
                if summary and key not in seen_reasoning:
                    seen_reasoning.add(key)
                    open_assistant_only_turn()
                    events.append(ProcessEvent(
                        item_id=_history_id(
                            p.get("id"), "reasoning", line_no, raw_ts),
                        kind="reasoning",
                        phase="end",
                        status="succeeded",
                        turn_id=_history_optional_turn_id(
                            active_turn_id or pending_turn_id),
                        title="思考",
                        summary=summary,
                    ))
            elif t == "event_msg" and payload_type == "agent_reasoning":
                summary, _ = bounded_text(p.get("text"), 64 * 1024)
                key = (str(active_turn_id or pending_turn_id or ""), summary)
                if summary and key not in seen_reasoning:
                    seen_reasoning.add(key)
                    open_assistant_only_turn()
                    events.append(ProcessEvent(
                        item_id=_history_id(
                            p.get("id") or p.get("event_id"),
                            "reasoning", line_no, raw_ts),
                        kind="reasoning",
                        phase="end",
                        status="succeeded",
                        turn_id=_history_optional_turn_id(
                            active_turn_id or pending_turn_id),
                        title="思考",
                        summary=summary,
                    ))
            elif t == "event_msg" and payload_type == "agent_message":
                pending_agent_message = (p, line_no, raw_ts)
            elif t == "event_msg" and payload_type == "patch_apply_end":
                open_assistant_only_turn()
                ensure_assistant(line_no, raw_ts)
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                descriptors = _change_descriptors(p.get("changes"))
                paths = _descriptor_paths(descriptors)
                if tool_id not in seen_tool_uses:
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(ToolUse(
                        message_id=cur_mid or "",
                        tool_use_id=tool_id,
                        tool="apply_patch",
                        input=bounded_tool_input({
                            "changes": descriptors,
                            "file_paths": paths,
                        }, 64 * 1024),
                        category="file",
                        title=_file_summary(paths, "running"),
                    ))
                if tool_id not in seen_tool_results:
                    seen_tool_results.add(tool_id)
                    turn_visible = True
                    success = p.get("success") is not False
                    diff, diff_truncated = bounded_text(
                        _changes_diff(p.get("changes")), 2 * 1024 * 1024)
                    output, output_truncated = bounded_text(
                        p.get("stdout") or p.get("stderr") or "",
                        tool_result_max)
                    events.append(ToolResult(
                        tool_use_id=tool_id,
                        content=output,
                        is_error=not success,
                        truncated=(True if diff_truncated or output_truncated
                                   else None),
                        status="succeeded" if success else "failed",
                        summary=_file_summary(
                            paths, "succeeded" if success else "failed"),
                        diff=diff or None,
                    ))
            elif t == "event_msg" and payload_type == "web_search_end":
                open_assistant_only_turn()
                ensure_assistant(line_no, raw_ts)
                tool_id = _history_id(
                    p.get("call_id"), "tool", line_no, raw_ts)
                query, _ = bounded_text(p.get("query"), 16 * 1024)
                if tool_id not in seen_tool_uses:
                    seen_tool_uses.add(tool_id)
                    turn_visible = True
                    events.append(ToolUse(
                        message_id=cur_mid or "",
                        tool_use_id=tool_id,
                        tool="webSearch",
                        input=bounded_tool_input({
                            "query": query, "action": p.get("action"),
                        }, 64 * 1024),
                        category="web_search",
                        title=(f"搜索 {query}" if query else "搜索网页")[:1024],
                    ))
                if tool_id not in seen_tool_results:
                    seen_tool_results.add(tool_id)
                    events.append(ToolResult(
                        tool_use_id=tool_id,
                        content="",
                        is_error=False,
                        status="succeeded",
                    ))
            elif t == "event_msg" and payload_type == "sub_agent_activity":
                open_assistant_only_turn()
                item = {
                    "id": p.get("event_id"),
                    "kind": p.get("kind"),
                    "agentThreadId": p.get("agent_thread_id"),
                    "agentPath": p.get("agent_path"),
                }
                events.append(_subagent_event(
                    item,
                    _history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                    completed=True,
                ))
                turn_visible = True
            elif t == "event_msg" and payload_type == "context_compacted":
                marker = (
                    _history_id(
                        p.get("id"), "compaction", line_no, raw_ts),
                    ts,
                    _history_optional_turn_id(
                        active_turn_id or pending_turn_id),
                )
                if turn_open:
                    open_assistant_only_turn("context_compacted")
                    append_compaction(marker)
                else:
                    pending_compactions.append(marker)
                    if (len(pending_compactions)
                            > _MAX_PENDING_HISTORY_COMPACTIONS):
                        del pending_compactions[0]
            elif t == "event_msg" and payload_type == "task_complete":
                materialize_pending_terminal(p.get("turn_id"))
                last = p.get("last_agent_message")
                if (not turn_open and isinstance(last, str) and last):
                    open_assistant_only_turn()
                if turn_open:
                    turn_key = str(active_turn_id or pending_turn_id or "")
                    last_already_visible = any(
                        key[0] == turn_key and key[2] == last
                        for key in seen_agent_messages)
                    if (not turn_final_visible and isinstance(last, str) and last
                            and not last_already_visible):
                        close_assistant()
                        ensure_assistant(
                            line_no, raw_ts, channel="final", force_new=True)
                        events.append(Delta(
                            message_id=cur_mid, text=last, channel="final"))
                        close_assistant()
                        seen_agent_messages.add((turn_key, "final", last))
                        turn_visible = True
                        turn_text_visible = True
                        turn_final_visible = True
                    terminal_error = p.get("error")
                    if terminal_error is None:
                        emit_completed_plan_answer(line_no, raw_ts)
                    if terminal_error is not None:
                        events.append(Error(
                            code=ERR_CC_CRASH,
                            message=_provider_failure_message(terminal_error),
                            msg_id=active_msg_id,
                        ))
                        close_turn("error", _duration(p), True,
                                   _completed_ts(p, ts), p.get("turn_id"))
                    elif turn_visible:
                        close_turn("success", _duration(p), False,
                                   _completed_ts(p, ts), p.get("turn_id"))
                    else:
                        events.append(Error(
                            code=ERR_CC_CRASH,
                            message=_EMPTY_COMPLETED_MESSAGE,
                            msg_id=active_msg_id,
                        ))
                        close_turn("error", _duration(p), True,
                                   _completed_ts(p, ts), p.get("turn_id"))
                task_segment_index = None
            elif t == "event_msg" and payload_type == "turn_aborted":
                materialize_pending_terminal(p.get("turn_id"))
                if turn_open:
                    # Current Codex rollouts can omit ``reason`` for an
                    # intentional interrupt. Explicit failure reasons remain
                    # errors; a bare turn_aborted is an interruption.
                    reason = str(p.get("reason") or "").lower()
                    interrupted = reason not in {"error", "failed", "crash"}
                    close_turn(
                        "error_during_execution" if interrupted else "error",
                        _duration(p), True, _completed_ts(p, ts),
                        p.get("turn_id"))
                task_segment_index = None
            elif t == "event_msg" and payload_type in {
                    "task_failed", "turn_failed", "task_error"}:
                materialize_pending_terminal(p.get("turn_id"))
                if turn_open:
                    close_turn("error", _duration(p), True,
                               _completed_ts(p, ts), p.get("turn_id"))
                task_segment_index = None
            # session_meta / world_state / token_count / private reasoning : skipped
            if ts is not None:
                last_ts = ts
    if pending_agent_message is not None and not snapshot_in_progress:
        payload, pending_line, pending_ts = pending_agent_message
        emit_agent_message(payload, pending_line, pending_ts)
    # A file can be read while Codex is still appending the current turn. Close
    # only its current text block; deliberately omit TurnEnd so the reducer keeps
    # the turn not-done instead of fabricating a completed status.
    close_assistant()
    return events, model


def _hist_tool_name(name) -> str:
    if name in ("exec", "exec_command", "shell", "local_shell"):
        return "shell"
    if name in ("apply_patch",):
        return "apply_patch"
    return name or "tool"


def _normalized_history_tool(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _history_plan_event(
    name,
    tool_input: dict,
    turn_id: str | None,
    id_builder,
    tool_id: str,
    line_no: int,
    raw_ts: str,
):
    normalized = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if not normalized.endswith("updateplan"):
        return None
    raw_plan = tool_input.get("plan")
    if not isinstance(raw_plan, list):
        return None
    plan = []
    for entry in raw_plan[:128]:
        if not isinstance(entry, dict):
            continue
        step, _ = bounded_text(entry.get("step"), 16 * 1024)
        if not step:
            continue
        plan.append({
            "step": step,
            "status": _plan_status(entry.get("status")),
        })
    explanation, _ = bounded_text(tool_input.get("explanation"), 64 * 1024)
    identity = f"plan:{turn_id or tool_id}"
    return TurnPlan(
        item_id=id_builder(identity, "plan", line_no, raw_ts),
        turn_id=turn_id,
        explanation=explanation or None,
        plan=plan,
    )


def _hist_tool_presentation(
    name, tool_input: dict,
) -> tuple[str, str, str | None, str | None]:
    raw_name = str(name or "tool")
    tool = _hist_tool_name(raw_name)
    if tool == "shell":
        return tool, "command", "运行命令", None
    if tool == "apply_patch":
        return tool, "file", "修改文件", None
    if raw_name in {"web_search", "webSearch", "search_web"}:
        query = tool_input.get("query")
        title = f"搜索 {query}" if query else "搜索网页"
        return "webSearch", "web_search", title[:1024], None
    if raw_name in {
        "spawn_agent", "spawnAgent", "send_input", "sendInput",
        "resume_agent", "resumeAgent", "wait_agent", "wait",
        "close_agent", "closeAgent",
    }:
        return raw_name[:1024], "agent", "协作代理", None
    if raw_name.startswith("mcp__"):
        parts = raw_name.split("__", 2)
        server = parts[1] if len(parts) > 1 and parts[1] else "MCP"
        mcp_tool = parts[2] if len(parts) > 2 and parts[2] else raw_name
        return mcp_tool[:1024], "mcp", f"{server} · {mcp_tool}"[:1024], server[:1024]
    return tool[:1024], "tool", raw_name[:1024], None


def _hist_tool_input(arguments, name=None) -> dict:
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except Exception:
        if isinstance(arguments, str):
            mapped = _hist_tool_name(name)
            key = "command" if mapped == "shell" else (
                "patch" if mapped == "apply_patch" else "input")
            return bounded_tool_input({key: arguments}, 64 * 1024)
        a = {}
    if not isinstance(a, dict):
        return bounded_tool_input(
            {"args": _redact_credentials(a)}, 64 * 1024)
    out: dict = {}
    if a.get("cmd") is not None:
        out["command"] = a["cmd"]
    if a.get("workdir") is not None:
        out["cwd"] = a["workdir"]
    for k, v in a.items():
        if k not in ("cmd", "workdir", "yield_time_ms"):
            out[k] = v
    return bounded_tool_input(_redact_credentials(out), 64 * 1024)


def _history_structured_tool_output(output) -> tuple[object, bool]:
    """Allow-list replayable MCP/dynamic result fields from rollout output."""
    parsed = output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            # Opaque strings can embed serialized `_meta` or credentials without
            # field boundaries, so never replay them as trusted MCP history.
            return "MCP 工具调用已完成（历史结果格式不可解析）", False
    if isinstance(parsed, list):
        return {"content": _redact_credentials(parsed)}, False
    if not isinstance(parsed, dict):
        return "MCP 工具调用已完成", False

    candidate = parsed.get("result")
    if not isinstance(candidate, dict):
        candidate = parsed
    error = parsed.get("error")
    if error is None and candidate is not parsed:
        error = candidate.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message")
        else:
            message = str(error)
        safe_error, _ = bounded_text(message or "MCP tool call failed", 64 * 1024)
        return safe_error, True

    safe = {}
    aliases = (
        ("content", "content"),
        ("structuredContent", "structuredContent"),
        ("structured_content", "structuredContent"),
        ("contentItems", "content"),
    )
    for source, target in aliases:
        if source in candidate and target not in safe:
            safe[target] = _redact_credentials(candidate[source])
    failed = parsed.get("success") is False or str(
        parsed.get("status") or "").lower() in {"failed", "error"}
    return (safe or "MCP 工具调用已完成"), failed


def _legacy_command_text(command) -> str:
    """Normalize persisted ``exec_command_end.command`` into display text."""
    if isinstance(command, str):
        text, _ = bounded_text(command, 256 * 1024)
        return text
    if isinstance(command, (list, tuple)):
        argv = [str(part) for part in list(command)[:256]]
        try:
            text = shlex.join(argv)
        except (TypeError, ValueError):
            text = " ".join(argv)
        text, _ = bounded_text(text, 256 * 1024)
        return text
    text, _ = bounded_text(command, 256 * 1024)
    return text


def _legacy_command_title(parsed_command) -> str:
    """Give old rollout command records the same semantic title as live items."""
    actions = parsed_command if isinstance(parsed_command, list) else []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    action_type = re.sub(
        r"[^a-z0-9]", "", str(first.get("type") or "").lower())
    if action_type == "read":
        path = first.get("path") or first.get("name")
        return (f"读取 {path}" if path else "读取文件")[:1024]
    if action_type in {"list", "listfiles"}:
        path = first.get("path")
        return (f"列出 {path}" if path else "列出文件")[:1024]
    if action_type in {"search", "grep"}:
        query = first.get("query") or first.get("pattern")
        return (f"搜索 {query}" if query else "搜索内容")[:1024]
    return "运行命令"


def _legacy_duration_ms(duration) -> int | None:
    """Convert persisted protobuf-style ``{secs, nanos}`` durations."""
    if not isinstance(duration, dict):
        return _duration_ms(duration)
    secs = duration.get("secs")
    nanos = duration.get("nanos")
    if isinstance(secs, bool) or isinstance(nanos, bool):
        return None
    try:
        milliseconds = int(secs or 0) * 1000 + int(nanos or 0) // 1_000_000
    except (TypeError, ValueError, OverflowError):
        return None
    return milliseconds if milliseconds >= 0 else None


def _legacy_mcp_result(result) -> tuple[object, bool]:
    """Decode persisted Rust ``Result`` while excluding server-private metadata."""
    if not isinstance(result, dict):
        return "MCP 工具调用已完成", False
    if "Err" in result:
        # Err is an opaque provider string and may itself contain connector
        # credentials. Preserve failure semantics without replaying it verbatim.
        return "MCP 工具调用失败", True
    value = result.get("Ok")
    if not isinstance(value, dict):
        return "MCP 工具调用已完成", False
    safe = _redact_credentials({
        key: value.get(key) for key in ("content", "structuredContent")
        if value.get(key) is not None
    })
    return safe or "MCP 工具调用已完成", bool(value.get("isError"))


def _exit_is_error(output: str) -> bool:
    code = _history_exit_code(output)
    return code is not None and code != 0


def _history_exit_code(output: str) -> int | None:
    match = re.search(
        r"\b(?:process\s+)?(?:exited|exit)\s+(?:with\s+)?code\s*[:=]?\s*(-?\d+)",
        output or "",
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _history_optional_turn_id(value) -> str | None:
    return _optional_wire_id(value, "turn")


def _data_uri_to_img(url) -> dict | None:
    """`data:image/png;base64,XXXX` -> {media_type, data} (the web's QueryImg shape)."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        head, data = url.split(",", 1)
        mt = head[5:].split(";")[0] or "image/png"
        return {"media_type": mt, "data": data}
    except Exception:
        return None


def _record_containing_offset(
    source,
    *,
    file_size: int,
    offset: int,
    max_record_bytes: int = _MAX_HISTORY_BOUNDARY_RECORD_BYTES,
) -> tuple[int, bytes] | None:
    """Read one bounded JSONL record around a byte match."""
    prefix_start = max(0, offset - max_record_bytes)
    source.seek(prefix_start)
    prefix = source.read(offset - prefix_start)
    newline = prefix.rfind(b"\n")
    record_start = (
        prefix_start + newline + 1
        if newline >= 0
        else 0 if prefix_start == 0
        else None
    )
    if record_start is None:
        return None
    source.seek(record_start)
    line = source.readline(max_record_bytes + 1)
    if (
        len(line) > max_record_bytes
        or (
            not line.endswith(b"\n")
            and record_start + len(line) < file_size
        )
    ):
        return None
    return record_start, line.rstrip(b"\r\n")


def _codex_history_record_window(
    path: str,
    identity: str,
    accept: Callable[[bytes], bool],
    *,
    max_record_bytes: int = _MAX_HISTORY_BOUNDARY_RECORD_BYTES,
) -> tuple[int, int] | None:
    """Locate one structurally verified record using bounded byte searches."""
    if not isinstance(identity, str) or not _SAFE_WIRE_ID.fullmatch(identity):
        return None
    try:
        needle = identity.encode("ascii")
        with open(path, "rb") as source:
            file_size = os.fstat(source.fileno()).st_size
            left = 0
            right = file_size
            checked_records: set[int] = set()

            def inspect(data: bytes, absolute_start: int, *, reverse: bool):
                cursor = len(data) if reverse else 0
                while True:
                    match = (
                        data.rfind(needle, 0, cursor)
                        if reverse
                        else data.find(needle, cursor)
                    )
                    if match < 0:
                        return None
                    absolute_match = absolute_start + match
                    record = _record_containing_offset(
                        source,
                        file_size=file_size,
                        offset=absolute_match,
                        max_record_bytes=max_record_bytes,
                    )
                    if record is not None:
                        record_start, line = record
                        if record_start not in checked_records:
                            checked_records.add(record_start)
                            if len(checked_records) > _MAX_HISTORY_TURN_MATCHES:
                                return None
                            if accept(line):
                                return record_start, file_size
                    cursor = match if reverse else match + len(needle)

            overlap = max(0, len(needle) - 1)
            while left < right:
                forward_end = min(
                    right, left + _HISTORY_TURN_SEARCH_CHUNK_BYTES)
                source.seek(left)
                forward = source.read(
                    min(file_size, forward_end + overlap) - left)
                found = inspect(forward, left, reverse=False)
                if found is not None:
                    return found
                left = forward_end
                if left >= right:
                    break

                reverse_start = max(
                    left, right - _HISTORY_TURN_SEARCH_CHUNK_BYTES)
                source.seek(reverse_start)
                reverse = source.read(
                    min(file_size, right + overlap) - reverse_start)
                found = inspect(reverse, reverse_start, reverse=True)
                if found is not None:
                    return found
                right = reverse_start
    except (OSError, UnicodeEncodeError):
        return None
    return None


def _codex_native_turn_window(
    path: str,
    native_turn_id: str,
) -> tuple[int, int] | None:
    return _codex_history_record_window(
        path, native_turn_id,
        lambda line: _history_turn_cursor(line) == native_turn_id,
    )


def codex_history_user_images(path: str, message_id: str) -> list[dict]:
    """Recover inline uploads by exact native user-item id, never a nearby turn.

    Browser history can outlive both the official reader's locator LRU and the
    rebuildable detail index. The original response item is still authoritative;
    no app-server resume, full-history translation or localImage path is needed.
    """
    images: list[dict] = []

    def accept(line: bytes) -> bool:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(row, dict) or row.get("type") != "response_item":
            return False
        payload = row.get("payload")
        if _legacy_response_user_item_id(payload) != message_id:
            return False
        content = payload.get("content")
        if not isinstance(content, list):
            return False
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_image":
                image = _data_uri_to_img(item.get("image_url"))
                if image is not None:
                    images.append(image)
        return True

    _codex_history_record_window(
        path, message_id, accept, max_record_bytes=_MAX_HISTORY_RECORD_CHARS,
    )
    return images


def _history_image_payload(
    url: object,
) -> tuple[str, int, int, bytes] | None:
    image = _data_uri_to_img(url)
    if image is None:
        return None
    media_type = image.get("media_type")
    encoded = image.get("data")
    if (
        not isinstance(media_type, str)
        or media_type not in ALLOWED_IMAGE_TYPES
        or not isinstance(encoded, str)
    ):
        return None
    try:
        data = decode_attachment(encoded)
    except ValueError:
        return None
    if len(data) > MAX_SINGLE_ATTACHMENT_BYTES:
        return None
    dimensions = image_dimensions(data, media_type)
    if dimensions is None:
        return None
    width, height = dimensions
    if (
        width <= 0
        or height <= 0
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        return None
    normalized = "image/jpeg" if media_type == "image/jpg" else media_type
    return normalized, width, height, data


def _rollout_generated_image_item(payload: dict) -> dict | None:
    """Normalize the two native persisted image-generation envelopes only."""
    if payload.get("type") == "image_generation_end":
        return {
            "type": "imageGeneration", "id": payload.get("call_id"),
            "status": payload.get("status"), "result": payload.get("result"),
            "savedPath": payload.get("saved_path"),
            "revisedPrompt": payload.get("revised_prompt"),
        }
    item = payload.get("item")
    if (payload.get("type") == "item_completed"
            and isinstance(item, dict)
            and item.get("type") == "Extension"
            and item.get("kind") == "image_gen.generation"):
        return {**item, "type": "imageGeneration"}
    return None


def _generated_image_payload(result: object) -> tuple[str, int, int, bytes] | None:
    # Native imageGeneration.result is PNG base64 (or an image data URL).
    # Reject oversize bodies before decoding/copying; use the same image limits
    # as the existing authenticated history-image route.
    if not isinstance(result, str) or len(result) > (
            (MAX_SINGLE_ATTACHMENT_BYTES + 2) // 3 * 4 + 128):
        return None
    return _history_image_payload(
        result if result.startswith("data:") else "data:image/png;base64," + result)


def codex_generated_image_ref(
    turn_id: str, image: tuple[str, int, int, bytes],
) -> dict[str, object]:
    media_type, width, height, data = image
    # Public item ids and raw rollout call ids need not use the same spelling.
    # Content identity is stable across both projections, scoped to this task.
    digest = hashlib.sha256(data).hexdigest()
    identity = f"imageGeneration\0{turn_id}\0{digest}"
    return {
        "image_id": "img-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        "media_type": media_type, "width": width, "height": height,
        "byte_size": len(data),
    }


def codex_history_image_views(
    path: str,
    native_turn_id: str,
    *,
    segment_index: int = 0,
) -> tuple[CodexHistoryImageView, ...]:
    """Recover image reads/generation from one exact native rollout segment.

    Official app-server 0.147 can return a successful full turn while omitting
    ``imageView`` items. This deliberately narrow supplement never translates
    ordinary messages/tools and never puts an ``input_image`` base64 body into
    a history event.
    """
    if (
        not isinstance(native_turn_id, str)
        or not _SAFE_WIRE_ID.fullmatch(native_turn_id)
        or isinstance(segment_index, bool)
        or not isinstance(segment_index, int)
        or segment_index < 0
    ):
        return ()
    window = _codex_native_turn_window(path, native_turn_id)
    if window is None:
        return ()
    start_offset, end_offset = window

    calls: list[dict[str, object]] = []
    by_call_id: dict[str, dict[str, object]] = {}
    current_segment = 0
    saw_visible_user = False
    image_bytes = 0
    last_anchor_id: str | None = None
    awaiting_next_anchor: list[dict[str, object]] = []
    source_complete = False
    try:
        source = open(path, "rb")
    except OSError:
        return ()
    with source:
        source.seek(start_offset)
        for _offset, line in _bounded_jsonl_records(
            source, end_offset=end_offset,
        ):
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            row_type = row.get("type")
            payload_type = payload.get("type")

            if row_type == "event_msg" and payload_type == "task_started":
                turn_id = payload.get("turn_id")
                if (
                    isinstance(turn_id, str)
                    and turn_id != native_turn_id
                ):
                    source_complete = True
                    break
                continue

            if (row_type == "event_msg" and payload_type in {
                    "task_complete", "turn_aborted"}
                    and payload.get("turn_id") == native_turn_id):
                source_complete = True

            if row_type == "event_msg":
                user = codex_rollout_user_message(payload)
            else:
                user = None
            if user is not None:
                if not user.prompt:
                    continue
                if saw_visible_user:
                    if current_segment == segment_index:
                        awaiting_next_anchor.clear()
                    last_anchor_id = None
                    current_segment += 1
                else:
                    saw_visible_user = True
                continue

            generated_item = (_rollout_generated_image_item(payload)
                              if row_type == "event_msg" else None)
            if generated_item is not None:
                if (current_segment != segment_index
                        or len(calls) >= _MAX_HISTORY_IMAGE_VIEWS_PER_SEGMENT):
                    continue
                if (payload.get("turn_id") is not None
                        and payload.get("turn_id") != native_turn_id):
                    continue
                raw_call_id = generated_item.get("id")
                if (not isinstance(raw_call_id, str)
                        or not _SAFE_WIRE_ID.fullmatch(raw_call_id)
                        or raw_call_id in by_call_id):
                    continue
                status = _process_status(generated_item.get("status"))
                if status != "succeeded":
                    continue
                image = _generated_image_payload(generated_item.get("result"))
                if image is None or image_bytes + len(image[3]) > (
                        _MAX_HISTORY_IMAGE_BYTES_PER_SEGMENT):
                    continue
                image_bytes += len(image[3])
                image_path, _ = bounded_text(generated_item.get("savedPath"), 16 * 1024)
                record = {
                    "call_id": raw_call_id, "item_id": raw_call_id,
                    "path": image_path, "timestamp": row.get("timestamp"),
                    "output_seen": True, "image": image, "generated": True,
                    "previous_item_id": last_anchor_id, "next_item_id": None,
                }
                calls.append(record)
                by_call_id[raw_call_id] = record
                awaiting_next_anchor.append(record)
                continue

            if (
                row_type == "response_item"
                and payload_type == "function_call"
                and payload.get("name") == "view_image"
            ):
                if (
                    current_segment != segment_index
                    or len(calls) >= _MAX_HISTORY_IMAGE_VIEWS_PER_SEGMENT
                ):
                    continue
                metadata = payload.get(
                    "internal_chat_message_metadata_passthrough")
                metadata_turn = (
                    metadata.get("turn_id")
                    if isinstance(metadata, dict)
                    else None
                )
                if (
                    isinstance(metadata_turn, str)
                    and metadata_turn != native_turn_id
                ):
                    continue
                call_id = _live_id(
                    payload.get("call_id") or payload.get("id"),
                    "history-image-call",
                )
                arguments = payload.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (json.JSONDecodeError, ValueError):
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                raw_path = arguments.get("path") or arguments.get("file_path")
                image_path, _ = bounded_text(raw_path, 16 * 1024)
                record: dict[str, object] = {
                    "call_id": call_id,
                    "item_id": _live_id(
                        payload.get("id") or call_id,
                        "history-image-view",
                    ),
                    "path": image_path,
                    "segment_index": current_segment,
                    "timestamp": row.get("timestamp"),
                    "output_seen": False,
                    "image": None,
                    "previous_item_id": last_anchor_id,
                    "next_item_id": None,
                }
                calls.append(record)
                by_call_id[call_id] = record
                awaiting_next_anchor.append(record)
                continue

            if row_type != "response_item":
                continue

            if payload_type == "function_call_output":
                call_id = _live_id(
                    payload.get("call_id"), "history-image-call")
                record = by_call_id.get(call_id)
                if record is not None:
                    record["output_seen"] = True
                    if record.get("image") is None:
                        output = payload.get("output")
                        if isinstance(output, list):
                            for item in output:
                                if (
                                    not isinstance(item, dict)
                                    or item.get("type") != "input_image"
                                ):
                                    continue
                                image = _history_image_payload(
                                    item.get("image_url"))
                                if (
                                    image is not None
                                    and image_bytes + len(image[3])
                                    <= _MAX_HISTORY_IMAGE_BYTES_PER_SEGMENT
                                ):
                                    record["image"] = image
                                    image_bytes += len(image[3])
                                    break
                    continue

            if current_segment != segment_index:
                continue
            raw_anchor = payload.get("id") or payload.get("call_id")
            anchor_id = (
                raw_anchor
                if isinstance(raw_anchor, str)
                and _SAFE_WIRE_ID.fullmatch(raw_anchor)
                else None
            )
            if anchor_id is None:
                continue
            for pending in awaiting_next_anchor:
                pending["next_item_id"] = anchor_id
            awaiting_next_anchor.clear()
            last_anchor_id = anchor_id

    views: list[CodexHistoryImageView] = []
    for record in calls:
        call_id = str(record["call_id"])
        image = record.get("image")
        image_ref: dict[str, object] | None = None
        media_type: str | None = None
        width: int | None = None
        height: int | None = None
        data: bytes | None = None
        if isinstance(image, tuple) and len(image) == 4:
            media_type, width, height, data = image
            digest = hashlib.sha256(data).hexdigest()
            image_id = "img-" + hashlib.sha256(
                (
                    f"{native_turn_id}\0{call_id}\0{digest}"
                ).encode("utf-8", "surrogatepass")
            ).hexdigest()[:24]
            image_ref = {
                "image_id": image_id,
                "media_type": media_type,
                "width": width,
                "height": height,
                "byte_size": len(data),
            }
            if record.get("generated"):
                image_ref = codex_generated_image_ref(native_turn_id, image)
        image_path = str(record.get("path") or "")
        event_input: dict[str, object] = {}
        if image_path:
            event_input["file_path"] = image_path
        if image_ref is not None:
            event_input["history_image"] = image_ref
        succeeded = bool(record.get("output_seen"))
        event = ProcessEvent(
            item_id=str(record["item_id"]),
            kind="server_tool",
            phase="end",
            status="succeeded" if succeeded else "interrupted",
            turn_id=native_turn_id,
            title="生成图片" if record.get("generated") else "查看图片",
            summary=image_path or None,
            input=event_input or None,
            tool="image_generation" if record.get("generated") else "view_image",
        )
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            try:
                event.ts = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                pass
        views.append(CodexHistoryImageView(
            call_id=call_id,
            event=event,
            previous_item_id=(
                str(record["previous_item_id"])
                if record.get("previous_item_id")
                else None
            ),
            next_item_id=(
                str(record["next_item_id"])
                if record.get("next_item_id")
                else None
            ),
            media_type=media_type,
            width=width,
            height=height,
            data=data,
            source_complete=source_complete,
        ))
    return tuple(views)
