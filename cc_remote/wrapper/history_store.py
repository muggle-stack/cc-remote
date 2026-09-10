"""Persistent, rebuildable materialized history pages.

The engine transcript/rollout remains the source of truth.  This store keeps
only derived conversation pages so opening a session does not repeatedly scan
and translate the same JSONL bytes.  Every row is bound to a strong-enough
source fingerprint and can therefore be discarded without affecting engine
state or recovery.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cc_remote.attachments import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_SINGLE_ATTACHMENT_BYTES,
    decode_attachment,
    image_dimensions,
)
from cc_remote.protocol import ConversationTurn


# v17 also discards Codex pages whose legacy rollout user rows were materialized
# without the adjacent native app-server item id used by the live stream. v18
# rebuilds page projections once: older summary pages could be truncated against
# source-complete event size before lightweight turns were materialized. v19
# rebuilds summaries with exact/unknown process-detail metadata; raw details,
# image assets, and compact ancestry remain source-valid. v20 adds the Claude
# Agent detail cache. v21 discards Codex pages/details that may contain parser-
# time timestamps created when official turns omitted startedAt/completedAt.
# v22 rebuilds Claude pages/details whose browser-message alias may have been
# attached to a synthetic interrupt marker instead of the replacement prompt.
# v23 rebuilds Codex projections whose bounded single-turn tail treated a late
# compaction marker as the first public-process timestamp.
# v24 rebuilds Codex pages after source-bound live process clocks become an
# independent input which can change without modifying rollout bytes. v25
# rebuilds Codex pages whose leading compact marker was projected as a separate
# prompt-less turn before the owning user item reached the full snapshot. v26
# rebuilds Claude pages/details whose terminal clock could be extended by a
# cold-resume task notification appended after the final answer. v27 rebuilds
# Claude narrative projections so image-producing Read results use the lazy
# view-image projection instead of cached textual tool output. v28 rebuilds
# Codex narrative projections that discarded native async question metadata.
# v29 rebuilds Codex summary pages where an async question hid a normal answer
# without phase metadata. Source-complete details and binary assets remain valid.
# v30 rebuilds Codex narrative rows with bounded generated-image references.
# Binary assets and other engines' projections remain source-valid.
# v31 rebuilds Codex failures previously cached as successful task_complete.
# v32 restores reviewed policy-refusal copy previously reduced to a generic
# failure. Only Codex narrative projections need to be rebuilt.
_SCHEMA_VERSION = 32
_FINGERPRINT_SAMPLE_BYTES = 64 * 1024
_DEFAULT_MAX_ENTRIES = 128
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_SUMMARY_PROMPT_MAX_CHARS = 128 * 1024
_SUMMARY_TEXT_MAX_CHARS = 256 * 1024
_SUMMARY_LIVE_TEXT_MAX_CHARS = 64 * 1024
_SUMMARY_LIVE_MESSAGE_MAX_CHARS = 16 * 1024
_SUMMARY_LIVE_FIELD_MAX_CHARS = 4 * 1024
_SUMMARY_LIVE_BLOCK_MAX = 24
_SUMMARY_BLOCK_MAX = 32
_VOLATILE_EVENT_FIELDS = frozenset({"ts", "seq", "to", "route_id"})
_SUMMARY_PAGE_PAYLOAD_SQL = """
json_set(
    payload_json,
    '$.events',
    COALESCE(
        (
            SELECT json_group_array(json(event.value))
            FROM json_each(payload_json, '$.events') AS event
            WHERE json_extract(event.value, '$.type') IN ('model', 'effort')
        ),
        json('[]')
    )
)
""".strip()
_COMPACT_SOURCE_LIMIT = 16
_SAFE_COMPACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_DETAIL_SNAPSHOT_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_DETAIL_SNAPSHOT_TURN_HASH = re.compile(r"^[0-9a-f]{16}$")
_GENERIC_HISTORY_TURN_FAILURE = "该轮未正常结束"


def history_turn_snapshot_hash(turn_id: str) -> str:
    """Return the compact, non-reversible turn key used in detail cursors."""
    return hashlib.sha256(
        turn_id.encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]


def _summary_projection_fallback(
    error: sqlite3.OperationalError,
) -> str | None:
    """Classify only JSON projection failures that a raw read can recover."""
    message = str(error).casefold()
    if (
        ("no such function" in message or "no such table" in message)
        and any(
            name in message
            for name in ("json_set", "json_each", "json_group_array")
        )
    ):
        return "unsupported"
    if (
        "malformed json" in message
        or "json cannot hold blob" in message
    ):
        return "payload"
    return None
_PROVIDER_AUTH_TURN_FAILURE = (
    "模型服务认证已失效或当前账号无权限，"
    "请检查当前服务的凭据或账号权限后重试。"
)
_SAFE_HISTORY_TURN_FAILURES = frozenset({
    "网络异常，连接失败，请重新尝试。",
    "网络连接异常，请检查网络后重试。",
    _PROVIDER_AUTH_TURN_FAILURE,
    "请求过于频繁或当前额度受限，请稍后重试。",
    "请求超时，请重新尝试。",
    "Codex 上游服务暂时不可用，请稍后重试。",
    "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    "若属误判请向服务提供方反馈。",
})
_LEGACY_HISTORY_TURN_FAILURES = {
    "Codex 登录已失效或当前账号无权限，请重新登录后重试。":
        _PROVIDER_AUTH_TURN_FAILURE,
    "Codex 本次回复未完成，请重试。": _GENERIC_HISTORY_TURN_FAILURE,
    "Claude 本次回复未完成，请稍后重试。": _GENERIC_HISTORY_TURN_FAILURE,
    "本次回复未完成，请重试。": _GENERIC_HISTORY_TURN_FAILURE,
    "error": _GENERIC_HISTORY_TURN_FAILURE,
}


def _historical_turn_failure(value: Any) -> str:
    """Keep only reviewed product copy in a persisted history summary."""
    if not isinstance(value, str):
        return _GENERIC_HISTORY_TURN_FAILURE
    message = value.strip()
    if not message:
        return _GENERIC_HISTORY_TURN_FAILURE
    legacy = _LEGACY_HISTORY_TURN_FAILURES.get(message)
    if legacy is not None:
        return legacy
    if message in _SAFE_HISTORY_TURN_FAILURES:
        return message
    return _GENERIC_HISTORY_TURN_FAILURE


@dataclass(frozen=True)
class HistorySourceFingerprint:
    """Identity of the exact transcript snapshot used to build a page."""

    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    head_sha256: str
    tail_sha256: str
    sample_sha256: str

    @classmethod
    def capture(cls, path: str | os.PathLike[str]) -> "HistorySourceFingerprint":
        resolved = os.path.realpath(os.fspath(path))
        with open(resolved, "rb") as source:
            stat = os.fstat(source.fileno())
            head = source.read(_FINGERPRINT_SAMPLE_BYTES)
            tail = head
            digest = hashlib.sha256()
            digest.update(head)
            if stat.st_size > _FINGERPRINT_SAMPLE_BYTES:
                source.seek(max(0, stat.st_size - _FINGERPRINT_SAMPLE_BYTES))
                tail = source.read(_FINGERPRINT_SAMPLE_BYTES)
                digest.update(tail)
        return cls(
            path=resolved,
            device=int(stat.st_dev),
            inode=int(stat.st_ino),
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            head_sha256=hashlib.sha256(head).hexdigest(),
            tail_sha256=hashlib.sha256(tail).hexdigest(),
            sample_sha256=digest.hexdigest(),
        )

    @property
    def token(self) -> str:
        payload = "\0".join((
            self.path,
            str(self.device),
            str(self.inode),
            str(self.size),
            str(self.mtime_ns),
            self.head_sha256,
            self.tail_sha256,
            self.sample_sha256,
        ))
        return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class ClaudeCompactChainIndex:
    """Persistent payload-free metadata for one bounded transcript snapshot."""

    leaf: str
    rows: dict[str, tuple[object, ...]]
    queued_notifications: frozenset[tuple[int, str]]
    indexed_size: int


def _prefix_samples(source, size: int) -> tuple[str, str]:
    sample_size = min(max(0, int(size)), _FINGERPRINT_SAMPLE_BYTES)
    if sample_size == 0:
        empty = hashlib.sha256(b"").hexdigest()
        return empty, empty
    source.seek(0)
    head = source.read(sample_size)
    source.seek(max(0, size - sample_size))
    tail = source.read(sample_size)
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()


def history_source_extends(
    previous: HistorySourceFingerprint,
    current: HistorySourceFingerprint,
) -> bool:
    """Return whether ``current`` preserves the exact previous file prefix."""
    if previous.token == current.token:
        return True
    if (
        previous.path != current.path
        or previous.device != current.device
        or previous.inode != current.inode
        or current.size < previous.size
    ):
        return False
    sample_size = min(previous.size, _FINGERPRINT_SAMPLE_BYTES)
    try:
        with open(current.path, "rb") as source:
            stat = os.fstat(source.fileno())
            if (
                int(stat.st_dev) != current.device
                or int(stat.st_ino) != current.inode
                or int(stat.st_size) < current.size
            ):
                return False
            head = source.read(sample_size)
            source.seek(max(0, previous.size - sample_size))
            tail = source.read(sample_size)
    except OSError:
        return False
    return (
        hashlib.sha256(head).hexdigest() == previous.head_sha256
        and hashlib.sha256(tail).hexdigest() == previous.tail_sha256
    )


@dataclass(frozen=True)
class MaterializedHistoryPage:
    """Immutable narrative projection stored independently of live control."""

    events: tuple[dict[str, Any], ...]
    has_more: bool
    oldest_id: str | None
    newest_id: str | None
    turns: tuple[dict[str, Any], ...] = ()
    # Claude transcript EOF is only a synthetic terminal. Bind newest-page
    # projections to the resident lifecycle state so an exact source
    # fingerprint cached during a turn cannot close it (or remain open) after
    # ResultMessage changes state without adding another JSONL row.
    in_progress: bool | None = None
    # Codex can resume one persisted rollout task under a different control
    # user-item id without appending another source record first. Bind a newest-
    # page projection to the complete source-proven active task set.
    active_task_ids: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "events": list(self.events),
            "has_more": self.has_more,
            "oldest_id": self.oldest_id,
            "newest_id": self.newest_id,
            "turns": list(self.turns),
            "in_progress": self.in_progress,
            "active_task_ids": list(self.active_task_ids),
        }

    def semantic_token(self) -> str:
        """Hash narrative identity while ignoring transport-time metadata.

        A few legacy translators construct synthetic start/end envelopes after
        reading the transcript, so their default ``ts`` can vary across equal
        parses.  That timestamp is useful for the first materialization but is
        not evidence that the underlying conversation changed.
        """
        events = [
            {key: value for key, value in event.items()
             if key not in _VOLATILE_EVENT_FIELDS}
            for event in self.events
        ]
        payload = {
            "events": events,
            "has_more": self.has_more,
            "oldest_id": self.oldest_id,
            "newest_id": self.newest_id,
            "turns": list(self.turns),
            "in_progress": self.in_progress,
            "active_task_ids": list(self.active_task_ids),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def semantically_equals(self, other: "MaterializedHistoryPage") -> bool:
        return self.semantic_token() == other.semantic_token()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MaterializedHistoryPage":
        events = payload.get("events")
        if not isinstance(events, list) or not all(
                isinstance(event, dict) for event in events):
            raise ValueError("invalid materialized history events")
        turns = payload.get("turns")
        if turns is None:
            turns = materialize_history_turns(events)
        if not isinstance(turns, (list, tuple)) or not all(
                isinstance(turn, dict) for turn in turns):
            raise ValueError("invalid materialized history turns")
        raw_in_progress = payload.get("in_progress")
        if raw_in_progress is not None and not isinstance(raw_in_progress, bool):
            raise ValueError("invalid materialized history lifecycle")
        raw_active_task_ids = payload.get("active_task_ids", [])
        if not isinstance(raw_active_task_ids, (list, tuple)) or not all(
                isinstance(value, str) and value
                for value in raw_active_task_ids):
            raise ValueError("invalid materialized history active tasks")
        # The SQLite projection is rebuildable, but can outlive a wire-schema
        # change.  Validate its cached summary at this single boundary so an
        # obsolete field is never served to the client; callers invalidate the
        # whole session and rebuild from the engine source on validation error.
        normalized_turns: list[dict[str, Any]] = []
        for turn in turns:
            # Preserve omitted defaults in old cache rows: they remain omitted
            # on the wire today, while values that *are* present get Pydantic's
            # canonical JSON form.  Unknown keys are rejected by the model.
            normalized = ConversationTurn.model_validate(turn).model_dump(
                mode="json")
            normalized_turns.append({key: normalized[key] for key in turn})
        return cls(
            events=tuple(events),
            has_more=bool(payload.get("has_more")),
            oldest_id=(payload.get("oldest_id")
                       if isinstance(payload.get("oldest_id"), str) else None),
            newest_id=(payload.get("newest_id")
                       if isinstance(payload.get("newest_id"), str) else None),
            turns=tuple(normalized_turns),
            in_progress=raw_in_progress,
            active_task_ids=tuple(sorted(set(raw_active_task_ids))),
        )


@dataclass(frozen=True)
class MaterializedTurnDetail:
    """One source-bound heavyweight turn projection.

    ``source_token`` is populated only for a standalone immutable detail row.
    A group recovered from a compact page remains readable, but must not issue
    snapshot-bound cursors because its tighter detail row may already be gone.
    """

    events: tuple[dict[str, Any], ...]
    turn_id: str
    source_token: str | None = None


def _event_ms(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) * 1000)


def group_history_events(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> list[list[dict[str, Any]]]:
    """Split translated wire events into stable completed/in-flight turns."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("type")
        if event_type in {"model", "effort", "perm", "fast",
                          "collaboration_mode", "session_control"}:
            continue
        if event_type == "user_msg" and current:
            groups.append(current)
            current = []
        current.append(event)
        if event_type == "turn_end":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _turn_id(group: list[dict[str, Any]]) -> str | None:
    for event in group:
        if event.get("type") == "user_msg" and isinstance(event.get("msg_id"), str):
            return event["msg_id"]
    for event in reversed(group):
        if event.get("type") == "turn_end" and isinstance(event.get("turn_id"), str):
            return event["turn_id"]
    for field in ("message_id", "item_id", "tool_use_id"):
        for event in group:
            if isinstance(event.get(field), str):
                return event[field]
    return None


def _turn_detail_identity(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    field = (
        "tool_use_id"
        if event_type in {"tool_use", "tool_delta", "tool_result"}
        else "item_id"
        if event_type in {"process", "turn_plan", "turn_diff"}
        else "message_id"
        if event_type in {
            "assistant_msg_start", "delta", "assistant_msg_end",
        }
        else None
    )
    value = event.get(field) if field is not None else None
    return value if isinstance(value, str) else None


def history_image_id(turn_id: str, index: int) -> str:
    digest = hashlib.sha256(
        f"{turn_id}\0{index}".encode("utf-8", "surrogatepass")
    ).hexdigest()[:24]
    return f"img-{digest}"


def _history_image_refs(turn_id: str, raw_images: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if not isinstance(raw_images, list):
        return refs
    for index, image in enumerate(raw_images):
        if not isinstance(image, dict):
            continue
        media_type = image.get("media_type")
        data = image.get("data")
        if not isinstance(media_type, str) or not isinstance(data, str):
            continue
        if media_type not in ALLOWED_IMAGE_TYPES:
            continue
        try:
            decoded = decode_attachment(data)
        except ValueError:
            continue
        if len(decoded) > MAX_SINGLE_ATTACHMENT_BYTES:
            continue
        dimensions = image_dimensions(decoded, media_type)
        if dimensions is None:
            continue
        width, height = dimensions
        if (
            width <= 0 or height <= 0
            or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS
        ):
            continue
        refs.append({
            "image_id": history_image_id(turn_id, index),
            "media_type": media_type,
            "width": width,
            "height": height,
            "byte_size": len(decoded),
        })
    return refs


def history_image_from_events(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    turn_id: str,
    image_id: str,
) -> dict[str, Any] | None:
    """Resolve an opaque history image id inside one indexed turn group."""
    for event in events:
        if event.get("type") == "process":
            tool_input = event.get("input")
            image = (
                tool_input.get("history_image")
                if isinstance(tool_input, dict)
                else None
            )
            if (
                isinstance(image, dict)
                and image.get("image_id") == image_id
            ):
                return image
            continue
        if event.get("type") != "user_msg" or event.get("msg_id") != turn_id:
            continue
        images = event.get("images")
        if not isinstance(images, list):
            continue
        for index, image in enumerate(images):
            if history_image_id(turn_id, index) == image_id and isinstance(image, dict):
                return image
    return None


def materialize_history_turns(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    include_live_detail: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Build the lightweight UI projection persisted beside full detail.

    Tool/process/commentary bodies deliberately stay in ``events`` for an
    explicit detail request.  The initial projection contains only the prompt,
    final answer, terminal state and a count advertising expandable detail.
    Native Codex App activity has no live app-server stream, so its current
    moving head may additionally carry bounded commentary and lifecycle
    summaries. Large inputs, outputs, diffs and reasoning remain deferred.
    """
    turns: list[dict[str, Any]] = []
    for group in group_history_events(events):
        turn_id = _turn_id(group)
        if turn_id is None:
            continue
        prompt = ""
        has_user = False
        client_msg_id = None
        prompt_truncated = False
        image_refs: list[dict[str, Any]] = []
        deferred_image_count = 0
        files = None
        started_ms = None
        done_ms = None
        duration_ms = None
        fork_point = None
        checkpoint_id = None
        done = False
        steered = False
        interrupted = False
        error = None
        channels: dict[str, str] = {}
        texts: dict[str, list[str]] = {}
        text_order: list[str] = []
        text_done: set[str] = set()
        text_first_ms: dict[str, int] = {}
        text_last_ms: dict[str, int] = {}
        text_done_ms: dict[str, int] = {}
        text_background: set[str] = set()
        async_messages: dict[str, dict[str, Any]] = {}
        detail_items: set[str] = set()
        process_evidence: dict[str, dict[str, Any]] = {}
        live_blocks: list[dict[str, Any]] = []
        live_texts: dict[str, dict[str, Any]] = {}
        live_tools: dict[str, dict[str, Any]] = {}
        live_processes: dict[str, dict[str, Any]] = {}
        generated_images: dict[str, dict[str, Any]] = {}

        def short(value: Any) -> str | None:
            if not isinstance(value, str) or not value:
                return None
            return value[:_SUMMARY_LIVE_FIELD_MAX_CHARS]

        def add_live_text(message_id: str, channel: str) -> dict[str, Any] | None:
            if not include_live_detail or channel not in {
                    "commentary", "thinking"}:
                return None
            block = live_texts.get(message_id)
            if block is None:
                block = {
                    "kind": "text",
                    "message_id": message_id,
                    "text": "",
                    "done": False,
                    "channel": channel,
                }
                live_texts[message_id] = block
                live_blocks.append(block)
            elif channel != "unknown":
                block["channel"] = channel
            return block

        def touch_process(
            item_id: str,
            event: dict[str, Any],
            *,
            visible: bool = True,
            terminal: bool = False,
        ) -> None:
            row = process_evidence.setdefault(item_id, {
                "visible": False,
                "terminal": False,
                "first_ms": None,
                "last_ms": None,
                "terminal_ms": None,
            })
            row["visible"] = bool(row["visible"] or visible)
            row["terminal"] = bool(row["terminal"] or terminal)
            # Hidden reasoning/success-hook plumbing cannot start the public
            # process clock. If a hook later becomes actionable, its first
            # visible failure event establishes the timestamp instead.
            if not row["visible"]:
                return
            stamp = _event_ms(event.get("ts"))
            if stamp is None:
                return
            if row["first_ms"] is None:
                row["first_ms"] = stamp
            row["last_ms"] = stamp
            if terminal:
                row["terminal_ms"] = stamp

        for event in group:
            event_type = event.get("type")
            if (event_type == "process" and event.get("tool") == "image_generation"
                    and event.get("phase") == "end"
                    and event.get("status") == "succeeded"
                    and isinstance(event.get("item_id"), str)):
                # A generated image is output, not just heavy process detail.
                # Keep a bounded reference in summary; never its result/prompt.
                image_input: dict[str, Any] = {}
                raw_input = event.get("input")
                if isinstance(raw_input, dict):
                    path = short(raw_input.get("file_path"))
                    if path:
                        image_input["file_path"] = path
                    ref = raw_input.get("history_image")
                    if isinstance(ref, dict):
                        image_input["history_image"] = {
                            key: ref[key] for key in (
                                "image_id", "media_type", "width", "height", "byte_size"
                            ) if key in ref
                        }
                generated_images[event["item_id"]] = {
                    "kind": "process", "item_id": event["item_id"],
                    "processKind": "server_tool", "phase": "end",
                    "status": "succeeded", "done": True, "title": "生成图片",
                    "turn_id": event.get("turn_id"), "tool": "image_generation",
                    "input": image_input,
                }
                while len(generated_images) > 8:
                    generated_images.pop(next(iter(generated_images)))
            if started_ms is None:
                started_ms = _event_ms(event.get("ts"))
            if event_type == "user_msg":
                has_user = True
                if isinstance(event.get("client_msg_id"), str):
                    client_msg_id = event["client_msg_id"]
                if isinstance(event.get("prompt"), str):
                    prompt = event["prompt"]
                    if len(prompt) > _SUMMARY_PROMPT_MAX_CHARS:
                        suffix = "\n\n…（完整问题请展开本轮过程）"
                        keep = _SUMMARY_PROMPT_MAX_CHARS - len(suffix)
                        prompt = prompt[:keep] + suffix
                        prompt_truncated = True
                raw_images = event.get("images")
                image_refs = _history_image_refs(turn_id, raw_images)
                if isinstance(raw_images, list):
                    deferred_image_count = max(
                        0, len(raw_images) - len(image_refs))
                files = event.get("files")
            elif event_type == "assistant_msg_start":
                message_id = event.get("message_id")
                if isinstance(message_id, str):
                    if event.get("background") is True:
                        text_background.add(message_id)
                    channels[message_id] = str(event.get("channel") or "unknown")
                    if message_id not in texts:
                        texts[message_id] = []
                        text_order.append(message_id)
                    add_live_text(message_id, channels[message_id])
                    stamp = _event_ms(event.get("ts"))
                    if stamp is not None:
                        text_first_ms.setdefault(message_id, stamp)
                        text_last_ms[message_id] = stamp
            elif event_type == "delta":
                message_id = event.get("message_id")
                if isinstance(message_id, str) and isinstance(event.get("text"), str):
                    if event.get("background") is True:
                        text_background.add(message_id)
                    channels[message_id] = str(
                        event.get("channel") or channels.get(message_id) or "unknown")
                    if message_id not in texts:
                        texts[message_id] = []
                        text_order.append(message_id)
                    texts[message_id].append(event["text"])
                    block = add_live_text(message_id, channels[message_id])
                    if block is not None:
                        block["text"] += event["text"]
                    stamp = _event_ms(event.get("ts"))
                    if stamp is not None:
                        text_first_ms.setdefault(message_id, stamp)
                        text_last_ms[message_id] = stamp
            elif event_type == "assistant_msg_end":
                message_id = event.get("message_id")
                if isinstance(message_id, str):
                    if event.get("delivery") == "async":
                        async_messages[message_id] = {
                            "delivery": "async", "questions": event.get("questions"),
                        }
                    if event.get("background") is True:
                        text_background.add(message_id)
                    text_done.add(message_id)
                    block = live_texts.get(message_id)
                    if block is not None:
                        block["done"] = True
                    stamp = _event_ms(event.get("ts"))
                    if stamp is not None:
                        text_first_ms.setdefault(message_id, stamp)
                        text_last_ms[message_id] = stamp
                        text_done_ms[message_id] = stamp
            elif event_type == "turn_binding":
                if isinstance(event.get("turn_id"), str):
                    fork_point = event["turn_id"]
            elif event_type == "turn_end":
                done = True
                done_ms = _event_ms(event.get("ts"))
                if isinstance(event.get("turn_id"), str):
                    fork_point = event["turn_id"]
                if isinstance(event.get("checkpoint_id"), str):
                    checkpoint_id = event["checkpoint_id"]
                result = event.get("result")
                if isinstance(result, dict):
                    subtype = str(result.get("subtype") or "")
                    steered = subtype == "steered"
                    if (
                        subtype != "steered"
                        and isinstance(result.get("duration_ms"), int)
                    ):
                        duration_ms = result["duration_ms"]
                    interrupted = subtype in {
                        "interrupted", "error_during_execution", "aborted",
                    }
                    if (
                        bool(result.get("is_error"))
                        and not interrupted
                    ):
                        error = _historical_turn_failure(
                            error if error is not None else subtype)
            elif event_type == "error":
                if isinstance(event.get("message"), str):
                    error = _historical_turn_failure(event["message"])
            if include_live_detail and event_type == "tool_use":
                tool_id = event.get("tool_use_id")
                message_id = event.get("message_id")
                tool = event.get("tool")
                if all(isinstance(value, str) for value in (
                        tool_id, message_id, tool)):
                    block = live_tools.get(tool_id)
                    if block is None:
                        block = {
                            "kind": "tool",
                            "message_id": message_id,
                            "tool_use_id": tool_id,
                            "tool": tool,
                            "input": {},
                            "category": event.get("category") or "tool",
                            "title": short(event.get("title")),
                            "parent_id": event.get("parent_id"),
                            "server": short(event.get("server")),
                            "done": False,
                        }
                        live_tools[tool_id] = block
                        live_blocks.append(block)
            if event_type == "tool_use":
                tool_id = event.get("tool_use_id")
                if isinstance(tool_id, str):
                    touch_process(f"tool:{tool_id}", event)
            elif include_live_detail and event_type == "tool_result":
                tool_id = event.get("tool_use_id")
                block = live_tools.get(tool_id) if isinstance(tool_id, str) else None
                if block is not None:
                    result = {
                        "content": "",
                        "is_error": bool(event.get("is_error")),
                    }
                    for key in ("status", "exit_code", "duration_ms"):
                        if event.get(key) is not None:
                            result[key] = event[key]
                    summary = short(event.get("summary"))
                    if summary is not None:
                        result["summary"] = summary
                    if event.get("truncated") is not None:
                        result["truncated"] = bool(event["truncated"])
                    block["result"] = result
                    block["done"] = True
            if event_type == "tool_result":
                tool_id = event.get("tool_use_id")
                if (
                    isinstance(tool_id, str)
                    and f"tool:{tool_id}" in process_evidence
                ):
                    touch_process(
                        f"tool:{tool_id}", event, terminal=True)
            elif (include_live_detail and event_type == "process"
                  and event.get("kind") != "reasoning"):
                item_id = event.get("item_id")
                if isinstance(item_id, str):
                    block = live_processes.get(item_id)
                    if block is None:
                        block = {
                            "kind": "process",
                            "item_id": item_id,
                            "processKind": event.get("kind") or "task",
                            "phase": event.get("phase") or "snapshot",
                            "status": event.get("status") or "unknown",
                            "turn_id": event.get("turn_id"),
                            "parent_id": event.get("parent_id"),
                            "title": short(event.get("title")) or "处理",
                            "done": False,
                        }
                        live_processes[item_id] = block
                        live_blocks.append(block)
                    for target, source in (
                        ("summary", "summary"),
                        ("progress", "progress"),
                        ("server", "server"),
                        ("tool", "tool"),
                    ):
                        value = short(event.get(source))
                        if value is not None:
                            block[target] = value
                    for key in ("exit_code", "duration_ms", "truncated"):
                        if event.get(key) is not None:
                            block[key] = event[key]
                    for key in ("command", "cwd"):
                        value = short(event.get(key))
                        if value is not None:
                            block[key] = value
                    if event.get("background") is True:
                        block["background"] = True
                    stamp = _event_ms(event.get("ts"))
                    if block.get("background") is True and stamp is not None:
                        block.setdefault("startedTs", stamp)
                        block["updatedTs"] = stamp
                    block["phase"] = event.get("phase") or block["phase"]
                    block["status"] = event.get("status") or block["status"]
                    block["done"] = (
                        block["phase"] == "end"
                        or block["status"] in {
                            "succeeded", "failed", "declined", "cancelled",
                            "interrupted",
                        }
                    )
                    if (block.get("background") is True
                            and block["done"] and stamp is not None):
                        block["terminalTs"] = stamp
            elif include_live_detail and event_type == "turn_plan":
                item_id = event.get("item_id")
                if isinstance(item_id, str):
                    raw_plan = event.get("plan")
                    plan = []
                    if isinstance(raw_plan, list):
                        for entry in raw_plan[:32]:
                            if not isinstance(entry, dict):
                                continue
                            step = short(entry.get("step"))
                            status = entry.get("status")
                            if step and status in {
                                    "pending", "inProgress", "completed"}:
                                plan.append({"step": step, "status": status})
                    succeeded = bool(
                        plan and all(entry["status"] == "completed"
                                     for entry in plan)
                    )
                    replacement = {
                        "kind": "process",
                        "item_id": item_id,
                        "processKind": "plan",
                        "phase": "snapshot",
                        "status": "succeeded" if succeeded else "running",
                        "turn_id": event.get("turn_id"),
                        "parent_id": None,
                        "title": "计划",
                        "done": succeeded,
                        "plan": plan,
                    }
                    explanation = short(event.get("explanation"))
                    if explanation is not None:
                        replacement["explanation"] = explanation
                    block = live_processes.get(item_id)
                    if block is None:
                        block = replacement
                        live_processes[item_id] = block
                        live_blocks.append(block)
                    else:
                        block.clear()
                        block.update(replacement)
            if event_type == "process":
                item_id = event.get("item_id")
                kind = event.get("kind")
                status = event.get("status")
                phase = event.get("phase")
                if isinstance(item_id, str):
                    actionable_hook = kind == "hook" and status in {
                        "failed", "declined", "cancelled", "interrupted",
                    }
                    visible = kind != "reasoning" and (
                        kind != "hook" or actionable_hook)
                    touch_process(
                        f"process:{item_id}",
                        event,
                        visible=visible,
                        terminal=(
                            phase == "end"
                            or status in {
                                "succeeded", "failed", "declined",
                                "cancelled", "interrupted",
                            }
                        ),
                    )
            elif event_type == "turn_plan":
                item_id = event.get("item_id")
                if isinstance(item_id, str):
                    raw_plan = event.get("plan")
                    terminal = bool(
                        isinstance(raw_plan, list)
                        and raw_plan
                        and all(
                            isinstance(entry, dict)
                            and entry.get("status") == "completed"
                            for entry in raw_plan
                        )
                    )
                    touch_process(
                        f"plan:{item_id}", event, terminal=terminal)
            elif event_type == "turn_diff":
                item_id = event.get("item_id")
                if isinstance(item_id, str):
                    # A diff snapshot is complete at the event boundary.
                    touch_process(
                        f"diff:{item_id}", event, terminal=True)
            if event_type in {"tool_use", "process", "turn_plan", "turn_diff"}:
                detail_id = event.get("tool_use_id") or event.get("item_id")
                if isinstance(detail_id, str):
                    detail_items.add(detail_id)

        # Async questions are displayable content, not evidence of a formal
        # final answer. Select ordinary answers (including the legacy unphased
        # fallback) independently, then include questions in source order.
        final_id_set = {
            message_id for message_id in text_order
            if channels.get(message_id) == "final"
            and message_id not in async_messages
        }
        if not final_id_set:
            final_id_set = {
                message_id for message_id in text_order
                if channels.get(message_id) in {None, "unknown"}
                and message_id not in async_messages
            }
        final_id_set.update(async_messages)
        final_ids = [
            message_id for message_id in text_order if message_id in final_id_set
        ]
        for message_id in text_order:
            if message_id not in final_id_set and any(texts.get(message_id, ())):
                detail_items.add(message_id)
                if channels.get(message_id) == "commentary":
                    process_evidence[f"text:{message_id}"] = {
                        "visible": True,
                        "terminal": message_id in text_done,
                        "first_ms": text_first_ms.get(message_id),
                        "last_ms": text_last_ms.get(message_id),
                        "terminal_ms": text_done_ms.get(message_id),
                    }
        # Codex reconstructs summary-only process/text envelopes while parsing
        # a rollout. For an assistant-only continuation those synthetic rows
        # can carry the parse time because there is no UserMsg to provide the
        # authoritative start. Never persist the impossible `ts > doneTs`
        # ordering: derive the start from the terminal and its duration instead.
        if (not has_user and done_ms is not None
                and (started_ms is None or started_ms > done_ms)):
            started_ms = max(0, done_ms - (duration_ms or 0))
        blocks = []
        final_block_count = sum(
            bool("".join(texts.get(message_id, ())))
            for message_id in final_ids
        )
        image_limit = max(0, _SUMMARY_BLOCK_MAX - final_block_count)
        image_summaries = list(generated_images.values())[-image_limit:] if image_limit else []
        if include_live_detail:
            final_id_set = set(final_ids)
            live_block_limit = max(
                0,
                min(_SUMMARY_LIVE_BLOCK_MAX,
                    _SUMMARY_BLOCK_MAX - final_block_count - len(image_summaries)),
            )
            candidates: list[dict[str, Any]] = []
            for block in live_blocks:
                if (block.get("tool") == "image_generation"
                        and block.get("status") == "succeeded"):
                    continue
                if (block.get("kind") == "text"
                        and block.get("message_id") in final_id_set):
                    continue
                candidate = dict(block)
                if candidate.get("kind") == "text":
                    text = str(candidate.get("text") or "")
                    if not text:
                        continue
                    candidate["done"] = (
                        candidate.get("message_id") in text_done or done)
                candidates.append(candidate)
            if done:
                terminal_status = (
                    "interrupted" if interrupted
                    else "failed" if error
                    else "succeeded"
                )
                for block in candidates:
                    if block.get("done"):
                        continue
                    if (
                        steered
                        and block.get("kind") == "process"
                        and block.get("processKind") == "plan"
                    ):
                        continue
                    block["done"] = True
                    if block.get("kind") == "process":
                        block["phase"] = "end"
                        block["status"] = terminal_status
                    elif block.get("kind") == "tool":
                        block["result"] = {
                            "content": "",
                            "is_error": terminal_status != "succeeded",
                            "status": terminal_status,
                        }
            if live_block_limit == 0:
                candidates = []
            elif len(candidates) > live_block_limit:
                tail_size = max(0, live_block_limit - 1)
                tail = candidates[-tail_size:] if tail_size else []
                first_text = next(
                    (block for block in candidates
                     if block.get("kind") == "text"),
                    None,
                )
                candidates = (
                    [first_text, *tail]
                    if first_text is not None and first_text not in tail
                    else candidates[-live_block_limit:]
                )
            remaining_live_chars = _SUMMARY_LIVE_TEXT_MAX_CHARS
            for block in candidates:
                if block.get("kind") != "text":
                    continue
                text = str(block.get("text") or "")
                keep = min(
                    len(text),
                    _SUMMARY_LIVE_MESSAGE_MAX_CHARS,
                    remaining_live_chars,
                )
                block["text"] = text[:keep]
                remaining_live_chars -= keep
            blocks.extend(candidates)
        blocks.extend(image_summaries)
        remaining_summary_chars = _SUMMARY_TEXT_MAX_CHARS
        summary_truncated = False
        for message_id in final_ids:
            text = "".join(texts.get(message_id, ()))
            if text:
                if remaining_summary_chars <= 0:
                    summary_truncated = True
                    continue
                if len(text) > remaining_summary_chars:
                    suffix = "\n\n…（完整内容请展开本轮过程）"
                    if remaining_summary_chars <= len(suffix):
                        text = suffix[:remaining_summary_chars]
                    else:
                        keep = remaining_summary_chars - len(suffix)
                        text = text[:keep] + suffix
                    summary_truncated = True
                remaining_summary_chars -= len(text)
                text_block = {
                    "kind": "text",
                    "message_id": message_id,
                    "text": text,
                    "done": done,
                    "channel": "final",
                }
                metadata = async_messages.get(message_id)
                if metadata:
                    text_block["delivery"] = "async"
                    metadata_chars = len(json.dumps(metadata, ensure_ascii=False))
                    if metadata_chars <= remaining_summary_chars:
                        text_block["questions"] = metadata["questions"]
                        remaining_summary_chars -= metadata_chars
                    else:
                        # Detail retains the complete native card. Summary
                        # budgets must also count its structured text/options.
                        summary_truncated = True
                if message_id in text_background:
                    text_block["background"] = True
                    started_ts = text_first_ms.get(message_id)
                    done_ts = text_done_ms.get(message_id)
                    if started_ts is not None:
                        text_block["startedTs"] = started_ts
                    if done_ts is not None:
                        text_block["doneTs"] = done_ts
                blocks.append(text_block)
        visible_process = [
            row for row in process_evidence.values()
            if row.get("visible")
        ]
        process_started_ms = None
        if visible_process and all(
                isinstance(row.get("first_ms"), int)
                for row in visible_process):
            process_started_ms = min(
                int(row["first_ms"]) for row in visible_process)
        process_done_ms = None
        if process_started_ms is not None and visible_process:
            if all(
                    row.get("terminal")
                    and isinstance(row.get("terminal_ms"), int)
                    for row in visible_process):
                process_done_ms = max(
                    int(row["terminal_ms"]) for row in visible_process)
            elif done and done_ms is not None:
                # TurnEnd is the final trustworthy fence for any process item
                # whose producer omitted its own terminal timestamp.
                process_done_ms = done_ms
            if process_done_ms is not None:
                process_done_ms = max(process_started_ms, process_done_ms)
        detail_reasons = []
        if visible_process:
            detail_reasons.append("process")
        if prompt_truncated:
            detail_reasons.append("prompt_truncated")
        if summary_truncated:
            detail_reasons.append("answer_truncated")
        if deferred_image_count:
            detail_reasons.append("image_deferred")
        turn: dict[str, Any] = {
            "id": turn_id,
            "prompt": prompt,
            "blocks": blocks,
            "done": done,
            "processDetailState": (
                "present" if visible_process else "none"),
            "detailReasons": detail_reasons,
            "detailEventCount": (
                len(detail_items)
                + int(prompt_truncated)
                + int(summary_truncated)
                + deferred_image_count
            ),
            "detailLoaded": False,
        }
        optional = {
            "clientMsgId": client_msg_id,
            "forkPointId": fork_point,
            "checkpointId": checkpoint_id,
            "imageRefs": image_refs or None,
            "files": files,
            "ts": started_ms,
            "doneTs": done_ms,
            "durationMs": duration_ms,
            "processStartedTs": process_started_ms,
            "processDoneTs": process_done_ms,
            "error": error,
        }
        turn.update({key: value for key, value in optional.items() if value is not None})
        if interrupted:
            turn["interrupted"] = True
        turns.append(turn)
    return tuple(turns)


class HistoryIndexStore:
    """Small SQLite LRU for source-bound materialized conversation pages.

    Connections are intentionally short lived.  History parsing runs in worker
    threads, and opening a connection per store operation avoids sharing one
    sqlite handle across the wrapper event loop and those workers.
    """

    def __init__(
        self,
        state_dir: Path,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(state_dir) / "history-index.sqlite3"
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._summary_json_sql_available: bool | None = None
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _select_page_row(
        self,
        connection: sqlite3.Connection,
        *,
        summary_only: bool,
        projected_sql: str,
        raw_sql: str,
        parameters: tuple[Any, ...],
    ) -> sqlite3.Row | None:
        """Read one page without turning unrelated SQLite faults into retries."""
        if not summary_only or self._summary_json_sql_available is False:
            return connection.execute(raw_sql, parameters).fetchone()
        try:
            row = connection.execute(
                projected_sql, parameters,
            ).fetchone()
        except sqlite3.OperationalError as exc:
            fallback = _summary_projection_fallback(exc)
            if fallback is None:
                raise
            if fallback == "unsupported":
                # A custom SQLite build without JSON1 would otherwise throw on
                # every summary read before doing the same full-payload query.
                self._summary_json_sql_available = False
            return connection.execute(raw_sql, parameters).fetchone()
        self._summary_json_sql_available = True
        return row

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current in range(10, 32):
                for table in ("history_pages", "history_turn_details"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='codex'")
            if current in range(10, 28):
                # v28 retains native async questions. Rollout bytes and source
                # fingerprints are unchanged; rebuild Codex narrative only.
                # Claude state, binary assets and Agent/compact indexes survive.
                for table in ("history_pages", "history_turn_details"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='codex'")
            if current == 28:
                # v29 changes only the summary selection. The source token is
                # unchanged, so old pages need an explicit invalidation; keep
                # complete details, Claude history and all independent assets.
                connection.execute(
                    "DELETE FROM history_pages WHERE engine='codex'")
            if current in (10, 11, 12, 13, 14):
                # v14 corrected Claude browser-message identity and v15
                # narrowly restores completed tails bypassed by delayed
                # request-retry branches. Exact transcript fingerprints cannot
                # reveal either projection change, so invalidate only Claude's
                # rebuildable pages/details/images. Preserve expensive Codex
                # projections and the independent compact-chain graph index.
                for table in (
                    "history_pages",
                    "history_turn_details",
                    "history_image_assets",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='claude'")
            if current in range(10, 27):
                # v22 changes Claude turn identity, v26 changes its terminal
                # clock, and v27 changes image Read projection without changing
                # transcript bytes. Rebuild only Claude narrative rows so every
                # repair reaches History;
                # source-bound images, compact ancestry, and Agent detail
                # payloads stay valid.
                for table in ("history_pages", "history_turn_details"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='claude'")
            if current in (21, 22, 23, 24):
                # v23 restores the first real public-process timestamp from an
                # omitted oversized prefix. v24 additionally overlays a
                # source-bound live clock which can appear without changing
                # rollout bytes. v25 repairs leading compaction ownership.
                # Only Codex narrative projections need rebuilding; Claude
                # history and binary assets remain valid.
                for table in ("history_pages", "history_turn_details"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='codex'")
            if current in (10, 11, 12, 13, 14, 15, 16):
                # v16 makes browser/native ownership durable; v17 reuses the
                # adjacent native response-item id for legacy Codex user rows.
                # Source fingerprints reveal neither projection change, so
                # rebuild pages and source-complete details once. Image assets
                # contain no turn-owner identity and remain valid/source-bound.
                for table in (
                    "history_pages",
                    "history_turn_details",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='codex'")
            elif current in (17, 18):
                # Page payloads are rebuildable and may contain either the old
                # pre-summary size truncation or no v36 process-detail state.
                # v21 additionally invalidates Codex details whose official
                # events may contain parser-time timestamps; the independent
                # v22 block above has already invalidated Claude details. All
                # source-bound image assets remain valid.
                connection.execute("DELETE FROM history_pages")
                connection.execute(
                    "DELETE FROM history_turn_details WHERE engine='codex'")
            elif current in (19, 20):
                # v20 adds an independent source-bound Claude Agent detail LRU.
                # v21 rebuilds timestamp-bearing Codex projections; the v22
                # block above rebuilds Claude narrative identity. Images and
                # Agent detail payloads remain byte-for-byte valid.
                for table in ("history_pages", "history_turn_details"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE engine='codex'")
            elif current in (21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31):
                # The independent v22-v32 invalidations above suffice.
                pass
            elif current not in (0, _SCHEMA_VERSION):
                # v9 changes the invariant of history_turn_details: those rows
                # must contain the source-complete translated turn, never the
                # transport-compacted History frame.  Both Claude and Codex v8
                # pages could have populated details from the wire payload, and
                # a retained page would then prevent the source from being
                # translated again.  This database is entirely derived, so one
                # full rebuild is the only safe migration.
                connection.execute("DROP TABLE IF EXISTS history_pages")
                connection.execute("DROP TABLE IF EXISTS history_turn_details")
                connection.execute("DROP TABLE IF EXISTS history_image_assets")
                connection.execute("DROP TABLE IF EXISTS history_agent_details")
                connection.execute("DROP TABLE IF EXISTS claude_compact_sources")
                connection.execute("DROP TABLE IF EXISTS claude_compact_records")
                connection.execute("DROP TABLE IF EXISTS claude_compact_queue")
                current = 0
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_pages (
                    session_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_device INTEGER NOT NULL,
                    source_inode INTEGER NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_head_sha256 TEXT NOT NULL,
                    source_tail_sha256 TEXT NOT NULL,
                    before_cursor TEXT NOT NULL,
                    page_limit INTEGER NOT NULL,
                    payload_json BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (
                        session_id, engine, source_token,
                        before_cursor, page_limit
                    )
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_pages_lru "
                "ON history_pages(accessed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_turn_details (
                    session_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    payload_json BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (session_id, engine, source_token, turn_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_turn_details_lookup "
                "ON history_turn_details(session_id, engine, source_path, "
                "turn_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_turn_details_lru "
                "ON history_turn_details(accessed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_agent_details (
                    session_id TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload_json BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (session_id, source_token, run_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_agent_details_lookup "
                "ON history_agent_details(session_id, source_path, run_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_agent_details_lru "
                "ON history_agent_details(accessed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_image_assets (
                    session_id TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    source_token TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    image_id TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    data BLOB NOT NULL,
                    payload_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    PRIMARY KEY (
                        session_id, engine, source_token,
                        turn_id, image_id, variant
                    )
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_image_assets_lookup "
                "ON history_image_assets(session_id, engine, source_path, "
                "turn_id, image_id, variant, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS history_image_assets_lru "
                "ON history_image_assets(accessed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claude_compact_sources (
                    source_path TEXT PRIMARY KEY,
                    source_device INTEGER NOT NULL,
                    source_inode INTEGER NOT NULL,
                    indexed_size INTEGER NOT NULL,
                    source_head_sha256 TEXT NOT NULL,
                    source_tail_sha256 TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claude_compact_records (
                    source_path TEXT NOT NULL,
                    source_device INTEGER NOT NULL,
                    source_inode INTEGER NOT NULL,
                    uuid TEXT NOT NULL,
                    row_type TEXT,
                    subtype TEXT,
                    parent_uuid TEXT,
                    logical_parent_uuid TEXT,
                    is_sidechain INTEGER,
                    source_offset INTEGER NOT NULL,
                    record_bytes INTEGER NOT NULL,
                    visible_user INTEGER NOT NULL,
                    PRIMARY KEY (source_path, source_device, source_inode, uuid)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS claude_compact_records_offset "
                "ON claude_compact_records(source_path, source_device, "
                "source_inode, source_offset)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claude_compact_queue (
                    source_path TEXT NOT NULL,
                    source_device INTEGER NOT NULL,
                    source_inode INTEGER NOT NULL,
                    content_length INTEGER NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    PRIMARY KEY (
                        source_path, source_device, source_inode,
                        content_length, content_sha256
                    )
                )
                """
            )
            if current != _SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _cursor(before: str | None) -> str:
        return before or ""

    def get_claude_compact_index(
        self,
        source_path: str,
        *,
        snapshot_size: int | None,
        max_record_bytes: int,
        max_entries: int,
        visible_user: Callable[[dict[str, Any]], bool],
    ) -> ClaudeCompactChainIndex | None:
        """Incrementally index compact ancestry without retaining row payloads.

        The SQLite rows are rebuildable metadata. An append verifies the exact
        previously indexed prefix, parses only new complete JSONL records, and
        lets every history page seek directly to its selected payload offsets.
        """
        resolved = os.path.realpath(source_path)
        bounded_record = max(1024, int(max_record_bytes))
        bounded_entries = max(1, int(max_entries))
        try:
            source = open(resolved, "rb")
        except OSError:
            return None
        with source:
            try:
                stat = os.fstat(source.fileno())
            except OSError:
                return None
            device = int(stat.st_dev)
            inode = int(stat.st_ino)
            target_size = int(stat.st_size)
            if snapshot_size is not None:
                target_size = min(target_size, max(0, int(snapshot_size)))
            with self._connect() as connection:
                state = connection.execute(
                    "SELECT * FROM claude_compact_sources WHERE source_path=?",
                    (resolved,),
                ).fetchone()
                rebuild = state is None
                indexed_size = 0
                record_count = 0
                if state is not None:
                    indexed_size = int(state["indexed_size"])
                    record_count = int(state["record_count"])
                    rebuild = (
                        int(state["source_device"]) != device
                        or int(state["source_inode"]) != inode
                        or int(stat.st_size) < indexed_size
                    )
                    if not rebuild:
                        head, tail = _prefix_samples(source, indexed_size)
                        rebuild = (
                            head != state["source_head_sha256"]
                            or tail != state["source_tail_sha256"]
                        )
                if rebuild:
                    connection.execute(
                        "DELETE FROM claude_compact_records WHERE source_path=?",
                        (resolved,),
                    )
                    connection.execute(
                        "DELETE FROM claude_compact_queue WHERE source_path=?",
                        (resolved,),
                    )
                    connection.execute(
                        "DELETE FROM claude_compact_sources WHERE source_path=?",
                        (resolved,),
                    )
                    indexed_size = 0
                    record_count = 0

                new_records: list[tuple[object, ...]] = []
                new_queue: list[tuple[object, ...]] = []
                scan_offset = indexed_size
                scan_complete = target_size <= indexed_size
                if target_size > indexed_size:
                    source.seek(indexed_size)
                    while source.tell() < target_size:
                        offset = source.tell()
                        remaining = target_size - offset
                        line = source.readline(min(remaining, bounded_record + 1))
                        if not line:
                            break
                        if not line.endswith(b"\n") and len(line) > bounded_record:
                            # Do not index past an unknown ancestry record. A
                            # raised configured record cap can safely retry from
                            # this exact byte on the next request.
                            scan_offset = offset
                            break
                        if not line.endswith(b"\n"):
                            try:
                                row = json.loads(line)
                            except Exception:
                                # Snapshot captured a writer's partial final row.
                                scan_offset = offset
                                break
                        else:
                            try:
                                row = json.loads(line)
                            except Exception:
                                scan_offset = source.tell()
                                continue
                        scan_offset = source.tell()
                        if not isinstance(row, dict):
                            continue
                        if (row.get("type") == "queue-operation"
                                and row.get("operation") == "enqueue"):
                            content = row.get("content")
                            if (isinstance(content, str) and content.lstrip().startswith(
                                    "<task-notification>")):
                                new_queue.append((
                                    resolved, device, inode, len(content),
                                    hashlib.sha256(content.encode(
                                        "utf-8", "surrogatepass")).hexdigest(),
                                ))
                        uid = row.get("uuid")
                        if not (isinstance(uid, str)
                                and _SAFE_COMPACT_ID.fullmatch(uid)):
                            continue
                        def text_field(name: str) -> str | None:
                            value = row.get(name)
                            return value if isinstance(value, str) else None
                        new_records.append((
                            resolved, device, inode, uid,
                            text_field("type"), text_field("subtype"),
                            text_field("parentUuid"),
                            text_field("logicalParentUuid"),
                            (1 if row.get("isSidechain") is True else 0
                             if row.get("isSidechain") is False else None),
                            offset, len(line), int(bool(visible_user(row))),
                        ))
                    scan_complete = scan_offset >= target_size
                    if new_records:
                        connection.executemany(
                            """
                            INSERT INTO claude_compact_records (
                                source_path, source_device, source_inode, uuid,
                                row_type, subtype, parent_uuid,
                                logical_parent_uuid, is_sidechain,
                                source_offset, record_bytes, visible_user
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (
                                source_path, source_device, source_inode, uuid
                            ) DO UPDATE SET
                                row_type=excluded.row_type,
                                subtype=excluded.subtype,
                                parent_uuid=excluded.parent_uuid,
                                logical_parent_uuid=excluded.logical_parent_uuid,
                                is_sidechain=excluded.is_sidechain,
                                source_offset=excluded.source_offset,
                                record_bytes=excluded.record_bytes,
                                visible_user=excluded.visible_user
                            """,
                            new_records,
                        )
                    if new_queue:
                        connection.executemany(
                            """
                            INSERT OR IGNORE INTO claude_compact_queue (
                                source_path, source_device, source_inode,
                                content_length, content_sha256
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            new_queue,
                        )
                    record_count = int(connection.execute(
                        """
                        SELECT COUNT(*) FROM claude_compact_records
                        WHERE source_path=? AND source_device=? AND source_inode=?
                        """,
                        (resolved, device, inode),
                    ).fetchone()[0])
                    if record_count > bounded_entries:
                        connection.execute(
                            "DELETE FROM claude_compact_records "
                            "WHERE source_path=?",
                            (resolved,),
                        )
                        connection.execute(
                            "DELETE FROM claude_compact_queue "
                            "WHERE source_path=?",
                            (resolved,),
                        )
                        connection.execute(
                            "DELETE FROM claude_compact_sources "
                            "WHERE source_path=?",
                            (resolved,),
                        )
                        return None
                indexed_size = max(indexed_size, scan_offset)
                head, tail = _prefix_samples(source, indexed_size)
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO claude_compact_sources (
                        source_path, source_device, source_inode, indexed_size,
                        source_head_sha256, source_tail_sha256,
                        record_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_path) DO UPDATE SET
                        source_device=excluded.source_device,
                        source_inode=excluded.source_inode,
                        indexed_size=excluded.indexed_size,
                        source_head_sha256=excluded.source_head_sha256,
                        source_tail_sha256=excluded.source_tail_sha256,
                        record_count=excluded.record_count,
                        updated_at=excluded.updated_at
                    """,
                    (resolved, device, inode, indexed_size, head, tail,
                     record_count, now),
                )
                # A partial writer row or a record above the configured cap
                # leaves a useful resumable prefix in SQLite, but that prefix
                # is not an authoritative view of this snapshot. In
                # particular, never return the previous leaf as if the unseen
                # suffix did not exist.
                if not scan_complete:
                    return None
                rows = connection.execute(
                    """
                    SELECT * FROM claude_compact_records
                    WHERE source_path=? AND source_device=? AND source_inode=?
                      AND source_offset + record_bytes <= ?
                    ORDER BY source_offset
                    """,
                    (resolved, device, inode, min(target_size, indexed_size)),
                ).fetchall()
                queued = connection.execute(
                    """
                    SELECT content_length, content_sha256
                    FROM claude_compact_queue
                    WHERE source_path=? AND source_device=? AND source_inode=?
                    """,
                    (resolved, device, inode),
                ).fetchall()
                leaf = next((str(row["uuid"]) for row in reversed(rows)
                             if row["is_sidechain"] != 1), None)
                if leaf is None:
                    return None
                metadata = {
                    str(row["uuid"]): (
                        row["row_type"], row["subtype"], row["parent_uuid"],
                        row["logical_parent_uuid"],
                        (True if row["is_sidechain"] == 1 else False
                         if row["is_sidechain"] == 0 else None),
                        int(row["source_offset"]), bool(row["visible_user"]),
                        int(row["record_bytes"]),
                    )
                    for row in rows
                }
                stale_sources = connection.execute(
                    """
                    SELECT source_path FROM claude_compact_sources
                    ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                    """,
                    (_COMPACT_SOURCE_LIMIT,),
                ).fetchall()
                for stale in stale_sources:
                    stale_path = str(stale["source_path"])
                    connection.execute(
                        "DELETE FROM claude_compact_records WHERE source_path=?",
                        (stale_path,),
                    )
                    connection.execute(
                        "DELETE FROM claude_compact_queue WHERE source_path=?",
                        (stale_path,),
                    )
                    connection.execute(
                        "DELETE FROM claude_compact_sources WHERE source_path=?",
                        (stale_path,),
                    )
                return ClaudeCompactChainIndex(
                    leaf=leaf,
                    rows=metadata,
                    queued_notifications=frozenset(
                        (int(row["content_length"]), str(row["content_sha256"]))
                        for row in queued
                    ),
                    indexed_size=indexed_size,
                )

    def get_page(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        *,
        before: str | None,
        limit: int,
        summary_only: bool = False,
    ) -> MaterializedHistoryPage | None:
        parameters = (
            session_id, engine, source.token,
            self._cursor(before), int(limit),
        )
        raw_sql = """
            SELECT payload_json FROM history_pages
            WHERE session_id=? AND engine=? AND source_token=?
              AND before_cursor=? AND page_limit=?
        """
        now = time.time()
        with self._connect() as connection:
            row = self._select_page_row(
                connection,
                summary_only=summary_only,
                projected_sql=f"""
                    SELECT {_SUMMARY_PAGE_PAYLOAD_SQL} AS payload_json
                    FROM history_pages
                    WHERE session_id=? AND engine=? AND source_token=?
                      AND before_cursor=? AND page_limit=?
                """,
                raw_sql=raw_sql,
                parameters=parameters,
            )
            if row is None:
                return None
            connection.execute(
                """
                UPDATE history_pages SET accessed_at=?
                WHERE session_id=? AND engine=? AND source_token=?
                  AND before_cursor=? AND page_limit=?
                """,
                (now, session_id, engine, source.token,
                 self._cursor(before), int(limit)),
            )
        try:
            raw_payload = row["payload_json"]
            payload = json.loads(
                raw_payload
                if isinstance(raw_payload, str)
                else bytes(raw_payload).decode("utf-8")
            )
            if not isinstance(payload, dict):
                return None
            return MaterializedHistoryPage.from_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            self.invalidate_session(session_id)
            return None

    def get_append_page(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        *,
        before: str | None,
        limit: int,
        summary_only: bool = False,
    ) -> MaterializedHistoryPage | None:
        """Return a cached page whose source is a verified file prefix.

        The caller may paint this page while rebuilding the exact appended
        snapshot. Device/inode/size checks plus both sampled ends of the old
        source reject truncation, replacement and in-place rewrites.
        """
        parameters = (
            session_id, engine, source.path, source.device,
            source.inode, source.size, self._cursor(before), int(limit),
        )
        raw_sql = """
            SELECT payload_json, source_size, source_head_sha256,
                   source_tail_sha256
            FROM history_pages
            WHERE session_id=? AND engine=? AND source_path=?
              AND source_device=? AND source_inode=?
              AND source_size<? AND before_cursor=? AND page_limit=?
            ORDER BY source_size DESC, created_at DESC
            LIMIT 1
        """
        with self._connect() as connection:
            row = self._select_page_row(
                connection,
                summary_only=summary_only,
                projected_sql=f"""
                    SELECT {_SUMMARY_PAGE_PAYLOAD_SQL} AS payload_json,
                           source_size, source_head_sha256,
                           source_tail_sha256
                    FROM history_pages
                    WHERE session_id=? AND engine=? AND source_path=?
                      AND source_device=? AND source_inode=?
                      AND source_size<? AND before_cursor=? AND page_limit=?
                    ORDER BY source_size DESC, created_at DESC
                    LIMIT 1
                """,
                raw_sql=raw_sql,
                parameters=parameters,
            )
        if row is None:
            return None
        old_size = int(row["source_size"])
        try:
            with open(source.path, "rb") as current:
                stat = os.fstat(current.fileno())
                if (int(stat.st_dev) != source.device
                        or int(stat.st_ino) != source.inode
                        or int(stat.st_size) < source.size):
                    return None
                sample_size = min(old_size, _FINGERPRINT_SAMPLE_BYTES)
                head = current.read(sample_size)
                current.seek(max(0, old_size - sample_size))
                tail = current.read(sample_size)
        except OSError:
            return None
        if (hashlib.sha256(head).hexdigest() != row["source_head_sha256"]
                or hashlib.sha256(tail).hexdigest()
                != row["source_tail_sha256"]):
            return None
        try:
            raw_payload = row["payload_json"]
            payload = json.loads(
                raw_payload
                if isinstance(raw_payload, str)
                else bytes(raw_payload).decode("utf-8")
            )
            if not isinstance(payload, dict):
                return None
            return MaterializedHistoryPage.from_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            self.invalidate_session(session_id)
            return None

    def put_page(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        *,
        before: str | None,
        limit: int,
        page: MaterializedHistoryPage,
        detail_events: (
            tuple[dict[str, Any], ...] | list[dict[str, Any]] | None
        ) = None,
    ) -> bool:
        payload = json.dumps(
            page.as_payload(), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        now = time.time()
        with self._connect() as connection:
            self._put_turn_details(
                connection,
                session_id,
                engine,
                source,
                detail_events if detail_events is not None else page.events,
                now,
                max_bytes=self.max_bytes,
            )
            self._prune_details(connection)
            if len(payload) > self.max_bytes:
                return False
            connection.execute(
                """
                INSERT INTO history_pages (
                    session_id, engine, source_token, source_path,
                    source_device, source_inode, source_size,
                    source_head_sha256, source_tail_sha256,
                    before_cursor, page_limit, payload_json, payload_bytes,
                    created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    session_id, engine, source_token, before_cursor, page_limit
                ) DO UPDATE SET
                    source_path=excluded.source_path,
                    source_device=excluded.source_device,
                    source_inode=excluded.source_inode,
                    source_size=excluded.source_size,
                    source_head_sha256=excluded.source_head_sha256,
                    source_tail_sha256=excluded.source_tail_sha256,
                    payload_json=excluded.payload_json,
                    payload_bytes=excluded.payload_bytes,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at
                """,
                (session_id, engine, source.token, source.path,
                 source.device, source.inode, source.size,
                 source.head_sha256, source.tail_sha256,
                 self._cursor(before), int(limit), payload, len(payload), now, now),
            )
            # A changed source makes every older snapshot for this session
            # unreachable.  Delete it eagerly rather than waiting for global LRU.
            connection.execute(
                """
                DELETE FROM history_pages
                WHERE session_id=? AND engine=? AND source_token<>?
                """,
                (session_id, engine, source.token),
            )
            self._prune(connection)
        return True

    @staticmethod
    def _put_turn_details(
        connection: sqlite3.Connection,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        now: float,
        *,
        max_bytes: int | None = None,
    ) -> None:
        """Write source-complete detail independently from the wire-safe page."""
        for group in group_history_events(events):
            turn_id = _turn_id(group)
            if turn_id is None:
                continue
            detail_payload = json.dumps(
                group, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            if max_bytes is not None and len(detail_payload) > max_bytes:
                continue
            connection.execute(
                """
                INSERT INTO history_turn_details (
                    session_id, engine, source_token, source_path,
                    turn_id, payload_json, payload_bytes,
                    created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id, engine, source_token, turn_id)
                DO UPDATE SET
                    source_path=excluded.source_path,
                    payload_json=excluded.payload_json,
                    payload_bytes=excluded.payload_bytes,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at
                """,
                (session_id, engine, source.token, source.path, turn_id,
                 detail_payload, len(detail_payload), now, now),
            )

    def put_turn_details(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> None:
        """Replace compact page detail with the full translated turn groups.

        The page cache must remain bounded by the WebSocket frame budget.  Its
        per-turn expansion rows, however, are derived from the coherent source
        snapshot before any transport-only compaction.
        """
        now = time.time()
        with self._connect() as connection:
            self._put_turn_details(
                connection,
                session_id,
                engine,
                source,
                events,
                now,
                max_bytes=self.max_bytes,
            )
            self._prune_details(connection)

    @staticmethod
    def _decode_turn_detail_payload(
        payload_json: Any,
        turn_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        try:
            payload = json.loads(bytes(payload_json).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
        if (
            not isinstance(payload, list)
            or not all(isinstance(event, dict) for event in payload)
            or _turn_id(payload) != turn_id
        ):
            return None
        return tuple(payload)

    def get_turn_detail_snapshot(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        turn_id: str,
    ) -> MaterializedTurnDetail | None:
        """Return one materialized turn without reading a complete page.

        Prefer the exact source snapshot.  If the transcript only appended
        after the page was painted, the newest row for the same source path is
        still a valid immutable completed turn.  Rollback explicitly removes
        every row for the session before its new revision is exposed.
        """
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_token, payload_json FROM history_turn_details
                WHERE session_id=? AND engine=? AND source_path=? AND turn_id=?
                ORDER BY (source_token = ?) DESC, created_at DESC
                LIMIT 1
                """,
                (session_id, engine, source.path, turn_id, source.token),
            ).fetchone()
            if row is not None:
                events = self._decode_turn_detail_payload(
                    row["payload_json"], turn_id)
                if events is not None:
                    connection.execute(
                        """
                        UPDATE history_turn_details SET accessed_at=?
                        WHERE session_id=? AND engine=?
                          AND source_token=? AND turn_id=?
                        """,
                        (now, session_id, engine,
                         row["source_token"], turn_id),
                    )
                    return MaterializedTurnDetail(
                        events=events,
                        turn_id=turn_id,
                        source_token=str(row["source_token"]),
                    )

                # A malformed derived row must not mask the canonical page
                # fallback below.
                connection.execute(
                    """
                    DELETE FROM history_turn_details
                    WHERE session_id=? AND engine=?
                      AND source_token=? AND turn_id=?
                    """,
                    (session_id, engine, row["source_token"], turn_id),
                )

            # Detail has a deliberately tighter LRU than complete pages.  A
            # browser may still be displaying a retained summary after its
            # standalone detail row was evicted, so recover the group from the
            # canonical page instead of returning a permanently retryable miss.
            pages = connection.execute(
                """
                SELECT rowid, payload_json FROM history_pages
                WHERE session_id=? AND engine=? AND source_path=?
                ORDER BY (source_token = ?) DESC,
                         accessed_at DESC, created_at DESC
                """,
                (session_id, engine, source.path, source.token),
            )
            for page_row in pages:
                try:
                    page_payload = json.loads(
                        bytes(page_row["payload_json"]).decode("utf-8"))
                    page = MaterializedHistoryPage.from_payload(page_payload)
                except (UnicodeDecodeError, json.JSONDecodeError,
                        ValueError, TypeError):
                    connection.execute(
                        "DELETE FROM history_pages WHERE rowid=?",
                        (page_row["rowid"],),
                    )
                    continue
                for group in group_history_events(page.events):
                    if _turn_id(group) != turn_id:
                        continue
                    summary = next((
                        turn for turn in page.turns
                        if turn.get("id") == turn_id
                        or turn.get("historyTurnId") == turn_id
                    ), None)
                    expected_detail = (
                        summary.get("detailEventCount")
                        if isinstance(summary, dict) else None
                    )
                    actual_detail = sum(
                        _turn_detail_identity(event) is not None
                        for event in group
                    )
                    if (isinstance(expected_detail, int)
                            and expected_detail > actual_detail):
                        # This page is intentionally transport-compacted. Its
                        # synthetic Error/terminal envelope is not a valid
                        # replacement for an evicted source-complete detail row.
                        return None
                    connection.execute(
                        "UPDATE history_pages SET accessed_at=? WHERE rowid=?",
                        (now, page_row["rowid"]),
                    )
                    return MaterializedTurnDetail(
                        events=tuple(group),
                        turn_id=turn_id,
                    )
        return None

    def get_turn_detail(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        turn_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        """Compatibility wrapper returning only the materialized events."""
        detail = self.get_turn_detail_snapshot(
            session_id, engine, source, turn_id)
        return detail.events if detail is not None else None

    def get_turn_detail_by_snapshot(
        self,
        session_id: str,
        engine: str,
        source_token: str,
        turn_hash: str,
    ) -> MaterializedTurnDetail | None:
        """Resolve one immutable detail row from an opaque scoped cursor.

        Query turn ids before payloads so a hash collision fails closed without
        decoding multiple potentially large detail blobs.
        """
        if (
            _DETAIL_SNAPSHOT_TOKEN.fullmatch(source_token) is None
            or _DETAIL_SNAPSHOT_TURN_HASH.fullmatch(turn_hash) is None
        ):
            return None
        now = time.time()
        with self._connect() as connection:
            turn_ids = connection.execute(
                """
                SELECT turn_id FROM history_turn_details
                WHERE session_id=? AND engine=? AND source_token=?
                """,
                (session_id, engine, source_token),
            )
            matches = [
                str(row["turn_id"])
                for row in turn_ids
                if history_turn_snapshot_hash(str(row["turn_id"])) == turn_hash
            ]
            if len(matches) != 1:
                return None
            turn_id = matches[0]
            row = connection.execute(
                """
                SELECT payload_json FROM history_turn_details
                WHERE session_id=? AND engine=?
                  AND source_token=? AND turn_id=?
                """,
                (session_id, engine, source_token, turn_id),
            ).fetchone()
            if row is None:
                return None
            events = self._decode_turn_detail_payload(
                row["payload_json"], turn_id)
            if events is None:
                connection.execute(
                    """
                    DELETE FROM history_turn_details
                    WHERE session_id=? AND engine=?
                      AND source_token=? AND turn_id=?
                    """,
                    (session_id, engine, source_token, turn_id),
                )
                return None
            connection.execute(
                """
                UPDATE history_turn_details SET accessed_at=?
                WHERE session_id=? AND engine=?
                  AND source_token=? AND turn_id=?
                """,
                (now, session_id, engine, source_token, turn_id),
            )
            return MaterializedTurnDetail(
                events=events,
                turn_id=turn_id,
                source_token=source_token,
            )

    def put_agent_detail(
        self,
        session_id: str,
        source: HistorySourceFingerprint,
        run_id: str,
        events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    ) -> None:
        payload = json.dumps(
            events, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > self.max_bytes:
            return
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO history_agent_details (
                    session_id, source_token, source_path, run_id,
                    payload_json, payload_bytes, created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id, source_token, run_id) DO UPDATE SET
                    source_path=excluded.source_path,
                    payload_json=excluded.payload_json,
                    payload_bytes=excluded.payload_bytes,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at
                """,
                (session_id, source.token, source.path, run_id,
                 payload, len(payload), now, now),
            )
            connection.execute(
                """
                DELETE FROM history_agent_details
                WHERE session_id=? AND run_id=? AND source_path=?
                  AND source_token<>?
                """,
                (session_id, run_id, source.path, source.token),
            )
            self._prune_agent_details(connection)

    def get_agent_detail(
        self,
        session_id: str,
        source: HistorySourceFingerprint,
        run_id: str,
    ) -> tuple[dict[str, Any], ...] | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM history_agent_details
                WHERE session_id=? AND source_token=? AND run_id=?
                """,
                (session_id, source.token, run_id),
            ).fetchone()
            if row is None:
                return None
            try:
                payload = json.loads(bytes(row["payload_json"]).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                payload = None
            if not (isinstance(payload, list)
                    and all(isinstance(event, dict) for event in payload)):
                connection.execute(
                    """
                    DELETE FROM history_agent_details
                    WHERE session_id=? AND source_token=? AND run_id=?
                    """,
                    (session_id, source.token, run_id),
                )
                return None
            connection.execute(
                """
                UPDATE history_agent_details SET accessed_at=?
                WHERE session_id=? AND source_token=? AND run_id=?
                """,
                (now, session_id, source.token, run_id),
            )
            return tuple(payload)

    def get_image_asset(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        turn_id: str,
        image_id: str,
        variant: str,
        *,
        exact_source: bool = False,
    ) -> tuple[str, int, int, bytes] | None:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_token, media_type, width, height, data
                FROM history_image_assets
                WHERE session_id=? AND engine=? AND source_path=?
                  AND turn_id=? AND image_id=? AND variant=?
                  AND (? = 0 OR source_token = ?)
                ORDER BY (source_token = ?) DESC, created_at DESC
                LIMIT 1
                """,
                (session_id, engine, source.path, turn_id, image_id,
                 variant, int(exact_source), source.token, source.token),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE history_image_assets SET accessed_at=?
                WHERE session_id=? AND engine=? AND source_token=?
                  AND turn_id=? AND image_id=? AND variant=?
                """,
                (now, session_id, engine, row["source_token"], turn_id,
                 image_id, variant),
            )
        return (
            str(row["media_type"]), int(row["width"]), int(row["height"]),
            bytes(row["data"]),
        )

    def put_image_asset(
        self,
        session_id: str,
        engine: str,
        source: HistorySourceFingerprint,
        turn_id: str,
        image_id: str,
        variant: str,
        media_type: str,
        width: int,
        height: int,
        data: bytes,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO history_image_assets (
                    session_id, engine, source_token, source_path,
                    turn_id, image_id, variant, media_type, width, height,
                    data, payload_bytes, created_at, accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    session_id, engine, source_token,
                    turn_id, image_id, variant
                ) DO UPDATE SET
                    media_type=excluded.media_type,
                    width=excluded.width,
                    height=excluded.height,
                    data=excluded.data,
                    payload_bytes=excluded.payload_bytes,
                    created_at=excluded.created_at,
                    accessed_at=excluded.accessed_at
                """,
                (session_id, engine, source.token, source.path, turn_id,
                 image_id, variant, media_type, width, height, data,
                 len(data), now, now),
            )
            self._prune_image_assets(connection)

    def _prune(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_pages"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        while entries > self.max_entries or total_bytes > self.max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, engine, source_token, before_cursor,
                       page_limit, payload_bytes
                FROM history_pages ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_pages
                WHERE session_id=? AND engine=? AND source_token=?
                  AND before_cursor=? AND page_limit=?
                """,
                tuple(victim[key] for key in (
                    "session_id", "engine", "source_token",
                    "before_cursor", "page_limit")),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def _prune_details(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_turn_details"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        max_entries = self.max_entries * 4
        while entries > max_entries or total_bytes > self.max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, engine, source_token, turn_id, payload_bytes
                FROM history_turn_details ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_turn_details
                WHERE session_id=? AND engine=? AND source_token=? AND turn_id=?
                """,
                tuple(victim[key] for key in (
                    "session_id", "engine", "source_token", "turn_id")),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def _prune_image_assets(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_image_assets"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        max_entries = self.max_entries * 8
        max_bytes = max(1024 * 1024, self.max_bytes // 2)
        while entries > max_entries or total_bytes > max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, engine, source_token, turn_id,
                       image_id, variant, payload_bytes
                FROM history_image_assets ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_image_assets
                WHERE session_id=? AND engine=? AND source_token=?
                  AND turn_id=? AND image_id=? AND variant=?
                """,
                tuple(victim[key] for key in (
                    "session_id", "engine", "source_token", "turn_id",
                    "image_id", "variant")),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def _prune_agent_details(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(payload_bytes), 0) AS bytes "
            "FROM history_agent_details"
        ).fetchone()
        entries = int(row["entries"])
        total_bytes = int(row["bytes"])
        max_entries = self.max_entries * 4
        while entries > max_entries or total_bytes > self.max_bytes:
            victim = connection.execute(
                """
                SELECT session_id, source_token, run_id, payload_bytes
                FROM history_agent_details ORDER BY accessed_at ASC LIMIT 1
                """
            ).fetchone()
            if victim is None:
                break
            connection.execute(
                """
                DELETE FROM history_agent_details
                WHERE session_id=? AND source_token=? AND run_id=?
                """,
                (victim["session_id"], victim["source_token"], victim["run_id"]),
            )
            entries -= 1
            total_bytes -= int(victim["payload_bytes"])

    def invalidate_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM history_pages WHERE session_id=?", (session_id,))
            connection.execute(
                "DELETE FROM history_turn_details WHERE session_id=?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM history_image_assets WHERE session_id=?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM history_agent_details WHERE session_id=?",
                (session_id,),
            )

    def close(self) -> None:
        """Compatibility hook for WrapperMachine shutdown.

        The store uses operation-scoped connections, so there is no live handle
        to close.  Keeping an explicit hook makes future connection pooling safe.
        """
