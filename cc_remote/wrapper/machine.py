"""The wrapper brain: a pool of per-session contexts + per-turn consumers.

Each SessionContext owns one cc subprocess (SdkHandle) plus its conversation
state (ring buffer, seq, state machine, turn task, translator, pending asks,
emit lock). The machine holds a pool `dict[key, SessionContext]` and a
`focused_sid`; relay/transport are singletons multiplexed by the `sid` field.

Per ctx, the drain contract is unchanged from the single-session design: one
async turn task per query; its consumer always runs to the terminal
ResultMessage (normal `success` or interrupted `error_during_execution`) before
that ctx's state returns to idle. interrupt() sets state=interrupting and the
SAME consumer keeps iterating until the terminal ResultMessage (the drain). A
drain timeout force-reconnects that ctx's SDK.

Reader/queue split: a background task iterates the SDK's async generator
WITHOUT asyncio.wait_for — wrapping __anext__ in wait_for corrupts the generator
when the short poll times out. The turn reads from an asyncio.Queue instead,
and cancelling queue.get (for the drain timeout) is safe and corrupts nothing.

Multi-session model:
- Routing identity is `ctx.key` — the real sid once known, else a temp
  `tmp-<uuid>` for a brand-new session. It equals the pool dict key and is what
  every emit stamps as `sid`, so a pre-capture new session's frames route to the
  right client runtime instead of leaking into whatever is focused.
- Switching the viewed session is FOCUS ONLY (SessionFocus) — no disconnect, so
  the previously-viewed session's turn keeps streaming in the background.
- When a new session captures its real cc id mid-turn, that is a re-key
  (SessionRekey: rename tmp-key -> sid), NOT a focus change — else a background
  session's capture would steal the user's view.
- Concurrency cap `max_concurrent_sessions`: over the cap, evict an idle,
  non-focused session (tear down its subprocess; the client keeps its runtime
  and re-spawns on re-focus). Reject only if ALL resident sessions are running.
- Token-aware: focus switching never reconnects a resident session; resume
  (cold prompt cache = full context re-send) happens ONLY on first spawn or on
  re-focus-after-eviction. Raising the cap trades RAM for fewer cold re-sends —
  don't evict sessions you're actively bouncing between.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import hashlib
import io
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path
from uuid import uuid4
from typing import Optional

from claude_agent_sdk import (
    PermissionResultAllow, PermissionResultDeny, delete_session,
    fork_session, get_session_info, get_session_messages, list_sessions,
    rename_session, tag_session,
)
from claude_agent_sdk.types import (
    HookEventMessage, ResultMessage, TaskNotificationMessage,
    TaskProgressMessage, TaskStartedMessage, TaskUpdatedMessage,
)

from cc_remote.attachments import (
    MAX_IMAGE_PIXELS,
    decode_attachment,
    image_dimensions,
    validate_attachments,
)
from cc_remote.claude_paths import claude_config_dir
from cc_remote.config import WrapperConfig
from cc_remote.codex_daemon_restart import (
    CodexDaemonRestartState,
    read_restart_state,
    restart_state_is_stale,
    restart_state_path,
)
from cc_remote.log import logger
from cc_remote.workspaces import WorkStores
from cc_remote.protocol import (
    ASK_OPTION_MAX_COUNT, ARTIFACT_PREVIEW_MAX_BYTES, FILE_PREVIEW_MAX_BYTES,
    MAX_QUERY_QUEUE_BYTES, MAX_QUERY_QUEUE_ITEMS, PREVIEW_ASSET_MAX_BYTES,
    Error, Hello, Query, QueryQueueState, QueuedQueryDetail, QueuedQueryInfo,
    QueuedQueryUpdated,
    Interrupt, CommandAck, Model, Models, EngineCapabilities, Effort, Fast,
    CollaborationMode, Perm, PermissionProfile, PermissionProfiles, WebSearch,
    BtwOpened, ContextReport, StatusReport, Notice,
    RateLimitUpdate, DiffReport, FilePreview, FileSaveResult, PreviewAsset,
    ConversationTurn, History, TurnDetail, HistoryImage,
    HistoryInvalidated, ArtifactInvalidated, AskUser, AskUserClosed,
    GoalState, Snapshot, StateEvent, State, TakeoverState, SessionControl,
    UserMsg, TurnSteered,
    ToolUse, ToolResult, TurnBinding, TurnEnd, TurnNotificationContext,
    TurnResult, is_downstream,
    is_reliable_command,
    SessionInfo, SessionList, SessionActivity, ListSessions, SessionFocus,
    SessionRekey, SessionForked, SessionMigrated, DirList,
    WorkDashboard, WorkArtifacts, RollbackResult,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL, ERR_INVALID_CWD, ERR_AUTH, ERR_PROTOCOL,
    ERR_FORK_RECONCILING, ERR_NOT_STEERABLE, ERR_STEER_UNKNOWN,
    ERR_QUEUE_FULL,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.session_pins import SessionPinStore, SessionPinStoreError
from cc_remote.wrapper.claude_controls import (
    ClaudeControlStore,
    ClaudeControlStoreError,
    ClaudeControls,
    last_completed_assistant_controls,
    valid_claude_model,
)
from cc_remote.wrapper.codex_controls import (
    CODEX_WEB_SEARCH_MODES,
    CodexControls,
    CodexControlStore,
    CodexControlStoreError,
)
from cc_remote.wrapper.codex_daemon import CodexDaemonManager
from cc_remote.claude_broker import BrokerClient, BrokerClientError
from cc_remote.wrapper.claude_broker_handle import ClaudeBrokerHandle
from cc_remote.wrapper.claude_broker_history import (
    claude_broker_tail_state,
    parse_claude_broker_lifecycle,
)
from cc_remote.wrapper.ask import make_ask_server
from cc_remote.wrapper.claude_questions import (
    AskCancelled,
    AskSuperseded,
    AskTimeout,
    AskUnavailable,
    normalize_claude_questions,
)
from cc_remote.wrapper.sdk import (
    CLAUDE_DEFAULT_EFFORT,
    CLAUDE_DEFAULT_MODEL,
    SdkHandle,
)
from cc_remote.wrapper.claude_rewind import ClaudeRewindError
from cc_remote.wrapper.rollback_commands import (
    RollbackCommandJournal,
    RollbackJournalError,
)
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.session_ctx import CodexGoalMutation, SessionContext
from cc_remote.wrapper.history_store import (
    HistoryIndexStore,
    HistorySourceFingerprint,
    MaterializedHistoryPage,
    history_image_from_events,
    materialize_history_turns,
)
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_session_id, extract_model,
    translate_history, last_assistant_model, transcript_internal_user_events,
    transcript_timestamps, transcript_path,
    translate_subagent_history, merge_subagent_history,
)
from cc_remote.wrapper.codex_handle import (
    CodexAppServerError, CodexHandle, CodexManagedOverflow,
    CodexNoActiveTurnError, CodexNoActiveTurnFence,
    CodexSteerOutcomeUnknown,
    CodexSpontaneousClosed, CodexSpontaneousOverflow, CodexSteerFence,
)
from cc_remote.wrapper.codex_turn_leases import CodexTurnLeaseStore
from cc_remote.wrapper.codex_permissions import codex_permission_profiles
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_session_id, is_turn_terminal,
    codex_history_boundary_user, codex_history_window,
    codex_native_rollback_turns,
    codex_translate_history,
)
from cc_remote.wrapper.codex_sessions import (
    list_codex_sessions, codex_session_cwd, codex_rollout_path, codex_model,
    codex_session_settings,
)
from cc_remote.wrapper.codex_models import codex_catalog, clamp_effort
from cc_remote.wrapper.codex_rpc import (
    CodexRpcOutcomeUnknown, CodexRpcRejected, codex_rpc,
)
from cc_remote.wrapper.engine_capabilities import (
    engine_capabilities, manage_engine_plugin, manage_engine_skill,
    manage_engine_hook,
)
from cc_remote.wrapper.git_diff import (
    bounded_process_output,
    read_git_diff,
)
from cc_remote.wrapper.source_fetch import capture_public_source
from cc_remote.wrapper.work_context import (
    recover_work_context_baseline,
    work_context_metrics,
)
from cc_remote.wrapper.codex_worktrees import (
    WorktreeError, prepare_worktree, rollback_worktree,
)
from cc_remote.wrapper.codex_checkpoints import (
    CheckpointConflict, CheckpointError, CodexCheckpointJournal,
    NotGitWorkspaceError,
)
from cc_remote.wrapper.codex_forks import (
    CodexForkJournal, ForkJournalError, find_rollout_fork,
    fork_thread_source,
)
from cc_remote.wrapper.claude_forks import (
    ClaudeForkJournal, ClaudeForkJournalError, claude_fork_marker,
    find_claude_fork,
)
from cc_remote.wrapper.claude_external import (
    claude_session_holders,
    classify_claude_growth,
)
from cc_remote.wrapper.codex_external import (
    CodexTuiLogTracker, HolderScan,
    parse_turn_markers,
    writable_rollout_holders,
)
from cc_remote.wrapper.process_scan import (
    ProcessIdentity,
    process_identity,
    process_owner_uid,
)
from cc_remote.wrapper.command_router import CommandRouter, UNHANDLED_COMMAND
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper.machine")

CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})
CODEX_PERMISSION_MODES = frozenset({"never", "on-request", "untrusted"})
CODEX_COLLABORATION_MODES = frozenset({"default", "plan"})
CODEX_FAST_SERVICE_TIERS = frozenset({"fast", "priority"})
_CLAUDE_OPUS_5_1M_ALIASES = frozenset({
    "opus",
    "opus[1m]",
    "claude-opus-5",
    "claude-opus-5[1m]",
})


def _normalize_claude_new_session_model(model: Optional[str]) -> Optional[str]:
    """Pin only fresh-session Opus 5 aliases to the explicit 1M model."""
    if model is None:
        return None
    if model.strip().lower() in _CLAUDE_OPUS_5_1M_ALIASES:
        return CLAUDE_DEFAULT_MODEL
    return model


CODEX_ACCOUNT_SWITCH_CONTINUATION = """\
<codex_internal_context source="cc_remote_account_switch">
The previous turn was interrupted only because the authenticated Codex account
changed. Continue the same user task from the durable conversation and workspace
state. Do not repeat work that is already complete. Do not discuss the account
switch unless it prevents completion.
</codex_internal_context>"""
_CODEX_DAEMON_UNMARKED_EPOCH = "unmarked"


def _codex_fast_on(value: Optional[str]) -> bool:
    """0.144.1 accepts ``fast`` but reports the persisted tier as ``priority``."""
    return value in CODEX_FAST_SERVICE_TIERS


def _codex_terminal_status(message: dict) -> str:
    params = message.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    status = turn.get("status") if isinstance(turn, dict) else None
    return status if isinstance(status, str) and status else "completed"


def _codex_success_terminal(message: dict, fallback_turn_id: str) -> TurnEnd:
    params = message.get("params")
    turn = params.get("turn") if isinstance(params, dict) else None
    turn = turn if isinstance(turn, dict) else {}
    turn_id = turn.get("id")
    if not isinstance(turn_id, str) or not turn_id:
        turn_id = fallback_turn_id
    duration = turn.get("durationMs")
    try:
        duration_ms = max(0, int(duration or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    return TurnEnd(
        result=TurnResult(
            subtype="success", duration_ms=duration_ms, is_error=False),
        turn_id=turn_id,
    )


def _codex_user_message_identity(
    message: dict,
) -> tuple[str, str] | None:
    """Return the official client/turn identity for one userMessage item."""
    if message.get("method") not in {"item/started", "item/completed"}:
        return None
    params = message.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict) or item.get("type") != "userMessage":
        return None
    client_id = item.get("clientId")
    turn_id = params.get("turnId")
    if (
        not isinstance(client_id, str)
        or not client_id
        or len(client_id) > 128
        or not isinstance(turn_id, str)
        or not turn_id
        or len(turn_id) > 128
    ):
        return None
    return client_id, turn_id


def _turn_detail_event_group(event: dict) -> str | None:
    """Return the display-block identity used for intra-turn pagination."""
    event_type = event.get("type")
    if event_type in {"assistant_msg_start", "delta", "assistant_msg_end"}:
        identity = event.get("message_id")
        return f"message:{identity}" if isinstance(identity, str) else None
    if event_type in {"tool_use", "tool_delta", "tool_result"}:
        identity = event.get("tool_use_id")
        return f"tool:{identity}" if isinstance(identity, str) else None
    if event_type in {"process", "turn_plan", "turn_diff"}:
        identity = event.get("item_id")
        return f"item:{identity}" if isinstance(identity, str) else None
    # Control/user/terminal frames reconstruct the turn envelope but do not
    # consume one visible process slot.
    return None


def _coalesce_turn_detail_group(rows: list[dict]) -> list[dict]:
    """Fold repeated live snapshots without changing the rendered block.

    A display block is the atomic pagination unit.  Some app-server activities
    emit thousands of snapshots for one item id; replaying every snapshot makes
    that single atomic group larger than the WebSocket frame even though the UI
    ultimately retains only the last merged block.
    """
    if not rows:
        return []
    event_types = {row.get("type") for row in rows}
    if event_types == {"process"}:
        merged = dict(rows[0])
        for row in rows[1:]:
            append_to = row.get("append_to")
            delta = row.get("delta")
            if isinstance(append_to, str) and isinstance(delta, str):
                field = "progress" if append_to == "progress" else append_to
                current = merged.get(field)
                merged[field] = (
                    (current if isinstance(current, str) else "") + delta
                )
            else:
                for key, value in row.items():
                    if value is not None and key not in {"append_to", "delta"}:
                        merged[key] = value
        merged["append_to"] = None
        merged["delta"] = None
        return [merged]
    if event_types == {"turn_plan"} or event_types == {"turn_diff"}:
        return [dict(rows[-1])]
    if event_types <= {
        "assistant_msg_start", "delta", "assistant_msg_end",
    }:
        start = next(
            (dict(row) for row in rows
             if row.get("type") == "assistant_msg_start"),
            None,
        )
        deltas = [
            row.get("text") for row in rows
            if row.get("type") == "delta"
            and isinstance(row.get("text"), str)
        ]
        end = next(
            (dict(row) for row in reversed(rows)
             if row.get("type") == "assistant_msg_end"),
            None,
        )
        result: list[dict] = []
        if start is not None:
            result.append(start)
        if deltas:
            template = next(
                dict(row) for row in rows if row.get("type") == "delta")
            template["text"] = "".join(deltas)
            result.append(template)
        if end is not None:
            result.append(end)
        return result
    if event_types <= {"tool_use", "tool_delta", "tool_result"}:
        result = []
        use = next(
            (dict(row) for row in rows if row.get("type") == "tool_use"),
            None,
        )
        if use is not None:
            result.append(use)
        deltas: dict[str, dict] = {}
        delta_order: list[str] = []
        for row in rows:
            if row.get("type") != "tool_delta":
                continue
            stream = row.get("stream")
            if not isinstance(stream, str):
                continue
            if stream not in deltas:
                deltas[stream] = dict(row)
                deltas[stream]["delta"] = ""
                delta_order.append(stream)
            value = row.get("delta")
            if isinstance(value, str):
                deltas[stream]["delta"] += value
        result.extend(deltas[stream] for stream in delta_order)
        completed = next(
            (dict(row) for row in reversed(rows)
             if row.get("type") == "tool_result"),
            None,
        )
        if completed is not None:
            result.append(completed)
        return result
    # Mixed legacy groups are uncommon and already bounded per event. Preserve
    # their exact order; the final budget guard below still prevents an
    # oversized frame.
    return [dict(row) for row in rows]


def _bound_turn_detail_group(rows: list[dict], max_bytes: int) -> list[dict]:
    """Last-resort field compaction for one otherwise oversized atomic group."""
    if len(json.dumps(
        rows, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) <= max_bytes:
        return rows
    bounded = []
    for row in rows:
        item = dict(row)
        if item.get("type") == "tool_use":
            item["input"] = {
                "_cc_remote_notice": "工具输入过大，已在此详情页截断",
            }
        elif item.get("type") == "process" and item.get("input") is not None:
            item["input"] = {
                "_cc_remote_notice": "处理输入过大，已在此详情页截断",
            }
        for key in (
            "text", "delta", "content", "summary", "detail", "output",
            "diff", "progress", "command", "explanation",
        ):
            value = item.get(key)
            if isinstance(value, str) and len(value) > 256 * 1024:
                item[key] = value[:256 * 1024] + "\n…（内容过大，已截断）"
                item["truncated"] = True
        bounded.append(item)
    if len(json.dumps(
        bounded, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) <= max_bytes:
        return bounded

    template = rows[-1]
    base = {
        key: template[key] for key in ("v", "ts", "sid", "seq")
        if key in template
    }
    message = "此处理项本身超过单帧上限，已在本页显式截断。"
    group = _turn_detail_event_group(template)
    if group and group.startswith("message:"):
        message_id = group.removeprefix("message:")
        return [
            {**base, "type": "assistant_msg_start",
             "message_id": message_id, "channel": "unknown"},
            {**base, "type": "delta", "message_id": message_id,
             "text": message, "channel": "unknown"},
            {**base, "type": "assistant_msg_end",
             "message_id": message_id, "channel": "unknown"},
        ]
    if group and group.startswith("tool:"):
        tool_id = group.removeprefix("tool:")
        return [
            {**base, "type": "tool_use", "message_id": tool_id,
             "tool_use_id": tool_id, "tool": "oversized_detail",
             "input": {"notice": message}},
            {**base, "type": "tool_result", "tool_use_id": tool_id,
             "content": message, "is_error": False, "truncated": True},
        ]
    item_id = (
        group.removeprefix("item:")
        if group and group.startswith("item:")
        else f"oversized-detail-{hashlib.sha256(message.encode()).hexdigest()[:16]}"
    )
    return [{
        **base,
        "type": "process",
        "item_id": item_id,
        "kind": "task",
        "phase": "snapshot",
        "status": "succeeded",
        "title": "处理项过大",
        "summary": message,
        "truncated": True,
    }]


def _turn_detail_page(
    rows: tuple[dict, ...] | list[dict],
    *,
    before: str | None,
    limit: int,
    max_bytes: int | None = None,
) -> tuple[list[dict], bool, str | None, bool, str | None]:
    """Select a source-ordered, block-complete detail window."""
    grouped_rows: dict[str, list[dict]] = {}
    for row in rows:
        group = _turn_detail_event_group(row)
        if group is not None:
            grouped_rows.setdefault(group, []).append(row)
    coalesced_groups = {
        group: _bound_turn_detail_group(
            _coalesce_turn_detail_group(group_rows),
            max_bytes or 8 * 1024 * 1024,
        )
        for group, group_rows in grouped_rows.items()
    }

    event_groups: list[str | None] = []
    wire_rows: list[dict] = []
    ordered_groups: list[str] = []
    seen: set[str] = set()
    group_bytes: dict[str, int] = {}
    envelope_bytes = 0
    emitted_groups: set[str] = set()
    source_rows: list[dict] = []
    for row in rows:
        group = _turn_detail_event_group(row)
        if group is None:
            source_rows.append(row)
        elif group not in emitted_groups:
            emitted_groups.add(group)
            source_rows.extend(coalesced_groups[group])
    for row in source_rows:
        group = _turn_detail_event_group(row)
        event_groups.append(group)
        wire_row = {
            **row,
            # Conversation images already have source-bound thumbnail/full
            # endpoints. Never put their base64 bodies back into a heavy detail
            # frame; the summary row retains canonical imageRefs in the browser.
            **({"images": None} if row.get("type") == "user_msg"
               and row.get("images") else {}),
        }
        wire_rows.append(wire_row)
        row_bytes = len(json.dumps(
            wire_row, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if group is None:
            envelope_bytes += row_bytes
        else:
            group_bytes[group] = group_bytes.get(group, 0) + row_bytes
        if group is not None and group not in seen:
            seen.add(group)
            ordered_groups.append(group)

    total = len(ordered_groups)
    requested_end: int | None = None
    if before is not None:
        if not before.isascii() or not before.isdigit():
            raise ValueError("invalid turn detail cursor")
        requested_end = int(before)
        if requested_end < 0 or requested_end > total:
            raise ValueError("turn detail cursor is out of range")
    page_limit = max(1, limit)

    def page_start(end: int) -> int:
        start = end
        page_bytes = envelope_bytes
        while start > 0 and end - start < page_limit:
            candidate = ordered_groups[start - 1]
            candidate_bytes = group_bytes.get(candidate, 0)
            if (max_bytes is not None and start < end
                    and page_bytes + candidate_bytes > max_bytes):
                break
            start -= 1
            page_bytes += candidate_bytes
        return start

    # Derive one canonical page chain from newest to oldest. Byte-bounded pages
    # can contain fewer than ``limit`` groups, so adding ``limit`` to the current
    # cursor can skip a whole intermediate page. Only advertise exact adjacent
    # boundaries from this chain in either direction.
    boundaries = [total]
    while boundaries[-1] > 0:
        boundaries.append(page_start(boundaries[-1]))
    end = total if requested_end is None else requested_end
    try:
        boundary_index = boundaries.index(end)
    except ValueError as exc:
        raise ValueError("turn detail cursor is not a page boundary") from exc
    start = (
        boundaries[boundary_index + 1]
        if boundary_index + 1 < len(boundaries)
        else end
    )
    selected = set(ordered_groups[start:end])
    page = [
        dict(row) for row, group in zip(wire_rows, event_groups)
        if group is None or group in selected
    ]
    has_more = start > 0
    oldest_cursor = str(start) if has_more else None
    has_newer = boundary_index > 0
    newer_cursor = (
        str(boundaries[boundary_index - 1]) if has_newer else None
    )
    return page, has_more, oldest_cursor, has_newer, newer_cursor


def _render_history_image(
    image: dict,
    variant: str,
) -> tuple[str, int, int, bytes]:
    """Validate and optionally thumbnail one indexed historical image."""
    media_type = image.get("media_type")
    encoded = image.get("data")
    if not isinstance(media_type, str) or not isinstance(encoded, str):
        raise ValueError("历史图片数据无效")
    if validate_attachments([image], None) is not None:
        raise ValueError("历史图片超出安全限制")
    raw = decode_attachment(encoded)
    dimensions = image_dimensions(raw, media_type)
    if dimensions is None:
        raise ValueError("历史图片格式无效")
    width, height = dimensions
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("历史图片尺寸过大")
    normalized = "image/jpeg" if media_type == "image/jpg" else media_type
    if variant == "full":
        return normalized, width, height, raw

    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(io.BytesIO(raw)) as source:
        if int(getattr(source, "n_frames", 1)) != 1:
            raise ValueError("动态图片暂不支持预览")
        source.load()
        source.thumbnail((360, 360), Image.Resampling.LANCZOS)
        if source.mode not in {"RGB", "RGBA"}:
            source = source.convert("RGBA" if "transparency" in source.info else "RGB")
        output = io.BytesIO()
        source.save(
            output,
            format="WEBP",
            lossless=normalized == "image/png",
            quality=82,
            method=4,
        )
        thumb = output.getvalue()
        if len(thumb) > 512 * 1024:
            source.thumbnail((240, 240), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            source.convert("RGB").save(
                output, format="WEBP", quality=70, method=4)
            thumb = output.getvalue()
        return "image/webp", source.width, source.height, thumb


def _session_permission_mode(ctx: SessionContext) -> str:
    """Return the permission mode actually configured on this live engine."""
    if ctx.engine == "codex":
        return getattr(ctx.sdk, "approval", "never")
    return getattr(ctx.sdk, "permission_mode", "bypassPermissions")


def _session_permission_profile(ctx: SessionContext) -> Optional[str]:
    """Return the official active Codex named profile, when available."""
    if ctx.engine != "codex":
        return None
    value = getattr(ctx.sdk, "permission_profile", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _session_web_search(ctx: SessionContext) -> Optional[str]:
    if ctx.engine != "codex":
        return None
    value = getattr(ctx.sdk, "web_search", None)
    return value if value in CODEX_WEB_SEARCH_MODES else None


def _session_model(ctx: SessionContext) -> Optional[str]:
    """Return the live engine's authoritative per-session model, if known."""
    value = getattr(ctx.sdk, "model", None) or ctx.announced_model
    return value.strip() if isinstance(value, str) and value.strip() else None


def _session_effort(ctx: SessionContext) -> Optional[str]:
    """Return the live engine's desired reasoning strength, if known."""
    value = getattr(ctx.sdk, "effort", None) or ctx.announced_effort
    return value.strip() if isinstance(value, str) and value.strip() else None


def _codex_list_state(status: Optional[str]) -> Optional[State]:
    if status == "active":
        return "running"
    if status == "idle":
        return "idle"
    return None


class _BtwSpawnFailure(Exception):
    """Expected /btw rejection that must be correlated and ACKed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _SpawnFailure(Exception):
    """Synchronous new-session failure routed only to its initiating client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _ForkOutcomeUncertain(RuntimeError):
    """A persistent fork may have committed and must not be ACKed/replayed."""


class _FileRevisionConflict(RuntimeError):
    """The Markdown file changed after the editor loaded it."""

    def __init__(self, message: str, *, size: int = 0, mtime_ns: int = 0,
                 revision: Optional[str] = None):
        super().__init__(message)
        self.size = size
        self.mtime_ns = mtime_ns
        self.revision = revision


class WrapperMachine:
    # Browser/TUI outboxes hold at most 256 commands. Keep a 2x retry window per
    # client and at most the relay's configured maximum of 64 client identities;
    # cached one-shot responses must not become an unbounded second history store.
    COMMAND_IDS_PER_CLIENT = 512
    COMMAND_CLIENTS = 64
    COMMAND_RESPONSE_BYTES = 24 * 1024 * 1024
    SESSION_ALIAS_CAP = 256
    SESSION_ALIAS_TTL = 7 * 24 * 3600
    SESSION_ALIAS_FILE_MAX_BYTES = 2 * 1024 * 1024
    # A worst-case 4096-byte cwd can expand ~6x under JSON escaping. 64 entries
    # therefore still fit the 2 MiB state-file cap while matching the maximum
    # number of resident sessions allowed by config validation.
    PRIVATE_BTW_CAP = 64
    PRIVATE_BTW_FILE_MAX_BYTES = 2 * 1024 * 1024
    PREVIEW_EXTERNAL_PATH_CAP = 256
    PREVIEW_WRITE_CANDIDATE_CAP = 64
    NOTIFICATION_TITLE_CAP = 512
    NOTIFICATION_TITLE_LENGTH = 120
    PREVIEW_WRITE_TOOLS = frozenset({
        "write", "edit", "multiedit", "notebookedit", "editfile",
        "apply_patch", "filechange",
    })
    CLAUDE_SETTINGS_MAX_BYTES = 1024 * 1024
    BG_JOB_SCAN_MAX = 1_000
    BG_JOB_STATE_MAX_BYTES = 64 * 1024
    FORK_RECONCILE_ATTEMPTS = 4
    FORK_RECONCILE_DELAY = 0.1
    FORK_BACKGROUND_ATTEMPTS = 100
    UNCERTAIN_FORK_CAP = 4096
    MARKDOWN_PREVIEW_SUFFIXES = frozenset({".md", ".markdown"})
    HTML_PREVIEW_SUFFIXES = frozenset({".htm", ".html", ".svg"})
    OFFICE_PREVIEW_SUFFIXES = frozenset({
        ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods",
        ".ppt", ".pptx", ".odp",
    })
    OFFICE_PREVIEW_INPUT_MAX_BYTES = 32 * 1024 * 1024
    OFFICE_PREVIEW_TIMEOUT_SECONDS = 45
    HISTORY_REFRESH_MIN_INTERVAL_SECONDS = 1.0
    HISTORY_REFRESH_MAX_INTERVAL_SECONDS = 10.0
    PREVIEW_ASSET_MEDIA_TYPES = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".avif": "image/avif",
    }
    ARTIFACT_PREVIEW_MEDIA_TYPES = {
        **PREVIEW_ASSET_MEDIA_TYPES,
        ".pdf": "application/pdf",
    }
    SAFE_RETRY_COMMANDS = frozenset({
        "list_sessions", "get_history", "get_turn_detail", "get_history_image",
        "get_models", "get_permission_profiles", "get_engine_capabilities",
        "get_context", "get_status", "get_diff", "get_file_preview",
        "get_preview_asset", "get_goal", "get_queued_query", "list_dir",
        "get_work_dashboard",
    })
    # Commands whose target is a runtime ``sid``.  A /btw runtime is private to
    # the client that created it, so every operation against that sid must pass
    # the owner check before its handler is allowed to read or mutate state.
    BTW_SID_COMMANDS = frozenset({
        "query", "cancel_queued_query", "get_queued_query",
        "update_queued_query", "steer", "interrupt", "takeover",
        "set_model", "set_effort",
        "set_service_tier", "set_collaboration_mode", "open_btw", "close_btw",
        "set_perm", "get_permission_profiles", "set_permission_profile",
        "set_web_search",
        "get_context", "get_status", "get_diff", "get_file_preview", "save_markdown",
        "get_preview_asset", "answer_question", "get_goal", "set_goal", "clear_goal",
    })
    # These commands address a session through ``session_id`` instead.
    BTW_SESSION_COMMANDS = frozenset({
        "get_history", "get_turn_detail", "get_history_image", "switch_session",
        "rename_session", "archive_session", "pin_session",
        "delete_work_session", "delete_session", "rollback_session",
        "compact_session", "start_review",
        "fork_session", "fork_session_worktree", "migrate_session",
    })

    def __init__(self, cfg: WrapperConfig, transport: WrapperTransport):
        self.cfg = cfg
        self.transport = transport
        self._command_router = CommandRouter(self)
        self.instance_id = uuid4().hex
        # One lifecycle gate coordinates the official process-global Codex
        # daemon. Each resident Code handle still owns only its short-lived
        # proxy connection; Work remains per-session stdio and isolated.
        self._codex_daemon = CodexDaemonManager(
            getattr(cfg, "codex_daemon_mode", "auto"))
        self._codex_daemon_restart_path = restart_state_path(cfg.state_dir)
        self._codex_turn_leases = CodexTurnLeaseStore(cfg.state_dir)
        self._claude_broker = BrokerClient(
            getattr(cfg, "claude_broker_socket", None))
        # Claude's official SDK/CLI does not expose a supported multi-writer
        # control plane. Keep the PTY broker code available for development,
        # but never discover or adopt it in the customer path by default.
        self._claude_broker_enabled = bool(getattr(
            cfg, "experimental_claude_broker", False))
        # A History token changes for every wrapper process and every local
        # destructive conversation mutation.  Browsers persist this token with
        # IndexedDB turns, so a fresh wrapper can never merge a pre-crash cache
        # over its authoritative transcript.  The per-process generation also
        # makes a crash after native rollback safe without another disk journal.
        self._history_revision_epochs: dict[str, int] = {}
        # SessionContext objects are evicted and recreated while this wrapper
        # generation stays alive. Keep each sid's control revision outside the
        # resident context so a rebuilt Snapshot cannot move backwards and be
        # rejected by a browser that still holds the previous control watermark.
        self._control_revision_epochs: dict[str, int] = {}
        self._preview_conversion_limit = asyncio.Semaphore(2)
        # Pool of resident sessions, keyed by real session_id (or a `tmp-<uuid>`
        # temp key for a brand-new session until its id is captured).
        self.sessions: dict[str, SessionContext] = {}
        self.focused_sid: Optional[str] = None  # pool key of the viewed session
        self.transport.on_connected = self._on_transport_connected
        # Transcript mirror: sessions a client has opened (registered on GetHistory),
        # sid -> {"path", "size", "engine"}. The watcher polls each file's SIZE and,
        # when it grows without us having written it, mirrors the append to clients.
        self._watch: dict[str, dict] = {}
        # Sidebar activity watches are catalog-only and may be discarded before
        # explicit History/resident watches.  Keep them bounded so a long-lived
        # wrapper never accumulates one transcript watcher per historical thread.
        self._codex_sidebar_watches: OrderedDict[str, None] = OrderedDict()
        # Display metadata is populated by catalog reads and successful local
        # mutations. Keep it bounded and memory-only: transcripts remain the
        # authority, and a terminal event must never synchronously reread a
        # multi-gigabyte history merely to format an OS notification.
        self._notification_titles: OrderedDict[str, str] = OrderedDict()
        self._watch_task: Optional[asyncio.Task] = None
        self._work_schedule_task: Optional[asyncio.Task] = None
        self._work_schedule_runs: set[asyncio.Task] = set()
        self._codex_watch_lock = asyncio.Lock()
        self._codex_probe_warned = False
        self._codex_tui_log_tracker = CodexTuiLogTracker()
        self._claude_probe_warned = False
        # ``claude -c`` selects the cwd's latest conversation only once at
        # process startup. Keep that exact pid+start-ticks assignment across
        # later transcript writes so the terminal cannot appear to jump between
        # same-cwd sessions while it remains alive.
        self._claude_continue_bindings: dict[ProcessIdentity, str] = {}
        # A `-c` process may select a native session that no browser has watched
        # yet. Cache that startup result separately; it becomes an ownership
        # binding only after the exact sid enters the watched set.
        self._claude_continue_candidates: dict[ProcessIdentity, str] = {}
        # Newest-page History builds can race the watcher, another client, and
        # live stream events. Sequence them per session so browsers can discard
        # an older build that completes after a newer one. Pagination echoes the
        # current sequence instead of advancing it.
        self._history_build_sequences: dict[str, int] = {}
        # Code and Work are two filtered views over the same native Codex
        # catalog. The browser warms both back-to-back, so briefly reuse one
        # authoritative read instead of starting app-server twice.
        self._codex_session_list_cache: tuple[float, list[dict]] | None = None
        self._codex_session_list_refresh_task: Optional[asyncio.Task] = None
        self._codex_session_list_epoch = 0
        self._codex_session_list_refresh_epoch = -1
        # Catalog reads must never hold the serial command lane: a cold Codex
        # app-server startup can take tens of seconds on a very large store.
        self._session_list_command_tasks: set[asyncio.Task] = set()
        # Transcript/rollout parsing is read-only but can still take seconds for
        # a large session. Keep it off the serial query/interrupt lane and
        # coalesce reliable reconnect retries by their stable command id.
        self._history_command_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        self._history_page_tasks: dict[
            tuple[str, str, int, str], asyncio.Task
        ] = {}
        self._history_refresh_tasks: dict[
            tuple[str, str, int, str], asyncio.Task
        ] = {}
        self._history_refresh_dirty: set[tuple[str, str, int, str]] = set()
        # Rebuildable local projection of already-translated transcript pages.
        # Raw transcripts remain authoritative; exact source fingerprints make
        # the derived SQLite row a safe fast path rather than another history.
        try:
            self._history_index: HistoryIndexStore | None = HistoryIndexStore(
                Path(cfg.state_dir))
        except Exception as exc:
            self._history_index = None
            log.warning("history index unavailable", error=str(exc))
        # In-memory at-most-once window for client retries. The outer and inner
        # OrderedDicts are both bounded; wrapper process restart intentionally
        # resets this window (documented residual risk, not durable exactly-once).
        self._processed_commands: OrderedDict[
            str, OrderedDict[str, tuple[object, ...]]
        ] = OrderedDict()
        self._processed_command_sizes: dict[tuple[str, str], int] = {}
        self._processed_command_bytes = 0
        # Deferred browser queries are owned by resident SessionContexts, while
        # these machine-wide counters prevent many clients/sessions from each
        # allocating the full advertised queue allowance.
        self._queued_query_count = 0
        self._queued_query_bytes = 0
        # Durable temp-key -> real-id recovery. SessionRekey is a control frame;
        # if its one live send is lost after NewSession was ACKed, the next Hello
        # uses this map to replay the rekey before cursor catch-up.
        self._session_aliases = self._load_session_aliases()
        # Persistent thread/fork is a mutation. Journal it independently of the
        # in-memory command ACK cache so a wrapper restart cannot duplicate a
        # fork whose response was lost.
        self._codex_forks = CodexForkJournal(self.cfg.state_dir)
        # Volatile supplement for a transient journal write failure. The rollout
        # marker remains the cross-process authority; this map prevents a same-
        # process reliable-command retry from issuing a second mutation first.
        self._uncertain_codex_forks: OrderedDict[str, Optional[str]] = OrderedDict()
        self._codex_fork_tasks: dict[str, asyncio.Task] = {}
        self._codex_fork_locks: dict[str, asyncio.Lock] = {}
        # Claude's SDK chooses the child id itself. A separate durable journal
        # plus an atomically-written temporary title marker closes the crash
        # window between ``fork_session`` creating the transcript and the
        # browser receiving its correlated result.
        self._claude_forks = ClaudeForkJournal(self.cfg.state_dir)
        self._uncertain_claude_forks: OrderedDict[str, Optional[str]] = OrderedDict()
        self._claude_fork_tasks: dict[str, asyncio.Task] = {}
        self._claude_fork_locks: dict[str, asyncio.Lock] = {}
        try:
            self._session_pins: SessionPinStore | None = SessionPinStore(
                self.cfg.state_dir)
        except SessionPinStoreError:
            self._session_pins = None
            log.exception("session pin store unavailable")
        try:
            self._claude_controls: ClaudeControlStore | None = (
                ClaudeControlStore(self.cfg.state_dir)
            )
        except ClaudeControlStoreError:
            # A malformed preference cache must never prevent the engines from
            # starting. Keep Remote usable, but do not silently trust it.
            self._claude_controls = None
            log.exception("Claude Remote control store unavailable")
        try:
            self._codex_controls: CodexControlStore | None = (
                CodexControlStore(self.cfg.state_dir)
            )
        except CodexControlStoreError:
            self._codex_controls = None
            log.exception("Codex Remote control store unavailable")
        try:
            self._rollback_commands: RollbackCommandJournal | None = (
                RollbackCommandJournal(self.cfg.state_dir)
            )
        except RollbackJournalError:
            # Corrupt/missing idempotency evidence must disable destructive
            # rollback, not the rest of Remote.
            self._rollback_commands = None
            log.exception("rollback command journal unavailable")
        # Claude fork_session writes a real transcript even though /btw is an
        # ephemeral, owner-only UI. Persist tombstones until that transcript is
        # deleted so a crash or failed cleanup cannot expose it in SessionList or
        # let another client cold-resume it as a normal session.
        self._private_btw_sessions = self._load_private_btw_sessions()
        # Catalog/default reads stay off the serial mutation/query command lane
        # so opening New Chat can never delay an immediate NewSession or Interrupt.
        # Reliable command ids coalesce retries until the original task ACKs.
        self._models_command_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        # Account/status reads may wait behind the intentional Codex daemon
        # restart barrier. Keep that wait off the serial Query/Interrupt lane;
        # reliable reconnect retries coalesce by their stable command id.
        self._status_command_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        self._capabilities_command_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # A broker-owned Claude model change can pause for a Remote answer.
        # Keep it off the serial receive lane so AnswerQuestion can be handled.
        self._interactive_control_tasks: dict[
            tuple[str, str], asyncio.Task
        ] = {}
        # Narrow test/embedded configs often omit the new Work roots. Keep those
        # stores below their temporary state_dir instead of touching the real
        # user's ~/.claude or ~/.codex during a read-only Code session listing.
        fallback_work = Path(cfg.state_dir) / "work"
        self._work = WorkStores(
            getattr(cfg, "claude_work_root", fallback_work / "claude"),
            getattr(cfg, "codex_work_root", fallback_work / "codex"),
        )

    # ---- pool helpers ----

    @classmethod
    def _clean_notification_title(cls, value: object) -> Optional[str]:
        if not isinstance(value, str):
            return None
        # Newlines become word boundaries; other control/format characters are
        # removed. A small input cap prevents a first prompt from turning title
        # formatting into another large-history path.
        cleaned = "".join(
            " " if char.isspace()
            else "" if unicodedata.category(char).startswith("C")
            else char
            for char in value[:2048]
        )
        normalized = " ".join(cleaned.split())
        return normalized[:cls.NOTIFICATION_TITLE_LENGTH].strip() or None

    def _remember_notification_title(self, sid: Optional[str], value: object) -> None:
        if not sid:
            return
        title = self._clean_notification_title(value)
        if not title:
            return
        self._notification_titles[sid] = title
        self._notification_titles.move_to_end(sid)
        while len(self._notification_titles) > self.NOTIFICATION_TITLE_CAP:
            self._notification_titles.popitem(last=False)

    def _notification_context(
        self,
        ctx: SessionContext,
    ) -> TurnNotificationContext:
        route_sid = (
            ctx.parent_sid if ctx.btw and ctx.parent_sid
            else (ctx.session_id or ctx.key)
        )
        display_name = self._notification_titles.get(route_sid or "")
        return TurnNotificationContext(
            engine="codex" if ctx.engine == "codex" else "claude",
            space="work" if ctx.space == "work" else "code",
            display_name=display_name,
            parent_session_id=ctx.parent_sid if ctx.btw else None,
        )

    def _history_revision(self, sid: str) -> str:
        return f"{self.instance_id}-{self._history_revision_epochs.get(sid, 0)}"

    def _bump_history_revision(self, sid: str) -> str:
        self._history_revision_epochs[sid] = (
            self._history_revision_epochs.get(sid, 0) + 1
        )
        history_index = getattr(self, "_history_index", None)
        if history_index is not None:
            try:
                history_index.invalidate_session(sid)
            except Exception as exc:
                log.warning(
                    "history index invalidation failed", session_id=sid,
                    error=str(exc),
                )
        return self._history_revision(sid)

    def _focused_ctx(self) -> Optional[SessionContext]:
        return self.sessions.get(self.focused_sid) if self.focused_sid else None

    def _bind_control_revision(self, ctx: SessionContext) -> None:
        """Bind one resident context to the machine-local monotonic sid epoch."""
        key = ctx.session_id or ctx.key
        if not key:
            return
        if ctx.control_revision_key != key:
            previous = self._control_revision_epochs.get(key)
            if previous is not None:
                ctx.control_revision = max(ctx.control_revision, previous + 1)
            ctx.control_revision_key = key
        previous = self._control_revision_epochs.get(key)
        if previous is None or ctx.control_revision > previous:
            self._control_revision_epochs[key] = ctx.control_revision
        elif ctx.control_revision < previous:
            # A bound context must never emit below a revision already published
            # for this sid, even if a caller restored stale in-memory state.
            ctx.control_revision = previous

    def _session_control(self, ctx: SessionContext) -> SessionControl:
        """Build the small authoritative ownership projection for Web."""
        self._bind_control_revision(ctx)
        return SessionControl(
            control_mode=ctx.control_mode,
            write_state=ctx.write_state,
            terminal_attached=ctx.terminal_attached,
            reason=ctx.control_reason,
            can_takeover=ctx.control_can_takeover,
            generation=self.instance_id,
            revision=ctx.control_revision,
        )

    async def _set_session_control(
        self,
        ctx: SessionContext,
        *,
        control_mode: str,
        write_state: str,
        terminal_attached: bool,
        reason: Optional[str] = None,
        can_takeover: bool = False,
        emit: bool = True,
    ) -> SessionControl:
        """Update one control epoch and optionally publish it.

        Revision changes are value-driven, not poll-driven. This keeps repeated
        ownership scans idempotent while letting the browser reject a delayed
        read-only frame from an older process generation.
        """
        values = (
            control_mode,
            write_state,
            bool(terminal_attached),
            reason,
            bool(can_takeover),
        )
        current = (
            ctx.control_mode,
            ctx.write_state,
            ctx.terminal_attached,
            ctx.control_reason,
            ctx.control_can_takeover,
        )
        if values != current:
            ctx.control_mode = control_mode
            ctx.write_state = write_state
            ctx.terminal_attached = bool(terminal_attached)
            ctx.control_reason = reason
            ctx.control_can_takeover = bool(can_takeover)
            self._bind_control_revision(ctx)
            ctx.control_revision += 1
            key = ctx.control_revision_key
            if key:
                self._control_revision_epochs[key] = ctx.control_revision
        snapshot = self._session_control(ctx)
        if emit and values != current:
            await self._emit(ctx, snapshot)
        return snapshot

    @staticmethod
    def _codex_shared_affinity(ctx: SessionContext) -> bool:
        """Whether a Code context belongs to the shared Codex app-server.

        ``using_daemon_proxy`` describes only the current proxy process.  A
        restarted app-server closes that process before the replacement proxy
        connects, but ownership of the thread remains shared throughout.
        """
        if ctx.engine != "codex" or ctx.space != "code":
            return False
        return bool(
            getattr(ctx.sdk, "shared_daemon_affinity", False)
            or getattr(ctx.sdk, "using_daemon_proxy", False)
        )

    @staticmethod
    def _codex_shared_live(ctx: SessionContext) -> bool:
        return bool(
            ctx.engine == "codex"
            and getattr(ctx.sdk, "using_daemon_proxy", False)
        )

    def _is_resident_context(self, ctx: SessionContext) -> bool:
        """Whether ``ctx`` still belongs to this machine's resident pool."""
        return ctx.key is not None and self.sessions.get(ctx.key) is ctx

    async def _reconnect_codex_shared(
        self,
        ctx: SessionContext,
        *,
        reason: str,
        force: bool = False,
    ) -> bool:
        """Restore one interrupted shared proxy without changing ownership."""
        if not self._codex_shared_affinity(ctx):
            return False
        if self._codex_shared_live(ctx) and not force:
            return True
        watch = self._watch.get(ctx.session_id or "")
        await self._set_session_control(
            ctx,
            control_mode="codex_shared",
            write_state="writable",
            terminal_attached=bool((watch or {}).get("holders")),
            reason="Codex 共享通道连接断开，正在重新连接",
            can_takeover=False,
        )
        try:
            await ctx.sdk.force_reconnect(
                resume_id=ctx.session_id,
                cwd=ctx.cwd,
                reason=reason,
            )
        except Exception as exc:
            log.warning(
                "Codex shared proxy reconnect failed",
                session_id=ctx.session_id,
                reason=reason,
                error_type=type(exc).__name__,
            )
            await self._set_session_control(
                ctx,
                control_mode="codex_shared",
                write_state="writable",
                terminal_attached=bool((watch or {}).get("holders")),
                reason="Codex 共享通道连接断开；下次操作会自动重试",
                can_takeover=False,
            )
            return False
        if not self._codex_shared_live(ctx):
            await self._set_session_control(
                ctx,
                control_mode="codex_shared",
                write_state="writable",
                terminal_attached=bool((watch or {}).get("holders")),
                reason="Codex 共享通道尚未恢复；下次操作会自动重试",
                can_takeover=False,
            )
            return False
        await self._sync_external_control(ctx, watch)
        return True

    async def _codex_restart_state(
        self,
        *,
        wait: bool,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[CodexDaemonRestartState]:
        """Read the hook barrier, optionally waiting for restart completion."""
        while True:
            # The marker is a bounded (4 KiB), atomically replaced local file.
            # Reading it inline avoids default-executor starvation delaying an
            # active-turn interrupt beyond the daemon's restart window.
            state = read_restart_state(self._codex_daemon_restart_path)
            if state is not None and restart_state_is_stale(state):
                # A failed worker or an abandoned restarting marker is useful
                # only until its published deadline. Afterwards the currently
                # reachable daemon becomes the baseline again; a later hook
                # writes a fresh epoch and remains observable.
                state = None
            if (
                state is None
                or state.phase != "restarting"
                or not wait
                or (interrupt_event is not None and interrupt_event.is_set())
            ):
                return state
            remaining = max(0.0, state.deadline_at - time.time())
            if remaining <= 0:
                return None
            delay = min(0.1, remaining)
            if interrupt_event is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(
                    interrupt_event.wait(),
                    timeout=delay,
                )
            except asyncio.TimeoutError:
                pass

    async def _stamp_codex_daemon_epoch(self, ctx: SessionContext) -> None:
        if not self._codex_shared_affinity(ctx):
            return
        state = await self._codex_restart_state(wait=False)
        ctx.codex_daemon_epoch = (
            state.epoch
            if state is not None and state.phase == "ready"
            else _CODEX_DAEMON_UNMARKED_EPOCH
        )

    def _claim_codex_turn(
        self,
        ctx: SessionContext,
        turn_id: str,
        msg_id: Optional[str],
        *,
        automatic: bool = False,
    ) -> None:
        session_id = ctx.session_id
        if (
            not self._codex_shared_affinity(ctx)
            or not session_id
            or not turn_id
        ):
            return
        logical_msg_id = msg_id or turn_id
        try:
            self._codex_turn_leases.claim(
                session_id,
                turn_id,
                logical_msg_id,
                daemon_epoch=ctx.codex_daemon_epoch,
                automatic=automatic,
            )
        except Exception as exc:
            # A lease failure must not abort an accepted model turn. It narrows
            # only crash recovery; live ownership remains in SessionContext.
            log.warning(
                "Codex turn lease could not be persisted",
                session_id=session_id,
                turn_id=turn_id,
                error_type=type(exc).__name__,
            )
            return
        ctx.codex_owned_turn_id = turn_id

    def _release_codex_turn(
        self, ctx: SessionContext, turn_id: Optional[str] = None,
    ) -> None:
        session_id = ctx.session_id
        owned = ctx.codex_owned_turn_id
        target = turn_id or owned
        if not session_id or not target:
            return
        try:
            released = self._codex_turn_leases.release(
                session_id, turn_id=target)
        except Exception as exc:
            log.warning(
                "Codex turn lease could not be released",
                session_id=session_id,
                turn_id=target,
                error_type=type(exc).__name__,
            )
            return
        if released and owned == target:
            ctx.codex_owned_turn_id = None

    async def _recover_codex_owned_turn(
        self, ctx: SessionContext, session_id: str,
    ) -> bool:
        """Reattach only a three-way confirmed Remote-owned daemon turn."""
        if not self._codex_shared_affinity(ctx):
            return False
        try:
            lease = self._codex_turn_leases.get(session_id)
        except Exception as exc:
            log.warning(
                "Codex turn lease could not be read",
                session_id=session_id,
                error_type=type(exc).__name__,
            )
            return False
        if lease is None:
            return False

        path = await asyncio.to_thread(codex_rollout_path, session_id)
        try:
            size = (
                await asyncio.to_thread(os.path.getsize, path)
                if path else 0
            )
        except OSError:
            size = 0
        active, _partial, last_marker = (
            await asyncio.to_thread(self._codex_tail_snapshot, path, size)
            if path else (set(), b"", None)
        )
        restart_state = await self._codex_restart_state(wait=False)
        resumable_goal = False
        if lease.automatic:
            try:
                goal = await ctx.sdk.get_goal()
            except Exception as exc:
                log.warning(
                    "Codex goal state unavailable during owned turn recovery",
                    session_id=session_id,
                    error_type=type(exc).__name__,
                )
            else:
                resumable_goal = bool(
                    isinstance(goal, dict)
                    and goal.get("status") in {"active", "usageLimited"}
                )
        generation_changed = bool(
            lease.daemon_epoch
            and restart_state is not None
            and restart_state.epoch != lease.daemon_epoch
        )
        if generation_changed:
            # connect() observes the current marker, but this leased native turn
            # still belongs to the older daemon. Preserve that generation until
            # its consumer crosses the handoff; otherwise its restart watcher
            # would compare new==new and accept the old terminal as ordinary.
            ctx.codex_daemon_epoch = lease.daemon_epoch
        handoff_after_restart = bool(
            generation_changed
            and (
                active == {lease.turn_id}
                or (
                    lease.automatic
                    and resumable_goal
                    and len(active) == 1
                )
                or last_marker in {
                    ("turn_aborted", lease.turn_id),
                    ("task_failed", lease.turn_id),
                }
                or (
                    lease.automatic
                    and resumable_goal
                    and last_marker is not None
                    and last_marker[0] in {"turn_aborted", "task_failed"}
                )
            )
        )
        if handoff_after_restart:
            # A replacement wrapper may connect while the old generation is
            # still draining, or after it has already written its terminal
            # marker.  In both cases daemon ownership proves the logical Remote
            # turn must cross the account handoff instead of becoming idle.
            ctx.codex_owned_turn_id = lease.turn_id
            ctx.codex_recovered_turn_id = lease.turn_id
            ctx.codex_recovered_msg_id = lease.msg_id
            ctx.codex_recovered_automatic = lease.automatic
            ctx.codex_spontaneous_turn_id = lease.turn_id
            announce_running = ctx.state == "idle"
            if announce_running:
                ctx.interrupt_event.clear()
                ctx.interrupt_deadline = None
                ctx.state = "running"
            task = asyncio.create_task(
                self._run_codex_spontaneous_turn(
                    ctx,
                    lease.turn_id,
                    announce_running=announce_running,
                    recovered_msg_id=lease.msg_id,
                    pending_switch=restart_state,
                )
            )
            ctx.codex_spontaneous_task = task
            ctx.codex_recovered_turn_id = None
            ctx.codex_recovered_msg_id = None
            ctx.codex_recovered_automatic = None
            log.info(
                "recovering Remote-owned Codex turn across account switch",
                session_id=session_id,
                turn_id=lease.turn_id,
                old_epoch=lease.daemon_epoch,
                new_epoch=restart_state.epoch,
                old_turn_active=active == {lease.turn_id},
            )
            return True
        rollout_matches = bool(
            active == {lease.turn_id}
            or (
                lease.automatic
                and resumable_goal
                and len(active) == 1
            )
        )
        if not rollout_matches:
            try:
                self._codex_turn_leases.release(
                    session_id, turn_id=lease.turn_id)
            except Exception as exc:
                log.warning(
                    "stale Codex turn lease could not be released",
                    session_id=session_id,
                    turn_id=lease.turn_id,
                    error_type=type(exc).__name__,
                )
            return False

        recover = getattr(ctx.sdk, "recover_owned_turn", None)
        if not callable(recover):
            return False
        ctx.codex_owned_turn_id = lease.turn_id
        ctx.codex_recovered_turn_id = lease.turn_id
        ctx.codex_recovered_msg_id = lease.msg_id
        ctx.codex_recovered_automatic = lease.automatic
        try:
            recovered = bool(await recover(lease.turn_id))
        except Exception as exc:
            log.warning(
                "Codex owned turn recovery failed",
                session_id=session_id,
                turn_id=lease.turn_id,
                error_type=type(exc).__name__,
            )
            recovered = False
        recovered = bool(
            recovered
            and ctx.codex_spontaneous_turn_id == lease.turn_id
            and ctx.codex_spontaneous_task is not None
        )
        if recovered:
            log.info(
                "recovered Remote-owned Codex turn",
                session_id=session_id,
                turn_id=lease.turn_id,
            )
            return True

        ctx.codex_owned_turn_id = None
        ctx.codex_recovered_turn_id = None
        ctx.codex_recovered_msg_id = None
        ctx.codex_recovered_automatic = None
        try:
            self._codex_turn_leases.release(
                session_id, turn_id=lease.turn_id)
        except Exception:
            pass
        return False

    async def _ensure_codex_daemon_generation(
        self,
        ctx: SessionContext,
        *,
        reason: str,
    ) -> bool:
        """Cross an intentional restart only between native Codex turns."""
        if not self._codex_shared_affinity(ctx):
            return False
        # Waiting is deliberately outside the lock. Automatic status reads run
        # in background tasks and must not make a Query wait behind the hook's
        # published outcome barrier; once ready, only the actual generation
        # handoff is shared.
        state = await self._codex_restart_state(
            wait=True,
            interrupt_event=ctx.interrupt_event,
        )
        # A background status read can outlive a delete/eviction while waiting
        # for the hook. Never reconnect a context that is no longer resident.
        if not self._is_resident_context(ctx):
            return False
        async with ctx.codex_daemon_generation_lock:
            # Eviction may race a previous generation reconnect while this task
            # waits on the per-context lock.  Do not resurrect a detached SDK.
            if not self._is_resident_context(ctx):
                return False
            if state is None:
                if self._codex_shared_live(ctx):
                    return True
                connected = await self._reconnect_codex_shared(ctx, reason=reason)
            else:
                if state.phase != "ready":
                    log.warning(
                        "Codex daemon restart barrier is not ready",
                        phase=state.phase,
                        epoch=state.epoch,
                        session_id=ctx.session_id,
                    )
                    return False
                generation_changed = ctx.codex_daemon_epoch != state.epoch
                if generation_changed:
                    # The manager caches readiness by binary/socket path, which
                    # remains stable across an official restart. Invalidate only
                    # its liveness cache; sticky per-thread shared affinity remains
                    # on the handle.
                    self._codex_daemon.invalidate()
                if not generation_changed and self._codex_shared_live(ctx):
                    return True
                connected = await self._reconnect_codex_shared(
                    ctx,
                    reason=reason,
                    force=generation_changed,
                )

            # ``force_reconnect`` awaits process setup.  The normal eviction
            # path may remove and disconnect this context in that interval;
            # close the newly-created proxy rather than leave an unrouteable
            # app-server connection alive.
            if not self._is_resident_context(ctx):
                if connected:
                    try:
                        await ctx.sdk.disconnect()
                    except Exception as exc:
                        log.warning(
                            "failed to disconnect evicted Codex shared proxy",
                            session_id=ctx.session_id,
                            error_type=type(exc).__name__,
                        )
                return False
            if state is None:
                return connected
            if connected:
                ctx.codex_daemon_epoch = state.epoch
            return connected

    async def _wait_for_codex_account_switch(
        self, *, starting_epoch: str,
    ) -> CodexDaemonRestartState:
        """Return as soon as the hook publishes a different generation.

        The marker is written before ``codex app-server daemon restart`` starts.
        Detecting ``restarting`` (rather than waiting for ``ready``) lets an
        accepted turn be interrupted on the old daemon so its graceful shutdown
        does not wait for exhausted-account work to finish.
        """
        while True:
            state = await self._codex_restart_state(wait=False)
            if state is not None and state.epoch != starting_epoch:
                return state
            await asyncio.sleep(0.05)

    async def _resume_codex_goal_after_account_switch(
        self,
        ctx: SessionContext,
        goal: dict,
    ) -> bool:
        """Use the official Goal idle transition until its turn is observed.

        ``thread/goal/set(status=active)`` is serialized by Codex's goal-state
        permit and calls ``try_start_turn_if_idle``. Reissuing that transition
        cannot create a competing turn, while a fixed "no notification yet"
        timeout followed by ``turn/start`` can. Keep the logical task waiting
        for the correlated spontaneous lifecycle or a user interrupt instead.
        """
        current = goal
        first_activation = True
        next_activation = 0.0
        loop = asyncio.get_running_loop()
        while True:
            if ctx.codex_spontaneous_turn_id is not None:
                return True
            if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                return False
            now = loop.time()
            if now >= next_activation:
                if not first_activation:
                    try:
                        current = await ctx.sdk.get_goal()
                    except Exception as exc:
                        log.warning(
                            "Codex goal state refresh failed while awaiting "
                            "account-switch continuation",
                            session_id=ctx.session_id,
                            error_type=type(exc).__name__,
                        )
                if (
                    not isinstance(current, dict)
                    or current.get("status") not in {"active", "usageLimited"}
                ):
                    return False
                prior_status = current.get("status")
                current = await ctx.sdk.set_goal(status="active")
                if (
                    first_activation
                    and prior_status != "active"
                    and ctx.goal_visible
                ):
                    await self._emit(ctx, GoalState(goal=current))
                first_activation = False
                next_activation = loop.time() + 5.0
                continue
            try:
                await asyncio.wait_for(
                    ctx.interrupt_event.wait(),
                    timeout=min(0.05, max(0.0, next_activation - now)),
                )
            except asyncio.TimeoutError:
                pass

    async def _sync_external_control(
        self,
        ctx: SessionContext,
        watch: Optional[dict],
    ) -> SessionControl:
        """Project legacy ownership detection into protocol-v15 control state."""
        if getattr(ctx.sdk, "is_claude_broker", False):
            unavailable = getattr(
                ctx.sdk, "cc_remote_unavailable_reason", None)
            if isinstance(unavailable, str) and unavailable:
                return await self._set_session_control(
                    ctx,
                    control_mode="claude_broker",
                    write_state="read_only",
                    # An unreachable broker cannot prove the TUI detached. Keep
                    # the last attachment fact while failing closed.
                    terminal_attached=ctx.terminal_attached,
                    reason=unavailable,
                    can_takeover=False,
                )
            metadata = getattr(ctx.sdk, "metadata", {})
            input_busy = bool(metadata.get("input_busy"))
            attached = bool(metadata.get("attached_count", 0))
            return await self._set_session_control(
                ctx,
                control_mode="claude_broker",
                write_state="input_busy" if input_busy else "writable",
                terminal_attached=attached,
                reason=(
                    "本机终端正在编辑输入，完成或取消后即可从 Remote 发送"
                    if input_busy else None
                ),
                can_takeover=False,
            )
        if ctx.engine == "codex" and bool(
                (watch or {}).get("desktop_active")):
            return await self._set_session_control(
                ctx,
                control_mode="desktop",
                write_state="read_only",
                terminal_attached=True,
                reason="Codex App 正在运行此会话；完成后 Web 会自动恢复可写",
                can_takeover=False,
            )
        if self._codex_shared_affinity(ctx):
            live = self._codex_shared_live(ctx)
            return await self._set_session_control(
                ctx,
                control_mode="codex_shared",
                write_state="writable",
                terminal_attached=bool((watch or {}).get("holders")),
                reason=(
                    None if live
                    else "Codex 共享通道连接断开；下次操作会自动重试"
                ),
                can_takeover=False,
            )
        if (ctx.engine == "claude" and ctx.needs_reload
                and ctx.control_mode == "remote"
                and ctx.write_state == "read_only"
                and ctx.control_reason
                == "Claude Remote 恢复失败，请重新进入会话后重试"):
            # The native writer is gone, but this resident SDK did not resume.
            # A later ownership poll cannot turn that failure into writable.
            return self._session_control(ctx)
        pending = bool((watch or {}).get("takeover_pending"))
        external = bool(ctx.session_id and self._is_external(ctx.session_id))
        if pending or external:
            return await self._set_session_control(
                ctx,
                control_mode="external_cli",
                write_state=("takeover_pending" if pending else "read_only"),
                terminal_attached=True,
                reason=(
                    "已登记接管，等待当前本机进程安全释放会话"
                    if pending else "会话正由本机原生 CLI 驱动"
                ),
                can_takeover=True,
            )
        if ctx.control_mode != "claude_broker":
            return await self._set_session_control(
                ctx,
                control_mode="remote",
                write_state="writable",
                terminal_attached=False,
                reason=None,
                can_takeover=False,
            )
        return self._session_control(ctx)

    def _configure_claude_sdk_callbacks(
        self, ctx: SessionContext, sdk: SdkHandle,
    ) -> None:
        """Install the complete per-session Claude bridge on one SDK handle."""
        sdk.ask_server = make_ask_server(
            lambda q, o: self._on_mcp_ask(ctx, q, o),
            lambda m: self._on_set_mode(ctx, m),
        )
        sdk.permission_callback = (
            lambda tool, tool_input, permission_context:
            self._on_claude_tool_permission(
                ctx, tool, tool_input, permission_context))
        sdk.background_message_callback = (
            lambda message, turn_id: self._on_claude_background_message(
                ctx, message, turn_id))

    @staticmethod
    def _copy_claude_runtime_options(
        ctx: SessionContext, source: object, target: object,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Carry the visible Claude controls across SDK/broker ownership swaps."""
        if getattr(target, "is_claude_broker", False):
            # The official TUI is already a live writer. Its broker metadata is
            # authoritative; copying stale SDK chips into it would only make
            # Remote lie about controls that were never applied to the TUI.
            permission = getattr(target, "permission_mode", None)
            model = getattr(target, "model", None)
            effort = getattr(target, "effort", None)
            return permission, model, effort

        permission = getattr(source, "permission_mode", None)
        if not isinstance(permission, str) or not permission:
            permission = ctx.announced_perm
        model = getattr(source, "model", None)
        if not isinstance(model, str) or not model:
            model = ctx.announced_model
        effort = getattr(source, "effort", None)
        if not isinstance(effort, str) or not effort:
            effort = ctx.announced_effort
        if isinstance(permission, str) and permission:
            setattr(target, "permission_mode", permission)
        if isinstance(model, str) and model:
            setattr(target, "model", model)
        if isinstance(effort, str) and effort:
            setattr(target, "effort", effort)
            setattr(target, "applied_effort", effort)
        return permission, model, effort

    async def _sync_claude_broker_runtime_controls(
        self, ctx: SessionContext,
    ) -> tuple[object, ...]:
        """Publish only controls durably observed from the official TUI."""
        if not getattr(ctx.sdk, "is_claude_broker", False):
            return ()
        events: list[object] = []
        model = getattr(ctx.sdk, "model", None)
        if isinstance(model, str) and model and model != ctx.announced_model:
            ctx.announced_model = model
            events.append(Model(model=model))
        effort = getattr(ctx.sdk, "effort", None)
        if isinstance(effort, str) and effort and effort != ctx.announced_effort:
            ctx.announced_effort = effort
            events.append(Effort(effort=effort))
        permission = getattr(ctx.sdk, "permission_mode", None)
        if (isinstance(permission, str) and permission
                and permission != ctx.announced_perm):
            ctx.announced_perm = permission
            events.append(Perm(mode=permission))
        for event in events:
            await self._emit(ctx, event)
        return tuple(events)

    async def _persist_claude_session_controls(self, ctx: SessionContext) -> None:
        """Persist Remote-owned Claude controls without touching global config."""
        if (ctx.engine != "claude" or ctx.space != "code"
                or not ctx.session_id
                or getattr(ctx.sdk, "is_claude_broker", False)):
            return
        if self._claude_controls is not None:
            try:
                await asyncio.to_thread(
                    self._claude_controls.update,
                    ctx.session_id,
                    model=getattr(ctx.sdk, "model", None),
                    effort=getattr(ctx.sdk, "effort", None),
                    permission_mode=getattr(
                        ctx.sdk, "permission_mode", None),
                )
            except Exception as exc:
                # A live runtime control change already succeeded. Do not roll
                # it back because its private durability cache is unavailable.
                log.warning(
                    "Claude Remote controls could not be persisted",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
        if not self._claude_broker_enabled:
            return
        try:
            await self._claude_broker.set_preferences(
                ctx.session_id,
                model=getattr(ctx.sdk, "model", None),
                effort=getattr(ctx.sdk, "effort", None),
                permission_mode=getattr(ctx.sdk, "permission_mode", None),
            )
        except Exception as exc:
            # The live SDK mutation already succeeded. Keep Remote usable if the
            # optional local broker is restarting, but make the durability gap
            # observable instead of pretending the next TUI is guaranteed.
            log.warning(
                "Claude session controls could not be persisted to broker",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )

    async def _load_claude_session_controls(
        self, session_id: str,
    ) -> ClaudeControls:
        """Load one private session override; invalid state fails closed."""
        if self._claude_controls is None:
            return ClaudeControls()
        try:
            return await asyncio.to_thread(
                self._claude_controls.get, session_id)
        except Exception as exc:
            log.warning(
                "Claude Remote controls could not be loaded",
                session_id=session_id,
                error_type=type(exc).__name__,
            )
            return ClaudeControls()

    async def _persist_codex_session_controls(
        self, ctx: SessionContext,
    ) -> None:
        if (ctx.engine != "codex" or ctx.space != "code"
                or not ctx.session_id or self._codex_controls is None):
            return
        try:
            await asyncio.to_thread(
                self._codex_controls.update,
                ctx.session_id,
                approval_policy=(
                    getattr(ctx.sdk, "approval_policy", None)
                    if isinstance(
                        getattr(ctx.sdk, "approval_policy", None), str)
                    else None
                ),
                permission_profile=getattr(
                    ctx.sdk, "permission_profile", None),
                web_search=getattr(
                    ctx.sdk, "web_search_override", None),
            )
        except Exception as exc:
            log.warning(
                "Codex Remote controls could not be persisted",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )

    async def _load_codex_session_controls(
        self, session_id: str,
    ) -> CodexControls:
        if self._codex_controls is None:
            return CodexControls()
        try:
            return await asyncio.to_thread(
                self._codex_controls.get, session_id)
        except Exception as exc:
            log.warning(
                "Codex Remote controls could not be loaded",
                session_id=session_id,
                error_type=type(exc).__name__,
            )
            return CodexControls()

    async def _reconcile_codex_cwd_override(
        self,
        session_id: str,
        controls: CodexControls,
    ) -> CodexControls:
        """Return a live override, atomically discarding a missing target."""
        for _attempt in range(4):
            cwd = controls.cwd_override
            if cwd is None or await asyncio.to_thread(os.path.isdir, cwd):
                return controls
            if self._codex_controls is None:
                raise CodexControlStoreError(
                    "Codex cwd override store is unavailable"
                )
            controls = await asyncio.to_thread(
                self._codex_controls.clear_cwd_override_if_matches,
                session_id,
                cwd,
            )
            self._invalidate_codex_session_catalog()
            log.warning(
                "discarded missing Codex cwd override",
                session_id=session_id,
                cwd=cwd,
            )
        raise CodexControlStoreError(
            "Codex cwd override changed repeatedly during validation"
        )

    async def _read_claude_handoff_controls(
        self, ctx: SessionContext,
    ) -> ClaudeControls:
        """Read only controls proved by the latest completed native turn."""
        if not ctx.session_id:
            return ClaudeControls()
        try:
            return await asyncio.to_thread(
                last_completed_assistant_controls,
                ctx.session_id,
                directory=ctx.cwd,
                max_bytes=self.cfg.history_source_max_bytes,
            )
        except Exception as exc:
            # Transcript controls are advisory during handoff. An unavailable or
            # malformed tail must preserve the last Remote-owned choices.
            log.warning(
                "Claude native handoff controls unavailable",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            return ClaudeControls()

    async def _reload_claude_after_takeover(
        self, ctx: SessionContext,
    ) -> Error | None:
        """Atomically resume SDK ownership after the native writer has exited."""
        controls = await self._read_claude_handoff_controls(ctx)
        previous_model = getattr(ctx.sdk, "model", None)
        previous_effort = getattr(ctx.sdk, "effort", None)
        model = controls.model or previous_model
        effort = controls.effort or previous_effort
        if model:
            ctx.sdk.model = model
        if effort:
            ctx.sdk.effort = effort
        ctx.needs_reload = True
        try:
            await ctx.sdk.force_reconnect(
                resume_id=ctx.session_id,
                cwd=ctx.cwd,
                reason="native Claude takeover handoff",
                preserve_model=True,
            )
        except Exception as exc:
            log.warning(
                "Claude takeover SDK reload failed",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            await self._set_session_control(
                ctx,
                control_mode="remote",
                write_state="read_only",
                terminal_attached=False,
                reason="Claude Remote 恢复失败，请重新进入会话后重试",
                can_takeover=False,
            )
            error = Error(
                code=ERR_CC_CRASH,
                message="Claude CLI 已退出，但 Remote 恢复失败；本次未开放写入",
            )
            await self._emit(ctx, error)
            return error

        ctx.needs_reload = False
        applied_model = valid_claude_model(
            getattr(ctx.sdk, "model", None)) or model
        applied_effort = getattr(ctx.sdk, "effort", None) or effort
        if applied_model:
            # A custom provider can expose its upstream id through context
            # usage even though the selected Claude alias was applied. Never
            # leak that implementation detail back into Remote's model chip or
            # its private session store.
            ctx.sdk.model = applied_model
        if applied_model and applied_model != ctx.announced_model:
            ctx.announced_model = applied_model
            await self._emit(ctx, Model(model=applied_model))
        if applied_effort and applied_effort != ctx.announced_effort:
            ctx.announced_effort = applied_effort
            await self._emit(ctx, Effort(effort=applied_effort))
        await self._persist_claude_session_controls(ctx)
        return None

    @staticmethod
    def _is_orphaned_claude_broker_turn(ctx: SessionContext) -> bool:
        """A terminal-only turn has no Remote task/message writer to preserve."""
        return (
            ctx.state in {"running", "interrupting", "draining"}
            and ctx.turn_task is None
            and ctx.active_msg_id is None
            and not ctx.claude_write_active
        )

    async def _set_claude_broker_unavailable(
        self, ctx: SessionContext, error_code: str,
    ) -> None:
        """Fail closed when broker liveness is unknown, without losing state."""
        if not getattr(ctx.sdk, "is_claude_broker", False):
            return
        reason = (
            "Claude broker 连接暂不可用，正在等待本机控制通道恢复"
            if error_code in {"broker_unavailable", "broker_disconnected"}
            else "Claude broker 状态无法安全确认，正在等待本机控制通道恢复"
        )
        setattr(ctx.sdk, "cc_remote_unavailable_reason", reason)
        await self._sync_external_control(
            ctx, self._watch.get(ctx.session_id or ""))

    async def _restore_sdk_after_claude_broker_exit(
        self,
        ctx: SessionContext,
        broker: object,
    ) -> bool:
        """Atomically resume an exited broker session through Agent SDK.

        A terminal ``session_exited``/``session_not_found`` is the only proof
        that permits creating a new writer. Before connecting, recheck the
        broker: a replacement generation wins; transport uncertainty remains
        read-only. The connected SDK is published to ``ctx`` only after all
        callbacks and live controls are restored.
        """
        current_turn = asyncio.current_task()
        orphaned_terminal_turn = self._is_orphaned_claude_broker_turn(ctx)
        if ((ctx.state != "idle" and not orphaned_terminal_turn)
                or (ctx.turn_task is not None
                    and ctx.turn_task is not current_turn)
                or ctx.claude_write_active
                or not ctx.session_id):
            return False

        replacement_broker: Optional[ClaudeBrokerHandle] = None
        terminal_confirmed = False
        try:
            response = await self._claude_broker.status(ctx.session_id)
        except BrokerClientError as exc:
            if exc.code in {"session_exited", "session_not_found"}:
                terminal_confirmed = True
            else:
                await self._set_claude_broker_unavailable(ctx, exc.code)
                return False
        else:
            metadata = response.get("session")
            if (not isinstance(metadata, dict)
                    or metadata.get("id") != ctx.session_id):
                await self._set_claude_broker_unavailable(
                    ctx, "invalid_status")
                return False
            if metadata.get("running") is True:
                try:
                    replacement_broker = ClaudeBrokerHandle(
                        self._claude_broker, ctx.session_id, metadata)
                    await replacement_broker.connect(
                        resume_id=ctx.session_id, cwd=ctx.cwd)
                except BrokerClientError as exc:
                    await self._set_claude_broker_unavailable(ctx, exc.code)
                    return False
            else:
                terminal_confirmed = True

        async with ctx.launch_lock:
            if ctx.sdk is not broker:
                return not bool(getattr(
                    ctx.sdk, "cc_remote_unavailable_reason", None))
            # Status revalidation awaited outside the launch gate. A Query may
            # have claimed the broker turn in that interval; never replace its
            # live adapter underneath submit/drain.
            orphaned_terminal_turn = self._is_orphaned_claude_broker_turn(ctx)
            if ((ctx.state != "idle" and not orphaned_terminal_turn)
                    or (ctx.turn_task is not None
                        and ctx.turn_task is not current_turn)
                    or ctx.claude_write_active):
                return False
            if replacement_broker is not None:
                # A live replacement generation proves that terminal ownership
                # still exists, but its metadata cannot prove whether the model
                # turn itself reached an assistant boundary. Keep an orphaned
                # running/interruption state fail-closed instead of publishing
                # a false idle transition merely because the generation changed.
                self._copy_claude_runtime_options(
                    ctx, broker, replacement_broker)
                ctx.sdk = replacement_broker
                ctx.claude_broker_generation = replacement_broker.generation
                await self._sync_claude_broker_runtime_controls(ctx)
                await self._sync_external_control(
                    ctx, self._watch.get(ctx.session_id))
                log.info(
                    "adopted replacement Claude broker generation",
                    session_id=ctx.session_id,
                    generation=replacement_broker.generation,
                )
                return True
            if not terminal_confirmed:
                return False

            sdk = SdkHandle(self.cfg)
            permission, model, effort = self._copy_claude_runtime_options(
                ctx, broker, sdk)
            self._configure_claude_sdk_callbacks(ctx, sdk)
            try:
                await sdk.connect(
                    resume_id=ctx.session_id,
                    cwd=ctx.cwd,
                    model_override=model,
                )
            except Exception as exc:
                try:
                    await sdk.disconnect()
                except Exception:
                    pass
                log.warning(
                    "Claude SDK restore after broker exit failed",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
                await self._set_claude_broker_unavailable(
                    ctx, "sdk_restore_failed")
                return False

            # Close the connect-time race as far as the broker protocol allows.
            # If another `claude-remote` generation claimed this sid while the
            # SDK was starting, discard our new child before publishing it.
            try:
                final_response = await self._claude_broker.status(
                    ctx.session_id)
            except BrokerClientError as exc:
                if exc.code not in {"session_exited", "session_not_found"}:
                    try:
                        await sdk.disconnect()
                    except Exception:
                        pass
                    await self._set_claude_broker_unavailable(ctx, exc.code)
                    return False
            else:
                final_metadata = final_response.get("session")
                if (not isinstance(final_metadata, dict)
                        or final_metadata.get("id") != ctx.session_id):
                    try:
                        await sdk.disconnect()
                    except Exception:
                        pass
                    await self._set_claude_broker_unavailable(
                        ctx, "invalid_status")
                    return False
                if final_metadata.get("running") is True:
                    try:
                        live_broker = ClaudeBrokerHandle(
                            self._claude_broker,
                            ctx.session_id,
                            final_metadata,
                        )
                        await live_broker.connect(
                            resume_id=ctx.session_id, cwd=ctx.cwd)
                    except BrokerClientError as exc:
                        try:
                            await sdk.disconnect()
                        except Exception:
                            pass
                        await self._set_claude_broker_unavailable(
                            ctx, exc.code)
                        return False
                    try:
                        await sdk.disconnect()
                    except Exception as exc:
                        log.warning(
                            "discarding raced Claude SDK failed",
                            session_id=ctx.session_id,
                            error_type=type(exc).__name__,
                        )
                    self._copy_claude_runtime_options(
                        ctx, broker, live_broker)
                    # As above, a connect-time replacement remains the writer;
                    # only a definitive terminal broker status may converge an
                    # orphaned terminal-authored turn back to idle.
                    ctx.sdk = live_broker
                    ctx.claude_broker_generation = live_broker.generation
                    await self._sync_claude_broker_runtime_controls(ctx)
                    await self._sync_external_control(
                        ctx, self._watch.get(ctx.session_id))
                    log.info(
                        "Claude broker reclaimed session during SDK restore",
                        session_id=ctx.session_id,
                        generation=live_broker.generation,
                    )
                    return True

            if orphaned_terminal_turn:
                # The official TUI exited without a durable assistant boundary.
                # This was a terminal-authored turn (no managed task/msg id), so
                # publish the same idle + authoritative History convergence used
                # by the normal broker lifecycle path before installing the SDK.
                # ``interrupting``/``draining`` may still carry the previous
                # stop request. It belongs to the now-proven-dead broker turn
                # and must not immediately interrupt the first restored SDK turn.
                ctx.interrupt_deadline = None
                ctx.interrupt_event.clear()
                watch = self._watch.get(ctx.session_id)
                if watch is not None:
                    watch["broker_active"] = False
                    watch["broker_partial"] = b""
                await self._set_state(ctx, "idle")
                try:
                    await self._push_mirrored_history(ctx.session_id)
                except Exception as exc:
                    # The SDK handoff is still the ownership safety boundary;
                    # one failed optional mirror will be recovered by the next
                    # GetHistory/watch pass and must not leave a dead adapter.
                    log.warning(
                        "Claude orphaned broker history refresh failed",
                        session_id=ctx.session_id,
                        error_type=type(exc).__name__,
                    )

            ctx.sdk = sdk
            ctx.claude_broker_generation = None
            ctx.needs_reload = False
            ctx.external_ts = 0.0
            ctx.claude_write_active = False
            ctx.announced_perm = (
                permission or getattr(sdk, "permission_mode", None))
            ctx.announced_model = getattr(sdk, "model", None) or model
            ctx.announced_effort = getattr(sdk, "effort", None) or effort
            watch = self._watch.get(ctx.session_id)
            if watch is not None:
                watch["broker_active"] = False
                watch["broker_partial"] = b""
                watch["external"] = False
                watch["holders"] = set()
                watch["takeover_pending"] = False
                watch["scan_complete"] = True

        await self._set_session_control(
            ctx,
            control_mode="remote",
            write_state="writable",
            terminal_attached=False,
            reason=None,
            can_takeover=False,
        )
        log.info(
            "restored Claude SDK after broker session exit",
            session_id=ctx.session_id,
            permission_mode=ctx.announced_perm,
            model=ctx.announced_model,
            effort=ctx.announced_effort,
        )
        return True

    async def _refresh_claude_broker_handle(
        self, ctx: SessionContext,
    ) -> bool:
        """Refresh a broker, recover terminal exits, and fail closed on doubt."""
        if not getattr(ctx.sdk, "is_claude_broker", False):
            return False
        broker = ctx.sdk
        try:
            await broker.refresh_status()
        except BrokerClientError as exc:
            if exc.code in {"session_exited", "session_not_found", "stale_generation"}:
                return await self._restore_sdk_after_claude_broker_exit(
                    ctx, broker)
            await self._set_claude_broker_unavailable(ctx, exc.code)
            return False
        setattr(broker, "cc_remote_unavailable_reason", None)
        await self._sync_claude_broker_runtime_controls(ctx)
        await self._sync_external_control(
            ctx, self._watch.get(ctx.session_id or ""))
        return True

    async def _adopt_claude_broker_handle(
        self,
        ctx: SessionContext,
        replacement: Optional[ClaudeBrokerHandle] = None,
    ) -> bool:
        """Atomically replace an idle SDK child with its exact broker TUI.

        ``claude-remote resume`` can be launched after Remote has already made
        the same session resident through the Agent SDK.  Treating that official
        TUI as a foreign CLI makes the browser read-only and lets Takeover kill
        the broker child.  Discover the exact sid, stop only our idle SDK child,
        and switch the context to the non-owning broker adapter instead.
        """
        if not self._claude_broker_enabled:
            return False
        if (ctx.engine != "claude" or ctx.space != "code"
                or not ctx.session_id):
            return False
        if getattr(ctx.sdk, "is_claude_broker", False):
            return await self._refresh_claude_broker_handle(ctx)
        if (ctx.state != "idle" or ctx.turn_task is not None
                or ctx.claude_write_active):
            return False

        original = ctx.sdk
        if replacement is None:
            try:
                replacement = await ClaudeBrokerHandle.discover(
                    self._claude_broker, ctx.session_id)
            except BrokerClientError as exc:
                if exc.code not in {"broker_unavailable", "session_not_found"}:
                    log.warning(
                        "Claude broker adoption discovery failed",
                        session_id=ctx.session_id,
                        error_code=exc.code,
                    )
                return False
        if (replacement is None
                or replacement.session_id != ctx.session_id
                or os.path.realpath(replacement.cwd) != os.path.realpath(ctx.cwd)):
            return False

        async with ctx.launch_lock:
            # A query, interrupt, another watcher pass, or a pool replacement may
            # have changed the context while broker discovery was in flight.
            if (ctx.sdk is not original or ctx.state != "idle"
                    or ctx.turn_task is not None or ctx.claude_write_active):
                return bool(getattr(ctx.sdk, "is_claude_broker", False))
            try:
                # Revalidate the generation and cwd immediately before removing
                # the SDK writer.  A stale list response must never leave the
                # session without either owner.
                await replacement.connect(
                    resume_id=ctx.session_id, cwd=ctx.cwd)
            except BrokerClientError as exc:
                log.info(
                    "Claude broker disappeared before adoption",
                    session_id=ctx.session_id,
                    error_code=exc.code,
                )
                return False
            try:
                await original.disconnect()
            except Exception as exc:
                # SdkHandle.disconnect() clears ``client`` in a finally block.
                # If teardown already removed the writer, complete the validated
                # broker handoff despite a late disconnect error. If a live client
                # remains (or the handle cannot prove otherwise), fail closed and
                # keep the original owner rather than installing a second writer.
                missing = object()
                client = getattr(original, "client", missing)
                if client is not None:
                    log.warning(
                        "Claude SDK disconnect blocked broker adoption",
                        session_id=ctx.session_id,
                        error=str(exc),
                    )
                    return False
                log.warning(
                    "Claude SDK disconnect failed after writer teardown; "
                    "continuing broker adoption",
                    session_id=ctx.session_id,
                    error=str(exc),
                )

            self._copy_claude_runtime_options(ctx, original, replacement)
            ctx.sdk = replacement
            ctx.claude_broker_generation = replacement.generation
            ctx.needs_reload = False
            ctx.external_ts = 0.0
            ctx.claude_write_active = False

            watch = self._watch.get(ctx.session_id)
            if watch is not None:
                watch["cwd"] = ctx.cwd
                watch["external"] = False
                watch["holders"] = set()
                watch["takeover_pending"] = False
                watch["scan_complete"] = True
                try:
                    active, partial = claude_broker_tail_state(watch["path"])
                except OSError:
                    active, partial = False, b""
                watch["broker_active"] = active
                watch["broker_partial"] = partial

        if ctx.session_id not in self._watch:
            self._watch_session(ctx.session_id)
        watch = self._watch.get(ctx.session_id)
        if (watch is not None and watch.get("broker_active")
                and ctx.turn_task is None and ctx.state == "idle"):
            await self._set_state(ctx, "running")

        await self._sync_claude_broker_runtime_controls(ctx)
        await self._sync_external_control(ctx, watch)
        log.info(
            "adopted live Claude broker session",
            session_id=ctx.session_id,
            generation=replacement.generation,
        )
        return True

    async def _adopt_live_claude_broker_sessions(self) -> None:
        """Upgrade resident idle contexts from one bounded broker list read."""
        if not self._claude_broker_enabled:
            return
        candidates = [
            ctx for ctx in list(self.sessions.values())
            if (ctx.engine == "claude" and ctx.space == "code"
                and ctx.session_id and ctx.state == "idle"
                and not getattr(ctx.sdk, "is_claude_broker", False))
        ]
        if not candidates:
            return
        try:
            response = await self._claude_broker.list()
        except BrokerClientError as exc:
            if exc.code not in {"broker_unavailable", "session_not_found"}:
                log.warning(
                    "Claude broker list unavailable during adoption",
                    error_code=exc.code,
                )
            return
        rows = response.get("sessions")
        if not isinstance(rows, list):
            return
        by_sid: dict[str, ClaudeBrokerHandle] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("running") is not True:
                continue
            sid = row.get("id")
            if not isinstance(sid, str) or not sid or len(sid) > 256:
                continue
            try:
                by_sid[sid] = ClaudeBrokerHandle(
                    self._claude_broker, sid, row)
            except BrokerClientError:
                continue
        for ctx in candidates:
            replacement = by_sid.get(ctx.session_id or "")
            if replacement is not None:
                await self._adopt_claude_broker_handle(ctx, replacement)

    def _resolve_session_alias(self, sid: Optional[str]) -> Optional[str]:
        if not sid:
            return sid
        alias = self._session_aliases.get(sid)
        if alias is None:
            return sid
        self._session_aliases.move_to_end(sid)
        return alias["session_id"]

    def _ctx_for(self, sid: Optional[str]) -> Optional[SessionContext]:
        """Resolve a command's target ctx. An EXPLICIT sid that isn't resident
        returns None — NOT the focused ctx: a session whose spawn failed (e.g. bad
        codex config) must not silently reroute its query to whatever is focused
        (that made a failed session look like "no response" while its "在？" landed
        on another session). Only an absent sid (legacy/untagged commands) falls
        back to the focused view. Iterates a snapshot (a turn may re-key mid-scan)."""
        if sid:
            sid = self._resolve_session_alias(sid)
            ctx = self.sessions.get(sid)
            if ctx:
                return ctx
            for c in list(self.sessions.values()):
                if c.session_id == sid:
                    return c
            return None
        return self._focused_ctx()

    def _btw_ctx_for_command(self, cmd) -> Optional[SessionContext]:
        """Return the private /btw context targeted by ``cmd``, if any."""
        if cmd.type in self.BTW_SID_COMMANDS:
            sid = getattr(cmd, "sid", None)
        elif cmd.type in self.BTW_SESSION_COMMANDS:
            sid = getattr(cmd, "session_id", None)
        else:
            return None
        if not sid:
            return None
        for ctx in list(self.sessions.values()):
            if ctx.btw and sid in {ctx.key, ctx.session_id, ctx.btw_real_id}:
                return ctx
        return None

    async def _reject_nonowner_btw_command(self, cmd) -> Optional[Error]:
        """Fail closed when a client tries to operate another client's fork."""
        ctx = self._btw_ctx_for_command(cmd)
        target = (getattr(cmd, "sid", None) if cmd.type in self.BTW_SID_COMMANDS
                  else getattr(cmd, "session_id", None)
                  if cmd.type in self.BTW_SESSION_COMMANDS else None)
        tombstoned = bool(target and target in self._private_btw_sessions)
        if ctx is None and not tombstoned:
            return None
        client_id = getattr(cmd, "client_id", None)
        owner = ctx.owner_client_id if ctx is not None else None
        # Only the stable btw-* key is a valid live target. The persisted Claude
        # session id is internal, and session-store operations (switch/history/
        # rename/archive) would turn an ephemeral fork into a normal cold session.
        invalid_private_target = bool(
            tombstoned
            or cmd.type in self.BTW_SESSION_COMMANDS
            or (ctx is not None and target == ctx.btw_real_id)
        )
        if (not invalid_private_target and client_id and client_id == owner):
            return None
        # Relay-bound commands always carry a client id.  If an internal/legacy
        # caller omits it, route the denial to the owner rather than accidentally
        # broadcasting a frame about a private runtime.
        recipient = client_id or owner
        error = Error(
            code=ERR_AUTH,
            message="btw session belongs to another client",
            request_id=getattr(cmd, "request_id", None),
            msg_id=getattr(cmd, "msg_id", None),
            sid=(ctx.key if ctx is not None else target),
            to=recipient,
        )
        if recipient:
            await self.transport.send(error)
        log.warning(
            "rejected private btw command",
            type=cmd.type,
            btw_sid=(ctx.key if ctx is not None else target),
            client_id=client_id,
        )
        return error

    def _alias_file(self):
        return self.cfg.state_dir / "session-aliases.json"

    def _load_session_aliases(self) -> OrderedDict[str, dict]:
        aliases: OrderedDict[str, dict] = OrderedDict()
        try:
            path = self._alias_file()
            if path.stat().st_size > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            with path.open() as stream:
                raw_text = stream.read(self.SESSION_ALIAS_FILE_MAX_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) \
                    > self.SESSION_ALIAS_FILE_MAX_BYTES:
                raise ValueError("session alias state exceeds size limit")
            raw = json.loads(raw_text)
            now = time.time()
            for old_key, entry in (raw.items() if isinstance(raw, dict) else []):
                if not isinstance(entry, dict):
                    continue
                real = entry.get("session_id")
                cwd = entry.get("cwd")
                created = entry.get("created_at", 0)
                if (
                    isinstance(old_key, str)
                    and re.fullmatch(r"tmp-[0-9a-f]{32}", old_key)
                    and isinstance(real, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", real)
                    and isinstance(cwd, str) and "\x00" not in cwd
                    and len(cwd.encode("utf-8", "surrogatepass")) <= 4096
                    and isinstance(created, (int, float))
                    and 0 <= now - created <= self.SESSION_ALIAS_TTL
                ):
                    aliases[old_key] = {
                        "session_id": real, "cwd": cwd, "created_at": created,
                    }
            while len(aliases) > self.SESSION_ALIAS_CAP:
                aliases.popitem(last=False)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.warning("session alias state ignored", error_type=type(exc).__name__)
        return aliases

    def _remember_session_alias(self, old_key: str, session_id: str, cwd: str) -> None:
        self._session_aliases[old_key] = {
            "session_id": session_id,
            "cwd": cwd,
            "created_at": time.time(),
        }
        self._session_aliases.move_to_end(old_key)
        while len(self._session_aliases) > self.SESSION_ALIAS_CAP:
            self._session_aliases.popitem(last=False)
        try:
            path = self._alias_file()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._session_aliases, separators=(",", ":")))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except Exception as exc:
            log.warning("session alias state not persisted",
                        error_type=type(exc).__name__)

    def _private_btw_file(self):
        return self.cfg.state_dir / "private-btw-sessions.json"

    def _load_private_btw_sessions(self) -> OrderedDict[str, dict]:
        entries: OrderedDict[str, dict] = OrderedDict()
        try:
            path = self._private_btw_file()
            if path.stat().st_size > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            with path.open() as stream:
                raw_text = stream.read(self.PRIVATE_BTW_FILE_MAX_BYTES + 1)
            if len(raw_text.encode("utf-8", "surrogatepass")) \
                    > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            raw = json.loads(raw_text)
            if not isinstance(raw, dict) or len(raw) > self.PRIVATE_BTW_CAP:
                raise ValueError("private btw state has an invalid shape")
            for sid, entry in raw.items():
                if not isinstance(entry, dict):
                    raise ValueError("private btw state has an invalid entry")
                cwd = entry.get("cwd")
                created = entry.get("created_at", 0)
                if (
                    isinstance(sid, str)
                    and re.fullmatch(
                        r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
                        sid,
                    )
                    and isinstance(cwd, str) and "\x00" not in cwd
                    and len(cwd.encode("utf-8", "surrogatepass")) <= 4096
                    and isinstance(created, (int, float))
                ):
                    entries[sid] = {"cwd": cwd, "created_at": created}
                else:
                    raise ValueError("private btw state has an invalid entry")
        except FileNotFoundError:
            pass
        except Exception as exc:
            # This file is the durable privacy boundary for Claude fork_session
            # transcripts. Treating corruption as "no private sessions" would
            # publish every surviving fork on the next SessionList.
            raise RuntimeError(
                "private btw state is unreadable; refusing fail-open startup"
            ) from exc
        return entries

    def _persist_private_btw_sessions(
        self, entries: Optional[OrderedDict[str, dict]] = None,
    ) -> None:
        entries = self._private_btw_sessions if entries is None else entries
        path = self._private_btw_file()
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            payload = json.dumps(entries, separators=(",", ":"))
            if len(payload.encode("utf-8")) > self.PRIVATE_BTW_FILE_MAX_BYTES:
                raise ValueError("private btw state exceeds size limit")
            with tmp.open("w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            # Make the rename durable, not merely atomic in the page cache.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise RuntimeError("private btw state could not be persisted") from exc

    def _remember_private_btw(self, session_id: str, cwd: str) -> None:
        updated = OrderedDict(self._private_btw_sessions)
        updated[session_id] = {
            "cwd": cwd, "created_at": time.time(),
        }
        updated.move_to_end(session_id)
        # Reaching this bound means cleanup is persistently broken. Fail closed by
        # retaining the oldest tombstones and refusing to forget a private id.
        if len(updated) > self.PRIVATE_BTW_CAP:
            raise RuntimeError("private btw tombstone capacity exhausted")
        self._persist_private_btw_sessions(updated)
        self._private_btw_sessions = updated

    async def _delete_private_btw(
        self, session_id: str, cwd: str, *, forget: bool = True,
    ) -> bool:
        try:
            await asyncio.to_thread(delete_session, session_id, directory=cwd)
        except FileNotFoundError:
            pass  # already absent is the desired state
        except Exception as exc:
            log.warning("btw fork transcript delete failed", forked=session_id,
                        error=str(exc))
            return False
        if forget:
            updated = OrderedDict(self._private_btw_sessions)
            updated.pop(session_id, None)
            try:
                self._persist_private_btw_sessions(updated)
            except RuntimeError as exc:
                # The transcript is gone, so retaining a stale in-memory/on-disk
                # tombstone is safe. Never forget it only in RAM while disk still
                # claims the private fork exists.
                log.warning("private btw tombstone removal not persisted",
                            error_type=type(exc).__name__)
            else:
                self._private_btw_sessions = updated
        log.info("btw fork transcript deleted", forked=session_id)
        return True

    async def _cleanup_private_btw_sessions(self) -> None:
        for session_id, entry in list(self._private_btw_sessions.items()):
            await self._delete_private_btw(session_id, entry["cwd"])

    # ---- lifecycle ----

    async def run(self) -> None:
        self._cleanup_tmp()
        await asyncio.to_thread(self._work.initialize)
        # A previous process may have died while a Claude /btw fork was live.
        # Remove its persisted private transcript before accepting any client
        # command or publishing SessionList.
        await self._cleanup_private_btw_sessions()
        # Bring up the relay first.  Claude is an optional engine: a missing CLI or
        # incompatible SDK must not prevent the wrapper from accepting a subsequent
        # `new_session(engine="codex")` command.
        await self.transport.start()
        try:
            bootstrap_sid = (
                self.cfg.resume_session_id
                or load_session_id(self.cfg.state_dir, self.cfg.cc_cwd)
            )
            # Shared-daemon turns already accepted on behalf of Remote outrank
            # creating an idle bootstrap resident. Recover every durable lease
            # first; recovery may temporarily exceed the normal resident cap
            # because those native turns are already consuming daemon capacity.
            await self._restore_codex_owned_turns()
            ctx = (
                self._ctx_by_sid(bootstrap_sid)
                if bootstrap_sid else None
            )
            if (
                ctx is None
                and len(self.sessions) < self.cfg.max_concurrent_sessions
            ):
                ctx = await self._spawn(
                    resume_id=bootstrap_sid,
                    cwd=self.cfg.cc_cwd,
                    bootstrap=True,
                )
            elif ctx is None and self.sessions:
                ctx = next(iter(self.sessions.values()))
                log.info(
                    "idle bootstrap deferred behind recovered Codex turns",
                    resident=len(self.sessions),
                    cap=self.cfg.max_concurrent_sessions,
                )
            if ctx is not None:
                self.focused_sid = ctx.key
                log.info("wrapper running", session_id=ctx.session_id,
                         key=self.focused_sid)
                # If the transport connected while bootstrap was still spawning, its
                # first hello described an empty pool. Re-announce the focused session;
                # when still disconnected this remains a harmless best-effort send.
                await self._on_transport_connected()
            else:
                log.warning("Claude bootstrap unavailable; continuing with empty pool")
                await self._on_transport_connected()

            self._watch_task = asyncio.create_task(self._watch_loop())
            self._work_schedule_task = asyncio.create_task(
                self._work_schedule_loop())
            async for cmd in self.transport.incoming():
                if cmd.type == "list_sessions":
                    self._start_session_list_command(cmd)
                    continue
                if cmd.type in {"get_history", "get_turn_detail"}:
                    self._start_history_command(cmd)
                    continue
                if cmd.type == "get_models":
                    self._start_models_command(cmd)
                    continue
                if cmd.type == "steer":
                    # turn/steer is a short app-server RPC, but it must not
                    # monopolize the serial command lane and delay an explicit
                    # Stop arriving from another client.
                    self._start_interactive_control_command(cmd)
                    continue
                if cmd.type == "get_permission_profiles":
                    self._start_models_command(cmd)
                    continue
                if cmd.type == "get_status":
                    self._start_status_command(cmd)
                    continue
                if cmd.type in {
                    "get_engine_capabilities", "manage_engine_plugin",
                    "manage_engine_skill", "manage_engine_hook",
                }:
                    self._start_capabilities_command(cmd)
                    continue
                if cmd.type == "set_model":
                    ctx = self._ctx_for(getattr(cmd, "sid", None))
                    if (ctx is not None and ctx.engine == "claude"
                            and ctx.space == "code"):
                        self._start_interactive_control_command(cmd)
                        continue
                await self._process_command_safely(cmd)
        finally:
            models_tasks = list(self._models_command_tasks.values())
            for task in models_tasks:
                task.cancel()
            if models_tasks:
                await asyncio.gather(*models_tasks, return_exceptions=True)
            self._models_command_tasks.clear()
            status_tasks = list(self._status_command_tasks.values())
            for task in status_tasks:
                task.cancel()
            if status_tasks:
                await asyncio.gather(*status_tasks, return_exceptions=True)
            self._status_command_tasks.clear()
            capabilities_tasks = list(self._capabilities_command_tasks.values())
            for task in capabilities_tasks:
                task.cancel()
            if capabilities_tasks:
                await asyncio.gather(*capabilities_tasks, return_exceptions=True)
            self._capabilities_command_tasks.clear()
            control_tasks = list(self._interactive_control_tasks.values())
            for task in control_tasks:
                task.cancel()
            if control_tasks:
                await asyncio.gather(*control_tasks, return_exceptions=True)
            self._interactive_control_tasks.clear()
            list_tasks = list(self._session_list_command_tasks)
            for task in list_tasks:
                task.cancel()
            if list_tasks:
                await asyncio.gather(*list_tasks, return_exceptions=True)
            self._session_list_command_tasks.clear()
            history_tasks = list(self._history_command_tasks.values())
            for task in history_tasks:
                task.cancel()
            if history_tasks:
                await asyncio.gather(*history_tasks, return_exceptions=True)
            self._history_command_tasks.clear()
            page_tasks = list(self._history_page_tasks.values())
            for task in page_tasks:
                task.cancel()
            if page_tasks:
                await asyncio.gather(*page_tasks, return_exceptions=True)
            self._history_page_tasks.clear()
            refresh_tasks = list(self._history_refresh_tasks.values())
            for task in refresh_tasks:
                task.cancel()
            if refresh_tasks:
                await asyncio.gather(*refresh_tasks, return_exceptions=True)
            self._history_refresh_tasks.clear()
            self._history_refresh_dirty.clear()
            if self._codex_session_list_refresh_task is not None:
                self._codex_session_list_refresh_task.cancel()
                await asyncio.gather(
                    self._codex_session_list_refresh_task,
                    return_exceptions=True,
                )
                self._codex_session_list_refresh_task = None
            fork_tasks = [
                *self._codex_fork_tasks.values(),
                *self._claude_fork_tasks.values(),
            ]
            for task in fork_tasks:
                task.cancel()
            if fork_tasks:
                await asyncio.gather(*fork_tasks, return_exceptions=True)
            self._codex_fork_tasks.clear()
            self._claude_fork_tasks.clear()
            if self._work_schedule_task:
                self._work_schedule_task.cancel()
                await asyncio.gather(
                    self._work_schedule_task, return_exceptions=True)
            work_runs = list(self._work_schedule_runs)
            for task in work_runs:
                task.cancel()
            if work_runs:
                await asyncio.gather(*work_runs, return_exceptions=True)
            self._work_schedule_runs.clear()
            if self._watch_task:
                self._watch_task.cancel()
                try:
                    await self._watch_task
                except asyncio.CancelledError:
                    pass
            spontaneous_tasks = [
                c.codex_spontaneous_task for c in self.sessions.values()
                if c.codex_spontaneous_task is not None
                and not c.codex_spontaneous_task.done()
            ]
            for task in spontaneous_tasks:
                task.cancel()
            if spontaneous_tasks:
                await asyncio.gather(*spontaneous_tasks, return_exceptions=True)
            queue_tasks = [
                c.queued_query_drain_task for c in self.sessions.values()
                if c.queued_query_drain_task is not None
                and not c.queued_query_drain_task.done()
            ]
            for task in queue_tasks:
                task.cancel()
            if queue_tasks:
                await asyncio.gather(*queue_tasks, return_exceptions=True)
            for c in self.sessions.values():
                c.queued_query_drain_task = None
            await self.transport.stop()
            for c in list(self.sessions.values()):
                disconnected = False
                try:
                    await c.sdk.disconnect()
                    disconnected = True
                except Exception:
                    pass
                finally:
                    await self._cleanup_codex_steer_attachments(c)
                if c.btw and c.engine != "codex" and c.btw_real_id:
                    await self._delete_private_btw(
                        c.btw_real_id, c.cwd, forget=disconnected)

    async def _work_schedule_loop(self) -> None:
        """Claim and launch due Work tasks without stealing the UI focus."""
        while True:
            try:
                now = time.time()
                for engine in ("claude", "codex"):
                    due = await asyncio.to_thread(
                        self._work.for_engine(engine).claim_due_schedules, now)
                    for schedule in due:
                        task = asyncio.create_task(
                            self._run_work_schedule(engine, schedule))
                        self._work_schedule_runs.add(task)
                        task.add_done_callback(self._finish_work_schedule_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Work schedule scan failed")
            await asyncio.sleep(15)

    def _finish_work_schedule_task(self, task: asyncio.Task) -> None:
        """Consume background failures so asyncio never drops them silently."""
        self._work_schedule_runs.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "Work schedule task escaped its failure boundary",
                error_type=type(error).__name__,
            )

    async def _run_work_schedule(
        self, engine: str, schedule: dict[str, object]
    ) -> None:
        store = self._work.for_engine(engine)
        schedule_id = str(schedule["schedule_id"])
        run_id = str(schedule["run_id"])
        record = None
        ctx = None
        lease_task: asyncio.Task | None = None
        try:
            started = await asyncio.to_thread(
                store.mark_schedule_running, run_id, time.time()
            )
            if not started:
                log.warning(
                    "Work schedule lease was no longer claimable",
                    engine=engine,
                    schedule_id=schedule_id,
                    run_id=run_id,
                )
                return
            lease_task = asyncio.create_task(
                self._renew_work_schedule_lease(store, run_id)
            )
            record = await asyncio.to_thread(
                store.create_session, schedule.get("project_id")
            )
            ctx = await self._spawn(
                resume_id=None,
                cwd=record.cwd,
                engine=engine,
                space="work",
                work_id=record.work_id,
                permission_mode=("on-request" if engine == "codex" else None),
            )
            if ctx is None:
                raise RuntimeError("engine spawn failed")
            result = await self._handle_query(
                Query(
                    sid=ctx.key,
                    prompt=str(schedule["prompt"]),
                    msg_id=f"scheduled-{uuid4().hex}",
                )
            )
            if getattr(result, "type", None) == "error" or ctx.turn_task is None:
                raise RuntimeError("scheduled turn rejected")
            await ctx.turn_task
            if not ctx.session_id:
                raise RuntimeError("scheduled session id unavailable")
            status = await asyncio.to_thread(
                store.complete_schedule, run_id, ctx.session_id, None
            )
            log.info(
                "Work schedule completed",
                engine=engine,
                schedule_id=schedule_id,
                run_id=run_id,
                session_id=ctx.session_id,
                status=status,
            )
            await self._broadcast_work_schedule_state(
                engine, ctx.session_id, str(schedule["title"]), status, None
            )
        except asyncio.CancelledError:
            # Do not mark a cancelled wrapper-owned task failed. Its lease will
            # expire and the next process will recover the durable run row.
            raise
        except Exception as exc:
            log.warning(
                "Work schedule failed",
                engine=engine,
                schedule_id=schedule_id,
                error_type=type(exc).__name__,
            )
            message = "执行失败，请检查引擎和权限配置"
            status = await asyncio.to_thread(
                store.complete_schedule,
                run_id,
                ctx.session_id if ctx is not None else None,
                message,
            )
            if ctx is not None and not ctx.session_id:
                try:
                    await ctx.sdk.disconnect()
                except Exception:
                    pass
                self.sessions.pop(ctx.key, None)
            if record is not None and record.session_id is None:
                await asyncio.to_thread(store.abandon, record.work_id)
            await self._broadcast_work_schedule_state(
                engine,
                ctx.session_id if ctx is not None else None,
                str(schedule["title"]),
                status,
                message,
            )
        finally:
            if lease_task is not None:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)

    async def _renew_work_schedule_lease(self, store, run_id: str) -> None:
        while True:
            await asyncio.sleep(30)
            renewed = await asyncio.to_thread(
                store.renew_schedule_run, run_id, time.time()
            )
            if not renewed:
                return

    async def _broadcast_work_schedule_state(
        self,
        engine: str,
        session_id: str | None,
        title: str,
        status: str,
        error: str | None,
    ) -> None:
        try:
            await self.transport.send(await self._work_dashboard(engine))
            await self._handle_list_sessions(ListSessions(engine=engine, space="work"))
            if session_id:
                store = self._work.for_engine(engine)
                artifacts = await asyncio.to_thread(store.artifacts, session_id)
                await self.transport.send(
                    WorkArtifacts(
                        engine=engine, session_id=session_id, artifacts=artifacts
                    )
                )
                await self.transport.send(
                    Notice(
                        notice_id=f"schedule-{uuid4().hex}",
                        severity="warning" if error else "info",
                        category="runtime",
                        title=(
                            "定时任务等待重试"
                            if status == "queued"
                            else "定时任务失败"
                            if error
                            else "定时任务已完成"
                        ),
                        message=f"{title}：{error or '交付物已更新'}",
                        thread_id=session_id,
                        sid=session_id,
                    )
                )
        except Exception:
            # The durable run is already committed. A browser reconnect will
            # reload dashboard/session/artifact state even if this live fan-out
            # failed with the transport.
            log.exception(
                "Work schedule result broadcast failed",
                engine=engine,
                session_id=session_id,
            )

    async def _on_transport_connected(self) -> None:
        ctx = self._focused_ctx()
        await self.transport.send(Hello(
            role="wrapper",
            machine_id=self.cfg.machine_id,
            wrapper_generation=self.instance_id,
            cc_session_id=ctx.session_id if ctx else None,
            state=(ctx.state if ctx else "idle"),
            buffer_head_seq=(ctx.buffer.head_seq if ctx else 0),
            buffer_tail_seq=(ctx.buffer.tail_seq if ctx else 0),
        ))

    async def _restore_codex_owned_turns(self) -> None:
        """Rehydrate background daemon turns before accepting client commands."""
        try:
            leases = self._codex_turn_leases.list()
        except Exception as exc:
            log.warning(
                "Codex turn leases could not be listed",
                error_type=type(exc).__name__,
            )
            return
        for lease in leases:
            if self._ctx_by_sid(lease.session_id) is not None:
                continue
            ctx = await self._spawn(
                resume_id=lease.session_id,
                engine="codex",
                space="code",
                bootstrap=True,
            )
            if ctx is None:
                continue
            if (
                ctx.codex_owned_turn_id == lease.turn_id
                and ctx.state == "running"
            ):
                continue
            # The lease was stale or the official turn completed while the
            # replacement wrapper connected. It was spawned only for recovery;
            # leave the resident slot available until the user focuses it.
            self.sessions.pop(ctx.key, None)
            try:
                await ctx.sdk.disconnect()
            except Exception as exc:
                log.warning(
                    "stale Codex recovery proxy could not be disconnected",
                    session_id=lease.session_id,
                    error_type=type(exc).__name__,
                )

    # ---- emit (per-ctx seq + buffer + best-effort send), serialized per ctx ----

    @staticmethod
    def _path_is_below(root: str, candidate: str) -> bool:
        try:
            return os.path.commonpath((root, candidate)) == root
        except ValueError:
            return False

    def _observe_preview_path_event(self, ctx: SessionContext, msg) -> None:
        """Grant an exact, bounded preview capability after a successful write.

        Normal previews remain cwd-confined.  Claude/Codex can, however, be
        explicitly asked to create a deliverable elsewhere (for example
        ``/tmp/test.md``).  The resulting ToolUse + successful ToolResult is an
        auditable capability for that exact path; knowing any other absolute
        path is still insufficient to read it through Remote.
        """
        if isinstance(msg, ToolUse):
            if (msg.tool or "").lower() not in self.PREVIEW_WRITE_TOOLS:
                return
            raw_paths = self._tool_write_paths(msg.input)
            if not raw_paths:
                return
            # ``file_paths`` is the engine-neutral presentation field.  Keep the
            # original payload beside it so old clients and detailed tool views
            # retain the exact SDK/app-server input.
            if msg.input.get("file_paths") != list(raw_paths):
                msg.input = dict(msg.input)
                msg.input["file_paths"] = list(raw_paths)
            pending = ctx.preview_write_candidates
            pending[msg.tool_use_id] = raw_paths
            while len(pending) > self.PREVIEW_WRITE_CANDIDATE_CAP:
                pending.pop(next(iter(pending)))
            return

        if not isinstance(msg, ToolResult):
            return
        raw_paths = ctx.preview_write_candidates.pop(msg.tool_use_id, None)
        if raw_paths is None or msg.is_error or msg.status in {
                "failed", "declined", "cancelled", "interrupted"}:
            return
        root = os.path.realpath(ctx.cwd)
        paths = ctx.preview_external_paths
        for raw_path in raw_paths:
            candidate = os.path.realpath(
                raw_path if os.path.isabs(raw_path)
                else os.path.join(root, raw_path))
            if self._path_is_below(root, candidate):
                continue
            paths.pop(candidate, None)
            paths[candidate] = None
        while len(paths) > self.PREVIEW_EXTERNAL_PATH_CAP:
            paths.pop(next(iter(paths)))

    @staticmethod
    def _tool_write_paths(tool_input: dict) -> tuple[str, ...]:
        """Extract exact mutation targets from Claude and Codex tool payloads.

        Claude exposes one ``file_path`` while Codex fileChange/apply_patch uses
        ``changes`` (a descriptor list live, a path-keyed map in rollouts).  The
        canonical ``file_paths`` field is accepted first so replaying already-
        normalized events is idempotent.
        """
        candidates: list[object] = []
        canonical = tool_input.get("file_paths")
        if isinstance(canonical, (list, tuple)):
            candidates.extend(canonical)
        for key in ("file_path", "path", "notebook_path"):
            candidates.append(tool_input.get(key))

        changes = tool_input.get("changes")
        if isinstance(changes, list):
            for change in changes[:64]:
                if not isinstance(change, dict):
                    continue
                for key in ("path", "move_path", "destination_path", "to"):
                    candidates.append(change.get(key))
        elif isinstance(changes, dict):
            for path, change in list(changes.items())[:64]:
                candidates.append(path)
                if not isinstance(change, dict):
                    continue
                for key in ("path", "move_path", "destination_path", "to"):
                    candidates.append(change.get(key))

        paths: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if (not isinstance(candidate, str) or not candidate
                    or candidate.startswith("~") or len(candidate) > 4096
                    or candidate in seen):
                continue
            seen.add(candidate)
            paths.append(candidate)
            if len(paths) >= 64:
                break
        return tuple(paths)

    @staticmethod
    def _preview_external_paths(ctx: SessionContext) -> frozenset[str]:
        # Copy before handing the set to a worker thread.  Live tool events may
        # add another path while an Office conversion or file read is running.
        return frozenset(ctx.preview_external_paths)

    async def _emit_locked(self, ctx: SessionContext, msg) -> None:
        # Stamp routing before buffering so byte accounting includes the final
        # wire shape.  Every live and replayable /btw frame is owner-only; relay
        # treats an absent ``to`` as broadcast, so fail closed for an impossible
        # ownerless fork rather than leaking its contents.
        msg.sid = ctx.session_id or ctx.key
        if ctx.btw:
            if not ctx.owner_client_id:
                log.error("dropping frame for ownerless btw", sid=ctx.key,
                          type=getattr(msg, "type", None))
                return
            msg.to = ctx.owner_client_id
        if isinstance(msg, TurnEnd):
            # The replayable object is deliberately notification-free. Only a
            # copy sent on this live call receives presentation metadata.
            msg.notification_context = None
        if is_downstream(msg):
            msg.seq = ctx.next_seq()
            ctx.buffer.append(msg)
        live = (
            msg.model_copy(update={
                "notification_context": self._notification_context(ctx),
            })
            if isinstance(msg, TurnEnd)
            else msg
        )
        await self.transport.send(live)

    async def _emit(self, ctx: SessionContext, msg) -> None:
        self._observe_preview_path_event(ctx, msg)
        async with ctx.emit_lock:
            await self._emit_locked(ctx, msg)

    async def _emit_focused(self, msg) -> None:
        """For control-path errors with no target ctx (cap reached, bad cwd):
        route to the focused ctx if any, else send unbuffered."""
        ctx = self._focused_ctx()
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = self.focused_sid
            await self.transport.send(msg)

    async def _emit_to_sid(self, sid: Optional[str], msg) -> None:
        """Route a control-path frame to a SPECIFIC session's view (tagged by sid),
        even when it has no ctx — so a failed-to-spawn session's error surfaces on
        that session, not on whatever happens to be focused."""
        ctx = self.sessions.get(sid) if sid else None
        if ctx is not None:
            await self._emit(ctx, msg)
        else:
            msg.sid = sid or self.focused_sid
            await self.transport.send(msg)

    async def _missing_session_error(self, cmd, action: str) -> Error:
        """Reject a routed command whose session is no longer resident.

        Reliable commands are ACKed after their handler returns.  Returning a
        targeted Error here prevents an expired sid from becoming an ACK-only
        false success and keeps the failure out of an unrelated focused view.
        """
        sid = getattr(cmd, "sid", None)
        error = Error(
            code=ERR_NOT_RUNNING,
            message=f"该会话未启动，无法{action}",
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit_to_sid(sid, error)
        return error

    @staticmethod
    def _queued_query_size(cmd: Query) -> int:
        """Bound retained payloads without serializing another giant JSON copy."""
        size = 128 + len(cmd.prompt.encode("utf-8", "surrogatepass"))
        for image in cmd.images or ():
            size += (
                64
                + len(image.get("media_type", "").encode("utf-8"))
                + len(image.get("data", ""))
            )
        for file in cmd.files or ():
            size += (
                64
                + len(file.get("filename", "").encode(
                    "utf-8", "surrogatepass"))
                + len(file.get("data", ""))
            )
        return size

    @staticmethod
    def _query_queue_task_active(ctx: SessionContext) -> bool:
        task = ctx.queued_query_drain_task
        return task is not None and not task.done()

    def _queued_query_info(
        self, ctx: SessionContext, cmd: Query,
    ) -> QueuedQueryInfo:
        return QueuedQueryInfo(
            msg_id=cmd.msg_id,
            kind="replace" if cmd.delivery == "replace" else "queue",
            prompt_preview=cmd.prompt[:512],
            image_count=len(cmd.images or ()),
            file_count=len(cmd.files or ()),
            retained_bytes=self._queued_query_size(cmd),
            error=ctx.queued_query_errors.get(cmd.msg_id),
        )

    def _query_queue_state(self, ctx: SessionContext) -> QueryQueueState:
        return QueryQueueState(
            items=[
                self._queued_query_info(ctx, command)
                for command in ctx.queued_queries
            ],
            total_count=self._queued_query_count,
            total_bytes=self._queued_query_bytes,
        )

    async def _emit_deferred_query_error(
        self,
        ctx: SessionContext,
        cmd: Query,
        code: str,
        message: str,
    ) -> Error:
        error = Error(
            code=code,
            message=message,
            msg_id=cmd.msg_id,
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
            sid=ctx.session_id or ctx.key,
        )
        # A rejected enqueue is private command feedback, not shared session
        # narrative. Reliable-command retry caches it for the origin; buffering
        # it in the session ring would expose it to other clients on Hello.
        await self.transport.send(error)
        # A rejected replacement may have optimistically hidden the previous
        # server-owned replacement in the browser. Re-publish the unchanged
        # authoritative projection after the private error so it is restored
        # without waiting for another reconnect or queue mutation.
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                try:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
                except Exception as exc:
                    log.warning(
                        "query queue rejection projection delayed",
                        session_id=ctx.session_id or ctx.key,
                        error_type=type(exc).__name__,
                    )
        return error

    async def _enqueue_deferred_query(
        self, ctx: SessionContext, cmd: Query,
    ) -> Error | None:
        """Transfer one append/replace query to the wrapper-owned bounded FIFO."""
        if not cmd.prompt and not cmd.images and not cmd.files:
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BAD_PROMPT,
                "消息内容为空，请输入内容或添加附件。",
            )
        attachment_error = validate_attachments(cmd.images, cmd.files)
        if attachment_error:
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BAD_PROMPT,
                "附件不符合要求，请调整后重试。",
            )
        if ctx.write_state != "writable":
            return await self._emit_deferred_query_error(
                ctx, cmd, ERR_BUSY,
                "该会话当前不可写，本次排队未提交。",
            )

        size = self._queued_query_size(cmd)
        accepted = False
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                duplicate = next((
                    queued for queued in ctx.queued_queries
                    if queued.msg_id == cmd.msg_id
                ), None)
                if duplicate is not None:
                    # An ACK-lost replay is idempotent. Re-publish the current
                    # projection so the origin can recover without another turn.
                    state = self._query_queue_state(ctx)
                else:
                    replace_index = next((
                        index for index, queued in enumerate(ctx.queued_queries)
                        if queued.delivery == "replace"
                    ), None) if cmd.delivery == "replace" else None
                    replaced = (
                        ctx.queued_queries[replace_index]
                        if replace_index is not None else None
                    )
                    if (
                        replaced is not None
                        and replaced.msg_id
                        == ctx.queued_query_starting_msg_id
                    ):
                        error = Error(
                            code=ERR_BUSY,
                            message=(
                                "上一条替换消息正在启动，本次排队未提交；"
                                "请稍后重试。"
                            ),
                            msg_id=cmd.msg_id,
                            request_id=getattr(cmd, "cmd_id", None),
                            to=getattr(cmd, "client_id", None),
                            sid=ctx.session_id or ctx.key,
                        )
                        try:
                            await self.transport.send(error)
                        except Exception as exc:
                            log.warning(
                                "deferred query rejection delivery delayed",
                                session_id=ctx.session_id or ctx.key,
                                error_type=type(exc).__name__,
                            )
                        try:
                            await self._emit_locked(
                                ctx, self._query_queue_state(ctx))
                        except Exception as exc:
                            log.warning(
                                "query queue rejection projection delayed",
                                session_id=ctx.session_id or ctx.key,
                                error_type=type(exc).__name__,
                            )
                        return error
                    replaced_size = (
                        self._queued_query_size(replaced)
                        if replaced is not None else 0
                    )
                    projected_count = (
                        self._queued_query_count
                        - (1 if replaced is not None else 0)
                        + 1
                    )
                    projected_bytes = (
                        self._queued_query_bytes - replaced_size + size
                    )
                    if (
                        projected_count > MAX_QUERY_QUEUE_ITEMS
                        or projected_bytes > MAX_QUERY_QUEUE_BYTES
                    ):
                        error = Error(
                            code=ERR_QUEUE_FULL,
                            message=(
                                "服务端排队已满（最多 32 条 / 64 MiB），"
                                "请等待已有任务开始后重试。"
                            ),
                            msg_id=cmd.msg_id,
                            request_id=getattr(cmd, "cmd_id", None),
                            to=getattr(cmd, "client_id", None),
                            sid=ctx.session_id or ctx.key,
                        )
                        try:
                            await self.transport.send(error)
                        except Exception as exc:
                            log.warning(
                                "deferred query rejection delivery delayed",
                                session_id=ctx.session_id or ctx.key,
                                error_type=type(exc).__name__,
                            )
                        try:
                            await self._emit_locked(
                                ctx, self._query_queue_state(ctx))
                        except Exception as exc:
                            log.warning(
                                "query queue rejection projection delayed",
                                session_id=ctx.session_id or ctx.key,
                                error_type=type(exc).__name__,
                            )
                        return error

                    if replace_index is not None:
                        ctx.queued_queries.pop(replace_index)
                        ctx.queued_query_errors.pop(replaced.msg_id, None)
                        ctx.queued_query_bytes -= replaced_size
                        self._queued_query_count -= 1
                        self._queued_query_bytes -= replaced_size
                    if cmd.delivery == "replace":
                        ctx.queued_queries.insert(0, cmd)
                    else:
                        ctx.queued_queries.append(cmd)
                    ctx.queued_query_bytes += size
                    self._queued_query_count += 1
                    self._queued_query_bytes += size
                    state = self._query_queue_state(ctx)
                    accepted = True
                try:
                    await self._emit_locked(ctx, state)
                except Exception as exc:
                    # The replayable state is already in the ring. Queue
                    # ownership and execution must not depend on a live browser.
                    log.warning(
                        "query queue live projection delayed",
                        session_id=ctx.session_id or ctx.key,
                        error_type=type(exc).__name__,
                    )

        if accepted:
            log.info(
                "deferred query accepted",
                session_id=ctx.session_id or ctx.key,
                msg_id=cmd.msg_id,
                delivery=cmd.delivery,
                queued=len(ctx.queued_queries),
            )
        self._schedule_query_queue_drain(ctx)
        return None

    async def _handle_cancel_queued_query(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "取消排队消息")
        removed = None
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                index = next((
                    index for index, queued in enumerate(ctx.queued_queries)
                    if queued.msg_id == cmd.msg_id
                ), None)
                if index is not None:
                    candidate = ctx.queued_queries[index]
                    if (
                        candidate.msg_id
                        != ctx.queued_query_starting_msg_id
                    ):
                        removed = ctx.queued_queries.pop(index)
                        ctx.queued_query_errors.pop(removed.msg_id, None)
                        size = self._queued_query_size(removed)
                        ctx.queued_query_bytes -= size
                        self._queued_query_count -= 1
                        self._queued_query_bytes -= size
                try:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
                except Exception as exc:
                    log.warning(
                        "query queue cancellation projection delayed",
                        session_id=ctx.session_id or ctx.key,
                        error_type=type(exc).__name__,
                    )
        if removed is not None:
            ctx.queued_query_wakeup.set()
        if removed is not None:
            log.info(
                "deferred query cancelled",
                session_id=ctx.session_id or ctx.key,
                msg_id=cmd.msg_id,
            )

    async def _handle_get_queued_query(self, cmd) -> QueuedQueryDetail:
        """Return one full prompt privately without buffering queue payloads."""
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            detail = QueuedQueryDetail(
                sid=cmd.sid,
                msg_id=cmd.msg_id,
                request_id=cmd.cmd_id,
                error="排队消息所在会话已不在运行。",
                to=cmd.client_id,
            )
        else:
            async with ctx.queued_query_lock:
                queued = next((
                    candidate for candidate in ctx.queued_queries
                    if candidate.msg_id == cmd.msg_id
                ), None)
                if queued is None:
                    detail = QueuedQueryDetail(
                        sid=ctx.session_id or ctx.key,
                        msg_id=cmd.msg_id,
                        request_id=cmd.cmd_id,
                        error="该消息已开始执行或已从队列移除。",
                        to=cmd.client_id,
                    )
                else:
                    detail = QueuedQueryDetail(
                        sid=ctx.session_id or ctx.key,
                        msg_id=cmd.msg_id,
                        request_id=cmd.cmd_id,
                        prompt=queued.prompt,
                        kind=(
                            "replace"
                            if queued.delivery == "replace"
                            else "queue"
                        ),
                        image_count=len(queued.images or ()),
                        file_count=len(queued.files or ()),
                        error=ctx.queued_query_errors.get(queued.msg_id),
                        to=cmd.client_id,
                    )
        await self.transport.send(detail)
        return detail

    async def _handle_update_queued_query(self, cmd) -> QueuedQueryUpdated:
        """Atomically edit one queued prompt while preserving its attachments."""
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            result = QueuedQueryUpdated(
                sid=cmd.sid,
                msg_id=cmd.msg_id,
                request_id=cmd.cmd_id,
                updated=False,
                error="排队消息所在会话已不在运行。",
                to=cmd.client_id,
            )
            await self.transport.send(result)
            return result

        updated = False
        error = None
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                index = next((
                    index for index, queued in enumerate(ctx.queued_queries)
                    if queued.msg_id == cmd.msg_id
                ), None)
                if index is None:
                    error = "该消息已开始执行或已从队列移除。"
                elif (
                    ctx.queued_queries[index].msg_id
                    == ctx.queued_query_starting_msg_id
                ):
                    error = "该消息正在启动，请等待本次启动结果后再编辑。"
                else:
                    previous = ctx.queued_queries[index]
                    if not cmd.prompt and not previous.images and not previous.files:
                        error = "消息内容为空，请输入内容后再保存。"
                    else:
                        replacement = previous.model_copy(
                            deep=True, update={"prompt": cmd.prompt})
                        previous_size = self._queued_query_size(previous)
                        replacement_size = self._queued_query_size(replacement)
                        projected_bytes = (
                            self._queued_query_bytes
                            - previous_size
                            + replacement_size
                        )
                        if projected_bytes > MAX_QUERY_QUEUE_BYTES:
                            error = (
                                "编辑后的服务端队列超过 64 MiB，"
                                "请缩短消息后重试。"
                            )
                        else:
                            ctx.queued_queries[index] = replacement
                            ctx.queued_query_errors.pop(
                                previous.msg_id, None)
                            ctx.queued_query_bytes += (
                                replacement_size - previous_size)
                            self._queued_query_bytes = projected_bytes
                            updated = True
                            try:
                                await self._emit_locked(
                                    ctx, self._query_queue_state(ctx))
                            except Exception as exc:
                                # The authoritative projection is already in
                                # the replay ring. Editing must not depend on a
                                # currently connected browser.
                                log.warning(
                                    "query queue edit projection delayed",
                                    session_id=ctx.session_id or ctx.key,
                                    error_type=type(exc).__name__,
                                )

        result = QueuedQueryUpdated(
            sid=ctx.session_id or ctx.key,
            msg_id=cmd.msg_id,
            request_id=cmd.cmd_id,
            updated=updated,
            error=error,
            to=cmd.client_id,
        )
        await self.transport.send(result)
        if updated:
            log.info(
                "deferred query updated",
                session_id=ctx.session_id or ctx.key,
                msg_id=cmd.msg_id,
            )
            self._schedule_query_queue_drain(ctx)
        return result

    def _schedule_query_queue_drain(self, ctx: SessionContext) -> None:
        if not ctx.queued_queries or not self._is_resident_context(ctx):
            return
        current = ctx.queued_query_drain_task
        if current is not None and not current.done():
            ctx.queued_query_wakeup.set()
            return
        task = asyncio.create_task(
            self._drain_query_queue(ctx),
            name=f"query-queue-{ctx.session_id or ctx.key}",
        )
        ctx.queued_query_drain_task = task

    async def _drain_query_queue(self, ctx: SessionContext) -> None:
        """Start wrapper-owned queries in order without any browser callback."""
        current_task = asyncio.current_task()
        retry_delay = 1.0
        cancelled = False
        try:
            while ctx.queued_queries and self._is_resident_context(ctx):
                active = next((
                    task for task in (
                        ctx.turn_task, ctx.codex_spontaneous_task)
                    if task is not None and not task.done()
                    and task is not current_task
                ), None)
                if active is not None or ctx.state != "idle":
                    ctx.queued_query_wakeup.clear()
                    if active is not None:
                        active.add_done_callback(
                            lambda _done: ctx.queued_query_wakeup.set())
                    # Close the clear/recheck race with a terminal state or task
                    # completion that happened immediately before the callback.
                    if (
                        ctx.state == "idle"
                        and not any(
                            task is not None and not task.done()
                            for task in (
                                ctx.turn_task, ctx.codex_spontaneous_task)
                        )
                    ):
                        ctx.queued_query_wakeup.set()
                    await ctx.queued_query_wakeup.wait()
                    retry_delay = 1.0
                    continue

                command = None
                result = None
                launch_error: str | None = None
                # Any meaningful queue/state mutation after this point requests
                # another preflight.  If this attempt fails and nobody changes
                # anything, wait instead of emitting the same error in a loop.
                ctx.queued_query_wakeup.clear()
                try:
                    # The browser command lane and this worker share query_lock:
                    # once an item starts preflight, no later immediate query can
                    # claim the same idle boundary first.  The item deliberately
                    # remains queued until every synchronous rejection path has
                    # passed, so a daemon/ownership failure cannot drop work.
                    async with ctx.query_lock:
                        active_now = any(
                            task is not None and not task.done()
                            for task in (
                                ctx.turn_task, ctx.codex_spontaneous_task)
                        )
                        if ctx.state != "idle" or active_now:
                            continue
                        async with ctx.queued_query_lock:
                            if ctx.queued_queries:
                                command = ctx.queued_queries[0]
                                ctx.queued_query_starting_msg_id = (
                                    command.msg_id)
                        if command is not None:
                            immediate = command.model_copy(
                                deep=True, update={"delivery": "immediate"})
                            result = await self._handle_immediate_query(
                                ctx, immediate)
                            if not isinstance(result, Error):
                                async with ctx.emit_lock:
                                    async with ctx.queued_query_lock:
                                        index = next((
                                            index for index, queued
                                            in enumerate(ctx.queued_queries)
                                            if queued.msg_id == command.msg_id
                                        ), None)
                                        if index is not None:
                                            removed = ctx.queued_queries.pop(
                                                index)
                                            size = self._queued_query_size(
                                                removed)
                                            ctx.queued_query_bytes -= size
                                            self._queued_query_count -= 1
                                            self._queued_query_bytes -= size
                                        ctx.queued_query_errors.pop(
                                            command.msg_id, None)
                                        if (
                                            ctx.queued_query_starting_msg_id
                                            == command.msg_id
                                        ):
                                            ctx.queued_query_starting_msg_id = (
                                                None)
                                        try:
                                            await self._emit_locked(
                                                ctx,
                                                self._query_queue_state(ctx),
                                            )
                                        except Exception as exc:
                                            log.warning(
                                                "query queue start projection "
                                                "delayed",
                                                session_id=(
                                                    ctx.session_id or ctx.key
                                                ),
                                                error_type=type(exc).__name__,
                                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if command is None:
                        raise
                    log.exception(
                        "deferred query launch failed",
                        session_id=ctx.session_id or ctx.key,
                        msg_id=command.msg_id,
                        error_type=type(exc).__name__,
                    )
                    launch_error = "排队消息启动失败；消息仍在队列中，请重试。"
                    try:
                        await self._emit(ctx, Error(
                            code=ERR_INTERNAL,
                            message=launch_error,
                            msg_id=command.msg_id,
                        ))
                    except Exception as emit_exc:
                        log.warning(
                            "deferred query launch error delivery delayed",
                            session_id=ctx.session_id or ctx.key,
                            error_type=type(emit_exc).__name__,
                        )
                if command is None:
                    continue
                if isinstance(result, Error):
                    launch_error = result.message
                if launch_error is not None:
                    async with ctx.emit_lock:
                        async with ctx.queued_query_lock:
                            if any(
                                queued.msg_id == command.msg_id
                                for queued in ctx.queued_queries
                            ):
                                ctx.queued_query_errors[
                                    command.msg_id] = launch_error
                            if (
                                ctx.queued_query_starting_msg_id
                                == command.msg_id
                            ):
                                ctx.queued_query_starting_msg_id = None
                            try:
                                await self._emit_locked(
                                    ctx, self._query_queue_state(ctx))
                            except Exception as exc:
                                log.warning(
                                    "query queue failure projection delayed",
                                    session_id=ctx.session_id or ctx.key,
                                    error_type=type(exc).__name__,
                                )
                    try:
                        await asyncio.wait_for(
                            ctx.queued_query_wakeup.wait(),
                            timeout=retry_delay,
                        )
                        retry_delay = 1.0
                    except asyncio.TimeoutError:
                        # A daemon/control transition can become usable without
                        # a browser command or lifecycle state change. Keep an
                        # autonomous, bounded-backoff retry path for sleeping
                        # clients while avoiding a hot rejection loop.
                        retry_delay = min(retry_delay * 2, 30.0)
                    continue
                log.info(
                    "deferred query started",
                    session_id=ctx.session_id or ctx.key,
                    msg_id=command.msg_id,
                )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            log.exception(
                "query queue drain stopped unexpectedly",
                session_id=ctx.session_id or ctx.key,
            )
        finally:
            if ctx.queued_query_drain_task is current_task:
                ctx.queued_query_drain_task = None
            if ctx.queued_query_starting_msg_id is not None:
                ctx.queued_query_starting_msg_id = None
            if (
                not cancelled
                and ctx.queued_queries
                and self._is_resident_context(ctx)
            ):
                self._schedule_query_queue_drain(ctx)

    async def _discard_query_queue(
        self, ctx: SessionContext, *, publish: bool = False,
    ) -> None:
        """Release bounded queue accounting before a resident is destroyed."""
        async with ctx.emit_lock:
            async with ctx.queued_query_lock:
                self._queued_query_count -= len(ctx.queued_queries)
                self._queued_query_bytes -= ctx.queued_query_bytes
                ctx.queued_queries = []
                ctx.queued_query_bytes = 0
                ctx.queued_query_errors = {}
                ctx.queued_query_starting_msg_id = None
                if publish:
                    await self._emit_locked(
                        ctx, self._query_queue_state(ctx))
        ctx.queued_query_wakeup.set()
        task = ctx.queued_query_drain_task
        if (
            task is not None
            and task is not asyncio.current_task()
            and not task.done()
        ):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ctx.queued_query_drain_task = None

    async def _set_state(self, ctx: SessionContext, state: State) -> None:
        ctx.state = state
        ctx.queued_query_wakeup.set()
        await self._emit(ctx, StateEvent(state=state))
        log.info("state transition", sid=ctx.session_id, state=state)

    # ---- command dispatch ----

    def _command_seen(self, client_id: str, cmd_id: str) -> tuple[bool, tuple[object, ...]]:
        bucket = self._processed_commands.get(client_id)
        if bucket is None or cmd_id not in bucket:
            return False, ()
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        return True, bucket[cmd_id]

    def _remember_command(self, client_id: str, cmd_id: str,
                          responses: tuple[object, ...] = ()) -> None:
        response_bytes = self._command_response_bytes(responses)
        if response_bytes > self.COMMAND_RESPONSE_BYTES:
            # The command identity still suppresses a duplicate mutation. Its
            # oversized narrative remains available from the session ring or
            # canonical history instead of monopolizing the global retry cache.
            responses = ()
            response_bytes = 0
        bucket = self._processed_commands.get(client_id)
        if bucket is None:
            bucket = OrderedDict()
            self._processed_commands[client_id] = bucket
        previous_size = self._processed_command_sizes.pop(
            (client_id, cmd_id), 0)
        self._processed_command_bytes -= previous_size
        bucket[cmd_id] = responses
        self._processed_command_sizes[(client_id, cmd_id)] = response_bytes
        self._processed_command_bytes += response_bytes
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        while len(bucket) > self.COMMAND_IDS_PER_CLIENT:
            dropped_id, _ = bucket.popitem(last=False)
            self._processed_command_bytes -= self._processed_command_sizes.pop(
                (client_id, dropped_id), 0)
        while len(self._processed_commands) > self.COMMAND_CLIENTS:
            dropped_client, dropped_bucket = (
                self._processed_commands.popitem(last=False))
            for dropped_id in dropped_bucket:
                self._processed_command_bytes -= (
                    self._processed_command_sizes.pop(
                        (dropped_client, dropped_id), 0))
        while (self._processed_command_bytes > self.COMMAND_RESPONSE_BYTES
               and self._processed_commands):
            dropped = False
            for oldest_client, oldest_bucket in self._processed_commands.items():
                for oldest_id in oldest_bucket:
                    key = (oldest_client, oldest_id)
                    size = self._processed_command_sizes.get(key, 0)
                    if size <= 0:
                        continue
                    # Preserve the at-most-once identity as an empty tombstone;
                    # only the replay payload is evicted under byte pressure.
                    oldest_bucket[oldest_id] = ()
                    self._processed_command_sizes[key] = 0
                    self._processed_command_bytes -= size
                    dropped = True
                    break
                if dropped:
                    break
            if not dropped:
                break

    @staticmethod
    def _command_response_bytes(responses: tuple[object, ...]) -> int:
        total = 0
        for response in responses:
            try:
                total += len(response.model_dump_json().encode("utf-8"))
            except Exception:
                total += 1024
        return total

    def _rekey_cached_create_responses(self, old_key: str, session_id: str,
                                       cwd: str) -> None:
        """Keep a cached create response usable if id capture happened while its
        original Focus/ACK were lost. Replay re-key first, then focus the real id."""
        for client_id, bucket in self._processed_commands.items():
            for cmd_id, responses in list(bucket.items()):
                updated: list[object] = []
                changed = False
                for response in responses:
                    if (isinstance(response, SessionFocus)
                            and response.session_id == old_key):
                        updated.append(SessionRekey(
                            old_key=old_key,
                            session_id=session_id,
                            cwd=cwd,
                            sid=session_id,
                            to=response.to,
                        ))
                        updated.append(response.model_copy(deep=True, update={
                            "session_id": session_id,
                            "cwd": cwd,
                            "sid": session_id,
                        }))
                        changed = True
                    elif (not isinstance(response, Snapshot)
                          and getattr(response, "sid", None) == old_key):
                        # Snapshot deliberately stays temp-keyed so the following
                        # SessionRekey has a runtime to migrate. All later cached
                        # frames must target the real id after that migration.
                        updated.append(response.model_copy(
                            deep=True, update={"sid": session_id}))
                        changed = True
                    else:
                        updated.append(response)
                if changed:
                    bucket[cmd_id] = tuple(updated)
                    key = (client_id, cmd_id)
                    previous_size = self._processed_command_sizes.get(key, 0)
                    next_size = self._command_response_bytes(bucket[cmd_id])
                    self._processed_command_sizes[key] = next_size
                    self._processed_command_bytes += next_size - previous_size

    async def _send_command_ack(self, client_id: str, cmd_id: str) -> None:
        await self.transport.send(CommandAck(
            cmd_id=cmd_id,
            client_id=client_id,
            to=client_id,
        ))

    async def _process_command_safely(self, cmd) -> None:
        """Run one command without letting a handler failure stop the loop."""
        try:
            await self._process_command(cmd)
        except Exception:
            log.exception("command handling failed", type=cmd.type)

    def _start_models_command(self, cmd) -> None:
        """Resolve catalogs/defaults without blocking the serial command lane.

        Keep the normal reliable-command lifecycle inside the task: Models is
        queued before CommandAck, and a reconnect retry with the same command id
        coalesces onto the still-running task instead of duplicating the read.
        """
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._models_command_tasks.get(key)
        if current is not None and not current.done():
            log.debug("models command already resolving",
                      client_id=client_id, cmd_id=cmd_id)
            return

        task = asyncio.create_task(self._process_command_safely(cmd))
        self._models_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._models_command_tasks.get(key) is done:
                self._models_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_status_command(self, cmd) -> None:
        """Read Codex status without blocking Query/Interrupt intake."""
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._status_command_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._status_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._status_command_tasks.get(key) is done:
                self._status_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_session_list_command(self, cmd) -> None:
        """Resolve sidebar catalogs without blocking query/new-session input."""
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._session_list_command_tasks.add(task)
        task.add_done_callback(self._session_list_command_tasks.discard)

    def _start_history_command(self, cmd) -> None:
        """Read one history page without blocking query/interrupt intake."""
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._history_command_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._history_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._history_command_tasks.get(key) is done:
                self._history_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_capabilities_command(self, cmd) -> None:
        """Keep extension discovery off the serial query/mutation lane."""
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._capabilities_command_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._capabilities_command_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._capabilities_command_tasks.get(key) is done:
                self._capabilities_command_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _start_interactive_control_command(self, cmd) -> None:
        """Run one answerable control without blocking the receive loop.

        Reliable command ids coalesce reconnect retries while the question is
        open.  The normal command processor still owns the final ACK/cache
        boundary after the user answers and the native TUI confirms the change.
        """
        client_id = getattr(cmd, "client_id", None) or ""
        cmd_id = getattr(cmd, "cmd_id", None) or f"untracked-{id(cmd)}"
        key = (client_id, cmd_id)
        current = self._interactive_control_tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._process_command_safely(cmd))
        self._interactive_control_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._interactive_control_tasks.get(key) is done:
                self._interactive_control_tasks.pop(key, None)

        task.add_done_callback(forget)

    def _refresh_cached_response(self, response):
        """Copy a cached one-shot response, refreshing volatile snapshots.

        A create command can be retried after its first response was lost while
        the newly-created turn is already running. Replaying the original idle
        Snapshot would roll the client back to idle, so derive current state from
        the resident context without re-executing the command.
        """
        replay = response.model_copy(deep=True)
        if isinstance(replay, Snapshot) and replay.sid:
            ctx = self._ctx_for(replay.sid)
            if ctx is not None:
                replay.state = ctx.buffer.latest_state() or ctx.state
                replay.tail_text = ctx.buffer.latest_tail_text()
                replay.cc_session_id = ctx.session_id
                replay.cwd = ctx.cwd
                replay.generation = self.instance_id
        return replay

    async def _process_command(self, cmd) -> None:
        """Deduplicate reliable client commands and ACK completed handlers.

        A business rejection that emits Error still returns normally and is
        acknowledged. An unexpected exception is deliberately neither remembered
        nor ACKed, so the client can retry instead of accepting partial failure.
        """
        client_id = getattr(cmd, "client_id", None)
        cmd_id = getattr(cmd, "cmd_id", None)
        reliable = is_reliable_command(cmd) and client_id and cmd_id
        seen, cached_responses = (
            self._command_seen(client_id, cmd_id) if reliable else (False, ()))
        if seen:
            if cmd.type in self.SAFE_RETRY_COMMANDS:
                # The original one-shot response may have died on the same link as
                # its ACK. Safe reads are intentionally re-run to recreate it.
                await self._handle(cmd)
                log.info("duplicate read command replayed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type)
            else:
                for response in cached_responses:
                    replay = self._refresh_cached_response(response)
                    # A cached business error may originally have been broadcast
                    # with its turn. Retry it only to the command's origin.
                    if getattr(replay, "to", None) is None:
                        replay.to = client_id
                    await self.transport.send(replay)
                log.info("duplicate command suppressed", client_id=client_id,
                         cmd_id=cmd_id, type=cmd.type,
                         responses=len(cached_responses))
            await self._send_command_ack(client_id, cmd_id)
            return

        result = await self._handle(cmd)

        if reliable:
            # Safe reads are recreated on retry, so retaining their potentially
            # large History/file/image payloads in the command-id LRU buys
            # nothing and can multiply memory by clients × commands.
            if cmd.type in self.SAFE_RETRY_COMMANDS:
                responses = ()
            elif cmd.type == "steer":
                candidates = (
                    result if isinstance(result, (tuple, list)) else (result,))
                # Successful narrative is already in the bounded turn ring and
                # canonical history (with clientMsgId). Cache only correlated
                # failures; copying a legal multi-megabyte steer into the global
                # command LRU would duplicate user payloads.
                responses = tuple(
                    response.model_copy(deep=True)
                    for response in candidates
                    if isinstance(response, Error)
                )
            else:
                candidates = (
                    result if isinstance(result, (tuple, list)) else (result,))
                responses = tuple(
                    response.model_copy(deep=True)
                    for response in candidates
                    if getattr(response, "type", None)
                )
            self._remember_command(client_id, cmd_id, responses)
            await self._send_command_ack(client_id, cmd_id)

    async def _handle(self, cmd) -> None:
        rejected = await self._reject_nonowner_btw_command(cmd)
        if rejected is not None:
            return rejected
        result = await self._command_router.dispatch(cmd)
        if result is UNHANDLED_COMMAND:
            log.warning(
                "unexpected command",
                type=cmd.type,
                role=getattr(cmd, "role", None),
            )
            return None
        return result

    async def _handle_client_hello(self, cmd) -> None:
        # A fresh client (no cursor for a sid) gets exactly one lightweight
        # Snapshot for that session. A reconnecting client explicitly names the
        # sids it already knows and receives only seq > cursor from those rings.
        # This restores live tail loss without reviving the old all-session replay
        # flood. A concurrent turn may re-key sessions across the awaits below.
        supplied_cursors = getattr(cmd, "cursors", None)
        cursors = dict(supplied_cursors) if isinstance(supplied_cursors, dict) else {}
        supplied_generations = getattr(cmd, "generations", None)
        generations = (dict(supplied_generations)
                       if isinstance(supplied_generations, dict) else {})
        # Compatibility for the TUI/live scripts that predate per-session cursor
        # maps. It is necessarily scoped to the wrapper's focused session.
        legacy_cursor = getattr(cmd, "last_seq", None)
        focused = self._focused_ctx()
        if not cursors and isinstance(legacy_cursor, int) and focused is not None:
            focused_id = focused.session_id or focused.key
            if focused_id:
                cursors[focused_id] = legacy_cursor
        for old_key in list(cursors):
            alias = self._session_aliases.get(old_key)
            if alias is None:
                continue
            real_id = alias["session_id"]
            await self.transport.send(SessionRekey(
                old_key=old_key,
                session_id=real_id,
                cwd=alias["cwd"],
                sid=real_id,
                to=cmd.client_id,
                route_id=getattr(cmd, "route_id", None),
            ))
            old_cursor = cursors.pop(old_key)
            cursors[real_id] = max(cursors.get(real_id, 0), old_cursor)
            if old_key in generations:
                generations[real_id] = generations.pop(old_key)
            self._session_aliases.move_to_end(old_key)
        replayed = 0
        for key, ctx in list(self.sessions.items()):
            if ctx.btw and ctx.owner_client_id != cmd.client_id:
                continue  # ephemeral fork replay is private to its creating client
            sid = ctx.session_id or key
            tail = ctx.buffer.latest_tail_text()
            st = ctx.buffer.latest_state() or ctx.state
            async with ctx.emit_lock:
                if sid in cursors:
                    same_generation = generations.get(sid) == self.instance_id
                    frames = ctx.buffer.replay_from(
                        cursors[sid] if same_generation else 0,
                        cc_session_id=ctx.session_id,
                        state=st,
                        tail_text=tail,
                        cwd=ctx.cwd,
                        rebuild=not same_generation,
                        generation=self.instance_id,
                    )
                    replayed += 1
                else:
                    frames = [Snapshot(
                        cc_session_id=ctx.session_id,
                        state=st,
                        tail_text=tail,
                        cwd=ctx.cwd,
                        generation=self.instance_id,
                        control=self._session_control(ctx),
                    )]
                    if st != "idle":
                        frames.extend(ctx.buffer.current_turn_replay(
                            generation=self.instance_id,
                            message_id=ctx.active_msg_id))
                for frame in frames:
                    # Never mutate a shared ring event with per-client routing.
                    await self.transport.send(frame.model_copy(
                        deep=True, update={
                            "to": cmd.client_id,
                            "sid": sid,
                            "route_id": getattr(cmd, "route_id", None),
                        }))
                # ReplayStart's synthetic Snapshot predates protocol-v15 and is
                # built by RingBuffer. Always follow replay with the current
                # revisioned control value; same-revision delivery is idempotent.
                if sid in cursors:
                    await self.transport.send(self._session_control(ctx).model_copy(
                        deep=True, update={
                            "to": cmd.client_id,
                            "sid": sid,
                            "route_id": getattr(cmd, "route_id", None),
                        }))
                # Queue ownership lives in the wrapper, so a browser which slept
                # past enqueue/start/cancel transitions must receive the current
                # projection even when its replay cursor is already at the tail.
                async with ctx.queued_query_lock:
                    queue_state = self._query_queue_state(ctx)
                await self.transport.send(queue_state.model_copy(
                    deep=True, update={
                        "to": cmd.client_id,
                        "sid": sid,
                        "route_id": getattr(cmd, "route_id", None),
                    }))
                # Permission/collaboration modes are live control state, not
                # transcript history. Always seed them on hello even when the
                # browser's replay cursor is already at the ring tail.
                permission_mode = _session_permission_mode(ctx)
                ctx.announced_perm = permission_mode
                await self.transport.send(Perm(
                    mode=permission_mode,
                    sid=sid,
                    to=cmd.client_id,
                    route_id=getattr(cmd, "route_id", None),
                ))
                if ctx.engine == "codex":
                    permission_profile = _session_permission_profile(ctx)
                    ctx.announced_permission_profile = permission_profile
                    await self.transport.send(PermissionProfile(
                        profile=permission_profile,
                        sid=sid,
                        to=cmd.client_id,
                        route_id=getattr(cmd, "route_id", None),
                    ))
                    web_search = _session_web_search(ctx)
                    if web_search:
                        ctx.announced_web_search = web_search
                        await self.transport.send(WebSearch(
                            mode=web_search,
                            sid=sid,
                            to=cmd.client_id,
                            route_id=getattr(cmd, "route_id", None),
                        ))
                model = _session_model(ctx)
                if model:
                    ctx.announced_model = model
                    await self.transport.send(Model(
                        model=model,
                        sid=sid,
                        to=cmd.client_id,
                        route_id=getattr(cmd, "route_id", None),
                    ))
                effort = _session_effort(ctx)
                if effort:
                    ctx.announced_effort = effort
                    await self.transport.send(Effort(
                        effort=effort,
                        sid=sid,
                        to=cmd.client_id,
                        route_id=getattr(cmd, "route_id", None),
                    ))
                if ctx.engine == "codex":
                    await self.transport.send(CollaborationMode(
                        mode=getattr(ctx.sdk, "collaboration_mode", "default"),
                        sid=sid,
                        to=cmd.client_id,
                        route_id=getattr(cmd, "route_id", None),
                    ))
        log.info("client hello handled", client_id=cmd.client_id,
                 sessions=len(self.sessions), replayed=replayed)

    # ---- history: on-demand transcript read + EXTERNAL-append mirror ----

    # Codex-only fallback for an orphan task marker whose writer disappeared.
    # Claude ownership never expires by time; it follows stable process identity.
    EXTERNAL_TTL = 60.0
    CODEX_TURN_TRACK_MAX = 512
    CODEX_TURN_ATTRIBUTION_GRACE = 3.0
    CLAUDE_OWNED_MESSAGE_MAX = 512
    WATCH_READ_MAX = 4 * 1024 * 1024
    CODEX_TAIL_READ_MAX = 16 * 1024 * 1024
    MIRROR_LIMIT = 4        # lightweight moving head; older turns stay paged
    WATCH_MAX = 32          # cap on simultaneously watched transcripts

    def _ctx_by_sid(self, sid: str) -> Optional[SessionContext]:
        sid = self._resolve_session_alias(sid) or sid
        return self.sessions.get(sid) or next(
            (c for c in self.sessions.values() if c.session_id == sid), None)

    def _is_external(self, sid: str) -> bool:
        """Whether a stable external owner (or incomplete scan) blocks Remote."""
        ctx = self._ctx_by_sid(sid)
        if ctx is not None and getattr(ctx.sdk, "is_claude_broker", False):
            return False
        w = self._watch.get(sid)
        if ctx is not None and self._codex_shared_affinity(ctx):
            # Shared CLI clients are collaborators and never block Remote. A
            # private Codex App turn has no shared-daemon holder, so the watcher
            # marks only that case as desktop-owned/read-only.
            return bool(w and w.get("desktop_active"))
        if w and w.get("engine") == "codex":
            return bool(w.get("external"))
        return bool(w) and bool(
            w.get("external")
            or not w.get("scan_complete", False)
            or not w.get("file_available", True)
        )

    def _own_write(self, sid: str) -> bool:
        """True only after this wrapper has launched the current Claude query."""
        ctx = self._ctx_by_sid(sid)
        if ctx is None:
            return False                       # not resident => we cannot have written it
        return bool(ctx.engine == "claude" and ctx.claude_write_active)

    async def _terminate_external_claude_holders(
        self,
        holders: set[ProcessIdentity],
        *,
        timeout: float = 3.0,
    ) -> set[ProcessIdentity]:
        """Gracefully stop exact same-user Claude CLI identities for migration.

        This is called only from the reliable, explicit Takeover command. It
        never kills a process group (which may contain the user's shell), never
        escalates to SIGKILL, and re-checks the cross-platform stable identity
        immediately before SIGTERM so PID reuse cannot target another process.
        """
        remaining: set[ProcessIdentity] = set()
        for identity in holders:
            current = process_identity(identity.pid)
            if current != identity:
                continue
            owner_uid = process_owner_uid(identity.pid)
            if owner_uid is None:
                if process_identity(identity.pid) == identity:
                    remaining.add(identity)
                continue
            if owner_uid != os.getuid():
                log.warning(
                    "refusing to migrate Claude process owned by another uid",
                    pid=identity.pid,
                )
                remaining.add(identity)
                continue
            if process_identity(identity.pid) != identity:
                continue
            try:
                os.kill(identity.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                log.warning(
                    "permission denied while migrating Claude process",
                    pid=identity.pid,
                )
                remaining.add(identity)
                continue
            remaining.add(identity)

        deadline = asyncio.get_running_loop().time() + max(0.1, timeout)
        while remaining and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            remaining = {
                identity for identity in remaining
                if process_identity(identity.pid) == identity
            }
        return remaining

    def _resync_watch(self, sid: str) -> None:
        """Re-baseline a watched transcript's size after WE appended to it outside of
        a turn. rename_session/tag_session write an `operation` record straight into
        the .jsonl; without this the watcher would see that growth, call it external,
        and wrongly flag the user's own session read-only."""
        w = self._watch.get(sid)
        if not w:
            return
        try:
            st = os.stat(w["path"])
        except OSError:
            w["file_available"] = False
            return
        w["size"] = st.st_size
        w["file_id"] = (st.st_dev, st.st_ino)
        w["file_available"] = True

    def _watch_session(self, sid: str, *, sidebar: bool = False) -> None:
        """Start mirroring a session transcript when a client opens it.

        Claude combines stable CLI process identity with unattributed file growth.
        Codex cannot use that growth heuristic because app-server flushes its own
        rollout asynchronously; its primary signal is a second writable FD.
        """
        if not sid or sid.startswith("tmp-"):
            return
        ctx = self._ctx_by_sid(sid)
        existing = self._watch.get(sid)
        if existing is not None:
            if sidebar:
                self._codex_sidebar_watches[sid] = None
                self._codex_sidebar_watches.move_to_end(sid)
            else:
                # Opening the conversation promotes this to an explicit watch;
                # sidebar rotation must no longer evict it.
                self._codex_sidebar_watches.pop(sid, None)
            if ctx is None or existing.get("engine") == ctx.engine:
                if (ctx is not None and ctx.engine == "claude"
                        and not existing.get("cwd")):
                    existing["cwd"] = ctx.cwd
                return
            # A cold history request may have registered before the resident
            # engine was known. Correct the watcher once the real context exists.
            self._watch.pop(sid, None)
            self._codex_sidebar_watches.pop(sid, None)
        if ctx is not None:
            engine = ctx.engine
            path = codex_rollout_path(sid) if engine == "codex" else transcript_path(sid)
        else:
            # A session can be evicted from the resident pool while still present
            # in a browser. Resolve its store instead of silently parsing Codex as
            # a Claude transcript on the next history request.
            path = codex_rollout_path(sid)
            engine = "codex" if path else "claude"
            if path is None:
                path = transcript_path(sid)
        if not path:
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        if len(self._watch) >= self.WATCH_MAX:
            victim = next(
                (watched_sid for watched_sid in self._codex_sidebar_watches
                 if not self._is_external(watched_sid)
                 and self._ctx_by_sid(watched_sid) is None), None)
            if victim is None:
                victim = next(
                    (watched_sid for watched_sid in self._watch
                     if not self._is_external(watched_sid)
                     and self._ctx_by_sid(watched_sid) is None), None)
            if victim is None:
                log.warning("transcript watch cap reached; all watches are external",
                            session_id=sid)
                return
            self._watch.pop(victim, None)
            self._codex_sidebar_watches.pop(victim, None)
        watch = {
            "path": path, "size": st.st_size, "file_id": (st.st_dev, st.st_ino),
            "engine": engine, "external_ts": 0.0,
        }
        if engine == "codex":
            own_turn_ids = set(
                getattr(ctx.sdk, "owned_turn_ids", ())) if ctx is not None else set()
            tail_active, tail_partial = self._codex_tail_state(path, st.st_size)
            active_turns = {
                turn_id: time.time()
                for turn_id in tail_active
                if turn_id not in own_turn_ids
            }
            watch.update({
                "external": bool(active_turns),
                "holders": set(),
                "writers": set(),
                "active_external_turns": active_turns,
                # A cold tail cannot distinguish a live turn from a crashed old
                # task_started record. The first complete /proc scan confirms it.
                "seeded_external_turns": set(active_turns),
                # Codex App's private app-server may append through short-lived
                # opens and therefore expose no stable writable FD. A catalog
                # watch registered during that turn must retain the fresh tail
                # marker until its terminal record (or the normal TTL), instead
                # of being erased by the first empty holder scan.
                "preserve_seeded_without_holder": bool(sidebar and active_turns),
                "pending_wrapper_turns": {},
                "takeover_holders": set(),
                "takeover_interactive_holders": set(),
                "takeover_pending": None,
                "partial": tail_partial,
            })
            if active_turns:
                watch["external_ts"] = time.time()
        else:
            broker_active = False
            broker_partial = b""
            if ctx is not None and getattr(ctx.sdk, "is_claude_broker", False):
                try:
                    broker_active, broker_partial = claude_broker_tail_state(path)
                except OSError:
                    broker_active = False
                    broker_partial = b""
                if broker_active and ctx.state == "idle":
                    ctx.state = "running"
            watch.update({
                "cwd": ctx.cwd if ctx is not None else None,
                "external": False,
                "holders": set(),
                "takeover_pending": False,
                "file_available": True,
                # Fail closed until the first real process scan completes.
                "scan_complete": False,
                "broker_active": broker_active,
                "broker_partial": broker_partial,
                # Origin-less metadata rows reference UUIDs from SDK rows.
                # Keep a bounded attribution set instead of a post-turn TTL.
                "owned_message_ids": OrderedDict(),
            })
        self._watch[sid] = watch
        if sidebar and engine == "codex":
            self._codex_sidebar_watches[sid] = None
            self._codex_sidebar_watches.move_to_end(sid)
        log.info("watching transcript", session_id=sid, engine=engine, size=st.st_size)

    def _prime_codex_sidebar_watches(self, rows: list[dict]) -> None:
        """Watch the newest cold Codex threads for native App lifecycle.

        A private Codex App app-server reports such threads as ``notLoaded`` to
        our standalone ``thread/list`` even while their rollout is active.  The
        ordered task markers in the rollout are authoritative, and the existing
        watcher already parses them incrementally.  Reserve only half the global
        watch budget so explicit browser/resident watches always have room.
        """
        limit = max(1, self.WATCH_MAX // 2)
        candidates = [
            row.get("session_id") for row in rows
            if row.get("tag") != "archived"
            and isinstance(row.get("session_id"), str)
        ][:limit]
        wanted = set(candidates)
        for sid in list(self._codex_sidebar_watches):
            if (sid in wanted or self._is_external(sid)
                    or self._ctx_by_sid(sid) is not None):
                continue
            self._codex_sidebar_watches.pop(sid, None)
            self._watch.pop(sid, None)
        for sid in candidates:
            self._watch_session(sid, sidebar=True)

    @staticmethod
    def _codex_sidebar_watch_state(watch: Optional[dict]) -> Optional[State]:
        if not watch or watch.get("engine") != "codex":
            return None
        return "running" if watch.get("active_external_turns") else None

    def _codex_own_processes(self) -> set[ProcessIdentity]:
        own: set[ProcessIdentity] = set()
        for ctx in list(self.sessions.values()):
            if ctx.engine != "codex":
                continue
            proc = getattr(ctx.sdk, "proc", None)
            pid = getattr(proc, "pid", None)
            if not isinstance(pid, int) or getattr(proc, "returncode", None) is not None:
                continue
            identity = process_identity(pid, parent_pid=os.getpid())
            if identity is not None:
                own.add(identity)
        return own

    def _codex_watch_paths(self, only_sid: Optional[str] = None) -> dict[str, str]:
        return {
            sid: w["path"] for sid, w in self._watch.items()
            if w.get("engine") == "codex" and (only_sid is None or sid == only_sid)
        }

    async def _probe_codex_holders(self, paths: dict[str, str]):
        initial_own = self._codex_own_processes()
        scan = await asyncio.to_thread(
            writable_rollout_holders, paths, initial_own)
        # A reconnect can replace an app-server while /proc is being scanned.
        # Remove both the initial and current exact child identities before the
        # result is allowed to influence ownership.
        current_own = self._codex_own_processes()
        for holders in scan.holders.values():
            holders.difference_update(initial_own)
            holders.difference_update(current_own)
        for holders in scan.passive_holders.values():
            holders.difference_update(initial_own)
            holders.difference_update(current_own)
        for holders in scan.private_holders.values():
            holders.difference_update(initial_own)
            holders.difference_update(current_own)
        for identity in initial_own | current_own:
            scan.client_proxies.pop(identity, None)
        tui_bindings, log_complete = await asyncio.to_thread(
            self._codex_tui_log_tracker.bindings,
            paths,
            scan.client_proxies,
        )
        for sid, holders in tui_bindings.items():
            scan.holders.setdefault(sid, set()).update(holders)
        if not log_complete and scan.client_proxies:
            scan = HolderScan(
                scan.holders,
                False,
                scan.passive_holders,
                scan.client_proxies,
                scan.private_holders,
            )
        if not scan.complete:
            if not self._codex_probe_warned:
                log.warning("codex rollout owner scan incomplete; preserving prior state")
                self._codex_probe_warned = True
        elif self._codex_probe_warned:
            log.info("codex rollout owner scan recovered")
            self._codex_probe_warned = False
        return scan

    def _claude_watch_inputs(
        self, only_sid: Optional[str] = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        paths: dict[str, str] = {}
        cwds: dict[str, str] = {}
        for sid, watch in self._watch.items():
            if (watch.get("engine") != "claude"
                    or (only_sid is not None and sid != only_sid)):
                continue
            paths[sid] = watch["path"]
            cwd = watch.get("cwd")
            if isinstance(cwd, str) and cwd:
                cwds[sid] = cwd
        return paths, cwds

    async def _probe_claude_holders(
        self, paths: dict[str, str], cwds: dict[str, str],
    ):
        scan = await asyncio.to_thread(
            claude_session_holders,
            paths,
            cwds,
            wrapper_pid=os.getpid(),
            continue_bindings=self._claude_continue_bindings,
            continue_candidates=self._claude_continue_candidates,
            continue_resolver=self._latest_claude_session_for_cwd,
        )
        if not scan.complete:
            if not self._claude_probe_warned:
                log.warning(
                    "Claude process owner scan incomplete; failing closed")
                self._claude_probe_warned = True
        elif self._claude_probe_warned:
            log.info("Claude process owner scan recovered")
            self._claude_probe_warned = False
        return scan

    @staticmethod
    def _latest_claude_session_for_cwd(cwd: str) -> Optional[str]:
        """Return Claude's native cwd-global ``-c`` target.

        This runs inside the bounded process-scan worker and is called only for
        a new ``ProcessIdentity``; the sticky binding is the per-process cache.
        A catalog failure deliberately propagates so the ownership scan remains
        incomplete/fail-closed instead of guessing from the watched subset.
        """
        infos = list_sessions(
            directory=cwd,
            limit=1,
            include_worktrees=False,
        )
        if not infos:
            return None
        sid = getattr(infos[0], "session_id", None)
        if not isinstance(sid, str) or not sid or len(sid) > 256:
            raise RuntimeError("Claude catalog returned an invalid session id")
        return sid

    async def _prime_claude_ownership(self, sid: str) -> bool:
        """Synchronously close the watcher interval before History/Query."""
        watch = self._watch.get(sid)
        if not watch or watch.get("engine") != "claude":
            return False
        async with self._codex_watch_lock:
            # An unqualified ``claude -c`` means "latest in this cwd". Always
            # resolve it against the complete watched catalog; probing only the
            # requested sid made every same-cwd sid look uniquely eligible and
            # caused a warning to flash only after the user attempted a send.
            paths, cwds = self._claude_watch_inputs()
            scan = await self._probe_claude_holders(paths, cwds)
            holders = set(scan.holders.get(sid, ()))
            if not scan.complete:
                holders.update(watch.get("holders", ()))
            await self._poll_claude_watch(
                sid,
                watch,
                holders,
                time.time(),
                ownership_scan_complete=scan.complete,
            )
            return self._is_external(sid)

    @staticmethod
    def _codex_holder_sets(
        w: dict, scan, sid: str,
    ) -> tuple[
        set[ProcessIdentity], set[ProcessIdentity], set[ProcessIdentity]
    ]:
        """Return (interactive owners, writers, private app-server writers)."""
        raw = set(scan.holders.get(sid, ()))
        seeded: set[str] = w.setdefault("seeded_external_turns", set())
        if seeded and scan.complete:
            # A tail marker is only a cold-start candidate. Without any live
            # foreign writer, an unmatched historical task_started is a crashed
            # orphan and must not manufacture a fresh 60-second read-only lock.
            if not raw and not w.get("preserve_seeded_without_holder"):
                active = w.setdefault("active_external_turns", {})
                for turn_id in seeded:
                    active.pop(turn_id, None)
            seeded.clear()
            w["preserve_seeded_without_holder"] = False
        ignored: set[ProcessIdentity] = w.setdefault("takeover_holders", set())
        ignored_interactive: set[ProcessIdentity] = w.setdefault(
            "takeover_interactive_holders", set())
        if scan.complete:
            # Exact start ticks make this safe against PID reuse. Once a captured
            # process exits, a later process with the same PID is a new owner.
            ignored.intersection_update(raw)
            ignored_interactive.intersection_update(raw)
        passive = set(scan.passive_holders.get(sid, ()))
        private = set(scan.private_holders.get(sid, ()))
        writers = raw.difference(ignored)
        return writers.difference(passive), writers, private.intersection(writers)

    async def _prime_codex_ownership(self, sid: str) -> bool:
        """Atomically consume growth and refresh one owner before History/Query."""
        w = self._watch.get(sid)
        if not w or w.get("engine") != "codex":
            return False
        async with self._codex_watch_lock:
            scan = await self._probe_codex_holders({sid: w["path"]})
            holders, writers, private_holders = self._codex_holder_sets(
                w, scan, sid)
            if not scan.complete:
                holders.update(w.get("holders", ()))
                writers.update(w.get("writers", ()))
                private_holders.update(w.get("private_holders", ()))
            await self._poll_codex_watch(
                sid, w, holders, time.time(), writers=writers,
                private_holders=private_holders,
                ownership_scan_complete=scan.complete)
            return bool(w.get("external"))

    @classmethod
    def _codex_tail_snapshot(
        cls, path: str, size: int,
    ) -> tuple[set[str], bytes, Optional[tuple[str, str]]]:
        """Return the latest bounded lifecycle state and exact last marker."""
        try:
            start = max(0, size - cls.CODEX_TAIL_READ_MAX)
            with open(path, "rb") as stream:
                stream.seek(start)
                data = stream.read(cls.CODEX_TAIL_READ_MAX)
        except OSError:
            return set(), b"", None
        if start:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return set(), b"", None
        markers = parse_turn_markers(data)
        # A Codex thread has one current turn. Historical crash/orphan starts can
        # lack a matching terminal record, so set subtraction would resurrect an
        # ancient turn forever. The last ordered lifecycle marker is authoritative.
        active: set[str] = set()
        for kind, turn_id in markers.ordered:
            active = {turn_id} if kind == "task_started" else set()
        last_marker = markers.ordered[-1] if markers.ordered else None
        return active, markers.partial, last_marker

    @classmethod
    def _codex_tail_state(cls, path: str, size: int) -> tuple[set[str], bytes]:
        """Best-effort seed when a watch begins during an external Codex turn."""
        active, partial, _last_marker = cls._codex_tail_snapshot(path, size)
        return active, partial

    @classmethod
    def _read_watch_growth(cls, path: str, offset: int, available: int) -> bytes:
        with open(path, "rb") as stream:
            stream.seek(offset)
            return stream.read(min(max(0, available), cls.WATCH_READ_MAX))

    async def _push_mirrored_history(self, sid: str) -> History:
        # Native CLI/Desktop growth is a moving conversation head. Broadcasting
        # the old full translated page made every external append re-send
        # megabytes to every browser. Keep the same authoritative replacement
        # semantics with the lightweight projection; detail remains on demand.
        hist = await self._build_history(
            sid, limit=self.MIRROR_LIMIT, detail="summary", allow_stale=True)
        hist.sid = sid
        await self.transport.send(hist)
        return hist

    async def _repair_codex_projection_after_overflow(
        self, ctx: SessionContext, turn_id: str,
    ) -> None:
        """Replace shed live detail from the authoritative local rollout.

        The terminal status has already reached the browser, so this read must
        never turn a successful model turn into a user-visible transport error.
        A newest-page summary is small and merges over the incomplete live tail;
        heavyweight process bodies remain in the materialized detail index.
        """
        sid = ctx.session_id or ctx.key
        if not sid:
            return
        try:
            await self._push_mirrored_history(sid)
            log.info(
                "codex overflow projection repaired",
                session_id=sid,
                turn_id=turn_id,
            )
        except Exception as exc:
            log.warning(
                "codex overflow projection repair failed",
                session_id=sid,
                turn_id=turn_id,
                error_type=type(exc).__name__,
            )

    @classmethod
    def _remember_watch_turn(cls, bucket: dict[str, float], turn_id: str,
                             seen_at: float) -> None:
        bucket[turn_id] = seen_at
        while len(bucket) > cls.CODEX_TURN_TRACK_MAX:
            bucket.pop(next(iter(bucket)))

    @staticmethod
    def _revoke_codex_takeover(
        w: dict,
        holders: set[ProcessIdentity],
        writers: set[ProcessIdentity],
    ) -> None:
        """Restore a captured owner once it produces a new external turn."""
        captured = w.get("takeover_holders")
        if not captured:
            return
        writers.update(captured)
        holders.update(w.get("takeover_interactive_holders", ()))
        captured.clear()
        w["takeover_interactive_holders"].clear()

    @staticmethod
    def _grant_codex_takeover(
        w: dict,
        holders: set[ProcessIdentity],
        writers: set[ProcessIdentity],
        *,
        allowed_writers: Optional[set[ProcessIdentity]] = None,
        allowed_interactive: Optional[set[ProcessIdentity]] = None,
    ) -> None:
        """Capture current owners and make Remote authoritative without killing."""
        captured_writers = set(writers)
        captured_interactive = set(holders)
        if allowed_writers is not None:
            captured_writers.intersection_update(allowed_writers)
        if allowed_interactive is not None:
            captured_interactive.intersection_update(allowed_interactive)
        w.setdefault("takeover_holders", set()).update(captured_writers)
        w.setdefault("takeover_interactive_holders", set()).update(
            captured_interactive)
        holders.difference_update(captured_interactive)
        writers.difference_update(captured_writers)
        w.setdefault("pending_wrapper_turns", {}).clear()
        w["takeover_pending"] = None

    async def _poll_codex_watch(
        self, sid: str, w: dict, holders: set[ProcessIdentity], now: float,
        *, writers: Optional[set[ProcessIdentity]] = None,
        private_holders: Optional[set[ProcessIdentity]] = None,
        ownership_scan_complete: bool = True,
    ) -> None:
        if writers is None:
            writers = set(holders)
        if private_holders is None:
            private_holders = set()
        was_external = bool(w.get("external"))
        was_sidebar_running = bool(w.get("active_external_turns"))
        was_desktop_active = bool(w.get("desktop_active"))
        w["holders"] = holders
        w["writers"] = writers
        w["private_holders"] = private_holders
        external_growth = False
        takeover_cleared = False
        takeover_clear_message: Optional[str] = None
        data = b""
        visible_user_growth = False
        try:
            st = await asyncio.to_thread(os.stat, w["path"])
        except OSError:
            return

        ctx = self._ctx_by_sid(sid)
        shared_affinity = bool(
            ctx is not None and self._codex_shared_affinity(ctx))
        if shared_affinity and not w.get("shared_activity_initialized"):
            # Retire ownership debris produced by the old shared-daemon branch,
            # but preserve a genuine active tail seeded when this watch opened.
            # Focusing a session while a private Codex App turn is already
            # running creates this shared context mid-turn; that passive attach
            # must not erase the App's authoritative task_started marker.
            seeded = set(w.get("seeded_external_turns", ()))
            active = w.setdefault("active_external_turns", {})
            for turn_id in list(active):
                if turn_id not in seeded and not private_holders:
                    active.pop(turn_id, None)
            w.setdefault("pending_wrapper_turns", {}).clear()
            w["takeover_pending"] = None
            w["shared_activity_initialized"] = True

        pending_takeover = w.get("takeover_pending")
        if pending_takeover:
            new_writers = writers.difference(
                pending_takeover.get("writers", ()))
            if new_writers:
                # A click authorizes one exact ownership epoch. A later process
                # must never inherit that authority without another click. Only
                # mutate after stat succeeds so every cancellation can notify UI.
                w["takeover_pending"] = None
                pending_takeover = None
                takeover_cleared = True
                takeover_clear_message = "终端出现了新的占用者，本次自动接管已取消，请重新点击接管"
                log.info(
                    "queued takeover cancelled by new holder",
                    session_id=sid, new_holders=len(new_writers))

        file_id = (st.st_dev, st.st_ino)
        if file_id != w.get("file_id") or st.st_size < w["size"]:
            # Codex is append-only. Rotation/truncation is itself an external
            # mutation unless caused by a process we already classify as external;
            # in either case the resident context must reload. Re-baseline at EOF
            # to avoid replaying a potentially huge replacement as fresh turns.
            w["file_id"] = file_id
            w["size"] = st.st_size
            w["partial"] = b""
            w["active_external_turns"].clear()
            w["pending_wrapper_turns"].clear()
            if w.get("takeover_pending"):
                takeover_cleared = True
                takeover_clear_message = "会话文件已变化，本次自动接管已取消，请重新点击接管"
            w["takeover_pending"] = None
            external_growth = True
        elif st.st_size > w["size"]:
            data = await asyncio.to_thread(
                self._read_watch_growth, w["path"], w["size"], st.st_size - w["size"])
            w["size"] += len(data)

        sdk = ctx.sdk if ctx is not None else None
        own_turn_ids = set(getattr(sdk, "owned_turn_ids", ())) if sdk else set()
        active: dict[str, float] = w["active_external_turns"]
        pending: dict[str, dict[str, object]] = w["pending_wrapper_turns"]
        attribution_pending = bool(
            sdk is not None and getattr(
                sdk, "turn_attribution_pending",
                getattr(sdk, "turn_start_pending", False),
            )
        )
        wrapper_may_own_turn = bool(
            sdk is not None and (
                attribution_pending
                or getattr(sdk, "turn_active", False)))

        # app-server stdout and rollout writes are independent channels.  An own
        # task_started record can win the race against the authoritative turn id.
        # A synchronous turn/start RPC is already bounded by the app-server request
        # timeout, so never replace that bound with the much shorter attribution
        # grace. Automatic continuations have no pending RPC and use the grace.
        # A live foreign holder bypasses either wait and is classified immediately.
        # Also reconcile defensively against active: a delayed turn/start response
        # may identify a marker after an earlier implementation already promoted it.
        for turn_id in own_turn_ids:
            active.pop(turn_id, None)
        for turn_id, record in list(pending.items()):
            seen_at = float(record.get("seen_at", now))
            if turn_id in own_turn_ids:
                pending.pop(turn_id, None)
                continue
            awaiting_rpc = bool(record.get("awaiting_rpc"))
            rpc_finished_without_match = (
                awaiting_rpc and not attribution_pending)
            automatic_grace_expired = (
                not awaiting_rpc
                and now - seen_at >= self.CODEX_TURN_ATTRIBUTION_GRACE)
            if (holders or not wrapper_may_own_turn
                    or rpc_finished_without_match or automatic_grace_expired):
                takeover = w.get("takeover_pending")
                if takeover and turn_id not in takeover.get("turn_ids", ()):
                    w["takeover_pending"] = None
                    takeover_cleared = True
                    takeover_clear_message = "终端开始了新回合，本次自动接管已取消，请重新点击接管"
                self._revoke_codex_takeover(w, holders, writers)
                if not bool(record.get("finished")):
                    self._remember_watch_turn(active, turn_id, now)
                pending.pop(turn_id, None)
                external_growth = True

        if data:
            markers = parse_turn_markers(data, w.get("partial", b""))
            w["partial"] = markers.partial
            visible_user_growth = markers.has_visible_user_message
            for turn_id in markers.started:
                if turn_id in own_turn_ids:
                    continue
                if not holders and wrapper_may_own_turn:
                    # Never guess an id merely because turn/start is pending: a
                    # short terminal turn can race the final probe and disappear
                    # before the next holder scan. Only the RPC response or an
                    # app-server turn/started notification is authoritative.
                    pending.setdefault(turn_id, {
                        "seen_at": now,
                        "finished": False,
                        "awaiting_rpc": attribution_pending,
                    })
                    while len(pending) > self.CODEX_TURN_TRACK_MAX:
                        pending.pop(next(iter(pending)))
                        # Losing an unclassified id must fail stale rather than
                        # silently treating a potentially foreign turn as ours.
                        external_growth = True
                else:
                    takeover = w.get("takeover_pending")
                    if takeover and turn_id not in takeover.get("turn_ids", ()):
                        w["takeover_pending"] = None
                        takeover_cleared = True
                        takeover_clear_message = "终端开始了新回合，本次自动接管已取消，请重新点击接管"
                    # The captured terminal spoke again after manual takeover.
                    # Restore interactive ownership; a headless app-server remains
                    # governed by the active marker below.
                    self._revoke_codex_takeover(w, holders, writers)
                    self._remember_watch_turn(active, turn_id, now)
                    external_growth = True
            for turn_id in markers.finished:
                if turn_id in pending:
                    pending[turn_id]["finished"] = True
                elif turn_id in active:
                    external_growth = True
                    active.pop(turn_id, None)

            if holders or active:
                external_growth = True
            if external_growth:
                for turn_id in active:
                    active[turn_id] = now

        if holders or (active and writers):
            w["external_ts"] = now
        elif external_growth:
            w["external_ts"] = now
        elif active and now - w["external_ts"] >= self.EXTERNAL_TTL:
            # Fallback for a crashed short-lived writer that never recorded a
            # terminal marker. A live writer's FD keeps the session locked.
            active.clear()

        # Clicking takeover during a live terminal response records the user's
        # ownership intent immediately, but waits for that response to reach a
        # terminal marker. This preserves the right to take over without allowing
        # two app-servers to write the same thread concurrently.
        pending_takeover = w.get("takeover_pending")
        if (pending_takeover and ownership_scan_complete
                and not active and not pending):
            self._grant_codex_takeover(
                w, holders, writers,
                allowed_writers=set(pending_takeover.get("writers", ())),
                allowed_interactive=set(
                    pending_takeover.get("interactive", ())),
            )
            takeover_cleared = True
            external_growth = True

        # An open Codex App window is only a subscriber, not an owner.  Ownership
        # begins at a foreign active turn.  Remote-owned turn ids were removed
        # from ``active`` above, so a turn mirrored into App remains writable and
        # attributed to Remote instead of bouncing the Web UI into read-only.
        desktop_active = bool(
            active and not holders and (shared_affinity or private_holders))
        private_app_loaded = bool(private_holders)
        w["desktop_active"] = desktop_active
        w["private_app_loaded"] = private_app_loaded
        is_external = (
            bool(desktop_active) if shared_affinity
            else bool(
                holders or active or w.get("takeover_pending"))
        )
        w["external"] = is_external
        if (external_growth or (is_external and not was_external)) and ctx is not None:
            ctx.needs_reload = bool(
                not shared_affinity
                or desktop_active or was_desktop_active)
            if ctx.codex_checkpoint not in (None, False):
                # Native terminal turns/rollback/compact are outside Remote's
                # pre-image boundary. Retire the old journal immediately so its
                # newest file checkpoint can never be paired with a different
                # newest app-server turn. A later Remote turn starts a fresh,
                # tail-aligned journal.
                await self._retire_codex_checkpoint(
                    ctx,
                    reason="external Codex transcript mutation",
                    allow_restart=True,
                )
            # A native Codex turn may itself switch `/plan`. Mirror that control
            # state with the transcript so Remote does not keep a stale override.
            await self._refresh_codex_collaboration_mode(ctx)

        if ctx is not None:
            await self._sync_external_control(ctx, w)

        sidebar_running = bool(w.get("active_external_turns"))
        if sidebar_running != was_sidebar_running:
            await self.transport.send(SessionActivity(
                engine="codex",
                session_id=sid,
                state="running" if sidebar_running else "idle",
            ))

        if (external_growth or visible_user_growth
                or is_external != was_external or takeover_cleared):
            ownership_changed = is_external != was_external
            logger_method = log.info if ownership_changed else log.debug
            logger_method(
                "codex external ownership changed" if ownership_changed
                else "codex rollout append -> mirroring",
                session_id=sid,
                external=is_external,
                holders=len(holders),
                private_holders=len(private_holders),
                active_turns=len(active),
                visible_user_growth=visible_user_growth,
            )
            await self._push_mirrored_history(sid)
        if takeover_cleared and ctx is not None:
            # This control frame follows History so a cancellation explanation is
            # not immediately erased by the authoritative history refresh.
            await self._emit(ctx, TakeoverState(
                pending=False, message=takeover_clear_message))

    async def _poll_claude_watch(
        self,
        sid: str,
        w: dict,
        holders: set[ProcessIdentity],
        now: float,
        *,
        ownership_scan_complete: bool,
    ) -> None:
        was_external = self._is_external(sid)
        was_file_available = w.get("file_available", True)
        ctx = self._ctx_by_sid(sid)
        external_growth = False
        try:
            file_stat = await asyncio.to_thread(os.stat, w["path"])
        except OSError:
            file_stat = None
        w["file_available"] = file_stat is not None

        # A broker-owned official TUI is intentionally writable from both the
        # terminal and Remote. Its process/transcript must therefore never flow
        # through the legacy "foreign owner => read-only" heuristic. Parse only
        # durable lifecycle boundaries, mirror all content from normal history,
        # and leave a managed Remote turn's state to its own drain task.
        if ctx is not None and getattr(ctx.sdk, "is_claude_broker", False):
            w["scan_complete"] = True
            w["external"] = False
            w["holders"] = set()
            lifecycle_events: tuple[tuple[str, str], ...] = ()
            changed = False
            if file_stat is not None:
                current_id = (file_stat.st_dev, file_stat.st_ino)
                old_size = int(w.get("size", 0))
                if current_id != w.get("file_id") or file_stat.st_size < old_size:
                    try:
                        active, partial = await asyncio.to_thread(
                            claude_broker_tail_state, w["path"])
                    except OSError:
                        active, partial = False, b""
                    w["file_id"] = current_id
                    w["size"] = file_stat.st_size
                    w["broker_active"] = active
                    w["broker_partial"] = partial
                    changed = True
                elif file_stat.st_size > old_size:
                    data = await asyncio.to_thread(
                        self._read_watch_growth,
                        w["path"],
                        old_size,
                        file_stat.st_size - old_size,
                    )
                    w["size"] = old_size + len(data)
                    parsed = parse_claude_broker_lifecycle(
                        data, w.get("broker_partial", b""))
                    w["broker_partial"] = parsed.partial
                    lifecycle_events = parsed.ordered
                    active = bool(w.get("broker_active", False))
                    for kind, _event_id in lifecycle_events:
                        active = kind == "started"
                    w["broker_active"] = active
                    changed = bool(data)

            try:
                await ctx.sdk.refresh_status()
            except BrokerClientError as exc:
                if not await self._refresh_claude_broker_handle(ctx):
                    log.warning(
                        "Claude broker status unavailable",
                        session_id=sid,
                        error_code=exc.code,
                    )
            else:
                await self._sync_claude_broker_runtime_controls(ctx)
                await self._sync_external_control(ctx, w)

            if ctx.turn_task is None:
                active = bool(w.get("broker_active", False))
                if active and ctx.state == "idle":
                    await self._set_state(ctx, "running")
                elif not active and ctx.state != "idle":
                    await self._set_state(ctx, "idle")
            if changed:
                await self._push_mirrored_history(sid)
            return

        takeover_cleared = False
        if ownership_scan_complete:
            w["scan_complete"] = True
            w["holders"] = set(holders)
            # A queued handoff is only safe once both the exact owner is gone
            # and the transcript is still available for an authoritative mirror.
            if (w.get("takeover_pending") and not holders
                    and file_stat is not None):
                w["takeover_pending"] = False
                takeover_cleared = True
            w["external"] = bool(holders or w.get("takeover_pending"))
        else:
            # A partial /proc view must never manufacture an unlock. Retain
            # every previously-known identity and let _is_external fail closed.
            w["scan_complete"] = False
            w.setdefault("holders", set()).update(holders)

        if file_stat is None:
            if was_file_available:
                log.warning(
                    "Claude transcript became unavailable; failing closed",
                    session_id=sid,
                )
        else:
            file_id = (file_stat.st_dev, file_stat.st_ino)
            size = file_stat.st_size
            if file_id != w.get("file_id") or size < w["size"]:
                w["file_id"] = file_id
                w["size"] = size
                external_growth = True
            elif size > w["size"]:
                old_size = int(w["size"])
                grew = size - old_size
                data = await asyncio.to_thread(
                    self._read_watch_growth,
                    w["path"],
                    old_size,
                    grew,
                )
                w["size"] = old_size + len(data)
                owned_ids = w.setdefault("owned_message_ids", OrderedDict())
                origin, new_owned_ids = classify_claude_growth(
                    data, owned_ids.keys())
                if origin == "sdk" and not holders:
                    for message_id in new_owned_ids:
                        owned_ids[message_id] = None
                        owned_ids.move_to_end(message_id)
                    while len(owned_ids) > self.CLAUDE_OWNED_MESSAGE_MAX:
                        owned_ids.popitem(last=False)
                # Explicit CLI provenance or a stable foreign process always
                # wins, even during a wrapper query. Conversely, sdk-py rows
                # remain ours when Claude flushes them after ResultMessage.
                external_growth = (
                    bool(holders)
                    or origin == "external"
                    or (origin == "unknown" and not self._own_write(sid))
                )
                log.info(
                    "Claude transcript append observed",
                    session_id=sid,
                    bytes=len(data),
                    external=external_growth,
                    origin=origin,
                    holders=len(holders),
                    resident=ctx is not None,
                )

        if external_growth and ctx is not None:
            w["external_ts"] = now
            ctx.external_ts = now
            ctx.needs_reload = True

        is_external = self._is_external(sid)
        if ctx is not None:
            await self._sync_external_control(ctx, w)
        if (external_growth or is_external != was_external or takeover_cleared
                or w["file_available"] != was_file_available):
            log.info(
                "Claude external ownership changed"
                if is_external != was_external
                else "Claude transcript append -> mirroring",
                session_id=sid,
                external=is_external,
                holders=len(w.get("holders", ())),
                scan_complete=ownership_scan_complete,
            )
            await self._push_mirrored_history(sid)
        if takeover_cleared and ctx is not None:
            await self._emit(ctx, TakeoverState(
                pending=False,
                message="终端进程已退出，会话已安全交给 Remote",
            ))

    async def _poll_watches_once(self) -> None:
        paths = self._codex_watch_paths()
        if paths:
            async with self._codex_watch_lock:
                scan = await self._probe_codex_holders(paths)
                now = time.time()
                for sid, path in paths.items():
                    w = self._watch.get(sid)
                    if w is None or w.get("path") != path:
                        continue
                    holders, writers, private_holders = self._codex_holder_sets(
                        w, scan, sid)
                    if not scan.complete:
                        holders.update(w.get("holders", ()))
                        writers.update(w.get("writers", ()))
                        private_holders.update(w.get("private_holders", ()))
                    await self._poll_codex_watch(
                        sid, w, holders, now, writers=writers,
                        private_holders=private_holders,
                        ownership_scan_complete=scan.complete)

        claude_paths, claude_cwds = self._claude_watch_inputs()
        if claude_paths:
            async with self._codex_watch_lock:
                scan = await self._probe_claude_holders(
                    claude_paths, claude_cwds)
                now = time.time()
                for sid, path in claude_paths.items():
                    watch = self._watch.get(sid)
                    if watch is None or watch.get("path") != path:
                        continue
                    holders = set(scan.holders.get(sid, ()))
                    if not scan.complete:
                        holders.update(watch.get("holders", ()))
                    await self._poll_claude_watch(
                        sid,
                        watch,
                        holders,
                        now,
                        ownership_scan_complete=scan.complete,
                    )

    async def _watch_loop(self) -> None:
        """Mirror EXTERNAL appends to watched transcripts.

        Claude uses stable process identities plus conservative cwd association.
        Codex uses a writable-holder scan plus turn-id attribution because its own
        app-server keeps flushing rollout records after turn/completed. External
        growth rebuilds history and marks the resident context stale before reuse."""
        while True:
            try:
                await asyncio.sleep(1.5)
                # A user may start ``claude-remote resume`` after this wrapper
                # already resumed the sid through the SDK. Upgrade that exact
                # idle context before the legacy external-process scan can
                # misclassify the broker-owned official TUI.
                await self._adopt_live_claude_broker_sessions()
                # A new broker session has a UUID before Claude creates its
                # first transcript. Retry watch registration so a turn typed
                # only in the official TUI still appears in Remote.
                for ctx in list(self.sessions.values()):
                    if (getattr(ctx.sdk, "is_claude_broker", False)
                            and ctx.session_id
                            and ctx.session_id not in self._watch):
                        self._watch_session(ctx.session_id)
                await self._poll_watches_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("transcript watch loop error")

    async def _build_history(
        self, sid: str, before=None, limit=None, cwd_hint=None,
        detail: str = "full",
        *, allow_stale: bool = False,
    ) -> History:
        """Read a session's transcript and assemble ONE History frame. Shared by
        GetHistory (routed to the requester) and the watcher (broadcast on external
        append). No spawn, no ring buffer; the parse runs in a thread."""
        # Reading requires the session's own cwd (transcript lives under it).
        # Prefer a resident ctx's cwd, else the client-provided cwd, else default.
        # Capture before transcript I/O. If a rollback crosses its mutation
        # boundary while this read is in flight, its marker carries a newer
        # token and the browser rejects this stale response.
        revision = self._history_revision(sid)
        if before is None:
            build_seq = self._history_build_sequences.get(sid, 0) + 1
            self._history_build_sequences[sid] = build_seq
        else:
            build_seq = self._history_build_sequences.get(sid, 0)
        ctx = self._ctx_by_sid(sid)
        control = self._session_control(ctx) if ctx is not None else None
        live_seq = ctx.seq if ctx is not None else None
        watch = self._watch.get(sid) or {}
        active_external_turns = watch.get("active_external_turns")
        in_progress = bool(
            (ctx is not None and ctx.state != "idle")
            or (isinstance(active_external_turns, dict)
                and active_external_turns)
        )
        events: list = []
        mdl = None
        watched_engine = watch.get("engine")
        is_codex_hist = bool(
            (ctx is not None and ctx.engine == "codex") or watched_engine == "codex")
        source_path = None
        source_fingerprint = None
        source_snapshot_stable: bool | None = None
        indexed_page = None
        stale_indexed_page = False
        source_too_large = False
        source_window_has_more = False
        source_window_oldest_cursor = None
        source_window_boundary_offset = None
        try:
            source_path = await asyncio.to_thread(
                codex_rollout_path if is_codex_hist else transcript_path, sid)
            source_too_large = bool(
                source_path and not is_codex_hist
                and await asyncio.to_thread(os.path.getsize, source_path)
                > self.cfg.history_source_max_bytes
            )
        except OSError:
            source_path = None
        if source_path:
            try:
                source_fingerprint = await asyncio.to_thread(
                    HistorySourceFingerprint.capture, source_path)
            except OSError:
                source_snapshot_stable = False
            except Exception as exc:
                source_snapshot_stable = False
                log.warning(
                    "history source fingerprint failed", session_id=sid,
                    error=str(exc),
                )
        if source_fingerprint is not None and self._history_index is not None:
            try:
                indexed_page = await asyncio.to_thread(
                    self._history_index.get_page,
                    sid,
                    "codex" if is_codex_hist else "claude",
                    source_fingerprint,
                    before=before,
                    limit=int(limit) if isinstance(limit, int) else 0,
                )
                if (indexed_page is None and allow_stale
                        and before is None):
                    indexed_page = await asyncio.to_thread(
                        self._history_index.get_append_page,
                        sid,
                        "codex" if is_codex_hist else "claude",
                        source_fingerprint,
                        before=None,
                        limit=int(limit) if isinstance(limit, int) else 0,
                    )
                    stale_indexed_page = indexed_page is not None
            except OSError:
                indexed_page = None
            except Exception as exc:
                log.warning(
                    "history index read failed", session_id=sid,
                    error=str(exc),
                )
        cached_full_events: list[dict] | None = None
        if (indexed_page is not None and detail == "full"
                and source_fingerprint is not None
                and self._history_index is not None):
            # Page rows intentionally omit inline image bodies. Compatibility
            # full-history callers hydrate each bounded turn from the
            # source-complete detail table instead of reparsing the rollout or
            # receiving a silently lossy summary page.
            cached_full_events = [
                dict(row) for row in indexed_page.events
                if row.get("type") in {"model", "effort"}
            ]
            for turn in indexed_page.turns:
                turn_id = turn.get("id")
                if not isinstance(turn_id, str):
                    cached_full_events = None
                    break
                detail_rows = await asyncio.to_thread(
                    self._history_index.get_turn_detail,
                    sid,
                    "codex" if is_codex_hist else "claude",
                    source_fingerprint,
                    turn_id,
                )
                if detail_rows is None:
                    cached_full_events = None
                    break
                cached_full_events.extend(dict(row) for row in detail_rows)
            if cached_full_events is None:
                # The page can outlive a tighter detail LRU. Rebuild from the
                # canonical source rather than returning an incomplete page.
                indexed_page = None
        if indexed_page is not None:
            cached_events = cached_full_events or [
                dict(row) for row in indexed_page.events
            ]
            if ctx is not None:
                # Rebuild exact preview capabilities from the materialized
                # ToolUse/ToolResult pairs.  A wrapper restart must not make a
                # previously valid cross-cwd file preview disappear merely
                # because JSONL translation was skipped.
                for row in cached_events:
                    try:
                        if row.get("type") == "tool_use":
                            self._observe_preview_path_event(
                                ctx, ToolUse.model_validate(row))
                        elif row.get("type") == "tool_result":
                            self._observe_preview_path_event(
                                ctx, ToolResult.model_validate(row))
                    except (TypeError, ValueError):
                        continue
            if before is None and ctx is not None:
                live_model = _session_model(ctx)
                live_effort = _session_effort(ctx)
                if live_model:
                    cached_events = [
                        row for row in cached_events
                        if row.get("type") != "model"
                    ]
                    cached_events.insert(
                        0, Model(model=live_model, sid=sid).model_dump(mode="json"))
                if live_effort:
                    cached_events = [
                        row for row in cached_events
                        if row.get("type") != "effort"
                    ]
                    model_rows = 1 if cached_events and (
                        cached_events[0].get("type") == "model") else 0
                    cached_events.insert(
                        model_rows,
                        Effort(effort=live_effort, sid=sid).model_dump(mode="json"),
                    )
            log.info(
                "history index hit", session_id=sid,
                events=len(cached_events), before=bool(before), limit=limit,
                source_bytes=source_fingerprint.size,
                stale=stale_indexed_page,
            )
            cached_history = History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=live_seq,
                events=cached_events,
                has_more=indexed_page.has_more,
                before=before,
                control=control,
                oldest_id=indexed_page.oldest_id,
                newest_id=indexed_page.newest_id,
                external=self._is_external(sid),
                takeover_pending=bool(
                    (self._watch.get(sid) or {}).get("takeover_pending")),
                in_progress=in_progress,
            )
            if stale_indexed_page:
                # A sampled append-prefix page is useful for first paint, but it
                # is not the exact current rollout/transcript projection. The
                # browser keeps it display-only until the refresh below commits
                # a matching authoritative page.
                cached_history.authoritative = False
            cached_source_stable = True
            try:
                cached_source_stable = (
                    await asyncio.to_thread(
                        HistorySourceFingerprint.capture,
                        source_fingerprint.path,
                    )
                    == source_fingerprint
                )
            except Exception:
                cached_source_stable = False
            if not cached_source_stable:
                # The source grew after the exact cache lookup but before this
                # response was assembled. Treat it like any other sampled first
                # paint instead of letting a stale cache hit replace live rows.
                cached_history.authoritative = False
            if detail == "summary":
                cached_history.turns = [
                    ConversationTurn.model_validate(turn)
                    for turn in indexed_page.turns
                ]
                cached_history.detail = "summary"
                cached_history.events = [
                    row for row in cached_events
                    if row.get("type") in {"model", "effort"}
                ]
            if (
                (stale_indexed_page or not cached_source_stable)
                and not source_too_large
                and before is None
            ):
                self._schedule_history_refresh(
                    sid,
                    before=before,
                    limit=limit,
                    cwd=cwd_hint,
                    detail=detail,
                )
            return cached_history
        if source_too_large:
            notice = Error(
                code=ERR_INTERNAL,
                message=("历史文件超过安全读取上限；请调大 "
                         "HISTORY_SOURCE_MAX_BYTES 或在终端中查看"),
                sid=sid,
            )
            return History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=live_seq,
                events=[notice.model_dump(mode="json")],
                has_more=False,
                before=before,
                external=self._is_external(sid),
                control=control,
                takeover_pending=bool(
                    (self._watch.get(sid) or {}).get("takeover_pending")),
                in_progress=in_progress,
            )
        history_error = None
        if is_codex_hist:
            # Codex history lives in ~/.codex/sessions rollout files, not the
            # Claude transcript store.
            try:
                path = source_path or await asyncio.to_thread(codex_rollout_path, sid)
                if path:
                    (start_offset, end_offset, source_window_has_more,
                     source_window_oldest_cursor,
                     source_window_boundary_offset) = await asyncio.to_thread(
                        codex_history_window,
                        path,
                        before=before,
                        limit=limit,
                        max_bytes=self.cfg.codex_history_window_max_bytes,
                    )
                    events, mdl = await asyncio.to_thread(
                        codex_translate_history,
                        path,
                        self.cfg.tool_result_max,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        source_continuation=(
                            "authoritative_page"
                            if source_window_oldest_cursor is not None
                            and source_window_boundary_offset is not None
                            else None
                        ),
                        snapshot_in_progress=(
                            in_progress and before is None
                        ),
                    )
                    if (source_window_oldest_cursor is not None
                            and source_window_boundary_offset is not None
                            and not any(isinstance(event, UserMsg)
                                        for event in events)):
                        recovered_user = await asyncio.to_thread(
                            codex_history_boundary_user,
                            path,
                            source_window_boundary_offset,
                            source_window_oldest_cursor,
                        )
                        if recovered_user is not None:
                            events.insert(0, recovered_user)
            except Exception as e:
                log.warning("codex get_history failed", session_id=sid, error=str(e))
                history_error = "历史暂时不可用，请稍后重试"
        elif (ctx is not None
              and getattr(ctx.sdk, "is_claude_broker", False)
              and source_path is None):
            # `claude-remote new` reserves its UUID before the official TUI
            # writes the first JSONL row. That is an authoritative empty history,
            # not a read failure banner.
            events = []
        else:
            # Existing Claude sessions must never inherit the wrapper's current
            # default cwd. Resolve their immutable transcript directory from
            # resident state or SDK metadata. The browser hint comes only from
            # an accepted SessionInfo and remains non-authoritative: a scoped
            # miss is retried through the SDK's all-project lookup below.
            directory = (ctx.cwd if ctx else None) or None
            if directory is None:
                try:
                    info = await asyncio.to_thread(get_session_info, sid)
                except Exception as exc:
                    log.warning(
                        "Claude history session metadata unavailable",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
                    info = None
                info_cwd = getattr(info, "cwd", None)
                if isinstance(info_cwd, str) and info_cwd:
                    directory = info_cwd
            if directory is None and isinstance(cwd_hint, str) and cwd_hint:
                expanded_hint = os.path.expanduser(cwd_hint)
                if os.path.isabs(expanded_hint) and "\x00" not in expanded_hint:
                    directory = os.path.realpath(expanded_hint)
            try:
                def _read():
                    messages = get_session_messages(sid, directory=directory)
                    if not messages and directory is not None:
                        messages = get_session_messages(sid, directory=None)
                    return (
                        messages,
                        transcript_timestamps(sid),
                        transcript_internal_user_events(sid),
                    )
                msgs, tss, internal_events = await asyncio.to_thread(_read)
                if internal_events:
                    events = translate_history(
                        msgs, self.cfg.tool_result_max, timestamps=tss,
                        internal_user_events=internal_events)
                else:
                    # Keep the long-standing call shape for embedders/tests
                    # which supply a compatible transcript translator.
                    events = translate_history(
                        msgs, self.cfg.tool_result_max, timestamps=tss)
                subagent_events = await asyncio.to_thread(
                    translate_subagent_history, sid, self.cfg.tool_result_max)
                events = merge_subagent_history(events, subagent_events)
                mdl = last_assistant_model(msgs)
            except Exception as e:
                log.warning("get_history failed", session_id=sid, error=str(e))
                history_error = "历史暂时不可用，请稍后重试"
        if history_error is not None:
            return History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=live_seq,
                authoritative=False,
                error=history_error,
                events=[],
                has_more=False,
                before=before,
                external=self._is_external(sid),
                control=control,
                takeover_pending=bool(
                    (self._watch.get(sid) or {}).get("takeover_pending")),
                in_progress=in_progress,
            )
        for ev in events:
            if ctx is not None:
                # Rebuild exact cross-cwd preview capabilities from the durable
                # transcript after a wrapper restart or resident-session
                # eviction.  The observer grants only completed Write/Edit
                # pairs, never a path merely mentioned by the assistant.
                self._observe_preview_path_event(ctx, ev)
            if isinstance(ev, UserMsg):
                ev.prompt, restored_files = self._strip_attachment_paths(ev.prompt)
                if restored_files and not ev.files:
                    ev.files = restored_files
            ev.sid = sid
        # Claude history has one visible turn per user_msg. Codex can additionally
        # create goal/background continuations with no user_message after the prior
        # turn has already completed. Preserve those as independent pages by closing
        # a Codex history group at every authoritative TurnEnd; otherwise an
        # arbitrarily long goal would collapse into the preceding user's page.
        turns: list[list] = []
        if is_codex_hist:
            current: list = []
            for ev in events:
                event_type = getattr(ev, "type", None)
                if event_type == "user_msg" and current:
                    # Retain a malformed/legacy unterminated prefix rather than
                    # attaching it to the next real user turn.
                    turns.append(current)
                    current = []
                current.append(ev)
                if event_type == "turn_end":
                    turns.append(current)
                    current = []
            if current:
                turns.append(current)
        else:
            # Claude keeps the legacy grouping contract: a turn is one user
            # message plus its reply; leading non-user events form group 0.
            for ev in events:
                if getattr(ev, "type", None) == "user_msg" or not turns:
                    turns.append([])
                turns[-1].append(ev)

        def _tid(grp):
            user_cursor = next(
                (e.msg_id for e in grp
                 if getattr(e, "type", None) == "user_msg"),
                None,
            )
            if user_cursor or not is_codex_hist:
                return user_cursor
            # An assistant-only Codex turn has no user msg_id. Its terminal app-
            # server turn id is authoritative and remains stable across reparses.
            terminal_cursor = next(
                (getattr(e, "turn_id", None) for e in reversed(grp)
                 if getattr(e, "type", None) == "turn_end"
                 and getattr(e, "turn_id", None)),
                None,
            )
            if terminal_cursor:
                return terminal_cursor
            # In-progress/legacy assistant-only records may not have reached a
            # terminal boundary yet. Translator-generated item ids are deterministic
            # for the same rollout record, so they are a stable best-effort cursor.
            for event in grp:
                for field in ("message_id", "item_id", "tool_use_id"):
                    cursor = getattr(event, field, None)
                    if cursor:
                        return cursor
            return None

        # Select the page of turns: newest `limit` turns, ending before `before`.
        end = len(turns)
        if before is not None:
            idx = next((i for i, g in enumerate(turns) if _tid(g) == before), None)
            if idx is not None:
                end = idx
        start = max(0, end - limit) if isinstance(limit, int) and limit > 0 else 0
        page = turns[start:end]

        # Prepend live control readouts only on the newest page (initial load).
        # Claude's SDK model is the selected alias; its transcript may instead
        # contain a proxy's raw upstream model and must not replace that alias.
        authoritative_model = _session_model(ctx) if ctx is not None else None
        history_model = authoritative_model or mdl
        control_rows: list[dict] = []
        if (before is None and history_model
                and (authoritative_model or is_codex_hist
                     or history_model.startswith("claude-"))):
            model_event = Model(model=history_model, sid=sid)
            control_rows.append(model_event.model_dump(mode="json"))
        history_effort = _session_effort(ctx) if ctx is not None else None
        if before is None and history_effort:
            effort_event = Effort(effort=history_effort, sid=sid)
            control_rows.append(effort_event.model_dump(mode="json"))

        def make_history(selected: list[list], effective_start: int) -> History:
            payload: list[dict] = [row.copy() for row in control_rows]
            for group in selected:
                payload.extend(ev.model_dump(mode="json") for ev in group)
            oldest_id = _tid(selected[0]) if selected else None
            if (source_window_oldest_cursor is not None
                    and selected
                    and turns
                    and selected[0] is turns[0]):
                oldest_id = source_window_oldest_cursor
            return History(
                session_id=sid,
                revision=revision,
                generation=self.instance_id,
                build_seq=build_seq,
                live_seq=live_seq,
                events=payload,
                has_more=source_window_has_more or effective_start > 0,
                before=before,
                control=control,
                oldest_id=oldest_id,
                newest_id=(_tid(selected[-1]) if selected else None),
                external=self._is_external(sid),
                takeover_pending=bool(
                    (self._watch.get(sid) or {}).get("takeover_pending")),
                in_progress=in_progress,
            )

        # A History response is one WebSocket frame. Keep it below the transport
        # cap by reducing the number of oldest turns in this page; pagination can
        # retrieve those turns on the next request. This prevents a large transcript
        # from tearing down the wrapper<->relay connection.
        selected = page
        effective_start = start
        history = make_history(selected, effective_start)
        margin = min(64 * 1024, max(1024, self.cfg.ws_max_size_bytes // 16))
        frame_budget = max(1024, self.cfg.ws_max_size_bytes - margin)
        frame_size = len(history.model_dump_json().encode())
        if frame_size > frame_budget and len(selected) > 1:
            # Find the smallest number of oldest turns to drop. Re-serializing
            # after every single removal is quadratic for a legal transcript
            # containing many small turns; binary search bounds this to O(log n)
            # complete serializations while retaining the largest fitting page.
            low, high = 1, len(selected) - 1
            best_drop = len(selected) - 1
            best_history = make_history(selected[-1:], start + best_drop)
            while low <= high:
                drop = (low + high) // 2
                candidate = make_history(selected[drop:], start + drop)
                candidate_size = len(candidate.model_dump_json().encode())
                if candidate_size <= frame_budget:
                    best_drop = drop
                    best_history = candidate
                    high = drop - 1
                else:
                    low = drop + 1
            selected = selected[best_drop:]
            effective_start = start + best_drop
            history = best_history
            frame_size = len(history.model_dump_json().encode())

        # Keep the coherent source-complete projection before applying
        # transport/cache-only image compaction. GetTurnDetail/GetHistoryImage
        # read this independent row; the lightweight page stores only opaque
        # image metadata and therefore never reparses base64 on every switch.
        detail_source_events = tuple(
            dict(row)
            for row in history.events
        )
        detail_source_turns = materialize_history_turns(
            detail_source_events,
            include_live_detail=bool(
                is_codex_hist and in_progress and before is None),
        )

        if frame_size > frame_budget:
            # A single legacy turn may predate today's attachment limits. First
            # omit historical image bodies. If it is still too large, preserve a
            # bounded prompt + terminal marker and surface an explicit error event
            # instead of silently dropping the connection or pretending completeness.
            for row in history.events:
                if row.get("type") == "user_msg" and row.get("images"):
                    row["images"] = None
            frame_size = len(history.model_dump_json().encode())
            if frame_size > frame_budget:
                compact: list[dict] = [row.copy() for row in control_rows]
                for row in history.events:
                    if row.get("type") == "user_msg":
                        kept = row.copy()
                        prompt_text = str(kept.get("prompt", ""))
                        kept["prompt"] = prompt_text[:32 * 1024]
                        kept["images"] = None
                        compact.append(kept)
                        break
                notice = Error(
                    code=ERR_INTERNAL,
                    message="该历史回合超过传输上限，已省略过大的回复或附件",
                )
                notice.sid = sid
                compact.append(notice.model_dump(mode="json"))
                terminal = next(
                    (row for row in reversed(history.events)
                     if row.get("type") == "turn_end"),
                    None,
                )
                if terminal:
                    compact.append(terminal)
                history.events = compact
                log.warning("oversized history turn compacted", session_id=sid,
                            frame_budget=frame_budget)
                frame_size = len(history.model_dump_json().encode())
        if frame_size > frame_budget:
            notice = Error(
                code=ERR_INTERNAL,
                message="该历史回合超过传输上限，无法在当前帧限制内显示",
            )
            notice.sid = sid
            history.events = [notice.model_dump(mode="json")]
            history.oldest_id = None
            history.newest_id = None
        page_events = tuple(
            {
                **row,
                **({"images": None} if row.get("type") == "user_msg"
                   and row.get("images") else {}),
            }
            for row in history.events
        )
        materialized = MaterializedHistoryPage(
            events=page_events,
            has_more=history.has_more,
            oldest_id=history.oldest_id,
            newest_id=history.newest_id,
            turns=detail_source_turns,
        )
        if source_fingerprint is not None:
            if (self._history_index is not None
                    and indexed_page is not None
                    and not indexed_page.semantically_equals(materialized)):
                # Shadow mismatches never affect the response.  Remove the row
                # and refresh it below so corruption or a missed source change
                # cannot become authoritative when the fast path is enabled.
                log.warning(
                    "history index parity mismatch", session_id=sid,
                    before=bool(before), limit=limit,
                )
                await asyncio.to_thread(
                    self._history_index.invalidate_session, sid)
            try:
                current_fingerprint = await asyncio.to_thread(
                    HistorySourceFingerprint.capture, source_fingerprint.path)
                source_snapshot_stable = (
                    current_fingerprint == source_fingerprint
                )
            except OSError:
                source_snapshot_stable = False
            except Exception as exc:
                source_snapshot_stable = False
                log.warning(
                    "history source verification failed", session_id=sid,
                    error=str(exc),
                )
            if source_snapshot_stable and self._history_index is not None:
                try:
                    await asyncio.to_thread(
                        self._history_index.put_page,
                        sid,
                        "codex" if is_codex_hist else "claude",
                        source_fingerprint,
                        before=before,
                        limit=int(limit) if isinstance(limit, int) else 0,
                        page=materialized,
                        detail_events=detail_source_events,
                    )
                except Exception as exc:
                    # The index is a rebuildable acceleration layer. A failed
                    # write cannot make an otherwise coherent source snapshot
                    # non-authoritative or trigger an endless refresh loop.
                    log.warning(
                        "history index write failed", session_id=sid,
                        error=str(exc),
                    )
        if source_snapshot_stable is False:
            # The translator did not observe one coherent source snapshot. It
            # may still provide a useful first paint, but it cannot replace the
            # canonical transcript or clear a replay/rollback barrier.
            history.authoritative = False
            if allow_stale and before is None:
                self._schedule_history_refresh(
                    sid,
                    before=before,
                    limit=limit,
                    cwd=cwd_hint,
                    detail=detail,
                )
        if detail == "summary":
            history.turns = [
                ConversationTurn.model_validate(turn)
                for turn in materialized.turns
            ]
            history.detail = "summary"
            history.events = [
                row for row in history.events
                if row.get("type") in {"model", "effort"}
            ]
        return history

    def _schedule_history_refresh(
        self,
        sid: str,
        *,
        before: str | None,
        limit: int | None,
        cwd: str | None,
        detail: str,
    ) -> None:
        """Refresh one provisional moving-source page off the first-paint path."""
        ctx = self._ctx_by_sid(sid)
        watch = self._watch.get(sid) or {}
        is_codex = bool(
            (ctx is not None and ctx.engine == "codex")
            or watch.get("engine") == "codex"
        )
        # Codex rollouts are addressed by sid and never use cwd. Normalizing it
        # prevents clients carrying different cwd hints from starting parallel
        # scans of the same multi-gigabyte rollout.
        refresh_cwd = None if is_codex else cwd
        key = (
            sid,
            before or "",
            limit or 0,
            f"{refresh_cwd or ''}\0{detail}",
        )
        current = self._history_refresh_tasks.get(key)
        if current is not None and not current.done():
            self._history_refresh_dirty.add(key)
            return

        async def refresh() -> None:
            provisional_attempts = 0
            try:
                while True:
                    self._history_refresh_dirty.discard(key)
                    scan_started = time.monotonic()
                    history = await self._build_history(
                        sid,
                        before=before,
                        limit=limit,
                        cwd_hint=refresh_cwd,
                        detail=detail,
                        allow_stale=False,
                    )
                    scan_elapsed = time.monotonic() - scan_started
                    if history.authoritative is not False:
                        history.sid = sid
                        await self.transport.send(history)
                        log.info(
                            "stale history refreshed",
                            session_id=sid,
                            turns=len(history.turns),
                            before=bool(before),
                        )
                    # Coalesce every append observed during the scan into one
                    # final exact rebuild. This converges at turn completion
                    # without launching one full transcript/rollout parse per
                    # tool delta. Rate-limit a continuously moving source scan
                    # so one long-running turn cannot monopolize disk and CPU.
                    needs_rescan = (
                        key in self._history_refresh_dirty
                        or (
                            history.authoritative is False
                            and history.error is None
                        )
                    )
                    source_still_moving = (
                        history.authoritative is False
                        and history.error is None
                    )
                    provisional_attempts = (
                        provisional_attempts + 1
                        if source_still_moving
                        else 0
                    )
                    if not needs_rescan:
                        return
                    if (
                        self._history_refresh_in_progress(sid)
                        or provisional_attempts > 1
                    ):
                        await asyncio.sleep(
                            self._history_refresh_backoff_seconds(scan_elapsed))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "stale history refresh failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        task = asyncio.create_task(refresh())
        self._history_refresh_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._history_refresh_tasks.get(key) is done:
                self._history_refresh_tasks.pop(key, None)
            self._history_refresh_dirty.discard(key)

        task.add_done_callback(forget)

    def _history_refresh_in_progress(self, sid: str) -> bool:
        ctx = self._ctx_by_sid(sid)
        active_external_turns = (
            (self._watch.get(sid) or {}).get("active_external_turns")
        )
        return bool(
            (ctx is not None and ctx.state != "idle")
            or (
                isinstance(active_external_turns, dict)
                and active_external_turns
            )
        )

    def _history_refresh_backoff_seconds(self, scan_elapsed: float) -> float:
        return min(
            max(self.HISTORY_REFRESH_MIN_INTERVAL_SECONDS, scan_elapsed),
            self.HISTORY_REFRESH_MAX_INTERVAL_SECONDS,
        )

    async def _build_requested_history(
        self,
        sid: str,
        *,
        before: str | None,
        limit: int | None,
        cwd: str | None,
        detail: str,
    ) -> History:
        """Build one requester-neutral page shared by concurrent clients."""
        self._watch_session(sid)
        # Content and ownership are independent projections.  A bounded ps/lsof
        # scan can take seconds on macOS and must not hold the conversation
        # first paint. The watch loop and switch/query safety paths refresh
        # SessionControl separately; history uses the last known control value.
        return await self._build_history(
            sid, before=before, limit=limit, cwd_hint=cwd, detail=detail,
            allow_stale=True)

    async def _history_page_singleflight(self, cmd, sid: str) -> History:
        before = getattr(cmd, "before", None)
        raw_limit = getattr(cmd, "limit", None)
        limit = int(raw_limit) if isinstance(raw_limit, int) else None
        cwd = getattr(cmd, "cwd", None)
        detail = getattr(cmd, "detail", "full")
        key = (sid, before or "", limit or 0, f"{cwd or ''}\0{detail}")
        task = self._history_page_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._build_requested_history(
                sid, before=before, limit=limit, cwd=cwd, detail=detail))
            self._history_page_tasks[key] = task

            def forget(done: asyncio.Task) -> None:
                if self._history_page_tasks.get(key) is done:
                    self._history_page_tasks.pop(key, None)

            task.add_done_callback(forget)
        # A browser disconnect cancels only its routing handler; other clients
        # awaiting the same immutable page keep the shared read alive.
        return await asyncio.shield(task)

    async def _handle_get_history(self, cmd) -> None:
        """Client opened a session: return its history as ONE bulk frame, routed to
        the requester — like a web chat's GET /conversation. Opening it also starts
        MIRRORING its transcript, so appends made by a native Claude/Codex process
        stream through to every client (read-only)."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        template = await self._history_page_singleflight(cmd, sid)
        # Routing and command correlation are requester-specific; never mutate
        # the shared task's History instance in place.
        hist = template.model_copy(deep=True)
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            hist.to = client_id            # relay routes it to just this client
        hist.sid = sid                     # tag with THIS session (never the focused one)
        await self.transport.send(hist)
        frame_bytes = len(hist.model_dump_json().encode("utf-8"))
        log.info("history sent", session_id=sid, events=len(hist.events),
                 turns=len(hist.turns), detail=hist.detail,
                 frame_bytes=frame_bytes,
                 has_more=hist.has_more, before=bool(hist.before),
                 external=hist.external, client_id=client_id,
                 elapsed_ms=round((time.perf_counter() - started_at) * 1000))
        return hist

    async def _handle_get_turn_detail(self, cmd) -> TurnDetail:
        """Return one heavyweight turn projection to its requesting browser."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        revision = self._history_revision(sid)
        client_id = getattr(cmd, "client_id", None)

        async def send(
            events: list[dict] | None = None,
            *,
            error: str | None = None,
            has_more: bool = False,
            oldest_cursor: str | None = None,
            has_newer: bool = False,
            newer_cursor: str | None = None,
        ) -> TurnDetail:
            detail = TurnDetail(
                session_id=sid,
                turn_id=cmd.turn_id,
                revision=revision,
                authoritative=error is None,
                error=error,
                events=events or [],
                has_more=has_more,
                oldest_cursor=oldest_cursor,
                has_newer=has_newer,
                newer_cursor=newer_cursor,
                before=getattr(cmd, "before", None),
                sid=sid,
                to=client_id,
            )
            await self.transport.send(detail)
            frame_bytes = len(detail.model_dump_json().encode("utf-8"))
            log.info(
                "turn detail sent",
                session_id=sid,
                turn_id=cmd.turn_id,
                events=len(detail.events),
                frame_bytes=frame_bytes,
                authoritative=detail.authoritative,
                has_more=detail.has_more,
                has_newer=detail.has_newer,
                client_id=client_id,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return detail

        requested_revision = getattr(cmd, "revision", None)
        if requested_revision and requested_revision != revision:
            return await send(error="会话历史已更新，请重新展开该轮")
        if self._history_index is None:
            return await send(error="详细过程暂时不可用，请稍后重试")

        self._watch_session(sid)
        watch = self._watch.get(sid) or {}
        ctx = self._ctx_by_sid(sid)
        is_codex = bool(
            (ctx is not None and ctx.engine == "codex")
            or watch.get("engine") == "codex"
        )
        try:
            source_path = await asyncio.to_thread(
                codex_rollout_path if is_codex else transcript_path, sid)
            if not source_path:
                return await send(error="详细过程尚未生成")
            source = await asyncio.to_thread(
                HistorySourceFingerprint.capture, source_path)
            rows = await asyncio.to_thread(
                self._history_index.get_turn_detail,
                sid,
                "codex" if is_codex else "claude",
                source,
                cmd.turn_id,
            )
        except OSError:
            rows = None
        except Exception as exc:
            log.warning(
                "turn detail index read failed",
                session_id=sid,
                turn_id=cmd.turn_id,
                error=str(exc),
            )
            rows = None
        if rows is None:
            return await send(error="详细过程已过期，请刷新会话后重试")
        try:
            page, has_more, oldest, has_newer, newer = _turn_detail_page(
                rows,
                before=getattr(cmd, "before", None),
                limit=getattr(cmd, "limit", 192),
                max_bytes=min(
                    8 * 1024 * 1024,
                    max(512 * 1024, self.cfg.ws_max_size_bytes // 2),
                ),
            )
        except ValueError:
            return await send(error="详细过程分页位置已失效，请重新展开该轮")
        return await send(
            page,
            has_more=has_more,
            oldest_cursor=oldest,
            has_newer=has_newer,
            newer_cursor=newer,
        )

    async def _handle_get_history_image(self, cmd) -> HistoryImage:
        """Return one source-bound historical image without resuming an engine."""
        started_at = time.perf_counter()
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        revision = self._history_revision(sid)
        client_id = getattr(cmd, "client_id", None)

        async def send(
            *,
            media_type: str | None = None,
            width: int | None = None,
            height: int | None = None,
            data: bytes | None = None,
            error: str | None = None,
        ) -> HistoryImage:
            response = HistoryImage(
                session_id=sid,
                turn_id=cmd.turn_id,
                image_id=cmd.image_id,
                variant=cmd.variant,
                request_id=cmd.request_id,
                revision=revision,
                media_type=media_type,
                width=width,
                height=height,
                data=(base64.b64encode(data).decode("ascii")
                      if data is not None else None),
                error=error,
                sid=sid,
                to=client_id,
            )
            await self.transport.send(response)
            log.info(
                "history image sent",
                session_id=sid,
                turn_id=cmd.turn_id,
                image_id=cmd.image_id,
                variant=cmd.variant,
                bytes=len(data or b""),
                authoritative=error is None,
                client_id=client_id,
                elapsed_ms=round((time.perf_counter() - started_at) * 1000),
            )
            return response

        requested_revision = getattr(cmd, "revision", None)
        if requested_revision and requested_revision != revision:
            return await send(error="会话历史已更新，请重新加载图片")
        if self._history_index is None:
            return await send(error="历史图片暂时不可用")

        self._watch_session(sid)
        watch = self._watch.get(sid) or {}
        ctx = self._ctx_by_sid(sid)
        is_codex = bool(
            (ctx is not None and ctx.engine == "codex")
            or watch.get("engine") == "codex"
        )
        engine = "codex" if is_codex else "claude"
        try:
            source_path = await asyncio.to_thread(
                codex_rollout_path if is_codex else transcript_path, sid)
            if not source_path:
                return await send(error="历史图片尚未生成")
            source = await asyncio.to_thread(
                HistorySourceFingerprint.capture, source_path)

            if cmd.variant == "thumbnail":
                cached = await asyncio.to_thread(
                    self._history_index.get_image_asset,
                    sid, engine, source, cmd.turn_id, cmd.image_id,
                    cmd.variant,
                )
                if cached is not None:
                    media_type, width, height, data = cached
                    return await send(
                        media_type=media_type, width=width,
                        height=height, data=data)

            rows = await asyncio.to_thread(
                self._history_index.get_turn_detail,
                sid, engine, source, cmd.turn_id,
            )
            if rows is None:
                return await send(error="历史图片已过期，请刷新会话")
            image = history_image_from_events(
                rows, cmd.turn_id, cmd.image_id)
            if image is None:
                return await send(error="未找到这张历史图片")
            media_type, width, height, data = await asyncio.to_thread(
                _render_history_image, image, cmd.variant)
            if cmd.variant == "thumbnail":
                await asyncio.to_thread(
                    self._history_index.put_image_asset,
                    sid, engine, source, cmd.turn_id, cmd.image_id,
                    cmd.variant, media_type, width, height, data,
                )
            return await send(
                media_type=media_type, width=width, height=height, data=data)
        except ValueError as exc:
            return await send(error=str(exc))
        except Exception as exc:
            log.warning(
                "history image read failed",
                session_id=sid,
                turn_id=cmd.turn_id,
                image_id=cmd.image_id,
                error_type=type(exc).__name__,
            )
            return await send(error="历史图片读取失败，请重试")

    async def _handle_takeover(self, cmd):
        """Explicitly transfer a read-only terminal session back to Remote.

        Codex can attribute terminal turns and capture an idle process identity.
        Claude has no equivalent control channel, so its takeover is deliberately
        fail-closed: record the user's intent while a terminal is alive and grant
        ownership only after that exact process exits.
        """
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            error = Error(code=ERR_NOT_RUNNING, message="该会话未启动，无法接管")
            await self._emit_to_sid(sid, error)
            return error
        if ctx.state != "idle":
            error = Error(code=ERR_BUSY, message="该会话正在运行，当前无需接管")
            await self._emit(ctx, error)
            return error

        # ``claude-remote`` is already a shared owner, not a foreign CLI to
        # terminate. This also closes the short interval before the periodic
        # watcher has upgraded a previously-resident SDK context.
        if (ctx.engine == "claude" and ctx.space == "code"
                and not getattr(ctx.sdk, "is_claude_broker", False)):
            await self._adopt_claude_broker_handle(ctx)

        resolved_sid = ctx.session_id or sid
        if not resolved_sid:
            error = Error(code=ERR_NOT_RUNNING, message="该会话尚无可接管的会话 ID")
            await self._emit(ctx, error)
            return error
        existing_watch = self._watch.get(resolved_sid)
        if (ctx.engine == "codex"
                and bool((existing_watch or {}).get("desktop_active"))):
            error = Error(
                code=ERR_BUSY,
                message=("Codex App 正在运行此会话，不能安全接管；"
                         "请等待当前回合结束后重试"),
            )
            await self._sync_external_control(ctx, existing_watch)
            await self._emit(ctx, error)
            return error
        if self._codex_shared_affinity(ctx):
            await self._sync_external_control(
                ctx, self._watch.get(resolved_sid))
            await self._emit(ctx, TakeoverState(
                pending=False,
                message="Codex 已通过共享 daemon 双向连接，无需迁移或接管",
            ))
            return None
        if getattr(ctx.sdk, "is_claude_broker", False):
            await self._sync_external_control(
                ctx, self._watch.get(resolved_sid))
            await self._emit(ctx, TakeoverState(
                pending=False,
                message="Claude 已通过 broker 双向连接，无需结束终端或迁移会话",
            ))
            return None
        self._watch_session(resolved_sid)
        w = self._watch.get(resolved_sid)
        if w is None:
            error = Error(code=ERR_INTERNAL, message="无法读取该会话的终端状态")
            await self._emit(ctx, error)
            return error

        if ctx.engine == "codex":
            async with self._codex_watch_lock:
                scan = await self._probe_codex_holders({resolved_sid: w["path"]})
                if not scan.complete:
                    error = Error(
                        code=ERR_BUSY,
                        message="终端状态扫描暂不完整，未执行接管，请重试",
                    )
                    await self._emit(ctx, error)
                    return error
                holders, writers, private_holders = self._codex_holder_sets(
                    w, scan, resolved_sid)
                await self._poll_codex_watch(
                    resolved_sid, w, holders, time.time(), writers=writers,
                    private_holders=private_holders,
                    ownership_scan_complete=scan.complete)
                if w.get("desktop_active"):
                    error = Error(
                        code=ERR_BUSY,
                        message=("Codex App 正在运行此会话，不能安全接管；"
                                 "请等待当前回合结束后重试"),
                    )
                    await self._emit(ctx, error)
                    return error
                if w["active_external_turns"]:
                    w["takeover_pending"] = {
                        "writers": set(writers),
                        "interactive": set(holders),
                        "turn_ids": set(w["active_external_turns"]),
                    }
                    await self._sync_external_control(ctx, w)
                    await self._emit(ctx, TakeoverState(
                        pending=True,
                        message=("已登记接管；当前回复结束后会自动交给 Remote。"
                                 "若期间出现新终端或新回合，本次登记会安全取消"),
                    ))
                    log.info("session takeover queued behind external turn",
                             session_id=resolved_sid)
                    return None
                self._grant_codex_takeover(w, holders, writers)
                w["external"] = bool(holders)
                ctx.needs_reload = True
        else:
            async with self._codex_watch_lock:
                paths, cwds = self._claude_watch_inputs()
                scan = await self._probe_claude_holders(paths, cwds)
                holders = set(scan.holders.get(resolved_sid, ()))
                if not scan.complete:
                    holders.update(w.get("holders", ()))
                await self._poll_claude_watch(
                    resolved_sid,
                    w,
                    holders,
                    time.time(),
                    ownership_scan_complete=scan.complete,
                )
                if not scan.complete or not w.get("file_available", False):
                    error = Error(
                        code=ERR_BUSY,
                        message="终端状态或会话文件暂不可确认，未执行接管，请重试",
                    )
                    await self._emit(ctx, error)
                    return error
                if holders:
                    w["takeover_pending"] = True
                    w["external"] = True
                    await self._sync_external_control(ctx, w)
                    await self._emit(ctx, TakeoverState(
                        pending=True,
                        message=("正在安全结束本机 Claude CLI，并把最新历史迁移给 "
                                 "Remote；不会终止终端 Shell"),
                    ))
                    remaining = await self._terminate_external_claude_holders(
                        holders)
                    if remaining:
                        w["takeover_pending"] = False
                        w["external"] = True
                        await self._sync_external_control(ctx, w)
                        await self._emit(ctx, TakeoverState(
                            pending=False,
                            message="本机 Claude 未在安全等待时间内退出，未强制结束进程",
                        ))
                        error = Error(
                            code=ERR_BUSY,
                            message=("本机 Claude 未退出，迁移已取消；可在终端退出后重试，"
                                     "Remote 仍保持只读"),
                        )
                        await self._emit(ctx, error)
                        return error
                    # Consume final JSONL flushes only after the exact CLI
                    # identity disappeared. Keep Web read-only until the
                    # resident SDK has resumed this exact transcript.
                    try:
                        final_stat = await asyncio.to_thread(os.stat, w["path"])
                    except OSError:
                        final_stat = None
                    if final_stat is not None:
                        w["size"] = final_stat.st_size
                        w["file_id"] = (final_stat.st_dev, final_stat.st_ino)
                        w["file_available"] = True
                    reload_error = await self._reload_claude_after_takeover(ctx)
                    w["holders"] = set()
                    w["takeover_pending"] = False
                    w["external"] = False
                    w["scan_complete"] = True
                    ctx.external_ts = 0.0
                    if reload_error is not None:
                        await self._emit(ctx, TakeoverState(
                            pending=False,
                            message=("本机 Claude 已退出，但 Remote 恢复失败；"
                                     "会话仍禁止写入"),
                        ))
                        return reload_error
                    log.info(
                        "Claude external CLI migrated to Remote",
                        session_id=resolved_sid,
                        holders=len(holders),
                    )
                    await self._emit(ctx, TakeoverState(
                        pending=False,
                        message="本机 Claude 已退出，会话已迁移到 Remote",
                    ))
                else:
                    reload_error = await self._reload_claude_after_takeover(ctx)
                    w["takeover_pending"] = False
                    w["external"] = False
                    w["scan_complete"] = True
                    ctx.external_ts = 0.0
                    if reload_error is not None:
                        await self._emit(ctx, TakeoverState(
                            pending=False,
                            message="Remote 恢复失败，会话仍禁止写入",
                        ))
                        return reload_error

        await self._sync_external_control(ctx, w)

        log.info("session manually taken over", session_id=resolved_sid,
                 engine=ctx.engine)
        # The browser stays read-only until this authoritative History arrives;
        # queued sends cannot overtake the takeover command on the WebSocket.
        await self._push_mirrored_history(resolved_sid)
        # Takeover is at-most-once. Do not cache/replay an old external=false
        # History: a new terminal owner may have appeared before an ACK-lost retry.
        # The duplicate receives only its ACK; GetHistory or another explicit click
        # can recover a response that was lost with the original WebSocket.
        return None

    async def _handle_query(self, cmd):
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            # sid given but not resident (spawn failed / evicted). Tag the error to
            # THAT session so the user sees it there — and never reroute the prompt
            # to a different session.
            error = Error(code=ERR_NOT_RUNNING,
                message="该会话未启动(可能启动失败),重新点进这个会话再发",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if getattr(cmd, "delivery", "immediate") != "immediate":
            return await self._enqueue_deferred_query(ctx, cmd)
        async with ctx.query_lock:
            return await self._handle_immediate_query(ctx, cmd)

    async def _handle_immediate_query(self, ctx: SessionContext, cmd):
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY, message="该会话正忙,先 interrupt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        if ctx.engine == "claude" and ctx.space == "code":
            await self._adopt_claude_broker_handle(ctx)
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY,
                    message="该会话正由终端中的 Claude 回合运行，先 interrupt",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        is_claude_broker = bool(getattr(ctx.sdk, "is_claude_broker", False))
        is_codex_shared = self._codex_shared_affinity(ctx)
        if (
            is_codex_shared
            and not await self._ensure_codex_daemon_generation(
                ctx, reason="query preflight")
        ):
            error = Error(
                code=ERR_NOT_RUNNING,
                message="Codex 共享通道重连失败，本次未发送；请重试",
                msg_id=getattr(cmd, "msg_id", None),
            )
            await self._emit(ctx, error)
            return error
        if is_claude_broker:
            try:
                metadata = await ctx.sdk.refresh_status()
            except BrokerClientError:
                refreshed = await self._refresh_claude_broker_handle(ctx)
                is_claude_broker = bool(getattr(
                    ctx.sdk, "is_claude_broker", False))
                if refreshed and is_claude_broker:
                    metadata = ctx.sdk.metadata
                elif is_claude_broker:
                    error = Error(
                        code=ERR_BUSY,
                        message="Claude 连接暂不可用，本次消息未发送，请稍后重试。",
                        msg_id=getattr(cmd, "msg_id", None),
                    )
                    await self._emit(ctx, error)
                    return error
                else:
                    # A terminal session exit was proven and the context is now
                    # a fully connected SDK handle. Continue through the normal
                    # final ownership checks in this same send operation.
                    metadata = {}
            if is_claude_broker:
                await self._sync_claude_broker_runtime_controls(ctx)
                await self._sync_external_control(
                    ctx, self._watch.get(ctx.session_id or ""))
            if is_claude_broker and metadata.get("input_busy"):
                error = Error(
                    code=ERR_BUSY,
                    message="本机终端正在编辑输入；完成、发送或取消后再从 Remote 发送",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        if ctx.session_id:
            self._watch_session(ctx.session_id)
            external = (
                await self._prime_codex_ownership(ctx.session_id)
                if ctx.engine == "codex"
                else (False if is_claude_broker
                      else await self._prime_claude_ownership(ctx.session_id))
            )
            if external:
                engine_name = "Codex" if ctx.engine == "codex" else "Claude"
                watch = self._watch.get(ctx.session_id) or {}
                if (ctx.engine == "codex"
                        and watch.get("desktop_active")):
                    message = (
                        "Codex App 正在运行此会话，本次未发送；"
                        "请等待当前回合结束后重试"
                    )
                else:
                    message = (
                        f"该 {engine_name} 会话正在被本机终端使用，或终端状态"
                        "暂不可确认；请退出终端或点击『接管』"
                    )
                error = Error(
                    code=ERR_BUSY,
                    message=message,
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        if not cmd.prompt and not cmd.images and not cmd.files:
            error = Error(
                code=ERR_BAD_PROMPT,
                message="消息内容为空，请输入内容或添加附件。",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message="附件不符合要求，请调整后重试。",
                msg_id=getattr(cmd, "msg_id", None),
            )
            await self._emit(ctx, error)
            return error
        if ctx.space == "work" and ctx.work_id:
            try:
                await asyncio.to_thread(
                    self._work.for_engine(ctx.engine).sync_work_id, ctx.work_id
                )
            except Exception:
                log.exception(
                    "Work context sync failed", engine=ctx.engine, work_id=ctx.work_id
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="工作资料同步失败，本轮尚未发送；请重试",
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        # All synchronous rejection paths have passed. A new conversation may
        # now finish before the next sidebar catalog read, so remember its first
        # accepted prompt under the temporary key; capture migrates it to the
        # real engine session id.
        title_sid = ctx.session_id or ctx.key
        if (
            ctx.session_id is None
            and title_sid
            and title_sid not in self._notification_titles
        ):
            self._remember_notification_title(title_sid, cmd.prompt)
        # claim synchronously so a concurrent query on THIS ctx can't race in
        ctx.interrupt_event.clear()
        ctx.interrupt_deadline = None
        ctx.active_msg_id = cmd.msg_id
        ctx.state = "running"
        async with ctx.emit_lock:
            await self._emit_locked(ctx, StateEvent(state="running"))
        runner = (self._run_claude_broker_turn
                  if is_claude_broker else self._run_turn)
        ctx.turn_task = asyncio.create_task(
            runner(ctx, cmd.prompt, getattr(cmd, "images", None),
                   getattr(cmd, "files", None)))

    async def _handle_steer(self, cmd):
        """Append one user instruction to the exact active Codex turn.

        Steering is neither a new engine turn nor an interrupt.  The successful
        narrative echo is replayable so every browser splits the visible task at
        the same point, while command failures remain correlated to the sender.
        """
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)

        async def reject(code: str, message: str) -> Error:
            error = Error(
                code=code,
                message=message,
                msg_id=getattr(cmd, "msg_id", None),
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
                sid=(ctx.session_id or ctx.key) if ctx is not None else sid,
            )
            # This is a correlated control rejection, not shared session
            # narrative. Buffering it would let a later client replay A's
            # targeted failure as its own after hello rewrites the recipient.
            await self.transport.send(error)
            return error

        if ctx is None:
            return await reject(
                ERR_NOT_STEERABLE, "该会话未启动，无法引导当前任务")
        if ctx.engine != "codex":
            return await reject(
                ERR_NOT_STEERABLE,
                "Claude 当前不支持无打断引导；请使用打断并发送或排队。",
            )
        if ctx.state != "running":
            return await reject(
                ERR_NOT_STEERABLE, "当前没有可引导的 Codex 任务")
        if ctx.write_state != "writable":
            return await reject(
                ERR_NOT_STEERABLE,
                "该会话当前为只读状态，无法从 Remote 引导")
        if ctx.codex_uncertain_steer is not None:
            return await reject(
                ERR_STEER_UNKNOWN,
                "上一条 Codex 引导结果仍未确认，请等待当前任务结束或刷新历史。",
            )
        if not cmd.prompt and not cmd.images and not cmd.files:
            return await reject(
                ERR_NOT_STEERABLE,
                "消息内容为空，请输入内容或添加附件。")
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            return await reject(
                ERR_NOT_STEERABLE,
                "附件不符合要求，请调整后重试。")

        original_prompt = cmd.prompt
        prompt = original_prompt
        images = getattr(cmd, "images", None)
        files = getattr(cmd, "files", None)
        file_meta = ([{"filename": item.get("filename", "attachment")}
                      for item in (files or [])] or None)
        temp_dir: str | None = None
        persistent_attachments = False
        accepted = False
        steer_gate_held = False
        steer_fence: Optional[CodexSteerFence] = None
        sdk_turn_id: str | None = None

        def retain_attachments() -> None:
            nonlocal accepted
            accepted = True
            if (temp_dir is not None and not persistent_attachments
                    and temp_dir not in ctx.codex_steer_attachment_dirs):
                ctx.codex_steer_attachment_dirs.append(temp_dir)

        try:
            if files or images:
                if ctx.space == "work":
                    upload_root = Path(ctx.cwd).parent / "uploads"
                    upload_dir = upload_root / cmd.msg_id
                    upload_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                    temp_dir = str(upload_dir)
                    persistent_attachments = True
                else:
                    temp_dir = tempfile.mkdtemp(prefix="cc-remote-turn-")
                    os.chmod(temp_dir, 0o700)
            if files:
                prompt = self._stash_files(
                    prompt, files, temp_dir, ctx.engine)
            image_paths = (
                self._stash_images(images, temp_dir) if images else [])

            # Multiple clients may steer concurrently. Serialize steer requests
            # per session, but do not take launch_lock: Stop must be able to send
            # turn/interrupt even when an app-server steer response is delayed.
            async with ctx.steer_lock:
                if ctx.state != "running":
                    return await reject(
                        ERR_NOT_STEERABLE,
                        "Codex 任务已结束，本次引导未发送。",
                    )
                sdk_turn_id = getattr(ctx.sdk, "turn_id", None)
                sdk_turn_active = bool(
                    getattr(ctx.sdk, "turn_active", False))
                if not sdk_turn_id or not sdk_turn_active:
                    return await reject(
                        ERR_NOT_STEERABLE,
                        "Codex 当前回合不支持引导，请等待后重试。",
                    )
                ctx.codex_steer_gate.clear()
                steer_gate_held = True
                steer_acceptance = await ctx.sdk.steer(
                    prompt,
                    images=image_paths,
                    client_user_message_id=cmd.msg_id,
                )
                turn_id = str(steer_acceptance)
                candidate_fence = getattr(
                    steer_acceptance, "fence", None)
                if isinstance(candidate_fence, CodexSteerFence):
                    steer_fence = candidate_fence
                    # Drain every raw frame admitted before the app-server RPC
                    # response. The in-band fence then pauses the consumer while
                    # this coroutine publishes the new user boundary.
                    ctx.codex_steer_gate.set()
                    await steer_fence.reached.wait()
                retain_attachments()
                ctx.active_msg_id = cmd.msg_id
                event = TurnSteered(
                    msg_id=cmd.msg_id,
                    turn_id=turn_id,
                    prompt=original_prompt,
                    images=images,
                    files=file_meta,
                )
                try:
                    await self._emit(ctx, event)
                except Exception as exc:
                    # _emit buffers before transport.send. Even if the live
                    # socket disappeared at that boundary, the native mutation
                    # is accepted and must never be retried.
                    log.warning(
                        "accepted Codex steer live echo delayed",
                        session_id=ctx.session_id,
                        error_type=type(exc).__name__,
                    )
                return event
        except CodexSteerOutcomeUnknown:
            # JSON-RPC timeout is not a rejection: app-server may accept and
            # route turn/steer after the client-side deadline. Keep attachment
            # paths valid through the native terminal and do not tell the user
            # to retry a mutation whose outcome is unknown.
            retain_attachments()
            ctx.codex_uncertain_steer = TurnSteered(
                msg_id=cmd.msg_id,
                turn_id=getattr(ctx.sdk, "turn_id", None) or sdk_turn_id,
                prompt=original_prompt,
                images=images,
                files=file_meta,
            )
            error = Error(
                code=ERR_STEER_UNKNOWN,
                message=(
                    "Codex 尚未确认本次引导是否生效；请先观察后续输出或"
                    "刷新历史，确认前不要重复发送。"
                ),
                msg_id=cmd.msg_id,
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
                sid=ctx.session_id or ctx.key,
            )
            try:
                await self.transport.send(error)
            except Exception as exc:
                log.warning(
                    "Codex steer uncertainty notice delayed",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
            log.warning(
                "Codex steer outcome unknown after transport wait failure",
                session_id=ctx.session_id,
            )
            return error
        except CodexAppServerError as exc:
            if exc.active_turn_not_steerable or exc.steer_turn_changed:
                kind = exc.unsteerable_turn_kind
                stage = (
                    "Review"
                    if kind and "review" in kind.lower()
                    else "自动压缩"
                    if kind and "compact" in kind.lower()
                    else "当前阶段"
                )
                return await reject(
                    ERR_NOT_STEERABLE,
                    f"Codex {stage}不支持引导，或任务已经切换；本次未发送。",
                )
            log.warning(
                "Codex steer rejected",
                session_id=ctx.session_id,
                error_code=exc.code,
            )
            return await reject(
                ERR_NOT_STEERABLE,
                "Codex 引导暂时失败，本次未发送；请重试。",
            )
        except Exception as exc:
            log.warning(
                "Codex steer failed",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            return await reject(
                ERR_NOT_STEERABLE,
                "Codex 引导暂时失败，本次未发送；请重试。",
            )
        finally:
            if steer_fence is not None:
                steer_fence.release_now()
            if temp_dir is not None and not accepted:
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    log.warning(
                        "steer attachment cleanup failed",
                        error_type=type(exc).__name__,
                    )
            if steer_gate_held:
                ctx.codex_steer_gate.set()

    async def _reconcile_codex_no_active_turn(
        self, ctx: SessionContext, error: CodexNoActiveTurnError,
    ) -> bool:
        """Unlock one proven-dead spontaneous turn without touching managed I/O."""
        if ctx.engine != "codex" or ctx.turn_task is not None:
            return False

        def settled() -> bool:
            return bool(
                ctx.state == "idle"
                and ctx.codex_spontaneous_turn_id != error.turn_id
                and ctx.codex_spontaneous_task is None
                and ctx.active_msg_id != error.turn_id
            )

        if settled():
            return True
        spontaneous_id = ctx.codex_spontaneous_turn_id
        spontaneous_task = ctx.codex_spontaneous_task
        if (spontaneous_id is not None
                and spontaneous_id != error.turn_id):
            return False
        if (
            spontaneous_id is None
            and spontaneous_task is not None
            and ctx.active_msg_id not in {None, error.turn_id}
        ):
            return False

        confirm = getattr(ctx.sdk, "confirm_no_active_turn", None)
        reconcile = getattr(ctx.sdk, "reconcile_no_active_turn", None)
        if confirm is None or reconcile is None:
            return False
        try:
            confirmation = await confirm(error.thread_id, error.turn_id)
        except Exception as exc:
            log.warning(
                "Codex inactive-turn confirmation failed",
                session_id=ctx.session_id,
                turn_id=error.turn_id,
                error_type=type(exc).__name__,
            )
            return False
        if confirmation is None:
            return settled()
        if (
            ctx.codex_spontaneous_task not in {None, spontaneous_task}
            or (
                ctx.codex_spontaneous_turn_id is not None
                and ctx.codex_spontaneous_turn_id != error.turn_id
            )
        ):
            return False

        # If app-server resolved turn/steer immediately before the interrupt
        # miss, its reader may have delivered both responses before the steer
        # coroutine emitted the user boundary. Preserve that boundary ordering.
        await ctx.codex_steer_gate.wait()

        emit_synthetic = not confirmation.authoritative_terminal
        fence = confirmation.fence
        if confirmation.authoritative_terminal:
            # A terminal admitted before thread/read remains authoritative. Wait
            # for the exact consumer without a time guess so its final tail and
            # status cannot be replaced by a synthetic interruption.
            if spontaneous_task is not None:
                await asyncio.gather(
                    spontaneous_task, return_exceptions=True)
            if settled():
                return True
            # If the consumer died before dequeuing the retained terminal, there
            # is no remaining owner which can publish it. The history projection
            # repair below recovers its durable tail after the synthetic close.
            emit_synthetic = bool(
                spontaneous_task is None
                or confirmation.terminal_pending()
            )
        elif isinstance(fence, CodexNoActiveTurnFence):
            if spontaneous_task is not None and not spontaneous_task.done():
                reached_task = asyncio.create_task(fence.reached.wait())
                try:
                    await asyncio.wait(
                        {spontaneous_task, reached_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not reached_task.done():
                        reached_task.cancel()
                        await asyncio.gather(
                            reached_task, return_exceptions=True)
            if fence.reached.is_set() and confirmation.terminal_pending():
                # A terminal may occupy the reserved end slot immediately after
                # the thread/read response. It follows this fence in FIFO order;
                # release the consumer and let the real terminal win.
                fence.release_now()
                if spontaneous_task is not None:
                    await asyncio.gather(
                        spontaneous_task, return_exceptions=True)
                if settled():
                    return True
                emit_synthetic = bool(
                    spontaneous_task is None
                    or confirmation.terminal_pending()
                )

        if settled():
            if fence is not None:
                fence.release_now()
            return True
        if (
            ctx.codex_spontaneous_task not in {None, spontaneous_task}
            or (
                ctx.codex_spontaneous_turn_id is not None
                and ctx.codex_spontaneous_turn_id != error.turn_id
            )
        ):
            if fence is not None:
                fence.release_now()
            return False
        if not reconcile(error.thread_id, error.turn_id):
            if fence is not None:
                fence.release_now()
            return False

        # Detach the routing identity before cancellation. The real spontaneous
        # consumer's finally block then observes that this turn was already
        # reconciled and cannot emit a second terminal or overwrite newer state.
        ctx.codex_spontaneous_turn_id = None
        ctx.codex_spontaneous_task = None
        if spontaneous_task is not None and not spontaneous_task.done():
            spontaneous_task.cancel()
        if fence is not None:
            fence.release_now()
        if spontaneous_task is not None:
            await asyncio.gather(spontaneous_task, return_exceptions=True)
        if emit_synthetic:
            await self._emit(ctx, TurnEnd(
                result=TurnResult(
                    subtype="interrupted", duration_ms=0, is_error=False),
                turn_id=error.turn_id,
            ))
        ctx.active_msg_id = None
        ctx.interrupt_deadline = None
        ctx.interrupt_event.clear()
        await self._cleanup_codex_steer_attachments(ctx)
        await self._set_state(ctx, "idle")
        if emit_synthetic:
            try:
                await self._push_mirrored_history(
                    ctx.session_id or ctx.key)
            except Exception as exc:
                log.warning(
                    "inactive Codex projection repair failed",
                    session_id=ctx.session_id,
                    turn_id=error.turn_id,
                    error_type=type(exc).__name__,
                )
        log.warning(
            "reconciled inactive Codex spontaneous turn",
            session_id=ctx.session_id,
            turn_id=error.turn_id,
            synthetic_terminal=emit_synthetic,
        )
        return True

    async def _handle_interrupt(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "打断")
        # Reliable command retries and impatient second clicks are expected.
        # Once the first interrupt has been accepted, another stop must be an
        # idempotent no-op rather than a misleading `not_running` error that also
        # leaves the browser's state machine stuck in interrupting.
        if ctx.state in {"interrupting", "draining"}:
            return
        if ctx.state != "running":
            error = Error(
                code=ERR_NOT_RUNNING,
                message="该会话没有正在运行的回合",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        self._cancel_pending_asks(ctx)
        # Set the deadline and wake the turn consumer before entering any await:
        # it may already be blocked in the queue.get() that began while running.
        ctx.interrupt_deadline = (
            asyncio.get_running_loop().time() + self.cfg.drain_timeout
        )
        ctx.state = "interrupting"
        ctx.interrupt_event.set()
        log.info(
            "interrupt accepted",
            session_id=ctx.session_id or ctx.key,
            engine=ctx.engine,
            client_id=getattr(cmd, "client_id", None),
            cmd_id=getattr(cmd, "cmd_id", None),
        )
        await self._emit(ctx, StateEvent(state="interrupting"))
        # A turn can still be reconnecting to apply effort/tier changes and may
        # not have submitted its query yet.  Serialize against that final launch
        # window: if the turn observes our event and aborts, it changes state to
        # idle; otherwise query() has returned and the new live turn is safe to
        # interrupt here.
        async with ctx.launch_lock:
            if ctx.state != "interrupting":
                return
            try:
                await ctx.sdk.interrupt()
            except CodexNoActiveTurnError as error:
                if await self._reconcile_codex_no_active_turn(ctx, error):
                    return
                log.warning(
                    "Codex no-active-turn could not be reconciled safely",
                    session_id=ctx.session_id,
                    has_managed_turn=ctx.turn_task is not None,
                    has_spontaneous_turn=ctx.codex_spontaneous_task is not None,
                )
            except Exception as e:
                log.exception("interrupt call failed", error=str(e))
                # The stream is still authoritative: a very fast turn may have
                # completed just before this RPC, with its terminal frame already
                # queued. Do not surface raw app-server text or force idle here.
                # The managed/spontaneous consumer drains that frame; if it never
                # arrives, the absolute interrupt deadline reconnects and unlocks.
                if ctx.turn_task is None and ctx.codex_spontaneous_task is None:
                    await self._set_state(ctx, "idle")

    @classmethod
    def _read_claude_settings(cls, path: str) -> Optional[dict]:
        """Read one settings object without trusting an unbounded config file."""
        try:
            with open(path, "rb") as handle:
                raw = handle.read(cls.CLAUDE_SETTINGS_MAX_BYTES + 1)
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        if len(raw) > cls.CLAUDE_SETTINGS_MAX_BYTES:
            log.warning("Claude settings ignored: file too large", path=path)
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("Claude settings ignored: invalid JSON", path=path)
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _claude_project_root(cwd: str) -> str:
        """Find Claude's project scope (git root, else nearest .claude scope)."""
        current = os.path.realpath(cwd)
        nearest_settings = None
        for _ in range(64):
            if os.path.exists(os.path.join(current, ".git")):
                return current
            if nearest_settings is None and any(os.path.exists(path) for path in (
                os.path.join(current, ".claude", "settings.json"),
                os.path.join(current, ".claude", "settings.local.json"),
            )):
                nearest_settings = current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return nearest_settings or os.path.realpath(cwd)

    @staticmethod
    def _claude_managed_settings_paths() -> list[str]:
        """Return file-managed policy sources in their documented merge order."""
        result: list[str] = []
        for root in (
            "/etc/claude-code",
            "/Library/Application Support/ClaudeCode",
        ):
            result.append(os.path.join(root, "managed-settings.json"))
            drop_in = os.path.join(root, "managed-settings.d")
            try:
                with os.scandir(drop_in) as entries:
                    names = sorted(
                        entry.name for entry in entries
                        if entry.is_file() and not entry.name.startswith(".")
                        and entry.name.endswith(".json"))
            except OSError:
                continue
            result.extend(os.path.join(drop_in, name) for name in names)
        return result

    @classmethod
    def _claude_configured_model(cls, cwd: str) -> Optional[str]:
        """Resolve an explicit new-session model without starting Claude CLI.

        Claude's account/organization runtime Default cannot be read without a
        live CLI, so an absent explicit value returns None. The caller then uses
        cc-remote's curated new-session default.
        """
        root = cls._claude_project_root(cwd)
        user_settings = str(claude_config_dir() / "settings.json")
        ordinary_paths = [
            user_settings,
            os.path.join(root, ".claude", "settings.json"),
            os.path.join(root, ".claude", "settings.local.json"),
        ]

        def explicit_model(value) -> tuple[bool, Optional[str]]:
            if not isinstance(value, str):
                return False, None
            value = value.strip()
            if not 0 < len(value) <= 256:
                return False, None
            return True, None if value.lower() == "default" else value

        model = None
        model_set = False
        settings_env_model = None
        settings_env_model_set = False
        for path in ordinary_paths:
            settings = cls._read_claude_settings(path)
            if settings is None:
                continue
            present, candidate = explicit_model(settings.get("model"))
            if present:
                model_set = True
                model = candidate
            env = settings.get("env")
            present, candidate = explicit_model(
                env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None)
            if present:
                settings_env_model_set = True
                settings_env_model = candidate

        managed_model = None
        managed_model_set = False
        managed_env_model = None
        managed_env_model_set = False
        for path in cls._claude_managed_settings_paths():
            settings = cls._read_claude_settings(path)
            if settings is None:
                continue
            present, candidate = explicit_model(settings.get("model"))
            if present:
                managed_model_set = True
                managed_model = candidate
            env = settings.get("env")
            present, candidate = explicit_model(
                env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None)
            if present:
                managed_env_model_set = True
                managed_env_model = candidate

        # Managed policy cannot be overridden. Claude then applies an `env`
        # value from settings over the process environment inherited at launch;
        # the scalar model field remains the lowest-precedence explicit source.
        external_model_set, external_model = explicit_model(
            os.environ.get("ANTHROPIC_MODEL"))
        if managed_env_model_set:
            return managed_env_model
        if managed_model_set:
            return managed_model
        if settings_env_model_set:
            return settings_env_model
        if external_model_set:
            return external_model
        return model if model_set else None

    async def _claude_new_session_defaults(
        self, cwd: Optional[str],
    ) -> tuple[Optional[str], str]:
        raw_cwd = cwd or self.cfg.cc_cwd
        target_cwd = os.path.realpath(os.path.expanduser(raw_cwd))
        if not os.path.isdir(target_cwd):
            return CLAUDE_DEFAULT_MODEL, CLAUDE_DEFAULT_EFFORT
        model = await asyncio.to_thread(
            self._claude_configured_model, target_cwd)
        # Claude's generic Opus aliases intentionally track the current Opus.
        # Pin cc-remote's new-session choice to the context-qualified id so a
        # provider cannot silently drop the requested 1M window. Exact custom
        # and non-Opus ids remain provider-owned and pass through unchanged.
        return (
            _normalize_claude_new_session_model(model)
            or CLAUDE_DEFAULT_MODEL,
            CLAUDE_DEFAULT_EFFORT,
        )

    async def _handle_get_models(self, cmd) -> None:
        """Answer with the engine's catalog and effective new-session defaults.

        Codex exposes its catalog through app-server. Claude has no side-effect-
        free catalog/default RPC, so its list stays empty while bounded settings
        reads resolve a cwd-aware model and fall back to the curated default;
        the client keeps its static presentation table.
        """
        engine = getattr(cmd, "engine", None) or "cc"
        models = await codex_catalog() if engine == "codex" else []
        default_model = None
        default_effort = None
        defaults_cwd = None
        if engine == "codex":
            # config.toml's `model` = what a NEW session (and the terminal codex)
            # starts on. Only offer it if the catalog actually has it, so a stale
            # config can't preselect a model that isn't there.
            cfg_model = await asyncio.to_thread(codex_model, "")
            if cfg_model and any(m["id"] == cfg_model for m in models):
                default_model = cfg_model
        elif engine in {"cc", "claude"}:
            defaults_cwd = getattr(cmd, "cwd", None) or self.cfg.cc_cwd
            default_model, default_effort = (
                await self._claude_new_session_defaults(
                    defaults_cwd))
        msg = Models(
            engine=engine, models=models, default_model=default_model,
            default_effort=default_effort, cwd=defaults_cwd)
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            msg.to = client_id           # relay routes it to just this client
        await self.transport.send(msg)
        log.info("models sent", engine=engine, count=len(models),
                 default_model=default_model, default_effort=default_effort,
                 client_id=client_id)
        return msg

    async def _handle_get_engine_capabilities(self, cmd):
        engine = cmd.engine
        space = getattr(cmd, "space", "code")
        target_cwd = getattr(cmd, "cwd", None)
        if not target_cwd:
            focused = self._focused_ctx()
            if (
                focused is not None
                and focused.engine == engine
                and focused.space == space
            ):
                target_cwd = focused.cwd
        if not target_cwd:
            target_cwd = self.cfg.cc_cwd
        client_id = getattr(cmd, "client_id", None)
        try:
            items, errors, notes = await engine_capabilities(
                engine,
                target_cwd,
                space,
                self.cfg.claude_bin,
                skills_only=getattr(cmd, "skills_only", False),
            )
        except Exception:
            log.exception(
                "engine capability discovery failed", engine=engine, space=space
            )
            items, errors, notes = [], ["capability discovery failed"], []
        result = EngineCapabilities(
            engine=engine,
            space=space,
            request_id=getattr(cmd, "cmd_id", None),
            cwd=target_cwd,
            items=items,
            errors=errors,
            notes=notes,
            skills_only=getattr(cmd, "skills_only", False),
            to=client_id,
        )
        await self.transport.send(result)
        return result

    def _engine_capability_cwd(self, cmd) -> str:
        target_cwd = getattr(cmd, "cwd", None)
        if target_cwd:
            return target_cwd
        focused = self._focused_ctx()
        if (
            focused is not None
            and focused.engine == cmd.engine
            and focused.space == getattr(cmd, "space", "code")
        ):
            return focused.cwd
        return self.cfg.cc_cwd

    async def _send_capability_mutation_error(self, cmd, exc: Exception):
        if isinstance(exc, ValueError):
            code, message = ERR_BAD_PROMPT, "扩展设置无效，请检查输入后重试。"
        else:
            code, message = ERR_INTERNAL, "扩展操作失败，状态未确认"
        error = Error(
            code=code,
            message=message,
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self.transport.send(error)
        return error

    async def _handle_manage_engine_plugin(self, cmd):
        target_cwd = self._engine_capability_cwd(cmd)
        try:
            await manage_engine_plugin(
                cmd.engine,
                cmd.plugin_id,
                cmd.action,
                target_cwd,
                space=getattr(cmd, "space", "code"),
                claude_bin=self.cfg.claude_bin,
            )
        except Exception as exc:
            logger_call = log.warning if isinstance(exc, ValueError) else log.exception
            logger_call("engine plugin mutation failed", engine=cmd.engine,
                        action=cmd.action, plugin_id=cmd.plugin_id,
                        error_type=type(exc).__name__)
            error = await self._send_capability_mutation_error(cmd, exc)
            await self._handle_get_engine_capabilities(cmd)
            return error
        return await self._handle_get_engine_capabilities(cmd)

    async def _handle_manage_engine_skill(self, cmd):
        try:
            await manage_engine_skill(
                cmd.engine,
                cmd.action,
                self._engine_capability_cwd(cmd),
                space=getattr(cmd, "space", "code"),
                skill_id=getattr(cmd, "skill_id", None),
                name=getattr(cmd, "name", None),
                description=getattr(cmd, "description", None) or "",
                instructions=getattr(cmd, "instructions", None) or "",
                scope=getattr(cmd, "scope", "user"),
            )
        except Exception as exc:
            logger_call = log.warning if isinstance(exc, ValueError) else log.exception
            logger_call("engine skill mutation failed", engine=cmd.engine,
                        action=cmd.action, error_type=type(exc).__name__)
            error = await self._send_capability_mutation_error(cmd, exc)
            await self._handle_get_engine_capabilities(cmd)
            return error
        return await self._handle_get_engine_capabilities(cmd)

    async def _handle_manage_engine_hook(self, cmd):
        try:
            await manage_engine_hook(
                cmd.engine,
                cmd.action,
                self._engine_capability_cwd(cmd),
                space=getattr(cmd, "space", "code"),
                hook_id=getattr(cmd, "hook_id", None),
                event=getattr(cmd, "event", None),
                matcher=getattr(cmd, "matcher", None) or "",
                command=getattr(cmd, "command", None) or "",
                timeout=getattr(cmd, "timeout", None) or 60,
                scope=getattr(cmd, "scope", "user"),
            )
        except Exception as exc:
            # Never log the Hook command: it may legitimately contain tokens or
            # other local secrets. Engine/action are sufficient diagnostics.
            logger_call = log.warning if isinstance(exc, ValueError) else log.exception
            logger_call("engine hook mutation failed", engine=cmd.engine,
                        action=cmd.action, error_type=type(exc).__name__)
            error = await self._send_capability_mutation_error(cmd, exc)
            await self._handle_get_engine_capabilities(cmd)
            return error
        return await self._handle_get_engine_capabilities(cmd)

    @staticmethod
    def _claude_broker_control_error(action: str, exc: Exception) -> Error:
        """Translate broker control failures without exposing internals as UI state."""
        code = getattr(exc, "code", None)
        if code == "input_busy":
            return Error(
                code=ERR_BUSY,
                message=f"Claude TUI 输入通道正忙，{action}未生效",
            )
        if code == "control_rejected":
            return Error(
                code=ERR_INTERNAL,
                message=f"Claude TUI 拒绝了{action}，界面状态未更改",
            )
        if code == "control_unconfirmed":
            return Error(
                code=ERR_INTERNAL,
                message=f"未收到 Claude TUI 对{action}的持久确认，界面状态未更改",
            )
        if code in {"bad_control", "unsupported_control"}:
            return Error(
                code=ERR_INTERNAL,
                message=f"当前 Claude TUI 不支持{action}，界面状态未更改",
            )
        return Error(
            code=ERR_INTERNAL,
            message=f"Claude TUI {action}失败，真实状态暂未确认",
        )

    async def _runtime_control_preflight(
        self, ctx: SessionContext, *, action: str,
        request_id: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> Optional[Error]:
        """Bind a control to the session's real writer before changing state.

        A normal Claude CLI and the wrapper's Agent SDK can resume the same
        transcript at once.  Model/effort/permission controls must never mutate
        the SDK copy and announce success while the user's terminal owns another
        live TUI.  Prefer an exact broker session; otherwise use the same
        fail-closed ownership scan as Query before touching any runtime option.
        """
        if (ctx.engine == "claude" and ctx.space == "code"
                and not getattr(ctx.sdk, "is_claude_broker", False)):
            await self._adopt_claude_broker_handle(ctx)

        is_claude_broker = bool(
            getattr(ctx.sdk, "is_claude_broker", False))
        if is_claude_broker and ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message=f"Claude TUI 正在处理回合，完成或打断后再{action}",
                request_id=request_id,
                to=client_id,
            )
            await self._emit(ctx, error)
            return error

        if not ctx.session_id or is_claude_broker:
            return None
        if ctx.engine == "claude" and ctx.space != "code":
            return None
        if self._codex_shared_affinity(ctx):
            if await self._ensure_codex_daemon_generation(
                ctx, reason=f"runtime control preflight: {action}"
            ):
                return None
            error = Error(
                code=ERR_NOT_RUNNING,
                message=f"Codex 共享通道重连失败，无法{action}；请重试",
                request_id=request_id,
                to=client_id,
            )
            await self._emit(ctx, error)
            return error

        self._watch_session(ctx.session_id)
        external = (
            await self._prime_codex_ownership(ctx.session_id)
            if ctx.engine == "codex"
            else await self._prime_claude_ownership(ctx.session_id)
        )
        if not external:
            return None

        await self._sync_external_control(
            ctx, self._watch.get(ctx.session_id))
        engine_name = "Codex" if ctx.engine == "codex" else "Claude"
        guidance = (
            "请先点击『接管』，或退出 CLI 后再操作"
            if ctx.engine == "claude"
            else "请先退出终端或点击『接管』"
        )
        error = Error(
            code=ERR_BUSY,
            message=(f"该会话正由本机原生 {engine_name} CLI 控制，{action}未生效；"
                     f"{guidance}"),
            request_id=request_id,
            to=client_id,
        )
        await self._emit(ctx, error)
        return error

    async def _confirm_claude_broker_model_switch(
        self,
        ctx: SessionContext,
        target: str,
        client_id: Optional[str],
    ) -> bool:
        """Mirror Claude TUI's cached-history model confirmation to Remote."""
        current = getattr(ctx.sdk, "model", None)
        if not client_id or current == target:
            return True

        previous_ask_id = ctx.pending_model_ask_id
        if previous_ask_id:
            previous = ctx.pending_asks.get(previous_ask_id)
            if previous is not None and not previous.done():
                previous.set_exception(AskSuperseded())

        ask_id = f"ask-{uuid4().hex}"
        accept_label = f"是，切换到 {target}"
        ctx.pending_model_ask_id = ask_id
        try:
            async with ctx.ask_lock:
                # Another SetModel may supersede this request while it waits
                # behind a different question batch. Never show the stale one.
                if ctx.pending_model_ask_id != ask_id:
                    return False
                answer = await self._on_ask_locked(
                    ctx,
                    (
                        f"当前会话已为现有模型建立缓存。切换到 {target} 后，"
                        "下一次回复会重新读取完整历史，因此速度更慢并消耗更多 token。"
                        "是否继续？"
                    ),
                    [
                        {"label": accept_label,
                         "ds": "确认切换；下一次回复会重新读取完整历史"},
                        {"label": "不，返回", "ds": "保留当前模型"},
                    ],
                    header="切换模型",
                    ask_id=ask_id,
                    to=client_id,
                )
            return answer == accept_label
        except AskUnavailable:
            log.warning(
                "Claude model switch confirmation ended without approval",
                session_id=ctx.session_id,
                ask_id=ask_id,
            )
            return False
        finally:
            if ctx.pending_model_ask_id == ask_id:
                ctx.pending_model_ask_id = None

    async def _handle_set_model(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换模型")
        control_error = await self._runtime_control_preflight(
            ctx, action="切换模型")
        if control_error is not None:
            return control_error
        try:
            if getattr(ctx.sdk, "is_claude_broker", False):
                confirmed = await self._confirm_claude_broker_model_switch(
                    ctx, cmd.model, getattr(cmd, "client_id", None))
                if not confirmed:
                    current = getattr(ctx.sdk, "model", None)
                    if isinstance(current, str) and current:
                        event = Model(model=current)
                        await self._emit(ctx, event)
                        return event
                    return None
                # The user may answer after the terminal exits or ownership is
                # replaced. Re-bind the command before touching a runtime.
                control_error = await self._runtime_control_preflight(
                    ctx, action="切换模型")
                if control_error is not None:
                    return control_error
            await ctx.sdk.set_model(cmd.model)
            await self._refresh_pending_claude_work_baseline(ctx)
            await self._persist_claude_session_controls(ctx)
            applied_model = getattr(ctx.sdk, "model", None) or cmd.model
            ctx.announced_model = applied_model
            model_event = Model(model=applied_model)
            await self._emit(ctx, model_event)
            responses = [model_event]
            if ctx.engine == "codex":
                # thread/settings/updated is authoritative. app-server may adjust
                # effort when the selected model cannot use the old level; never
                # overwrite that decision with a Web-side guess or stale chip.
                applied = getattr(ctx.sdk, "effort", None)
                if applied and applied != ctx.announced_effort:
                    ctx.announced_effort = applied
                    effort_event = Effort(effort=applied)
                    await self._emit(ctx, effort_event)
                    responses.append(effort_event)
            return tuple(responses)
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            error = (
                self._claude_broker_control_error("切换模型", e)
                if getattr(ctx.sdk, "is_claude_broker", False)
                else Error(code=ERR_INTERNAL, message="模型切换未完成，请重试。")
            )
            await self._emit(ctx, error)
            return error

    async def _apply_codex_effort(self, ctx, effort: Optional[str]) -> Optional[str]:
        """Clamp `effort` to what ctx's codex model supports and apply it to the live
        handle. Returns the APPLIED level (may differ from the request); the caller
        announces it. Doesn't touch ctx.announced_effort.

        Session-scoped on purpose: nothing here writes config.toml. codex applies
        model/effort per turn and records them in the session's own rollout, so this
        session's pick can't leak into another session — or into the terminal's
        default. Mirrors codex's own thread/settings/update, which never touches
        config.toml either."""
        applied = await clamp_effort(getattr(ctx.sdk, "model", None), effort)
        if not applied:
            return applied
        # Persist through app-server's official thread setting. turn/start repeats
        # it defensively, but a restart/eviction no longer loses the selection.
        await ctx.sdk.set_effort(applied)
        return getattr(ctx.sdk, "effort", None) or applied

    async def _handle_set_effort(self, cmd):
        # cc SDK: effort is a spawn-time flag (--effort), so record it and let
        # _run_turn respawn-with-resume lazily at the next turn. A broker-owned
        # official TUI instead applies the native /effort command immediately.
        # codex: effort is a per-turn turn/start param, so the live session honors it
        # immediately — no reconnect needed.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换思考强度")
        control_error = await self._runtime_control_preflight(
            ctx, action="切换思考强度")
        if control_error is not None:
            return control_error
        if ctx.engine == "codex":
            applied = await self._apply_codex_effort(ctx, cmd.effort) or cmd.effort
            ctx.announced_effort = applied
            event = Effort(effort=applied)
            await self._emit(ctx, event)
            log.info("effort set", sid=ctx.session_id, effort=applied,
                     requested=cmd.effort, engine=ctx.engine)
            return event
        if getattr(ctx.sdk, "is_claude_broker", False):
            try:
                await ctx.sdk.set_effort(cmd.effort)
            except Exception as e:
                log.exception("set_effort failed", error=str(e))
                error = self._claude_broker_control_error(
                    "切换思考强度", e)
                await self._emit(ctx, error)
                return error
            applied = getattr(ctx.sdk, "effort", None) or cmd.effort
            ctx.sdk.applied_effort = applied
        else:
            # Agent SDK applies effort only after its next lazy reconnect.
            # Do not mark it applied here or _run_turn will skip that reconnect.
            ctx.sdk.effort = cmd.effort
            applied = cmd.effort
            await self._persist_claude_session_controls(ctx)
        ctx.announced_effort = applied
        event = Effort(effort=applied)
        await self._emit(ctx, event)
        log.info("effort set", sid=ctx.session_id, effort=applied, engine=ctx.engine)
        return event

    async def _handle_set_service_tier(self, cmd):
        # Codex Fast is a thread setting in app-server 0.144.1. Never mutate the
        # user's global config.toml and never leak one session's choice to another.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换服务档位")
        if ctx.engine != "codex":
            error = Error(
                code=ERR_PROTOCOL,
                message="Fast 服务档位仅适用于 Codex 会话",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        if cmd.service_tier == "toggle":
            on = not _codex_fast_on(
                getattr(ctx.sdk, "service_tier", None))
        else:
            on = (cmd.service_tier == "fast")
        try:
            await ctx.sdk.set_service_tier("fast" if on else None)
            applied_on = _codex_fast_on(
                getattr(ctx.sdk, "service_tier", None))
            event = Fast(on=applied_on)
            await self._emit(ctx, event)
            log.info("codex thread service tier set", sid=ctx.session_id,
                     requested=on, applied=applied_on)
            return event
        except Exception as e:
            log.exception("set_service_tier failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="服务档位切换未完成，请重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

    async def _handle_set_collaboration_mode(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换协作模式")
        if ctx.engine != "codex" or cmd.mode not in CODEX_COLLABORATION_MODES:
            error = Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持 Codex 协作模式 {cmd.mode!r}; "
                         "可选: default, plan"),
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        try:
            await ctx.sdk.set_collaboration_mode(cmd.mode)
            applied = getattr(ctx.sdk, "collaboration_mode", cmd.mode)
            ctx.announced_collaboration_mode = applied
            event = CollaborationMode(mode=applied)
            await self._emit(ctx, event)
            return event
        except Exception as e:
            log.exception("set_collaboration_mode failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="协作模式切换未完成，请重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

    async def _refresh_codex_collaboration_mode(
            self, ctx: SessionContext) -> None:
        """Adopt settings persisted by a native Codex turn's rollout tail."""
        if ctx.engine != "codex" or not ctx.session_id:
            return
        if bool(getattr(ctx.sdk, "using_daemon_proxy", False)):
            # The shared app-server is the live authority.  A settings/update is
            # visible to every proxy immediately, while Codex does not append the
            # corresponding turn_context until the next turn starts.  Reading the
            # rollout here would therefore put an old model/effort back into the
            # handle (and Web) just after a successful Remote or TUI switch.
            model = getattr(ctx.sdk, "model", None)
            if (isinstance(model, str) and model
                    and ctx.announced_model != model):
                ctx.announced_model = model
                await self._emit(ctx, Model(model=model))
            effort = getattr(ctx.sdk, "effort", None)
            if (isinstance(effort, str) and effort
                    and ctx.announced_effort != effort):
                ctx.announced_effort = effort
                await self._emit(ctx, Effort(effort=effort))
            approval = getattr(ctx.sdk, "approval", None)
            if (approval in CODEX_PERMISSION_MODES
                    and ctx.announced_perm != approval):
                ctx.announced_perm = approval
                await self._emit(ctx, Perm(mode=approval))
            permission_profile = _session_permission_profile(ctx)
            if ctx.announced_permission_profile != permission_profile:
                ctx.announced_permission_profile = permission_profile
                await self._emit(ctx, PermissionProfile(
                    profile=permission_profile))
            web_search = _session_web_search(ctx)
            if (web_search
                    and ctx.announced_web_search != web_search):
                ctx.announced_web_search = web_search
                await self._emit(ctx, WebSearch(mode=web_search))
            mode = getattr(ctx.sdk, "collaboration_mode", None)
            if (mode in CODEX_COLLABORATION_MODES
                    and ctx.announced_collaboration_mode != mode):
                ctx.announced_collaboration_mode = mode
                await self._emit(ctx, CollaborationMode(mode=mode))
            return
        settings = await asyncio.to_thread(
            codex_session_settings, ctx.session_id,
            self.cfg.history_source_max_bytes)
        model = settings.get("model")
        if isinstance(model, str) and model and ctx.sdk.model != model:
            ctx.sdk.model = model
            ctx.announced_model = model
            await self._emit(ctx, Model(model=model))
        effort = settings.get("effort")
        if isinstance(effort, str) and effort and ctx.sdk.effort != effort:
            ctx.sdk.effort = effort
            ctx.sdk.applied_effort = effort
            ctx.announced_effort = effort
            await self._emit(ctx, Effort(effort=effort))
        approval = settings.get("approval_policy")
        if (ctx.space != "work" and approval in CODEX_PERMISSION_MODES
                and ctx.sdk.approval != approval):
            ctx.sdk.approval = approval
            ctx.announced_perm = approval
            await self._emit(ctx, Perm(mode=approval))
        if ctx.space != "work" and "permission_profile" in settings:
            permission_profile = settings.get("permission_profile")
            if permission_profile is None or (
                    isinstance(permission_profile, str)
                    and permission_profile):
                if ctx.sdk.permission_profile != permission_profile:
                    ctx.sdk.permission_profile = permission_profile
                    ctx.announced_permission_profile = permission_profile
                    await self._emit(ctx, PermissionProfile(
                        profile=permission_profile))
        if "service_tier" in settings:
            tier = settings.get("service_tier")
            if tier is None or isinstance(tier, str):
                if ctx.sdk.service_tier != tier:
                    ctx.sdk.service_tier = tier
                    await self._emit(ctx, Fast(on=_codex_fast_on(tier)))
        mode = settings.get("collaboration_mode")
        if (mode in CODEX_COLLABORATION_MODES
                and getattr(ctx.sdk, "collaboration_mode", "default") != mode):
            ctx.sdk.collaboration_mode = mode
            ctx.announced_collaboration_mode = mode
            await self._emit(ctx, CollaborationMode(mode=mode))

    async def _send_btw_error(self, cmd, code: str, message: str) -> Error:
        """Send a one-request /btw rejection without polluting another session.

        The Error is returned so reliable-command dedupe can cache and replay the
        same terminal response if its first copy or ACK was lost.
        """
        client_id = getattr(cmd, "client_id", None)
        error = Error(
            code=code,
            message=message,
            request_id=cmd.request_id,
            sid=getattr(cmd, "sid", None),
            to=client_id,
        )
        if client_id:
            await self.transport.send(error)
        else:
            # ``to=None`` is a relay broadcast. An ownerless request must never
            # turn even a terminal /btw control response into a shared frame.
            log.warning("dropping unroutable btw error", code=code)
        return error

    async def _handle_open_btw(self, cmd):
        if not getattr(cmd, "client_id", None):
            return await self._send_btw_error(
                cmd, ERR_AUTH, "btw requires a bound client")
        parent = self._ctx_for(getattr(cmd, "sid", None))
        if parent is None:
            return await self._send_btw_error(
                cmd, ERR_NOT_RUNNING, "没有可 fork 的会话")
        if parent.btw:  # never fork a fork — fork its parent instead
            parent = self.sessions.get(parent.parent_sid) or next(
                (c for c in self.sessions.values() if c.session_id == parent.parent_sid), parent)
        try:
            btw = await self._spawn_btw(
                parent, owner_client_id=getattr(cmd, "client_id", None))
        except _BtwSpawnFailure as exc:
            return await self._send_btw_error(cmd, exc.code, exc.message)
        ev = BtwOpened(
            request_id=cmd.request_id,
            btw_sid=btw.key,
            parent_sid=parent.session_id or parent.key,
            engine=btw.engine,
        )
        ev.sid = btw.key
        cid = getattr(cmd, "client_id", None)
        if cid:
            ev.to = cid
        await self.transport.send(ev)
        # a fresh Snapshot so the requester builds a runtime for the fork's key.
        snap = Snapshot(
            cc_session_id=None, state="idle", tail_text="", cwd=btw.cwd,
            generation=self.instance_id,
            control=self._session_control(btw))
        snap.sid = btw.key
        if cid:
            snap.to = cid
        await self.transport.send(snap)
        # Model and effort are mutable fork settings. Publish their current
        # authoritative values through the normal owner-only sequenced ring so
        # reconnect replay can recover them without freezing an initial value
        # into OpenBtw's static command-response cache.
        model = _session_model(btw)
        if model:
            btw.announced_model = model
            await self._emit(btw, Model(model=model))
        effort = _session_effort(btw)
        if effort:
            btw.announced_effort = effort
            await self._emit(btw, Effort(effort=effort))
        permission_mode = _session_permission_mode(btw)
        btw.announced_perm = permission_mode
        permission = Perm(mode=permission_mode, sid=btw.key, to=cid)
        await self.transport.send(permission)
        responses = [ev, snap, permission]
        if btw.engine == "codex":
            permission_profile = _session_permission_profile(btw)
            btw.announced_permission_profile = permission_profile
            profile_event = PermissionProfile(
                profile=permission_profile, sid=btw.key, to=cid)
            await self.transport.send(profile_event)
            responses.append(profile_event)
            web_search = _session_web_search(btw)
            if web_search:
                btw.announced_web_search = web_search
                search_event = WebSearch(
                    mode=web_search, sid=btw.key, to=cid)
                await self.transport.send(search_event)
                responses.append(search_event)
            # BtwOpened + Snapshot create the owner-only browser runtime before
            # pending app-server notices are released into that route.
            await btw.sdk.activate_runtime_events()
        log.info("btw opened", btw_sid=btw.key, parent=parent.session_id, client_id=cid)
        # All three one-shot frames are required to reconstruct the fork after a
        # lost response. Reliable-command retries replay them without re-forking.
        return tuple(responses)

    async def _handle_close_btw(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self.sessions.get(sid) if sid else None
        if ctx is None or not ctx.btw:
            return
        await self._discard_query_queue(ctx)
        self.sessions.pop(ctx.key, None)
        disconnected = False
        try:
            tasks = {
                task for task in (ctx.turn_task, ctx.codex_spontaneous_task)
                if task is not None and not task.done()
            }
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await ctx.sdk.disconnect()
            disconnected = True
        except Exception as e:
            log.warning("btw close disconnect failed", error=str(e))
        finally:
            await self._cleanup_codex_steer_attachments(ctx)
        # Codex forks are ephemeral (no rollout). Claude fork_session persists a
        # transcript under btw_real_id; keep its tombstone on deletion failure so
        # it stays hidden and cannot be cold-resumed.
        if ctx.engine != "codex" and ctx.btw_real_id:
            await self._delete_private_btw(
                ctx.btw_real_id, ctx.cwd, forget=disconnected)
        log.info("btw closed", btw_sid=sid)

    async def _handle_set_perm(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "切换权限模式")
        control_error = await self._runtime_control_preflight(
            ctx, action="切换权限模式")
        if control_error is not None:
            return control_error
        if ctx.space == "work" and ctx.engine == "codex" and cmd.mode != "never":
            error = Error(
                code=ERR_AUTH,
                message="Codex Work 权限由隔离工作区固定管理，不能升级为交互授权",
            )
            await self._emit(ctx, error)
            return error
        allowed = (CODEX_PERMISSION_MODES if ctx.engine == "codex"
                   else CLAUDE_PERMISSION_MODES)
        if cmd.mode not in allowed:
            log.warning("invalid permission mode", sid=ctx.session_id,
                        engine=ctx.engine, mode=cmd.mode)
            error = Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持权限模式 {cmd.mode!r}; "
                         f"可选: {', '.join(sorted(allowed))}"),
            )
            await self._emit(ctx, error)
            return error
        try:
            await ctx.sdk.set_permission_mode(cmd.mode)
            applied = (getattr(ctx.sdk, "approval", None)
                       or getattr(ctx.sdk, "permission_mode", None)
                       or cmd.mode)
            await self._persist_claude_session_controls(ctx)
            await self._persist_codex_session_controls(ctx)
            ctx.announced_perm = applied
            event = Perm(mode=applied)
            await self._emit(ctx, event)
            return event
        except Exception as e:
            log.exception("set_permission_mode failed", error=str(e))
            error = (
                self._claude_broker_control_error("切换权限模式", e)
                if getattr(ctx.sdk, "is_claude_broker", False)
                else Error(code=ERR_INTERNAL, message="权限模式切换未完成，请重试。")
            )
            await self._emit(ctx, error)
            return error

    async def _handle_get_permission_profiles(self, cmd):
        client_id = getattr(cmd, "client_id", None)
        requested_cwd = getattr(cmd, "cwd", None)
        ctx = (
            None if requested_cwd is not None
            else self._ctx_for(getattr(cmd, "sid", None))
        )
        if requested_cwd is None and ctx is None:
            return await self._missing_session_error(cmd, "读取执行环境")
        if ctx is not None and ctx.engine != "codex":
            error = Error(
                code=ERR_INTERNAL,
                message="只有 Codex 会话支持执行环境配置。",
                request_id=getattr(cmd, "cmd_id", None),
                sid=ctx.key,
                to=client_id,
            )
            await self.transport.send(error)
            return error
        try:
            if requested_cwd is not None:
                target_cwd = os.path.realpath(
                    os.path.expanduser(requested_cwd))
                if not os.path.isdir(target_cwd):
                    raise ValueError("permission profile cwd does not exist")
                profiles = await codex_permission_profiles(target_cwd)
            else:
                assert ctx is not None
                profiles = await ctx.sdk.list_permission_profiles()
            event = PermissionProfiles(
                profiles=profiles,
                request_id=getattr(cmd, "cmd_id", None),
                cwd=requested_cwd,
                sid=ctx.key if ctx is not None else None,
                to=client_id,
            )
            await self.transport.send(event)
            return event
        except Exception as exc:
            log.exception(
                "permission profile catalog failed",
                sid=ctx.session_id if ctx is not None else None,
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="执行环境列表读取失败，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                sid=ctx.key if ctx is not None else None,
                to=client_id,
            )
            await self.transport.send(error)
            return error

    async def _handle_set_permission_profile(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(
                cmd, "切换执行环境")
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message="Codex 正在处理回合，完成或中断后再切换执行环境。",
            )
            await self._emit(ctx, error)
            return error
        control_error = await self._runtime_control_preflight(
            ctx, action="切换执行环境")
        if control_error is not None:
            return control_error
        if ctx.engine != "codex":
            error = Error(
                code=ERR_INTERNAL,
                message="只有 Codex 会话支持执行环境配置。",
            )
            await self._emit(ctx, error)
            return error
        if ctx.space == "work":
            error = Error(
                code=ERR_AUTH,
                message="Codex Work 的执行环境由隔离工作区固定管理，不能切换。",
            )
            await self._emit(ctx, error)
            return error
        try:
            await ctx.sdk.set_permission_profile(cmd.profile)
            applied = _session_permission_profile(ctx)
            if not applied:
                raise RuntimeError(
                    "Codex did not report an active permission profile")
            ctx.announced_permission_profile = applied
            await self._persist_codex_session_controls(ctx)
            event = PermissionProfile(profile=applied)
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            log.exception(
                "set_permission_profile failed",
                sid=ctx.session_id,
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="执行环境切换未完成，请重试。",
            )
            await self._emit(ctx, error)
            return error

    async def _republish_codex_execution_controls(
        self, ctx: SessionContext,
    ) -> None:
        """Reassert the controls proven by a recovered Codex connection."""
        permission_mode = _session_permission_mode(ctx)
        if permission_mode in CODEX_PERMISSION_MODES:
            ctx.announced_perm = permission_mode
            await self._emit(ctx, Perm(mode=permission_mode))
        permission_profile = _session_permission_profile(ctx)
        ctx.announced_permission_profile = permission_profile
        await self._emit(ctx, PermissionProfile(
            profile=permission_profile))
        web_search = _session_web_search(ctx)
        if web_search in CODEX_WEB_SEARCH_MODES:
            ctx.announced_web_search = web_search
            await self._emit(ctx, WebSearch(mode=web_search))

    async def _handle_set_web_search(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(
                cmd, "切换网页搜索")
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message="Codex 正在处理回合，完成或中断后再切换网页搜索。",
            )
            await self._emit(ctx, error)
            return error
        control_error = await self._runtime_control_preflight(
            ctx, action="切换网页搜索")
        if control_error is not None:
            return control_error
        if ctx.engine != "codex" or ctx.space != "code":
            error = Error(
                code=ERR_AUTH,
                message="只有 Codex Code 会话支持切换网页搜索。",
            )
            await self._emit(ctx, error)
            return error
        try:
            await ctx.sdk.set_web_search(cmd.mode)
            await self._stamp_codex_daemon_epoch(ctx)
            await self._persist_codex_session_controls(ctx)
            applied = _session_web_search(ctx)
            if applied not in CODEX_WEB_SEARCH_MODES:
                raise RuntimeError(
                    "Codex did not report an active web search mode")
            ctx.announced_web_search = applied
            event = WebSearch(mode=applied)
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            log.exception(
                "set_web_search failed",
                sid=ctx.session_id,
                error_type=type(exc).__name__,
            )
            if getattr(ctx.sdk, "proc", None) is not None:
                try:
                    # A failed replacement can briefly publish its defaults.
                    # The rollback connection is authoritative; repeat all
                    # coupled controls so every browser converges immediately.
                    await self._persist_codex_session_controls(ctx)
                    await self._republish_codex_execution_controls(ctx)
                except Exception as publish_exc:
                    log.warning(
                        "restored Codex execution controls could not be "
                        "republished",
                        sid=ctx.session_id,
                        error_type=type(publish_exc).__name__,
                    )
            error = Error(
                code=ERR_INTERNAL,
                message="网页搜索模式切换未完成，请重试。",
            )
            await self._emit(ctx, error)
            return error

    async def _on_set_mode(self, ctx: SessionContext, mode: str) -> None:
        """Agent-facing set_mode MCP tool (called within a turn). Same effect as
        SetPerm: sdk.set_permission_mode + Perm broadcast on this ctx."""
        if ctx.engine != "claude" or mode not in CLAUDE_PERMISSION_MODES:
            raise ValueError(f"unsupported {ctx.engine} permission mode: {mode}")
        try:
            await ctx.sdk.set_permission_mode(mode)
            await self._persist_claude_session_controls(ctx)
            ctx.announced_perm = mode
            await self._emit(ctx, Perm(mode=mode))
            log.info("agent set permission mode", sid=ctx.session_id, mode=mode)
        except Exception as e:
            log.exception("agent set_mode failed", error=str(e))
            raise

    async def _persist_fresh_work_context_baseline(
        self, ctx: SessionContext, usage: dict,
    ) -> int | None:
        """Persist one baseline only for a newly-created Work conversation."""
        if (ctx.space != "work" or not ctx.work_id
                or not ctx.work_context_baseline_pending):
            return ctx.work_context_baseline_tokens
        candidate = ctx.work_context_baseline_tokens
        if candidate is None:
            raw_key = "used_tokens" if ctx.engine == "codex" else "totalTokens"
            raw_total = usage.get(raw_key)
            if (not isinstance(raw_total, int) or isinstance(raw_total, bool)
                    or raw_total <= 0):
                return None
            _, _, _, candidate = work_context_metrics(ctx.engine, usage, None)
        if candidate <= 0:
            return None
        try:
            persisted = await asyncio.to_thread(
                self._work.for_engine(ctx.engine).set_context_baseline,
                ctx.work_id, candidate,
            )
        except Exception:
            # Context accounting is display metadata. Keep the authoritative raw
            # total and retry later instead of failing an otherwise valid turn.
            log.exception(
                "fresh Work context baseline persistence failed",
                engine=ctx.engine,
                work_id=ctx.work_id,
            )
            return None
        ctx.work_context_baseline_tokens = persisted
        ctx.work_context_baseline_pending = False
        return persisted

    async def _refresh_pending_claude_work_baseline(
        self, ctx: SessionContext,
    ) -> None:
        """Refresh a still-uncommitted Claude baseline after a model change."""
        if (ctx.engine != "claude" or ctx.space != "work"
                or not ctx.work_context_baseline_pending):
            return
        try:
            usage = await ctx.sdk.get_context_usage()
        except Exception:
            log.warning(
                "fresh Claude Work baseline refresh unavailable",
                work_id=ctx.work_id,
            )
            return
        refreshed = usage.get("totalTokens")
        if (isinstance(refreshed, int) and not isinstance(refreshed, bool)
                and refreshed >= 0):
            ctx.sdk.work_context_baseline_tokens = refreshed
            ctx.work_context_baseline_tokens = refreshed

    async def _handle_get_context(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "读取上下文")
        try:
            usage = await ctx.sdk.get_context_usage()
            work_fields: dict[str, int | float] = {}
            if ctx.space == "work" and ctx.work_id:
                raw_key = "used_tokens" if ctx.engine == "codex" else "totalTokens"
                raw_total = usage.get(raw_key)
                # Codex exposes no usage before the first turn. Establish its
                # baseline from the first authoritative tokenUsage notification;
                # Claude's pre-turn baseline is persisted when its native id is
                # captured. Never manufacture a baseline for migrated sessions.
                if ctx.engine == "codex":
                    await self._persist_fresh_work_context_baseline(ctx, usage)
                baseline = ctx.work_context_baseline_tokens
                if (isinstance(baseline, int) and not isinstance(baseline, bool)
                        and isinstance(raw_total, int)
                        and not isinstance(raw_total, bool)
                        and raw_total >= baseline):
                    session_tokens, fixed_tokens, session_percentage, _ = (
                        work_context_metrics(ctx.engine, usage, baseline)
                    )
                    work_fields = {
                        "session_tokens": session_tokens,
                        "fixed_tokens": fixed_tokens,
                        "session_percentage": session_percentage,
                    }
            if ctx.engine == "codex":
                raw_used = usage.get("used_tokens")
                raw_win = usage.get("context_window")
                available = (
                    isinstance(raw_used, int)
                    and not isinstance(raw_used, bool)
                    and raw_used >= 0
                )
                used = raw_used if available else 0
                win = (
                    raw_win
                    if isinstance(raw_win, int)
                    and not isinstance(raw_win, bool)
                    and raw_win >= 0
                    else 0
                )
                event = ContextReport(
                    total_tokens=used, max_tokens=win,
                    percentage=(used / win * 100.0) if win else 0.0,
                    available=False if not available else None,
                    model=ctx.sdk.model, is_auto_compact_enabled=None,
                    categories=[], **work_fields)
                await self._emit(ctx, event)
                return event
            event = ContextReport(
                total_tokens=usage.get("totalTokens", 0),
                max_tokens=usage.get("maxTokens", 0),
                percentage=usage.get("percentage", 0.0),
                model=usage.get("model"),
                is_auto_compact_enabled=usage.get("isAutoCompactEnabled"),
                categories=usage.get("categories", []) or [],
                **work_fields,
            )
            await self._emit(ctx, event)
            return event
        except Exception as e:
            log.exception("get_context_usage failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="上下文状态暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

    async def _handle_get_status(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "读取状态")
        if ctx.engine != "codex":
            error = Error(
                code=ERR_INTERNAL,
                message="/status 需要 Codex app-server 会话",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error
        async with ctx.codex_status_lock:
            # Status belongs to the account backing the current daemon
            # generation. During a turn the old generation is authoritative;
            # once idle, cross an intentional account-switch restart before
            # reading limits. Serializing status reads ensures the new account's
            # report is always emitted after any already-started old read.
            if (
                ctx.state == "idle"
                and self._codex_shared_affinity(ctx)
                and not await self._ensure_codex_daemon_generation(
                    ctx, reason="status preflight")
            ):
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message="Codex 共享通道重连失败，账户额度暂不可用；请重试",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            try:
                event = StatusReport(
                    **await ctx.sdk.get_status(),
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, event)
                return event
            except Exception:
                # get_status already degrades individual RPC failures. Reaching
                # this path means the composed report itself could not be
                # produced; do not copy a raw provider/app-server exception
                # onto the wire.
                log.exception("get_status failed")
                error = Error(
                    code=ERR_INTERNAL,
                    message="Codex status unavailable",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error

    async def _goal_ctx(self, cmd):
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        return ctx

    async def _on_codex_goal(self, ctx: SessionContext,
                             goal: Optional[dict]) -> None:
        """Broadcast an authoritative app-server goal notification.

        ``CodexHandle`` has already copied the strict public allow-list.  _emit
        broadcasts normal-session updates to every signed-in client and
        automatically routes private /btw updates to their owning client.
        """
        if goal is None:
            ctx.codex_goal_mutation = None
        await self._emit(ctx, GoalState(goal=goal))

    async def _on_codex_runtime_event(
        self, ctx: SessionContext, event: Notice | RateLimitUpdate,
    ) -> None:
        """Route sanitized app-server notices only after ctx has an identity."""
        if not isinstance(event, (Notice, RateLimitUpdate)):
            return
        if not (ctx.session_id or ctx.key):
            # An absent sid is a broadcast at the relay.  Initialization events
            # must stay inside CodexHandle's pending queue until _spawn assigns a
            # temp/real key; fail closed if a caller activates out of order.
            log.warning(
                "codex runtime event has no session route; dropped",
                event_type=event.type,
            )
            return
        await self._emit(ctx, event)

    async def _on_claude_background_message(
        self, ctx: SessionContext, message, turn_id: str | None,
    ) -> None:
        """Forward Claude task/hook lifecycle frames emitted after Result.

        SdkHandle is the sole consumer of the SDK Query stream and calls this
        only while no managed turn owns a response.  A fresh translator is
        sufficient because stable item -> origin-turn/title/kind maps live on
        the SessionContext and are shared across all translator instances.
        """
        thread_id = ctx.session_id or ctx.key
        if not thread_id:
            return
        goal_changed, goal = ctx.sdk.observe_goal_message(
            message, thread_id)
        if goal_changed and ctx.goal_visible:
            await self._emit(ctx, GoalState(goal=goal))
        if not isinstance(message, (
            HookEventMessage, TaskStartedMessage, TaskProgressMessage,
            TaskUpdatedMessage, TaskNotificationMessage,
        )):
            return
        translator = StreamTranslator(
            self.cfg.tool_result_max,
            turn_id=turn_id,
            item_turns=ctx.claude_item_turns,
            item_titles=ctx.claude_item_titles,
            item_meta=ctx.claude_item_meta,
        )
        for event in translator.feed(message):
            await self._emit(ctx, event)

    async def _on_codex_turn_lifecycle(
        self, ctx: SessionContext, phase: str, turn_id: str,
    ) -> None:
        """Claim and schedule a Codex turn that started without ``query()``.

        This callback runs on the sole app-server stdout reader.  Its normal path
        intentionally performs no relay I/O: state is claimed synchronously and a
        separate task drains the handle's bounded raw-notification bridge.
        """
        if phase == "started":
            if (ctx.codex_spontaneous_turn_id == turn_id
                    and ctx.codex_spontaneous_task is not None):
                return
            if (ctx.codex_spontaneous_turn_id is not None
                    and ctx.codex_spontaneous_turn_id != turn_id):
                log.warning(
                    "overlapping codex spontaneous turn ignored",
                    active_turn_id=ctx.codex_spontaneous_turn_id,
                    incoming_turn_id=turn_id,
                )
                return
            recovered_msg_id = (
                ctx.codex_recovered_msg_id
                if ctx.codex_recovered_turn_id == turn_id
                else None
            )
            recovered_automatic = (
                ctx.codex_recovered_automatic
                if ctx.codex_recovered_turn_id == turn_id
                else None
            )
            ctx.codex_spontaneous_turn_id = turn_id
            self._claim_codex_turn(
                ctx,
                turn_id,
                recovered_msg_id or turn_id,
                automatic=(
                    recovered_automatic
                    if recovered_automatic is not None
                    else True
                ),
            )
            mutation = ctx.codex_goal_mutation
            if (
                mutation is not None
                and mutation.turn_id is None
                and ctx.state == "running"
            ):
                mutation.turn_id = turn_id
            if ctx.turn_task is not None and not ctx.codex_account_handoff:
                # A user send claimed the session but has not reached turn/start
                # yet (otherwise CodexHandle.turn_active would already be true).
                # Abort that launch rather than write concurrently with the
                # automatic turn that won the race.
                ctx.interrupt_event.set()
            announce_running = ctx.state == "idle"
            if announce_running:
                ctx.interrupt_event.clear()
                ctx.interrupt_deadline = None
                # Claim before yielding so an incoming query cannot slip between
                # turn/started and the bridge consumer's first scheduled step.
                ctx.state = "running"
            task = asyncio.create_task(self._run_codex_spontaneous_turn(
                ctx,
                turn_id,
                announce_running=announce_running,
                recovered_msg_id=recovered_msg_id,
            ))
            ctx.codex_spontaneous_task = task
            ctx.codex_recovered_turn_id = None
            ctx.codex_recovered_msg_id = None
            ctx.codex_recovered_automatic = None
            return
        if phase != "completed" or ctx.codex_spontaneous_turn_id != turn_id:
            return
        # The raw consumer receives the same authoritative turn/completed frame
        # and owns final emission/unlock. This fallback covers a direct lifecycle
        # callback or an unexpectedly failed consumer without double-emitting.
        task = ctx.codex_spontaneous_task
        if task is not None and not task.done():
            return
        await self._finish_codex_spontaneous_turn(ctx, turn_id)

    async def _finish_codex_spontaneous_turn(
        self, ctx: SessionContext, turn_id: str,
    ) -> None:
        if ctx.codex_spontaneous_turn_id != turn_id:
            return
        await self._record_codex_unavailable_turn(
            ctx,
            turn_id,
            reason="automatic turn began before Remote could capture a pre-image",
        )
        mutation = ctx.codex_goal_mutation
        if mutation is not None and mutation.turn_id == turn_id:
            ctx.codex_goal_mutation = None
        ctx.codex_spontaneous_turn_id = None
        self._release_codex_turn(ctx, turn_id)
        current = asyncio.current_task()
        if (ctx.codex_spontaneous_task is current
                or (ctx.codex_spontaneous_task is not None
                    and ctx.codex_spontaneous_task.done())):
            ctx.codex_spontaneous_task = None
        ctx.active_msg_id = None
        ctx.interrupt_deadline = None
        ctx.interrupt_event.clear()
        await self._cleanup_codex_steer_attachments(ctx)
        # If this continuation began while the previous managed consumer was
        # still unwinding, that task performs the final unlock after it releases
        # translator/queue ownership. Unlocking here would admit a second
        # _run_turn against the same SessionContext.
        if ctx.turn_task is None and ctx.state != "idle":
            await self._set_state(ctx, "idle")

    async def _run_codex_spontaneous_turn(
        self,
        ctx: SessionContext,
        turn_id: str,
        *,
        announce_running: bool,
        recovered_msg_id: Optional[str] = None,
        pending_switch: Optional[CodexDaemonRestartState] = None,
    ) -> None:
        """Translate one goal/automatic turn from the handle's bounded bridge."""
        translator = CodexStreamTranslator(self.cfg.tool_result_max)
        current_turn_id = turn_id
        logical_msg_id = recovered_msg_id or turn_id
        stream = ctx.sdk.receive_spontaneous_response(turn_id).__aiter__()
        restart_watch_task: Optional[asyncio.Task] = None
        overflowed = False
        terminal_seen = False
        stream_closed = False
        repair_history = False

        def start_restart_watch() -> None:
            nonlocal restart_watch_task
            if (
                self._codex_shared_affinity(ctx)
                and ctx.codex_daemon_epoch
            ):
                restart_watch_task = asyncio.create_task(
                    self._wait_for_codex_account_switch(
                        starting_epoch=ctx.codex_daemon_epoch,
                    )
                )

        async def cancel_restart_watch() -> None:
            nonlocal restart_watch_task
            if restart_watch_task is not None and not restart_watch_task.done():
                restart_watch_task.cancel()
                await asyncio.gather(
                    restart_watch_task, return_exceptions=True)
            restart_watch_task = None

        async def next_stream_item():
            """Race the live stream against an intentional daemon generation."""
            next_task = asyncio.create_task(anext(stream))
            try:
                if restart_watch_task is None:
                    return ("message", await next_task)
                done, _pending = await asyncio.wait(
                    {next_task, restart_watch_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # The hook marker is authoritative even when the old daemon
                # closes or emits a terminal in the same scheduler tick.
                if restart_watch_task in done:
                    return ("codex_account_switch",
                            restart_watch_task.result())
                return ("message", next_task.result())
            finally:
                if not next_task.done():
                    next_task.cancel()
                    await asyncio.gather(next_task, return_exceptions=True)

        async def handoff_account_switch(
            switch_state: CodexDaemonRestartState,
        ) -> str:
            """Move an automatic/goal turn to the replacement daemon."""
            nonlocal translator, current_turn_id, stream
            nonlocal overflowed, terminal_seen, stream_closed

            ctx.codex_account_handoff = True
            await self._emit(ctx, StateEvent(
                state="running",
                phase="waiting",
                detail="Codex 账号已切换，正在把当前任务转移到新账号…",
                msg_id=logical_msg_id,
            ))

            try:
                await ctx.sdk.interrupt()
            except Exception as exc:
                log.warning(
                    "old Codex automatic turn could not be interrupted "
                    "during account handoff",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )

            # Close any partial assistant/tool blocks from the old native turn,
            # but keep the logical browser turn busy: its failure boundary is an
            # implementation detail of the account switch.
            synthetic_old_terminal = {
                "method": "turn/completed",
                "params": {"turn": {
                    "id": current_turn_id,
                    "status": "interrupted",
                }},
            }
            for event in translator.feed(synthetic_old_terminal):
                if isinstance(event, (Error, TurnEnd)):
                    continue
                if isinstance(event, StateEvent) and event.detail:
                    event.state = "running"
                    event.msg_id = logical_msg_id
                await self._emit(ctx, event)

            await cancel_restart_watch()

            # Release the old native id before reconnect. thread/resume or
            # goal/set may synchronously announce a replacement automatic turn;
            # keeping the old id here would make the lifecycle callback reject it
            # as an overlapping writer.
            current_task = asyncio.current_task()
            if ctx.codex_spontaneous_turn_id == current_turn_id:
                ctx.codex_spontaneous_turn_id = None
            if ctx.codex_spontaneous_task is current_task:
                ctx.codex_spontaneous_task = None

            async def interrupted_handoff_result() -> Optional[str]:
                if (
                    not ctx.interrupt_event.is_set()
                    and ctx.state != "interrupting"
                ):
                    return None
                # A replacement automatic turn may already own the native
                # interrupt and its terminal drain. Let that consumer finish it
                # instead of emitting a duplicate terminal for the old id.
                if ctx.codex_spontaneous_turn_id is not None:
                    ctx.codex_account_handoff = False
                    return "spontaneous"
                await self._emit(ctx, TurnEnd(result=TurnResult(
                    subtype="error_during_execution",
                    duration_ms=0,
                    is_error=True,
                ), turn_id=current_turn_id))
                # Reclaim only for cleanup: finally -> _finish clears the
                # interrupt latch, active message, and running state together.
                ctx.codex_spontaneous_turn_id = current_turn_id
                ctx.codex_spontaneous_task = current_task
                ctx.codex_account_handoff = False
                return "interrupted"

            ready_state = await self._codex_restart_state(
                wait=True,
                interrupt_event=ctx.interrupt_event,
            )
            interrupted = await interrupted_handoff_result()
            if interrupted is not None:
                return interrupted
            if ready_state is None or ready_state.phase != "ready":
                phase = ready_state.phase if ready_state is not None else "missing"
                raise RuntimeError(
                    "Codex account-switch daemon restart did not become "
                    f"ready: {phase}"
                )
            if not await self._ensure_codex_daemon_generation(
                ctx, reason="continue automatic turn after account switch"
            ):
                raise RuntimeError(
                    "Codex account-switch daemon generation reconnect failed")

            interrupted = await interrupted_handoff_result()
            if interrupted is not None:
                return interrupted

            try:
                goal = await ctx.sdk.get_goal()
            except Exception as exc:
                goal = None
                log.warning(
                    "Codex goal state unavailable during automatic account "
                    "handoff",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
            resumable_goal = bool(
                isinstance(goal, dict)
                and goal.get("status") in {"active", "usageLimited"}
            )
            if resumable_goal:
                resumed = await self._resume_codex_goal_after_account_switch(
                    ctx, goal)
                if resumed:
                    ctx.codex_account_handoff = False
                    await self._emit(ctx, StateEvent(
                        state="running",
                        phase=None,
                        detail=None,
                        msg_id=logical_msg_id,
                    ))
                    log.info(
                        "Codex automatic turn transferred after account switch",
                        session_id=ctx.session_id,
                        requested_epoch=switch_state.epoch,
                        new_epoch=ctx.codex_daemon_epoch,
                        turn_id=ctx.codex_spontaneous_turn_id,
                    )
                    return "spontaneous"

            interrupted = await interrupted_handoff_result()
            if interrupted is not None:
                return interrupted
            if resumable_goal:
                raise RuntimeError(
                    "Codex Goal stopped before its account-switch "
                    "continuation started"
                )

            # An ordinary conversation has no Goal runtime. Submit one internal
            # continuation turn; history parsing recognizes the exact private
            # marker and never projects it as a user/rollback boundary.
            translator = CodexStreamTranslator(self.cfg.tool_result_max)
            try:
                new_turn_id = await ctx.sdk.query(
                    CODEX_ACCOUNT_SWITCH_CONTINUATION, images=[])
            except Exception:
                if ctx.codex_spontaneous_turn_id is not None:
                    ctx.codex_account_handoff = False
                    return "spontaneous"
                raise
            if not new_turn_id:
                raise RuntimeError(
                    "Codex continuation did not return a native turn id")
            current_turn_id = new_turn_id
            self._claim_codex_turn(
                ctx, current_turn_id, logical_msg_id)
            ctx.codex_spontaneous_turn_id = current_turn_id
            ctx.codex_spontaneous_task = current_task
            ctx.active_msg_id = logical_msg_id
            stream = ctx.sdk.receive_response().__aiter__()
            overflowed = False
            terminal_seen = False
            stream_closed = False
            if logical_msg_id:
                await self._emit(ctx, TurnBinding(
                    msg_id=logical_msg_id,
                    turn_id=current_turn_id,
                ))
            start_restart_watch()
            ctx.codex_account_handoff = False
            await self._emit(ctx, StateEvent(
                state="running",
                phase=None,
                detail=None,
                msg_id=logical_msg_id,
            ))
            log.info(
                "continued Codex automatic turn after account switch",
                session_id=ctx.session_id,
                requested_epoch=switch_state.epoch,
                new_epoch=ctx.codex_daemon_epoch,
                turn_id=current_turn_id,
            )
            return "continued"

        try:
            if announce_running:
                await self._emit(ctx, StateEvent(state="running"))
                log.info("state transition", sid=ctx.session_id, state="running")

            # A continuation can win just as the preceding managed consumer is
            # unwinding. Preserve wire order: its TurnEnd must land before this
            # new empty-prompt turn begins.
            managed_task = ctx.turn_task
            if managed_task is not None and managed_task is not asyncio.current_task():
                await asyncio.shield(managed_task)
            if ctx.codex_spontaneous_turn_id != turn_id:
                return

            ctx.active_msg_id = logical_msg_id
            # turn/started is delivered only after app-server has already begun
            # executing the automatic continuation. A filesystem pre-image taken
            # here could be a half-turn snapshot, so preserve count alignment with
            # an explicit unavailable slot instead of claiming code rollback.
            if recovered_msg_id is None:
                await self._record_codex_unavailable_turn(
                    ctx,
                    turn_id,
                    reason=(
                        "automatic turn has no safe pre-tool checkpoint boundary"
                    ),
                )

            # Automatic continuations have no user prompt. A real empty anchor
            # gives their assistant/process events a stable turn owner without
            # rendering a fabricated user bubble.
            if recovered_msg_id is None:
                await self._emit(ctx, UserMsg(msg_id=turn_id, prompt=""))

            if pending_switch is not None:
                handoff = await handoff_account_switch(pending_switch)
                if handoff in {"spontaneous", "interrupted"}:
                    return
            else:
                start_restart_watch()
            while True:
                try:
                    kind, value = await next_stream_item()
                except StopAsyncIteration:
                    # A graceful daemon close may win the same poll interval as
                    # the marker. Re-read once before treating EOF as failure.
                    switch_state = read_restart_state(
                        self._codex_daemon_restart_path)
                    if (
                        switch_state is not None
                        and ctx.codex_daemon_epoch
                        and switch_state.epoch != ctx.codex_daemon_epoch
                    ):
                        kind, value = "codex_account_switch", switch_state
                    else:
                        stream_closed = True
                        break
                if kind == "codex_account_switch":
                    handoff = await handoff_account_switch(value)
                    if handoff in {"spontaneous", "interrupted"}:
                        return
                    continue

                raw = value
                if isinstance(raw, CodexSteerFence):
                    raw.reached.set()
                    await raw.release.wait()
                    continue
                if isinstance(raw, CodexNoActiveTurnFence):
                    raw.reached.set()
                    await raw.release.wait()
                    continue
                if isinstance(raw, (
                    CodexSpontaneousOverflow, CodexManagedOverflow,
                )):
                    overflowed = True
                    continue
                if isinstance(raw, CodexSpontaneousClosed):
                    switch_state = read_restart_state(
                        self._codex_daemon_restart_path)
                    if (
                        switch_state is not None
                        and ctx.codex_daemon_epoch
                        and switch_state.epoch != ctx.codex_daemon_epoch
                    ):
                        handoff = await handoff_account_switch(switch_state)
                        if handoff in {"spontaneous", "interrupted"}:
                            return
                        continue
                    stream_closed = True
                    break
                if not isinstance(raw, dict):
                    continue

                await ctx.codex_steer_gate.wait()
                await self._confirm_uncertain_codex_steer(ctx, raw)
                events = translator.feed(raw)
                terminal = is_turn_terminal(raw)
                completed_after_overflow = (
                    overflowed and terminal
                    and _codex_terminal_status(raw) == "completed"
                )
                if completed_after_overflow:
                    # Feed the terminal frame so open assistant blocks close, but
                    # discard the translator's empty-output heuristic.  The
                    # official terminal is authoritative; missing live detail is
                    # rebuilt from the local rollout after the turn unlocks.
                    events = [event for event in events
                              if not isinstance(event, (Error, TurnEnd))]
                for event in events:
                    if isinstance(event, Error) and event.msg_id is None:
                        event.msg_id = logical_msg_id
                    if isinstance(event, StateEvent) and event.detail:
                        if ctx.state != "running":
                            continue
                        event.state = ctx.state
                        if event.msg_id is None:
                            event.msg_id = logical_msg_id
                    await self._emit(ctx, event)
                if terminal:
                    if completed_after_overflow:
                        await self._emit(
                            ctx, _codex_success_terminal(
                                raw, current_turn_id))
                        repair_history = True
                    terminal_seen = True
                    break

            if stream_closed and not terminal_seen:
                synthetic = {
                    "method": "turn/completed",
                    "params": {"turn": {
                        "id": current_turn_id,
                        "status": "failed",
                        "error": {"message": "Codex app-server connection closed"},
                    }},
                }
                for event in translator.feed(synthetic):
                    if isinstance(event, Error) and event.msg_id is None:
                        event.msg_id = logical_msg_id
                    await self._emit(ctx, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "codex spontaneous turn consumer failed",
                turn_id=current_turn_id,
                error_type=type(exc).__name__,
            )
            current_task = asyncio.current_task()
            replacement_owns = (
                ctx.codex_spontaneous_task is not None
                and ctx.codex_spontaneous_task is not current_task
            )
            if replacement_owns:
                return
            # Handoff releases the old id before reconnect so a replacement can
            # claim it. Reclaim only on failure, allowing normal cleanup to
            # unlock the session instead of leaving a permanent running state.
            ctx.codex_spontaneous_turn_id = current_turn_id
            ctx.codex_spontaneous_task = current_task
            try:
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message="Codex 自动回合实时同步失败；请刷新会话。",
                    msg_id=logical_msg_id,
                ))
                await self._emit(ctx, TurnEnd(
                    result=TurnResult(
                        subtype="error", duration_ms=0, is_error=True),
                    turn_id=current_turn_id,
                ))
            except Exception:
                log.warning(
                    "codex spontaneous failure event could not be emitted",
                    turn_id=current_turn_id,
                )
        finally:
            await cancel_restart_watch()
            ctx.codex_account_handoff = False
            try:
                # A replacement lifecycle task owns a different id. Its cleanup
                # is authoritative; the old task must not unlock it.
                await self._finish_codex_spontaneous_turn(
                    ctx, current_turn_id)
            except Exception as exc:
                log.warning(
                    "codex spontaneous turn cleanup failed",
                    turn_id=current_turn_id,
                    error_type=type(exc).__name__,
                )
            if repair_history:
                await self._repair_codex_projection_after_overflow(
                    ctx, current_turn_id)

    async def _run_codex_review_turn(
        self, ctx: SessionContext, turn_id: str,
    ) -> None:
        """Drain one inline review through the normal managed-turn contract.

        ``review/start`` returns a real turn but has no user query payload.  It
        still needs the same bounded reader, interrupt deadline and authoritative
        terminal handling as ``turn/start``; treating it as a spontaneous turn
        loses early review frames and can leave the session permanently busy.
        """
        translator = CodexStreamTranslator(self.cfg.tool_result_max)
        ctx.translator = translator
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, self.cfg.turn_reader_queue_cap))
        reader_exc: list[BaseException] = []
        reader_task: Optional[asyncio.Task] = None
        overflowed = False
        repair_history = False

        async def reader() -> None:
            cancelled = False
            try:
                async for message in ctx.sdk.receive_response():
                    await queue.put(message)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as exc:
                reader_exc.append(exc)
            finally:
                if not cancelled:
                    await queue.put(None)

        async def emit_failure(code: str, message: str, *, interrupted: bool) -> None:
            await self._emit(ctx, Error(
                code=code, message=message, msg_id=turn_id))
            await self._emit(ctx, TurnEnd(
                result=TurnResult(
                    subtype=("error_during_execution" if interrupted else "error"),
                    duration_ms=0,
                    is_error=True,
                ),
                turn_id=turn_id,
            ))

        try:
            # Start draining immediately: review/start may have filled the
            # handle's small queue before its response reached this coroutine.
            reader_task = asyncio.create_task(reader())
            await self._record_codex_unavailable_turn(
                ctx,
                turn_id,
                reason="inline review has no safe pre-tool checkpoint boundary",
            )
            # Review has no user prompt. The empty anchor owns reasoning/tools in
            # the reducer without rendering a fabricated user bubble.
            await self._emit(ctx, UserMsg(msg_id=turn_id, prompt=""))

            while True:
                raw = await self._next_from_queue(ctx, queue)
                if raw is None:
                    if reader_exc:
                        raise reader_exc[0]
                    raise RuntimeError(
                        "codex review stream ended without turn/completed")
                if isinstance(raw, CodexSteerFence):
                    raw.reached.set()
                    await raw.release.wait()
                    continue
                if isinstance(raw, CodexManagedOverflow):
                    overflowed = True
                    continue
                if not isinstance(raw, dict):
                    continue
                await ctx.codex_steer_gate.wait()
                await self._confirm_uncertain_codex_steer(ctx, raw)
                terminal = is_turn_terminal(raw)
                events = translator.feed(raw)
                completed_after_overflow = (
                    overflowed and terminal
                    and _codex_terminal_status(raw) == "completed"
                )
                if completed_after_overflow:
                    events = [event for event in events
                              if not isinstance(event, (Error, TurnEnd))]
                for event in events:
                    if isinstance(event, Error) and event.msg_id is None:
                        event.msg_id = turn_id
                    if isinstance(event, StateEvent) and event.detail:
                        if ctx.state != "running":
                            continue
                        event.state = ctx.state
                        if event.msg_id is None:
                            event.msg_id = turn_id
                    await self._emit(ctx, event)
                if terminal:
                    if completed_after_overflow:
                        await self._emit(
                            ctx, _codex_success_terminal(raw, turn_id))
                        repair_history = True
                    break

            await self._set_idle_after_managed_turn(ctx)
            if repair_history:
                await self._repair_codex_projection_after_overflow(
                    ctx, turn_id)
        except asyncio.TimeoutError:
            log.error(
                "codex review interrupt drain timed out", turn_id=turn_id)
            await emit_failure(
                ERR_DRAIN_TIMEOUT,
                "Codex Review 打断后未返回终态，已重连恢复会话。",
                interrupted=True,
            )
            try:
                await ctx.sdk.force_reconnect(ctx.session_id, ctx.cwd)
            except Exception as exc:
                log.exception(
                    "codex review reconnect failed", error=str(exc))
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message="Codex 会话重连失败，请刷新后重试。",
                    msg_id=turn_id,
                ))
            await self._set_idle_after_managed_turn(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "codex review turn failed", turn_id=turn_id, error=str(exc))
            await emit_failure(
                ERR_CC_CRASH,
                "Codex Review 实时同步失败，请刷新会话。",
                interrupted=(ctx.state == "interrupting"),
            )
            await self._set_idle_after_managed_turn(ctx)
        finally:
            ctx.translator = None
            if ctx.turn_task is asyncio.current_task():
                ctx.turn_task = None
            if ctx.codex_spontaneous_turn_id is None:
                ctx.active_msg_id = None
                ctx.interrupt_deadline = None
                ctx.interrupt_event.clear()
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)

    async def _set_idle_after_managed_turn(self, ctx: SessionContext) -> None:
        """Do not unlock a thread already claimed by an automatic continuation."""
        await self._finish_codex_checkpoint(ctx)
        await self._cleanup_codex_steer_attachments(ctx)
        if ctx.engine == "codex" and ctx.codex_spontaneous_turn_id is not None:
            return
        if ctx.engine == "codex":
            self._release_codex_turn(ctx)
        await self._set_state(ctx, "idle")

    async def _cleanup_codex_steer_attachments(
        self, ctx: SessionContext,
    ) -> None:
        """Remove accepted Code steer attachments after the native terminal."""
        ctx.codex_uncertain_steer = None
        directories = ctx.codex_steer_attachment_dirs
        if not directories:
            return
        ctx.codex_steer_attachment_dirs = []
        for directory in directories:
            try:
                await asyncio.to_thread(shutil.rmtree, directory)
            except FileNotFoundError:
                pass
            except Exception as exc:
                log.warning(
                    "accepted steer attachment cleanup failed",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )

    async def _confirm_uncertain_codex_steer(
        self, ctx: SessionContext, raw: dict,
    ) -> bool:
        """Publish a timed-out steer once app-server proves its client identity."""
        pending = ctx.codex_uncertain_steer
        if not isinstance(pending, TurnSteered):
            return False
        identity = _codex_user_message_identity(raw)
        if identity != (pending.msg_id, pending.turn_id):
            return False
        # Clear before relay I/O so item/started + item/completed cannot create
        # two boundaries if the first live send loses its socket.
        ctx.codex_uncertain_steer = None
        ctx.active_msg_id = pending.msg_id
        try:
            await self._emit(ctx, pending)
        except Exception as exc:
            log.warning(
                "confirmed Codex steer live echo delayed",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
        return True

    @staticmethod
    def _clear_codex_checkpoint_tracking(ctx: SessionContext) -> None:
        ctx.codex_checkpoint_turn_id = None
        ctx.codex_checkpoint_ready = False
        ctx.codex_checkpoint_accepted = False
        ctx.codex_checkpoint_unavailable_reason = None

    async def _record_codex_unavailable_turn(
        self, ctx: SessionContext, turn_id: str, *, reason: str,
    ) -> None:
        """Append one idempotent count slot when no safe pre-image exists."""
        if ctx.engine != "codex" or ctx.space != "code" or not turn_id:
            return
        journal = ctx.codex_checkpoint
        if journal is False:
            return
        try:
            if journal is None:
                journal = await asyncio.to_thread(
                    CodexCheckpointJournal,
                    ctx.cwd,
                    Path(self.cfg.state_dir),
                    ctx.session_id or ctx.key,
                )
                ctx.codex_checkpoint = journal
            await asyncio.to_thread(journal.record_unavailable, turn_id, reason)
        except NotGitWorkspaceError:
            ctx.codex_checkpoint = False
        except CheckpointError as exc:
            log.warning(
                "Codex unavailable turn could not be aligned",
                session_id=ctx.session_id,
                turn_id=turn_id,
                error_type=type(exc).__name__,
            )
            await self._retire_codex_checkpoint(
                ctx, reason="unavailable automatic turn could not be persisted"
            )

    async def _retire_codex_checkpoint(
        self,
        ctx: SessionContext,
        *,
        reason: str,
        allow_restart: bool = False,
    ) -> None:
        """Fail closed after journal alignment can no longer be guaranteed.

        A fresh journal may be created after the resident context is restarted,
        but this context must never append newer turns behind a missing barrier.
        Force cleanup also quarantines a corrupt manifest by renaming the whole
        private directory before deletion; it never touches the user's repo.
        """
        journal = ctx.codex_checkpoint
        self._clear_codex_checkpoint_tracking(ctx)
        ctx.codex_checkpoint = False
        if journal is None or journal is False:
            return
        try:
            await asyncio.to_thread(journal.cleanup, force=True)
            if allow_restart:
                # The next Remote-managed turn may start a new tail-aligned
                # journal. Requests that cross this external boundary still fail
                # safely because the fresh journal contains too few records.
                ctx.codex_checkpoint = None
        except Exception as exc:
            log.warning(
                "Codex checkpoint journal quarantine failed",
                session_id=ctx.session_id,
                reason=reason,
                error_type=type(exc).__name__,
            )

    async def _prepare_codex_conversation_rollback(
        self, ctx: SessionContext,
    ) -> bool:
        """Retire every file checkpoint before native history rollback.

        ``thread/rollback`` has no transaction id that can be reconciled after a
        lost app-server response.  Keeping count-based file records until after
        that RPC therefore leaves a fatal crash window: history may already be
        shorter while the checkpoint tail still names the removed turns.  Remove
        the private journal first.  A later Remote turn can start a fresh journal
        aligned to the then-authoritative thread tail.

        This path is intentionally strict.  If an existing journal cannot be
        quarantined, do not submit the native conversation mutation.
        """
        journal = ctx.codex_checkpoint
        self._clear_codex_checkpoint_tracking(ctx)
        if journal is False:
            return True
        if journal is None:
            try:
                journal = await asyncio.to_thread(
                    CodexCheckpointJournal,
                    ctx.cwd,
                    Path(self.cfg.state_dir),
                    ctx.session_id or ctx.key,
                )
            except NotGitWorkspaceError:
                ctx.codex_checkpoint = False
                return True
            except CheckpointError:
                ctx.codex_checkpoint = False
                raise
            ctx.codex_checkpoint = journal
        # Disable file rollback before yielding to cleanup.  Even if quarantine
        # fails, this resident context must never reuse possibly stale records.
        ctx.codex_checkpoint = False
        try:
            await asyncio.to_thread(journal.cleanup, force=True)
        except Exception as exc:
            raise CheckpointError(
                "Codex checkpoint journal could not be retired before rollback"
            ) from exc
        # Successful quarantine removes the old count boundary.  New managed
        # turns may establish a fresh tail-aligned journal.
        ctx.codex_checkpoint = None
        return True

    async def _begin_codex_checkpoint(
        self, ctx: SessionContext, *, already_accepted: bool = False,
    ) -> None:
        """Best-effort capture for one managed Codex Code turn.

        Checkpoint availability must never prevent Codex from doing the user's
        work. Unsupported/non-Git workspaces remain usable and report the
        limitation only if the user later requests code restoration.
        """
        if (
            ctx.engine != "codex"
            or ctx.space != "code"
            or not ctx.session_id
            or not ctx.active_msg_id
        ):
            return
        turn_id = ctx.active_msg_id
        ctx.codex_checkpoint_turn_id = turn_id
        ctx.codex_checkpoint_ready = False
        ctx.codex_checkpoint_accepted = already_accepted
        ctx.codex_checkpoint_unavailable_reason = None
        journal = ctx.codex_checkpoint
        try:
            if journal is False:
                self._clear_codex_checkpoint_tracking(ctx)
                return
            if journal is None:
                journal = await asyncio.to_thread(
                    CodexCheckpointJournal,
                    ctx.cwd,
                    Path(self.cfg.state_dir),
                    ctx.session_id,
                )
                ctx.codex_checkpoint = journal
            try:
                await asyncio.to_thread(journal.begin_turn, turn_id)
            except CheckpointError:
                # A process crash can leave the previous accepted native turn's
                # pre-image active. Preserve its count slot as unavailable,
                # then retry the current pre-turn capture exactly once.
                recovered = await asyncio.to_thread(
                    journal.recover_active_as_unavailable,
                    "wrapper stopped before checkpoint finalization",
                )
                if recovered is None:
                    raise
                recovered_turn, recovered_accepted = recovered
                log.warning(
                    (
                        "recovered accepted Codex checkpoint as unavailable"
                        if recovered_accepted
                        else "discarded unaccepted Codex checkpoint after restart"
                    ),
                    session_id=ctx.session_id,
                    turn_id=recovered_turn,
                )
                await asyncio.to_thread(journal.begin_turn, turn_id)
            ctx.codex_checkpoint_ready = True
        except NotGitWorkspaceError:
            ctx.codex_checkpoint = False
            self._clear_codex_checkpoint_tracking(ctx)
        except CheckpointError as exc:
            log.warning(
                "Codex pre-turn checkpoint unavailable",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            # Do not append a tombstone yet: turn/start may still fail. It is
            # committed only by _accept_codex_checkpoint after app-server has
            # accepted the corresponding native turn.
            if journal is None:
                # An unreadable existing journal cannot receive a tombstone.
                # Disable file rollback for this resident context rather than
                # appending later checkpoints behind an unknown gap.
                ctx.codex_checkpoint = False
                self._clear_codex_checkpoint_tracking(ctx)
            else:
                ctx.codex_checkpoint_ready = False
                ctx.codex_checkpoint_unavailable_reason = type(exc).__name__
        if already_accepted:
            await self._accept_codex_checkpoint(ctx)

    async def _accept_codex_checkpoint(self, ctx: SessionContext) -> None:
        turn_id = ctx.codex_checkpoint_turn_id
        journal = ctx.codex_checkpoint
        if not turn_id or journal is None or journal is False:
            return
        if ctx.codex_checkpoint_ready:
            try:
                await asyncio.to_thread(journal.accept_turn, turn_id)
            except CheckpointError as exc:
                log.warning(
                    "Codex checkpoint acceptance marker failed",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
                await self._retire_codex_checkpoint(
                    ctx, reason="turn acceptance could not be persisted"
                )
                return
        ctx.codex_checkpoint_accepted = True
        reason = ctx.codex_checkpoint_unavailable_reason
        if reason is None:
            return
        try:
            await asyncio.to_thread(journal.record_unavailable, turn_id, reason)
        except Exception as exc:
            log.warning(
                "Codex unavailable checkpoint marker failed; retrying at turn finish",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            # The native turn has already been accepted, so dropping tracking
            # here would also drop its count slot.  Keep the pending reason until
            # the authoritative finish boundary and retry exactly once there.
            return
        ctx.codex_checkpoint_unavailable_reason = None

    async def _abort_codex_checkpoint(self, ctx: SessionContext) -> None:
        turn_id = ctx.codex_checkpoint_turn_id
        journal = ctx.codex_checkpoint
        ready = ctx.codex_checkpoint_ready
        self._clear_codex_checkpoint_tracking(ctx)
        if not ready or not turn_id or journal is None or journal is False:
            return
        try:
            await asyncio.to_thread(journal.abort_turn, turn_id)
        except CheckpointError as exc:
            log.warning(
                "Codex checkpoint abort failed",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            await self._retire_codex_checkpoint(
                ctx, reason="unaccepted checkpoint could not be aborted"
            )

    async def _finish_codex_checkpoint(self, ctx: SessionContext) -> None:
        turn_id = ctx.codex_checkpoint_turn_id
        journal = ctx.codex_checkpoint
        ready = ctx.codex_checkpoint_ready
        accepted = ctx.codex_checkpoint_accepted
        unavailable_reason = ctx.codex_checkpoint_unavailable_reason
        if not turn_id or journal is None or journal is False:
            self._clear_codex_checkpoint_tracking(ctx)
            return
        if not accepted:
            await self._abort_codex_checkpoint(ctx)
            return
        if not ready:
            # Capture failed before turn/start.  Acceptance normally commits the
            # unavailable tombstone; if that first write failed, retry once at
            # the finish boundary before either clearing state or accepting a
            # newer turn behind a missing count slot.
            if unavailable_reason is not None:
                try:
                    await asyncio.to_thread(
                        journal.record_unavailable,
                        turn_id,
                        unavailable_reason,
                    )
                except Exception as exc:
                    log.warning(
                        "Codex unavailable checkpoint marker retry failed",
                        session_id=ctx.session_id,
                        error_type=type(exc).__name__,
                    )
                    await self._retire_codex_checkpoint(
                        ctx, reason="unavailable marker retry could not be persisted"
                    )
                    return
            self._clear_codex_checkpoint_tracking(ctx)
            return
        self._clear_codex_checkpoint_tracking(ctx)
        try:
            await asyncio.to_thread(journal.finish_turn, turn_id)
        except CheckpointError as exc:
            try:
                await asyncio.to_thread(journal.abort_turn, turn_id)
            except CheckpointError:
                pass
            try:
                await asyncio.to_thread(
                    journal.record_unavailable, turn_id, type(exc).__name__
                )
            except CheckpointError:
                log.warning(
                    "Codex unavailable checkpoint marker failed",
                    session_id=ctx.session_id,
                )
                await self._retire_codex_checkpoint(
                    ctx, reason="post-turn marker could not be persisted"
                )
            log.warning(
                "Codex post-turn checkpoint unavailable",
                session_id=ctx.session_id,
                error_type=type(exc).__name__,
            )
            await self._emit(
                ctx,
                Notice(
                    notice_id=f"checkpoint-{uuid4().hex}",
                    severity="warning",
                    category="runtime",
                    title="本轮代码回滚不可用",
                    message=(
                        "本轮修改了 Git 暂存区或包含当前无法安全记录的文件；"
                        "对话仍可正常回滚。"
                    ),
                    thread_id=ctx.session_id,
                ),
            )

    async def _emit_goal_error(self, ctx: SessionContext, cmd,
                               message: str) -> Error:
        error = Error(
            code=ERR_INTERNAL,
            message=message,
            request_id=getattr(cmd, "cmd_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit(ctx, error)
        return error

    @staticmethod
    def _codex_goal_update_already_applied(
        ctx: SessionContext, cmd,
    ) -> Optional[dict]:
        """Prove a retried Goal mutation owns the currently live auto-turn."""
        mutation = ctx.codex_goal_mutation
        if ctx.state != "running" or mutation is None:
            return None
        requested_command_id = getattr(cmd, "cmd_id", None)
        requested_client_id = getattr(cmd, "client_id", None)
        same_command = bool(
            requested_command_id
            and requested_command_id == mutation.command_id
        )
        same_client_retry = requested_client_id == mutation.client_id
        if not same_command and not same_client_retry:
            return None
        if (
            getattr(cmd, "objective", None) != mutation.objective
            or getattr(cmd, "status", None) != mutation.status
            or getattr(cmd, "token_budget", None) != mutation.token_budget
        ):
            return None
        if (
            not mutation.turn_id
            or ctx.codex_spontaneous_turn_id != mutation.turn_id
        ):
            return None
        current_goal_revision = int(
            getattr(ctx.sdk, "goal_revision", 0) or 0
        )
        notification_proves_apply = (
            current_goal_revision > mutation.goal_revision_before
        )
        if not mutation.applied and not notification_proves_apply:
            return None
        notified_turn_id = getattr(ctx.sdk, "last_goal_turn_id", None)
        if (
            notification_proves_apply
            and notified_turn_id is not None
            and notified_turn_id != mutation.turn_id
        ):
            return None
        goal = getattr(ctx.sdk, "last_goal", None)
        if not isinstance(goal, dict):
            return None
        if goal.get("threadId") != (ctx.session_id or ctx.key):
            return None
        requested = (
            ("objective", mutation.objective),
            ("status", mutation.status),
            ("tokenBudget", mutation.token_budget),
        )
        supplied = [(key, value) for key, value in requested if value is not None]
        if not supplied or any(goal.get(key) != value for key, value in supplied):
            return None
        return goal

    async def _handle_get_goal(self, cmd) -> None:
        ctx = await self._goal_ctx(cmd)
        if ctx is None:
            return await self._missing_session_error(cmd, "读取 Goal")
        try:
            ctx.goal_visible = True
            goal = (await ctx.sdk.get_goal() if ctx.engine == "codex"
                    else await ctx.sdk.refresh_goal(ctx.session_id))
            event = GoalState(
                goal=goal,
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            log.warning("get_goal failed", error_type=type(exc).__name__)
            return await self._emit_goal_error(ctx, cmd, "Goal 状态暂不可用")

    async def _handle_set_goal(self, cmd) -> None:
        ctx = await self._goal_ctx(cmd)
        if ctx is None:
            return await self._missing_session_error(cmd, "设置 Goal")
        if ctx.engine != "codex":
            if getattr(cmd, "token_budget", None) is not None:
                error = Error(
                    code=ERR_PROTOCOL,
                    message="Claude /goal 不支持 token budget",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            if getattr(cmd, "status", None) not in (None, "active"):
                error = Error(
                    code=ERR_PROTOCOL,
                    message="Claude /goal 只支持设置目标或 clear，不支持暂停/状态切换",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            objective = (getattr(cmd, "objective", None) or "").strip()
            if not objective:
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="Claude /goal 需要非空完成条件",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY, message="该会话正忙,先 interrupt",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None))
                await self._emit(ctx, error)
                return error

            previous = dict(ctx.sdk.goal) if ctx.sdk.goal is not None else None
            try:
                goal = await ctx.sdk.prepare_goal(
                    ctx.session_id or ctx.key, objective)
                query_result = await self._handle_query(Query(
                    sid=ctx.key,
                    prompt=f"/goal {objective}",
                    msg_id=f"goal-{uuid4().hex}",
                ))
                if isinstance(query_result, Error):
                    ctx.sdk.restore_goal_state(previous)
                    return query_result
                ctx.goal_visible = True
                event = GoalState(goal=goal)
                await self._emit(ctx, event)
                return event
            except Exception as exc:
                ctx.sdk.restore_goal_state(previous)
                log.warning(
                    "Claude set_goal failed", error_type=type(exc).__name__)
                return await self._emit_goal_error(ctx, cmd, "设置 Goal 失败")
        try:
            # thread/goal/set(status=active) can synchronously launch an app-server
            # turn. Claim before the RPC so a query arriving before turn/started
            # cannot become a second writer. launch_lock also makes an immediate
            # interrupt wait until the authoritative automatic turn id is known.
            async with ctx.launch_lock:
                if ctx.state != "idle":
                    # The browser may retry after receiving GoalState but before
                    # its CommandAck, or two taps may enqueue equivalent command
                    # ids. The first set has already started the automatic turn,
                    # so rejecting the identical mutation as busy produces a
                    # false failure banner even though the Goal is active.
                    applied = self._codex_goal_update_already_applied(ctx, cmd)
                    if applied is not None:
                        event = GoalState(goal=applied)
                        await self._emit(ctx, event)
                        return event
                    error = Error(
                        code=ERR_BUSY, message="该会话正忙,先 interrupt",
                        request_id=getattr(cmd, "cmd_id", None),
                        to=getattr(cmd, "client_id", None))
                    await self._emit(ctx, error)
                    return error
                ctx.codex_goal_mutation = CodexGoalMutation(
                    command_id=getattr(cmd, "cmd_id", None),
                    client_id=getattr(cmd, "client_id", None),
                    objective=getattr(cmd, "objective", None),
                    status=getattr(cmd, "status", None),
                    token_budget=getattr(cmd, "token_budget", None),
                    goal_revision_before=int(
                        getattr(ctx.sdk, "goal_revision", 0) or 0
                    ),
                )
                ctx.interrupt_event.clear()
                ctx.interrupt_deadline = None
                ctx.state = "running"
                await self._emit(ctx, StateEvent(state="running"))
                goal = await ctx.sdk.set_goal(
                    objective=cmd.objective, status=cmd.status,
                    token_budget=cmd.token_budget)
                mutation = ctx.codex_goal_mutation
                if mutation is not None:
                    mutation.applied = True
                automatic_turn_live = bool(
                    ctx.codex_spontaneous_turn_id
                    or getattr(ctx.sdk, "turn_active", False))
                if automatic_turn_live:
                    if (
                        mutation is not None
                        and mutation.turn_id is None
                        and ctx.codex_spontaneous_turn_id is not None
                    ):
                        mutation.turn_id = ctx.codex_spontaneous_turn_id
                else:
                    ctx.codex_goal_mutation = None
                    if ctx.state != "idle":
                        await self._set_idle_after_managed_turn(ctx)
            event = GoalState(goal=goal)
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            applied = self._codex_goal_update_already_applied(ctx, cmd)
            if applied is not None:
                mutation = ctx.codex_goal_mutation
                if mutation is not None:
                    mutation.applied = True
                event = GoalState(goal=applied)
                await self._emit(ctx, event)
                return event
            automatic_turn_live = bool(
                ctx.codex_spontaneous_turn_id
                or getattr(ctx.sdk, "turn_active", False))
            if not automatic_turn_live:
                ctx.codex_goal_mutation = None
                if ctx.state != "idle":
                    await self._set_state(ctx, "idle")
            log.warning("set_goal failed", error_type=type(exc).__name__)
            return await self._emit_goal_error(ctx, cmd, "设置 Goal 失败")

    async def _handle_clear_goal(self, cmd) -> None:
        ctx = await self._goal_ctx(cmd)
        if ctx is None:
            return await self._missing_session_error(cmd, "清除 Goal")
        if ctx.engine != "codex":
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY, message="该会话正忙,先 interrupt",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None))
                await self._emit(ctx, error)
                return error
            previous = dict(ctx.sdk.goal) if ctx.sdk.goal is not None else None
            ctx.sdk.clear_goal_state()
            try:
                query_result = await self._handle_query(Query(
                    sid=ctx.key,
                    prompt="/goal clear",
                    msg_id=f"goal-{uuid4().hex}",
                ))
                if isinstance(query_result, Error):
                    ctx.sdk.restore_goal_state(previous)
                    return query_result
                ctx.goal_visible = False
                event = GoalState(goal=None)
                await self._emit(ctx, event)
                return event
            except Exception as exc:
                ctx.sdk.restore_goal_state(previous)
                log.warning(
                    "Claude clear_goal failed", error_type=type(exc).__name__)
                return await self._emit_goal_error(ctx, cmd, "清除 Goal 失败")
        if (ctx.state != "idle"
                and ctx.codex_spontaneous_turn_id is None):
            error = Error(
                code=ERR_BUSY, message="该会话正忙,先 interrupt",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit(ctx, error)
            return error
        try:
            async with ctx.launch_lock:
                await ctx.sdk.clear_goal()
            ctx.codex_goal_mutation = None
            # Clearing the condition does not necessarily stop the already-live
            # automatic turn. Route it through the normal interrupt state machine
            # so the thread cannot keep writing invisibly after the UI says clear.
            if (ctx.codex_spontaneous_turn_id is not None
                    and ctx.state == "running"):
                await self._handle_interrupt(Interrupt(sid=ctx.key))
            event = GoalState(goal=None)
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            log.warning("clear_goal failed", error_type=type(exc).__name__)
            return await self._emit_goal_error(ctx, cmd, "清除 Goal 失败")

    async def _handle_get_diff(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            error = Error(
                code=ERR_NOT_RUNNING,
                message="该会话未启动，无法读取代码差异",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit_to_sid(sid, error)
            return error
        try:
            diff = await self._git_diff(
                ctx.cwd, cmd.file, self._preview_external_paths(ctx))
            event = DiffReport(
                file=cmd.file,
                diff=diff,
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, event)
            return event
        except Exception as e:
            log.exception("get_diff failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="文件差异暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

    async def _handle_get_file_preview(self, cmd):
        sid = getattr(cmd, "sid", None)
        client_id = getattr(cmd, "client_id", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            response = FilePreview(
                path=cmd.path,
                request_id=cmd.request_id,
                format=self._preview_format(cmd.path),
                error="该会话未启动，无法读取文件",
                to=client_id,
            )
            await self._emit_to_sid(sid, response)
            return response

        try:
            suffix = os.path.splitext(cmd.path)[1].lower()
            external_paths = self._preview_external_paths(ctx)
            if suffix in self.OFFICE_PREVIEW_SUFFIXES:
                async with self._preview_conversion_limit:
                    preview = await asyncio.to_thread(
                        self._read_file_preview, ctx.cwd, cmd.path,
                        external_paths)
            else:
                preview = await asyncio.to_thread(
                    self._read_file_preview, ctx.cwd, cmd.path,
                    external_paths)
            response = FilePreview(
                path=preview["path"],
                request_id=cmd.request_id,
                format=preview["format"],
                content=preview.get("content", ""),
                media_type=preview.get("media_type"),
                data=(base64.b64encode(preview["data"]).decode("ascii")
                      if preview.get("data") is not None else None),
                converted_from=preview.get("converted_from"),
                size=preview["size"],
                truncated=preview.get("truncated", False),
                mtime_ns=str(preview["mtime_ns"]),
                revision=preview.get("revision"),
                to=client_id,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            response = FilePreview(
                path=cmd.path,
                request_id=cmd.request_id,
                format=self._preview_format(cmd.path),
                error=str(exc),
                to=client_id,
            )
        except Exception as exc:
            log.exception("file preview failed", error_type=type(exc).__name__)
            response = FilePreview(
                path=cmd.path,
                request_id=cmd.request_id,
                format=self._preview_format(cmd.path),
                error="读取文件失败",
                to=client_id,
            )
        await self._emit(ctx, response)
        return response

    async def _handle_save_markdown(self, cmd):
        sid = getattr(cmd, "sid", None)
        client_id = getattr(cmd, "client_id", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            response = FileSaveResult(
                path=cmd.path,
                request_id=cmd.request_id,
                status="error",
                error="该会话未启动，无法保存文件",
                to=client_id,
            )
            await self._emit_to_sid(sid, response)
            return response

        try:
            path, size, mtime_ns, revision = await asyncio.to_thread(
                self._write_markdown_file,
                ctx.cwd,
                cmd.path,
                cmd.content,
                cmd.expected_size,
                int(cmd.expected_mtime_ns),
                cmd.expected_revision,
                self._preview_external_paths(ctx),
            )
            response = FileSaveResult(
                path=path,
                request_id=cmd.request_id,
                status="saved",
                size=size,
                mtime_ns=str(mtime_ns),
                revision=revision,
                to=client_id,
            )
        except _FileRevisionConflict as exc:
            response = FileSaveResult(
                path=cmd.path,
                request_id=cmd.request_id,
                status="conflict",
                size=exc.size,
                mtime_ns=str(exc.mtime_ns),
                revision=exc.revision,
                error=str(exc),
                to=client_id,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            response = FileSaveResult(
                path=cmd.path,
                request_id=cmd.request_id,
                status="error",
                error=str(exc),
                to=client_id,
            )
        except Exception as exc:
            log.exception("Markdown save failed", error_type=type(exc).__name__)
            response = FileSaveResult(
                path=cmd.path,
                request_id=cmd.request_id,
                status="error",
                error="保存文件失败",
                to=client_id,
            )
        await self._emit(ctx, response)
        return response

    async def _handle_get_preview_asset(self, cmd):
        sid = getattr(cmd, "sid", None)
        client_id = getattr(cmd, "client_id", None)
        ctx = self._ctx_for(sid)
        if ctx is None:
            response = PreviewAsset(
                path=cmd.path,
                preview_id=cmd.preview_id,
                request_id=cmd.request_id,
                error="该会话未启动，无法读取预览图片",
                to=client_id,
            )
            await self._emit_to_sid(sid, response)
            return response

        try:
            _, media_type, data = await asyncio.to_thread(
                self._read_preview_asset, ctx.cwd, cmd.path,
                self._preview_external_paths(ctx))
            response = PreviewAsset(
                path=cmd.path,
                preview_id=cmd.preview_id,
                request_id=cmd.request_id,
                media_type=media_type,
                data=base64.b64encode(data).decode("ascii"),
                to=client_id,
            )
        except ValueError as exc:
            response = PreviewAsset(
                path=cmd.path,
                preview_id=cmd.preview_id,
                request_id=cmd.request_id,
                error=str(exc),
                to=client_id,
            )
        except Exception as exc:
            log.exception("preview asset failed", error_type=type(exc).__name__)
            response = PreviewAsset(
                path=cmd.path,
                preview_id=cmd.preview_id,
                request_id=cmd.request_id,
                error="读取预览图片失败",
                to=client_id,
            )
        await self._emit(ctx, response)
        return response

    # ---- ask_user MCP tool (agent asks the user a multiple-choice question) ----

    @staticmethod
    def _cancel_pending_asks(ctx: SessionContext) -> None:
        """Wake prompt handlers so interrupt can drain instead of waiting 30m."""
        for future in tuple(ctx.pending_asks.values()):
            if not future.done():
                future.set_exception(AskCancelled())

    async def _on_mcp_ask(
        self,
        ctx: SessionContext,
        question: str,
        options: list[dict[str, str]],
    ) -> str:
        """Keep the in-process MCP tool's historical textual timeout contract."""
        try:
            answer = await self._on_ask(ctx, question, options)
        except AskTimeout:
            return "(用户未回答，已超时)"
        except (AskCancelled, AskSuperseded):
            return "(用户未回答，问题已取消)"
        return answer if isinstance(answer, str) else ", ".join(answer)

    async def _on_ask(
        self,
        ctx: SessionContext,
        question: str,
        options: list[dict[str, str]],
        *,
        header: str | None = None,
        allow_text: bool = False,
        secret: bool = False,
        multi_select: bool = False,
        timeout: float = 30 * 60,
        ask_id: str | None = None,
        to: str | None = None,
    ) -> str | list[str]:
        """Called by THIS ctx's in-process MCP server when the agent invokes
        `ask_user`. Emits AskUser on the ctx and blocks until AnswerQuestion.
        Runs in the ctx's reader task while its turn loop is blocked on
        receive_response(); other ctxs' turns are unaffected."""
        async with ctx.ask_lock:
            return await self._on_ask_locked(
                ctx,
                question,
                options,
                header=header,
                allow_text=allow_text,
                secret=secret,
                multi_select=multi_select,
                timeout=timeout,
                ask_id=ask_id,
                to=to,
            )

    async def _on_ask_locked(
        self,
        ctx: SessionContext,
        question: str,
        options: list[dict[str, str]],
        *,
        header: str | None = None,
        allow_text: bool = False,
        secret: bool = False,
        multi_select: bool = False,
        timeout: float = 30 * 60,
        ask_id: str | None = None,
        to: str | None = None,
    ) -> str | list[str]:
        """Run one question while the caller owns ``ctx.ask_lock``."""
        # ask_id is an identity, not a downstream sequence.  Consuming next_seq
        # here would leave an invisible hole before _emit assigns AskUser.seq;
        # reconnect replay would then appear to have lost a frame.
        ask_id = ask_id or f"ask-{uuid4().hex}"
        # Validate model-originated text before registering a pending Future.
        # Otherwise a malformed/oversized AskUser raises during emit and leaves
        # an unreachable entry in pending_asks for the life of the session.
        event = AskUser(ask_id=ask_id, question=question, options=options,
                        header=header, allow_text=allow_text, secret=secret,
                        multi_select=multi_select, to=to)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        ctx.pending_asks[ask_id] = fut
        ctx.pending_ask_specs[ask_id] = {
            "labels": frozenset(option["label"] for option in options),
            "allow_text": allow_text,
            "multi_select": multi_select,
        }
        reason = "cancelled"
        try:
            await self._emit(ctx, event)
            log.info(
                "ask_user emitted",
                sid=ctx.session_id,
                ask_id=ask_id,
                options=len(options),
                multi_select=multi_select,
            )
            answer = await asyncio.wait_for(fut, timeout=timeout)
            reason = "answered"
            return answer
        except asyncio.TimeoutError:
            reason = "timeout"
            log.warning("ask_user timed out", ask_id=ask_id)
            raise AskTimeout from None
        except AskSuperseded:
            reason = "superseded"
            raise
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        finally:
            if ctx.pending_asks.get(ask_id) is fut:
                ctx.pending_asks.pop(ask_id, None)
                ctx.pending_ask_specs.pop(ask_id, None)
            try:
                await self._emit(
                    ctx,
                    AskUserClosed(ask_id=ask_id, reason=reason, to=to),
                )
            except Exception as exc:
                log.warning(
                    "ask_user close event delayed",
                    ask_id=ask_id,
                    reason=reason,
                    error_type=type(exc).__name__,
                )

    async def _on_ask_optional(self, *args, **kwargs) -> str | list[str] | None:
        """Map a structured no-answer outcome for fail-closed integrations."""
        try:
            return await self._on_ask(*args, **kwargs)
        except AskUnavailable:
            return None

    async def _on_codex_approval(self, ctx: SessionContext, method: str,
                                 params: dict) -> str:
        """Bridge Codex app-server approvals to the existing AskUser flow.

        The callback returns the current app-server schema's exact decision enum.
        Anything unexpected fails closed; CodexHandle independently enforces a
        shorter timeout than the generic ask-user tool.
        """
        def short(value, limit: int = 1200) -> str:
            text = str(value or "").strip()
            return text if len(text) <= limit else text[:limit] + "…"

        command_request = method in {
            "item/commandExecution/requestApproval", "execCommandApproval",
        }
        lines = ["Codex 请求执行命令：" if command_request else "Codex 请求修改文件："]
        if command_request:
            command = params.get("command")
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            if not command:
                command = params.get("parsedCmd") or params.get("itemId")
            if command:
                lines.append(short(command))
        else:
            changes = params.get("fileChanges")
            if isinstance(changes, dict) and changes:
                paths = list(changes)[:12]
                lines.append("\n".join(short(path, 240) for path in paths))
                if len(changes) > len(paths):
                    lines.append(f"另有 {len(changes) - len(paths)} 个文件")
            elif params.get("itemId"):
                lines.append(f"变更项 {short(params['itemId'], 240)}")
        if params.get("cwd"):
            lines.append(f"目录：{short(params['cwd'], 500)}")
        if params.get("grantRoot"):
            lines.append(f"授权目录：{short(params['grantRoot'], 500)}")
        if params.get("reason"):
            lines.append(f"原因：{short(params['reason'], 500)}")

        options = [
            {"label": "允许一次", "ds": "仅批准这一次操作"},
            {"label": "本会话允许", "ds": "本会话后续同类操作不再询问"},
            {"label": "拒绝", "ds": "拒绝这次操作"},
            {"label": "取消", "ds": "取消当前操作或回合"},
        ]
        answer = await self._on_ask_optional(ctx, "\n".join(lines), options)
        return {
            "允许一次": "accept",
            "本会话允许": "acceptForSession",
            "拒绝": "decline",
            "取消": "cancel",
        }.get(answer, "decline")

    async def _on_codex_interaction(self, ctx: SessionContext, method: str,
                                    params: dict) -> dict:
        if method == "item/permissions/requestApproval":
            requested = params.get("permissions")
            if not isinstance(requested, dict):
                raise ValueError("permissions request is missing its profile")
            detail = json.dumps(requested, ensure_ascii=False, indent=2)
            reason = str(params.get("reason") or "").strip()
            prompt = "Codex 请求额外权限：\n" + detail[:12000]
            if reason:
                prompt += "\n原因：" + reason[:1000]
            answer = await self._on_ask_optional(ctx, prompt, [
                {"label": "允许本回合", "ds": "仅在当前回合授予这些权限"},
                {"label": "允许本会话", "ds": "本会话后续保留这些权限"},
                {"label": "拒绝", "ds": "不授予额外权限"},
            ], header="权限审批")
            if answer == "允许本回合":
                return {"permissions": requested, "scope": "turn"}
            if answer == "允许本会话":
                return {"permissions": requested, "scope": "session"}
            return {"permissions": {}, "scope": "turn"}

        if method == "mcpServer/elicitation/request":
            return await self._on_codex_mcp_elicitation(ctx, params)

        if method != "item/tool/requestUserInput":
            raise ValueError(f"unsupported codex interaction: {method}")
        questions = params.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError("requestUserInput requires 1-3 questions")
        answers: dict[str, dict[str, list[str]]] = {}
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("invalid requestUserInput question")
            question_id = question.get("id")
            prompt = question.get("question")
            if not isinstance(question_id, str) or not question_id:
                raise ValueError("requestUserInput question id missing")
            if not isinstance(prompt, str) or not prompt:
                raise ValueError("requestUserInput question text missing")
            raw_options = question.get("options") or []
            options = []
            for option in raw_options[:ASK_OPTION_MAX_COUNT]:
                if isinstance(option, dict) and option.get("label"):
                    options.append({"label": str(option["label"])[:512],
                                    "ds": str(option.get("description") or "")[:2048]})
            # The wire question card requires either 2+ choices or a text box.
            # A one-option server payload is still answerable through text.
            allow_text = bool(question.get("isOther")) or len(options) < 2
            answer = await self._on_ask_optional(
                ctx, prompt, options, header=str(question.get("header") or "")[:512] or None,
                allow_text=allow_text, secret=bool(question.get("isSecret")))
            if answer is None:
                return {"answers": {}}
            if isinstance(answer, list):
                answer_values = answer
            else:
                answer_values = [answer]
            answers[question_id] = {"answers": answer_values}
        return {"answers": answers}

    async def _on_codex_mcp_elicitation(self, ctx: SessionContext,
                                        params: dict) -> dict:
        mode = params.get("mode")
        message = str(params.get("message") or "MCP 服务请求输入")[:16000]
        server = str(params.get("serverName") or "MCP")[:512]
        if mode == "url":
            url = str(params.get("url") or "")[:4096]
            answer = await self._on_ask_optional(ctx, f"{message}\n\n{url}", [
                {"label": "已完成并继续", "ds": "我已在链接页面完成操作"},
                {"label": "拒绝", "ds": "不继续这次 MCP 请求"},
                {"label": "取消", "ds": "取消当前操作"},
            ], header=f"{server} 请求网页操作")
            return {"action": {"已完成并继续": "accept", "取消": "cancel"}.get(answer, "decline")}

        schema = params.get("requestedSchema")
        if not isinstance(schema, dict):
            # openai/form schemas may be intentionally opaque. Preserve a usable
            # accept/decline path instead of rejecting the server request.
            answer = await self._on_ask_optional(ctx, message, [
                {"label": "接受", "ds": "继续此 MCP 表单请求"},
                {"label": "拒绝", "ds": "拒绝此 MCP 表单请求"},
            ], header=f"{server} 请求输入")
            return {"action": "accept" if answer == "接受" else "decline"}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or len(properties) > 32:
            raise ValueError("invalid MCP elicitation schema")
        required = set(schema.get("required") or [])
        content = {}
        for name, spec in properties.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise ValueError("invalid MCP elicitation field")
            title = str(spec.get("title") or name)[:512]
            question = str(spec.get("description") or title)[:16000]
            values = spec.get("enum") or []
            names = spec.get("enumNames") or []
            options = [
                {"label": str(names[i] if i < len(names) else value)[:512],
                 "ds": str(value)[:2048]}
                for i, value in enumerate(values[:5])
            ]
            answer = await self._on_ask_optional(
                ctx, question, options, header=f"{server} · {title}",
                allow_text=len(options) < 2, secret=bool(spec.get("format") == "password"))
            if answer is None:
                return {"action": "cancel"}
            if isinstance(answer, list):
                return {"action": "cancel"}
            if not answer and name in required:
                return {"action": "cancel"}
            field_type = spec.get("type")
            if field_type == "boolean":
                content[name] = answer.lower() in {"true", "yes", "1", "是", "同意"}
            elif field_type == "integer":
                content[name] = int(answer)
            elif field_type == "number":
                content[name] = float(answer)
            else:
                # Map a display label back to its enum wire value when possible.
                if answer in names:
                    content[name] = values[names.index(answer)]
                else:
                    content[name] = answer
        return {"action": "accept", "content": content}

    async def _on_claude_ask_user_question(
        self,
        ctx: SessionContext,
        tool_input: dict,
    ):
        """Answer Claude's built-in question tool instead of approving it."""
        try:
            questions = normalize_claude_questions(tool_input)
        except ValueError as exc:
            log.warning(
                "invalid Claude AskUserQuestion input",
                session_id=ctx.session_id,
                error=str(exc),
            )
            return PermissionResultDeny(
                message="Claude 的确认问题格式无效，已安全取消")

        answers: dict[str, str | list[str]] = {}
        try:
            # Hold the session-wide slot for the whole batch so a concurrent
            # approval/subagent question cannot interleave between its pages.
            async with ctx.ask_lock:
                for question in questions:
                    answer = await self._on_ask_locked(
                        ctx,
                        question.question,
                        list(question.options),
                        header=question.header,
                        multi_select=question.multi_select,
                    )
                    answers[question.question] = answer
        except AskUnavailable:
            return PermissionResultDeny(
                message="用户未完成 Claude 的确认问题")

        return PermissionResultAllow(updated_input={
            **tool_input,
            "answers": answers,
        })

    async def _on_claude_tool_permission(self, ctx: SessionContext,
                                         tool_name: str, tool_input: dict,
                                         permission_context):
        """Bridge Claude Agent SDK can_use_tool to the remote client."""
        if tool_name == "AskUserQuestion":
            return await self._on_claude_ask_user_question(ctx, tool_input)

        def short(value, limit: int = 1200) -> str:
            text = str(value or "").strip()
            return text if len(text) <= limit else text[:limit] + "…"

        lines = [f"Claude 请求使用工具：{short(tool_name, 240)}"]
        if tool_input:
            try:
                detail = json.dumps(tool_input, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                detail = repr(tool_input)
            lines.append(short(detail))
        if getattr(permission_context, "suggestions", None):
            lines.append("SDK 提供了可选权限建议；本次仅处理单次授权。")
        try:
            answer = await self._on_ask(ctx, "\n".join(lines), [
                {"label": "允许一次", "ds": "仅批准这一次工具调用"},
                {"label": "拒绝", "ds": "拒绝这次工具调用"},
            ])
        except AskUnavailable:
            return PermissionResultDeny(message="远程工具授权未完成")
        if answer == "允许一次":
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户拒绝了远程工具授权")

    async def _handle_answer_question(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return await self._missing_session_error(cmd, "回答交互问题")

        async def reject(code: str, message: str):
            error = Error(
                code=code,
                message=message,
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, error)
            return error

        fut = ctx.pending_asks.get(cmd.ask_id)
        if fut is None:
            log.warning("answer for unknown ask_id", ask_id=cmd.ask_id)
            return await reject(
                ERR_NOT_RUNNING, "该交互问题已经结束或不存在")
        if fut.done():
            log.warning("answer for already-done ask_id", ask_id=cmd.ask_id)
            return await reject(
                ERR_NOT_RUNNING, "该交互问题已经由其他客户端回答")

        spec = ctx.pending_ask_specs.get(cmd.ask_id)
        if spec is None:
            return await reject(ERR_INTERNAL, "交互问题状态不完整，请重新操作")
        answer = cmd.answer
        labels = spec["labels"]
        if isinstance(answer, list):
            if not spec["multi_select"]:
                return await reject(ERR_BAD_PROMPT, "该问题不支持多选")
            if len(set(answer)) != len(answer):
                return await reject(ERR_BAD_PROMPT, "多选答案不能包含重复选项")
            normalized: str | list[str] = answer
            values = answer
        else:
            if not answer.strip():
                return await reject(ERR_BAD_PROMPT, "回答不能为空")
            normalized = [answer] if spec["multi_select"] else answer
            values = [answer]
        if not spec["allow_text"] and any(value not in labels for value in values):
            return await reject(ERR_BAD_PROMPT, "回答不属于该问题的可选项")

        # No await occurs between the done check and set_result: the event loop
        # makes this the single atomic winner when multiple clients race.
        fut.set_result(normalized)
        log.info("ask_user answered", ask_id=cmd.ask_id)
        return None

    @staticmethod
    def _read_session_file(
        cwd: str,
        path: str,
        *,
        allowed_suffixes: Optional[frozenset[str]],
        max_bytes: int,
        allow_truncate: bool,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> tuple[str, bytes, os.stat_result, bool]:
        """Read a bounded regular file below cwd without following an escape.

        ``realpath`` contains relative or tool-reported absolute paths inside
        the session root while rejecting symlinks that escape it. ``O_NONBLOCK``
        prevents a malicious FIFO from hanging the wrapper before ``fstat`` can
        reject the special file.
        """
        if path.startswith("~"):
            raise ValueError("预览路径必须位于当前工作目录")

        root = os.path.realpath(cwd)
        candidate = os.path.realpath(
            path if os.path.isabs(path) else os.path.join(root, path))
        inside_root = WrapperMachine._path_is_below(root, candidate)
        if not inside_root and candidate not in allowed_external_paths:
            raise ValueError(
                "预览路径超出当前工作目录，且不是本会话成功创建或编辑的文件")

        suffix = os.path.splitext(candidate)[1].lower()
        if allowed_suffixes is not None and suffix not in allowed_suffixes:
            raise ValueError("不支持预览该文件类型")

        access_root = root if inside_root else os.path.dirname(candidate)
        relative = (os.path.relpath(candidate, root)
                    if inside_root else os.path.basename(candidate))
        display_path = relative.replace(os.sep, "/") if inside_root else candidate
        if relative in ("", "."):
            raise ValueError("预览目标必须是普通文件")
        parts = relative.split(os.sep)
        file_flags = os.O_RDONLY
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        dir_flags = os.O_RDONLY
        dir_flags |= getattr(os, "O_CLOEXEC", 0)
        dir_flags |= getattr(os, "O_DIRECTORY", 0)
        dir_flags |= getattr(os, "O_NOFOLLOW", 0)
        dir_fd: Optional[int] = None
        try:
            # Walk from an already-open cwd and refuse symlinks at every hop.
            # This closes the realpath/open race where a parent directory could
            # otherwise be replaced by an escaping symlink between both calls.
            dir_fd = os.open(access_root, dir_flags)
            for part in parts[:-1]:
                next_fd = os.open(part, dir_flags, dir_fd=dir_fd)
                os.close(dir_fd)
                dir_fd = next_fd
            fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
        except FileNotFoundError as exc:
            raise ValueError("文件不存在") from exc
        except PermissionError as exc:
            raise ValueError("没有权限读取该文件") from exc
        except OSError as exc:
            raise ValueError("无法打开该文件") from exc
        finally:
            if dir_fd is not None:
                os.close(dir_fd)

        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("预览目标必须是普通文件")
            if not allow_truncate and file_stat.st_size > max_bytes:
                raise ValueError(
                    f"预览文件超过 {max_bytes // (1024 * 1024)} MiB 限制")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(max_bytes + 1)
        finally:
            os.close(fd)

        truncated = len(data) > max_bytes or file_stat.st_size > max_bytes
        if truncated and not allow_truncate:
            raise ValueError(
                f"预览文件超过 {max_bytes // (1024 * 1024)} MiB 限制")
        return display_path, data[:max_bytes], file_stat, truncated

    @classmethod
    def _read_text_preview(
        cls, cwd: str, path: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> tuple[str, str, int, bool, int, str, Optional[str]]:
        relative, data, file_stat, truncated = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=None,
            max_bytes=FILE_PREVIEW_MAX_BYTES,
            allow_truncate=True,
            allowed_external_paths=allowed_external_paths,
        )
        if b"\0" in data:
            raise ValueError("文件不是可预览的 UTF-8 文本")
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        try:
            content = decoder.decode(data, final=not truncated)
        except UnicodeDecodeError as exc:
            raise ValueError("文件不是有效的 UTF-8 文本") from exc
        return (
            relative,
            content,
            file_stat.st_size,
            truncated,
            file_stat.st_mtime_ns,
            cls._preview_format(relative),
            None if truncated else hashlib.sha256(data).hexdigest(),
        )

    @classmethod
    def _read_file_preview(
        cls, cwd: str, path: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        """Read or render one artifact entirely on the wrapper host.

        The returned dictionary is short-lived and immediately serialized into
        a requester-routed WebSocket response. No artifact or converted preview
        is persisted by the relay/VPS.
        """
        suffix = os.path.splitext(path)[1].lower()
        if suffix in cls.OFFICE_PREVIEW_SUFFIXES:
            return cls._convert_office_preview(
                cwd, path, allowed_external_paths)

        if suffix in cls.HTML_PREVIEW_SUFFIXES:
            relative, data, file_stat, truncated = cls._read_session_file(
                cwd,
                path,
                allowed_suffixes=cls.HTML_PREVIEW_SUFFIXES,
                max_bytes=FILE_PREVIEW_MAX_BYTES,
                allow_truncate=True,
                allowed_external_paths=allowed_external_paths,
            )
            if b"\0" in data:
                raise ValueError("HTML 文件不是有效的 UTF-8 文本")
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            try:
                content = decoder.decode(data, final=not truncated)
            except UnicodeDecodeError as exc:
                raise ValueError("HTML 文件不是有效的 UTF-8 文本") from exc
            return {
                "path": relative,
                "format": "html",
                "content": content,
                "size": file_stat.st_size,
                "truncated": truncated,
                "mtime_ns": file_stat.st_mtime_ns,
                "revision": None if truncated else hashlib.sha256(data).hexdigest(),
            }

        media_type = cls.ARTIFACT_PREVIEW_MEDIA_TYPES.get(suffix)
        if media_type is not None:
            relative, data, file_stat, _ = cls._read_session_file(
                cwd,
                path,
                allowed_suffixes=frozenset(cls.ARTIFACT_PREVIEW_MEDIA_TYPES),
                max_bytes=ARTIFACT_PREVIEW_MAX_BYTES,
                allow_truncate=False,
                allowed_external_paths=allowed_external_paths,
            )
            cls._validate_rendered_preview(media_type, data)
            return {
                "path": relative,
                "format": "pdf" if media_type == "application/pdf" else "image",
                "media_type": media_type,
                "data": data,
                "size": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
            }

        relative, content, size, truncated, mtime_ns, file_format, revision = (
            cls._read_text_preview(cwd, path, allowed_external_paths))
        return {
            "path": relative,
            "format": file_format,
            "content": content,
            "size": size,
            "truncated": truncated,
            "mtime_ns": mtime_ns,
            "revision": revision,
        }

    @staticmethod
    def _validate_rendered_preview(media_type: str, data: bytes) -> None:
        valid = {
            "application/pdf": data.startswith(b"%PDF-"),
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": (len(data) >= 12 and data[:4] == b"RIFF"
                            and data[8:12] == b"WEBP"),
            "image/avif": (len(data) >= 12 and data[4:8] == b"ftyp"
                            and data[8:12] in {b"avif", b"avis"}),
        }.get(media_type, False)
        if not valid:
            raise ValueError("文件内容与预览格式不匹配")

    @classmethod
    def _convert_office_preview(
        cls, cwd: str, path: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        suffix = os.path.splitext(path)[1].lower()
        relative, data, file_stat, _ = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=cls.OFFICE_PREVIEW_SUFFIXES,
            max_bytes=cls.OFFICE_PREVIEW_INPUT_MAX_BYTES,
            allow_truncate=False,
            allowed_external_paths=allowed_external_paths,
        )
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        bwrap = shutil.which("bwrap")
        if not soffice:
            raise ValueError("本机未安装 LibreOffice，无法预览 Office 文件")
        if not bwrap:
            raise ValueError("本机未安装 bubblewrap，已拒绝不安全的 Office 转换")

        with tempfile.TemporaryDirectory(prefix="cc-remote-preview-") as temp:
            os.chmod(temp, 0o700)
            root = Path(temp)
            source = root / f"input{suffix}"
            output = root / "out"
            home = root / "home"
            output.mkdir(mode=0o700)
            home.mkdir(mode=0o700)
            source.write_bytes(data)
            source.chmod(0o600)

            command = [
                bwrap,
                "--die-with-parent",
                "--unshare-net",
                "--new-session",
                "--ro-bind", "/usr", "/usr",
                "--ro-bind", "/etc", "/etc",
                "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64",
                "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/sbin", "/sbin",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
                "--tmpfs", "/run",
                "--dev", "/dev",
                "--dir", "/mnt",
                "--bind", temp, "/mnt",
                "--chdir", "/mnt",
                "--setenv", "HOME", "/mnt/home",
                "--setenv", "TMPDIR", "/mnt/home",
                soffice,
                "-env:UserInstallation=file:///mnt/profile",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", "/mnt/out",
                f"/mnt/{source.name}",
            ]
            cls._run_office_conversion(command)
            candidates = list(output.glob("*.pdf"))
            if len(candidates) != 1:
                raise ValueError("Office 文件未能生成可预览的 PDF")
            converted = candidates[0]
            converted_stat = converted.lstat()
            if (not stat.S_ISREG(converted_stat.st_mode)
                    or converted.is_symlink()):
                raise ValueError("Office 转换结果无效")
            if converted_stat.st_size > ARTIFACT_PREVIEW_MAX_BYTES:
                raise ValueError("转换后的 PDF 超过 8 MiB 预览限制")
            preview = converted.read_bytes()
            cls._validate_rendered_preview("application/pdf", preview)
            return {
                "path": relative,
                "format": "pdf",
                "media_type": "application/pdf",
                "data": preview,
                "converted_from": suffix.removeprefix("."),
                "size": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
            }

    @classmethod
    def _run_office_conversion(cls, command: list[str]) -> None:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=cls.OFFICE_PREVIEW_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            finally:
                process.wait()
            raise ValueError("Office 预览转换超时") from exc
        if return_code != 0:
            raise ValueError("Office 文件转换失败")

    @classmethod
    def _write_markdown_file(
        cls,
        cwd: str,
        path: str,
        content: str,
        expected_size: int,
        expected_mtime_ns: int,
        expected_revision: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> tuple[str, int, int, str]:
        """CAS-style atomic save for one existing Markdown regular file.

        Every path component is opened relative to an already-open cwd with
        ``O_NOFOLLOW``. The replacement is written and fsynced in the same
        directory, then renamed over the file only after a second revision
        check. Existing UTF-8 BOM, dominant LF/CRLF style, and mode bits are
        preserved.
        """
        if path.startswith("~"):
            raise ValueError("保存路径必须位于当前工作目录")

        session_root = os.path.realpath(cwd)
        candidate = os.path.abspath(
            path if os.path.isabs(path) else os.path.join(session_root, path))
        real_candidate = os.path.realpath(candidate)
        lexical_inside = cls._path_is_below(session_root, candidate)
        inside_root = cls._path_is_below(session_root, real_candidate)
        if lexical_inside and not inside_root:
            raise ValueError("保存路径不能包含符号链接")
        if not inside_root:
            if real_candidate not in allowed_external_paths:
                raise ValueError(
                    "保存路径超出当前工作目录，且不是本会话成功创建或编辑的文件")
            candidate = real_candidate
            root = os.path.dirname(candidate)
            relative = os.path.basename(candidate)
            display_path = candidate
        else:
            root = session_root
            relative = os.path.relpath(candidate, root)
            display_path = relative.replace(os.sep, "/")

        if os.path.splitext(candidate)[1].lower() not in cls.MARKDOWN_PREVIEW_SUFFIXES:
            raise ValueError("只允许保存 Markdown 文件")
        parts = relative.split(os.sep)
        if relative in ("", ".") or any(part in ("", ".", "..") for part in parts):
            raise ValueError("保存路径无效")

        dir_flags = os.O_RDONLY
        dir_flags |= getattr(os, "O_CLOEXEC", 0)
        dir_flags |= getattr(os, "O_DIRECTORY", 0)
        dir_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY
        file_flags |= getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        dir_fd: Optional[int] = None
        temp_name: Optional[str] = None

        def read_current() -> tuple[bytes, os.stat_result, str]:
            try:
                fd = os.open(parts[-1], file_flags, dir_fd=dir_fd)
            except FileNotFoundError as exc:
                raise ValueError("文件不存在") from exc
            except PermissionError as exc:
                raise ValueError("没有权限保存该文件") from exc
            except OSError as exc:
                raise ValueError("保存目标不能是符号链接或特殊文件") from exc
            try:
                file_stat = os.fstat(fd)
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValueError("保存目标必须是普通文件")
                if file_stat.st_size > FILE_PREVIEW_MAX_BYTES:
                    raise ValueError("超过 512 KiB 的 Markdown 文件不可编辑")
                chunks = []
                remaining = FILE_PREVIEW_MAX_BYTES + 1
                while remaining > 0:
                    chunk = os.read(fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                final_stat = os.fstat(fd)
                if (final_stat.st_size != file_stat.st_size
                        or final_stat.st_mtime_ns != file_stat.st_mtime_ns):
                    revision = hashlib.sha256(data).hexdigest()
                    raise _FileRevisionConflict(
                        "文件正在被其他程序修改，请重新读取后再保存",
                        size=final_stat.st_size,
                        mtime_ns=final_stat.st_mtime_ns,
                        revision=revision,
                    )
                return data, final_stat, hashlib.sha256(data).hexdigest()
            finally:
                os.close(fd)

        def require_expected(file_stat: os.stat_result, revision: str) -> None:
            if (file_stat.st_size != expected_size
                    or file_stat.st_mtime_ns != expected_mtime_ns
                    or revision != expected_revision):
                raise _FileRevisionConflict(
                    "文件已在别处修改，请重新读取并合并后再保存",
                    size=file_stat.st_size,
                    mtime_ns=file_stat.st_mtime_ns,
                    revision=revision,
                )

        try:
            dir_fd = os.open(root, dir_flags)
            for part in parts[:-1]:
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=dir_fd)
                except OSError as exc:
                    raise ValueError("保存路径不能包含符号链接") from exc
                os.close(dir_fd)
                dir_fd = next_fd

            original, original_stat, original_revision = read_current()
            require_expected(original_stat, original_revision)
            if b"\0" in original:
                raise ValueError("Markdown 文件不是有效的 UTF-8 文本")
            try:
                original.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("Markdown 文件不是有效的 UTF-8 文本") from exc

            normalized = content.replace("\r\n", "\n").replace("\r", "\n")
            crlf_count = original.count(b"\r\n")
            lf_only_count = original.count(b"\n") - crlf_count
            newline = "\r\n" if crlf_count > lf_only_count else "\n"
            payload = normalized.replace("\n", newline).encode("utf-8")
            if original.startswith(codecs.BOM_UTF8):
                payload = codecs.BOM_UTF8 + payload
            if len(payload) > FILE_PREVIEW_MAX_BYTES:
                raise ValueError("保存内容超过 512 KiB 限制")

            temp_name = f".cc-remote-{uuid4().hex}.tmp"
            temp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            temp_flags |= getattr(os, "O_CLOEXEC", 0)
            temp_flags |= getattr(os, "O_NOFOLLOW", 0)
            temp_fd = os.open(temp_name, temp_flags, 0o600, dir_fd=dir_fd)
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    written += os.write(temp_fd, view[written:])
                os.fchmod(temp_fd, stat.S_IMODE(original_stat.st_mode))
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)

            _, latest_stat, latest_revision = read_current()
            require_expected(latest_stat, latest_revision)
            os.replace(
                temp_name,
                parts[-1],
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            temp_name = None
            os.fsync(dir_fd)
            saved_stat = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
            return (
                display_path,
                saved_stat.st_size,
                saved_stat.st_mtime_ns,
                hashlib.sha256(payload).hexdigest(),
            )
        finally:
            if temp_name is not None and dir_fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            if dir_fd is not None:
                os.close(dir_fd)

    @classmethod
    def _read_markdown_preview(
        cls, cwd: str, path: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> tuple[str, str, int, bool, int]:
        relative, data, file_stat, truncated = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=cls.MARKDOWN_PREVIEW_SUFFIXES,
            max_bytes=FILE_PREVIEW_MAX_BYTES,
            allow_truncate=True,
            allowed_external_paths=allowed_external_paths,
        )
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        try:
            content = decoder.decode(data, final=not truncated)
        except UnicodeDecodeError as exc:
            raise UnicodeDecodeError(
                exc.encoding,
                exc.object,
                exc.start,
                exc.end,
                "Markdown 文件不是有效的 UTF-8",
            ) from exc
        return relative, content, file_stat.st_size, truncated, file_stat.st_mtime_ns

    @classmethod
    def _preview_format(cls, path: str) -> str:
        suffix = os.path.splitext(path)[1].lower()
        if suffix in cls.MARKDOWN_PREVIEW_SUFFIXES:
            return "markdown"
        if suffix in cls.HTML_PREVIEW_SUFFIXES:
            return "html"
        if suffix in cls.OFFICE_PREVIEW_SUFFIXES or suffix == ".pdf":
            return "pdf"
        if suffix in cls.PREVIEW_ASSET_MEDIA_TYPES:
            return "image"
        return "text"

    @classmethod
    def _read_preview_asset(
        cls, cwd: str, path: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> tuple[str, str, bytes]:
        suffix = os.path.splitext(path)[1].lower()
        media_type = cls.PREVIEW_ASSET_MEDIA_TYPES.get(suffix)
        if media_type is None:
            raise ValueError("Markdown 预览只加载 PNG、JPEG、GIF、WebP 或 AVIF 图片")
        relative, data, _, _ = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=frozenset(cls.PREVIEW_ASSET_MEDIA_TYPES),
            max_bytes=PREVIEW_ASSET_MAX_BYTES,
            allow_truncate=False,
            allowed_external_paths=allowed_external_paths,
        )
        return relative, media_type, data

    async def _git_diff(
        self, cwd: str, file: str,
        allowed_external_paths: frozenset[str] = frozenset(),
    ) -> str:
        max_bytes = max(64 * 1024, min(4 * 1024 * 1024,
                                       self.cfg.ws_max_size_bytes // 2))
        return await read_git_diff(
            cwd,
            file,
            allowed_external_paths=allowed_external_paths,
            max_bytes=max_bytes,
            source_max_bytes=getattr(
                self.cfg, "history_source_max_bytes", 64 * 1024 * 1024),
            run_command=self._bounded_process_output,
        )

    @staticmethod
    async def _bounded_process_output(
        argv: tuple[str, ...], max_bytes: int, timeout: float = 10.0,
    ) -> str:
        return await bounded_process_output(argv, max_bytes, timeout)

    # ---- sessions (list / switch / new) ----

    async def _handle_list_sessions(self, cmd) -> None:
        engine = getattr(cmd, "engine", "claude")
        space = getattr(cmd, "space", "code")
        if engine == "codex":
            return await self._list_codex_sessions(cmd)
        # Claude may create the fork transcript before its init/session id reaches
        # our turn consumer. Until capture durably tombstones that real id, scanning
        # the global session store could publish it to another client. Fail closed
        # for this one requester; never broadcast a control-plane privacy error.
        if any(
            ctx.btw and ctx.engine != "codex" and not ctx.btw_real_id
            for ctx in self.sessions.values()
        ):
            client_id = getattr(cmd, "client_id", None)
            if client_id:
                error = Error(
                    code=ERR_BUSY,
                    message="临时 btw 会话正在初始化，请稍后刷新会话列表",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=client_id,
                )
                await self.transport.send(error)
                return error
            log.warning("Claude session list withheld during btw id capture",
                        client_id=client_id)
            return
        try:
            infos = await asyncio.to_thread(list_sessions, limit=200)
            blocked = await asyncio.to_thread(self._bg_blocked_session_ids)
            private_btw_ids = set(self._private_btw_sessions)
            resident_ids = {c.session_id for c in self.sessions.values() if c.session_id}
            resident_state = {c.session_id: c.state for c in self.sessions.values() if c.session_id}
            work_records = await asyncio.to_thread(
                self._work.for_engine("claude").records_by_session)
            pinned_ids = (self._session_pins.ids("claude")
                          if self._session_pins is not None else frozenset())
            sessions = []
            for info in infos:
                record = work_records.get(info.session_id)
                if ((record is not None) != (space == "work")
                        or info.session_id in blocked
                        or info.session_id in private_btw_ids):
                    continue
                sessions.append(SessionInfo(
                    session_id=info.session_id,
                    summary=(
                        (record.title if record else None)
                        or (info.custom_title if hasattr(info, "custom_title") else None)
                        or info.first_prompt
                        or info.summary
                        or ""
                    )[:500] or None,
                    last_modified=str(info.last_modified) if info.last_modified else None,
                    first_prompt=(info.first_prompt or "")[:2000] or None,
                    git_branch=(info.git_branch or "")[:500] or None,
                    cwd=(info.cwd or "")[:4096] or None,
                    tag=("archived" if record and record.archived else
                         (info.tag or "")[:128] or None),
                    pinned=info.session_id in pinned_ids,
                    state=resident_state.get(info.session_id),
                    engine="claude", space=space,
                    work_id=record.work_id if record else None,
                ))
            if space == "code" and self._claude_broker_enabled:
                # `claude-remote new` reserves the native session UUID before
                # Claude writes its first transcript row. Merge live broker
                # metadata so the TUI is immediately selectable in the sidebar;
                # once JSONL exists the ordinary catalog row wins.
                try:
                    broker_response = await self._claude_broker.list()
                except BrokerClientError as exc:
                    if exc.code not in {"broker_unavailable", "session_not_found"}:
                        log.warning(
                            "Claude broker list unavailable",
                            error_code=exc.code,
                        )
                else:
                    known = {item.session_id for item in sessions}
                    broker_sessions = broker_response.get("sessions")
                    if isinstance(broker_sessions, list):
                        for row in broker_sessions:
                            if not isinstance(row, dict) or row.get("running") is not True:
                                continue
                            broker_sid = row.get("id")
                            broker_cwd = row.get("cwd")
                            if (not isinstance(broker_sid, str) or not broker_sid
                                    or len(broker_sid) > 256
                                    or broker_sid in known
                                    or not isinstance(broker_cwd, str)
                                    or not broker_cwd):
                                continue
                            sessions.append(SessionInfo(
                                session_id=broker_sid,
                                summary="Claude Remote",
                                cwd=broker_cwd[:4096],
                                state=resident_state.get(broker_sid, "idle"),
                                pinned=broker_sid in pinned_ids,
                                engine="claude",
                                space="code",
                            ))
                            known.add(broker_sid)
            for session in sessions:
                self._remember_notification_title(
                    session.session_id, session.summary or session.first_prompt)
            event = SessionList(
                engine="claude",
                space=space,
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(event)
            log.info("listed sessions", count=len(sessions), resident=len(resident_ids),
                     client_id=getattr(cmd, "client_id", None))
            return event
        except Exception as e:
            log.exception("list_sessions failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="会话列表暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error

    def _invalidate_codex_session_catalog(self) -> None:
        self._codex_session_list_cache = None
        self._codex_session_list_epoch += 1

    async def _refresh_codex_session_catalog(self) -> list[dict]:
        """Share one cold app-server catalog read across concurrent clients."""
        epoch = self._codex_session_list_epoch
        task = self._codex_session_list_refresh_task
        if (task is None or task.done()
                or self._codex_session_list_refresh_epoch != epoch):
            task = asyncio.create_task(list_codex_sessions(200))
            self._codex_session_list_refresh_task = task
            self._codex_session_list_refresh_epoch = epoch
        try:
            raw = await asyncio.shield(task)
        finally:
            if task.done() and self._codex_session_list_refresh_task is task:
                self._codex_session_list_refresh_task = None
                self._codex_session_list_refresh_epoch = -1
        if epoch != self._codex_session_list_epoch:
            # A create/rename/archive committed while this snapshot was being
            # built. Never let the old rows replace the optimistic temp row.
            return await self._refresh_codex_session_catalog()
        self._codex_session_list_cache = (time.monotonic(), raw)
        return raw

    async def _list_codex_sessions(self, cmd) -> None:
        """Paint cached rows now, then reconcile a stale catalog in background."""
        cached = self._codex_session_list_cache
        cached_event = None
        if cached is not None:
            cached_event = await self._send_codex_session_list(cmd, cached[1])
            # Code and Work are normally requested back-to-back. One second
            # preserves that reuse while every later visit still reconciles
            # native CLI/Desktop changes after painting the cache.
            if time.monotonic() - cached[0] <= 1.0:
                return cached_event
        try:
            raw = await self._refresh_codex_session_catalog()
        except Exception as e:
            if cached_event is not None:
                log.warning("stale Codex session catalog refresh failed",
                            error_type=type(e).__name__)
                return cached_event
            log.exception("list_codex_sessions failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="Codex 会话列表暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if cached is not None and raw == cached[1]:
            return cached_event
        return await self._send_codex_session_list(cmd, raw)

    async def _send_codex_session_list(self, cmd, raw: list[dict]) -> None:
        """Filter and route one already-read native Codex catalog."""
        try:
            self._prime_codex_sidebar_watches(raw)
            resident_state = {c.session_id: c.state for c in self.sessions.values()
                              if c.session_id and c.engine == "codex"}
            resident_cwd = {c.session_id: c.cwd for c in self.sessions.values()
                            if c.session_id and c.engine == "codex"}
            space = getattr(cmd, "space", "code")
            cwd_overrides = (
                self._codex_controls.cwd_overrides()
                if space == "code" and self._codex_controls is not None
                else {}
            )
            if cwd_overrides:
                listed_ids = {
                    row.get("session_id")
                    for row in raw
                    if isinstance(row.get("session_id"), str)
                }
                valid_overrides: dict[str, str] = {}
                for session_id in listed_ids:
                    override = cwd_overrides.get(session_id)
                    if override is None:
                        continue
                    try:
                        controls = await self._reconcile_codex_cwd_override(
                            session_id,
                            CodexControls(cwd_override=override),
                        )
                    except Exception as exc:
                        log.warning(
                            "Codex sidebar cwd override could not be reconciled",
                            session_id=session_id,
                            error_type=type(exc).__name__,
                        )
                        continue
                    if controls.cwd_override is not None:
                        valid_overrides[session_id] = controls.cwd_override
                cwd_overrides = valid_overrides
            store = self._work.for_engine("codex")
            work_records = await asyncio.to_thread(store.records_by_session)
            pinned_ids = (self._session_pins.ids("codex")
                          if self._session_pins is not None else frozenset())
            # thread/start commits the native rollout before WorkRegistry can bind
            # it. If the wrapper dies in that narrow window, recover only an
            # unambiguous exact-cwd match inside the private Work root.
            unbound = await asyncio.to_thread(store.unbound_records_by_cwd)
            rows_by_cwd: dict[str, list[dict]] = {}
            for row in raw:
                row_cwd = row.get("cwd")
                if isinstance(row_cwd, str) and row_cwd:
                    rows_by_cwd.setdefault(os.path.realpath(row_cwd), []).append(row)
            for registered_cwd, record in unbound.items():
                matches = rows_by_cwd.get(registered_cwd, [])
                if len(matches) != 1:
                    if len(matches) > 1:
                        log.warning(
                            "ambiguous unbound Codex Work session",
                            work_id=record.work_id, matches=len(matches))
                    continue
                sid = matches[0].get("session_id")
                if not isinstance(sid, str) or not sid or sid in work_records:
                    continue
                try:
                    await asyncio.to_thread(store.bind_session, record.work_id, sid)
                except Exception:
                    log.exception(
                        "Codex Work session reconciliation failed",
                        work_id=record.work_id)
                    continue
                recovered = await asyncio.to_thread(store.get_by_session, sid)
                if recovered is not None:
                    work_records[sid] = recovered
                log.info(
                    "reconciled Codex Work session",
                    session_id=sid, work_id=record.work_id)
            sessions = []
            for row in raw:
                record = work_records.get(row["session_id"])
                if (record is not None) != (space == "work"):
                    continue
                sessions.append(SessionInfo(
                    session_id=row["session_id"],
                    summary=(record.title if record and record.title
                             else row.get("summary")),
                    first_prompt=row.get("first_prompt"),
                    cwd=(
                        resident_cwd.get(row["session_id"])
                        or cwd_overrides.get(row["session_id"])
                        or row.get("cwd")
                    ),
                    last_modified=row.get("last_modified"),
                    git_branch=row.get("git_branch"),
                    tag=("archived" if record and record.archived
                         else row.get("tag")),
                    pinned=row["session_id"] in pinned_ids,
                    engine="codex", space=space,
                    work_id=record.work_id if record else None,
                    state=(resident_state.get(row["session_id"])
                           or self._codex_sidebar_watch_state(
                               self._watch.get(row["session_id"]))
                           or _codex_list_state(row.get("status"))),
                    forked_from_id=row.get("forked_from_id"),
                    codex_status=row.get("status"),
                ))
            for session in sessions:
                self._remember_notification_title(
                    session.session_id, session.summary or session.first_prompt)
            event = SessionList(
                engine="codex",
                space=space,
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(event)
            log.info("listed codex sessions", count=len(sessions),
                     client_id=getattr(cmd, "client_id", None))
            return event
        except Exception as e:
            log.exception("list_codex_sessions failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="Codex 会话列表暂不可用，请稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error

    async def _handle_switch_session(self, cmd) -> None:
        # Focus change — NO disconnect. If the session is already resident, just
        # focus it (its turn keeps running in the background). If not resident,
        # spawn (resume) it. The previously-focused session is NOT interrupted.
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        engine = getattr(cmd, "engine", None) or "claude"
        requested_space = getattr(cmd, "space", "code")
        work_record = await asyncio.to_thread(
            self._work.for_engine(engine).get_by_session, sid)
        actual_space = "work" if work_record is not None else "code"
        if requested_space != actual_space:
            error = Error(
                code=ERR_AUTH,
                message="会话不属于当前 Work/Code 空间",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self.sessions.get(sid)
        if ctx is None:
            ctx = next((c for c in self.sessions.values() if c.session_id == sid), None)
        if ctx is None and engine == "claude" and actual_space == "code":
            # Claude's catalog accepts metadata-only JSONL files (for example,
            # an ai-title row) even though the native CLI cannot resume them:
            # there is no message cwd or conversation chain to restore.  Catch
            # that exact on-disk state before _spawn so one click produces one
            # session-scoped error instead of both _spawn's focused error and
            # this handler's fallback error.
            info = await asyncio.to_thread(get_session_info, sid)
            if (info is not None and not getattr(info, "cwd", None)
                    and transcript_path(sid) is not None):
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message=(
                        "Claude 会话历史不完整，无法恢复；"
                        "可从会话菜单删除该条目。"
                    ),
                    request_id=getattr(cmd, "cmd_id", None),
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        if (ctx is not None
                and getattr(ctx.sdk, "is_claude_broker", False)):
            # Terminal exit recovery is an in-place SDK handoff. Transport
            # uncertainty intentionally keeps this exact context read-only;
            # evicting it here would turn a tab switch into an unsafe second
            # writer and was why the old UI only recovered after switching.
            await self._refresh_claude_broker_handle(ctx)
        newly_spawned = ctx is None
        if ctx is None:
            ctx = await self._spawn(
                resume_id=sid, engine=engine, space=actual_space,
                work_id=work_record.work_id if work_record else None)
            if ctx is None:
                # Surface it on the session the user switched INTO (not the stale
                # focused one). _spawn has already emitted the specific cause;
                # this durable session-scoped row only closes the loading state.
                engine_name = "Codex" if engine == "codex" else "Claude"
                error = Error(
                    code=ERR_CC_CRASH,
                    message=f"{engine_name} 会话暂时无法打开，请稍后重试。",
                )
                await self._emit_to_sid(sid, error)
                return error
        self.focused_sid = ctx.key
        # A newly-spawned session isn't tracked by the client yet — send its
        # snapshot + full replay so the client builds a runtime for it (else the
        # client would show an empty/wrong view).
        snap = None
        if newly_spawned:
            # Build the client's runtime for a freshly-resumed session with a
            # lightweight Snapshot (state/cwd/id); its HISTORY arrives via the
            # client's GetHistory request — no full buffer replay (that was a flood).
            snap = Snapshot(cc_session_id=ctx.session_id,
                            state=ctx.buffer.latest_state() or ctx.state,
                            tail_text=ctx.buffer.latest_tail_text(), cwd=ctx.cwd,
                            generation=self.instance_id,
                            control=self._session_control(ctx))
            await self._emit(ctx, snap)
        focus = SessionFocus(
            session_id=ctx.session_id or self.focused_sid or sid, cwd=ctx.cwd)
        await self._emit(ctx, focus)
        cached_responses = [snap, focus] if snap is not None else [focus]
        control_event = self._session_control(ctx)
        await self._emit(ctx, control_event)
        cached_responses.append(control_event)
        permission_mode = _session_permission_mode(ctx)
        ctx.announced_perm = permission_mode
        permission_event = Perm(mode=permission_mode)
        await self._emit(ctx, permission_event)
        cached_responses.append(permission_event)
        if ctx.engine != "codex":
            model = _session_model(ctx)
            if model:
                ctx.announced_model = model
                model_event = Model(model=model)
                await self._emit(ctx, model_event)
                cached_responses.append(model_event)
            effort = _session_effort(ctx)
            if effort:
                ctx.announced_effort = effort
                effort_event = Effort(effort=effort)
                await self._emit(ctx, effort_event)
                cached_responses.append(effort_event)
        # Seed the chips on entering a Codex session from the authoritative
        # thread-local settings restored in _spawn. Without the Model frame the composer falls back
        # to the engine's first model, so a luna session came up labelled "Sol".
        if ctx.engine == "codex":
            permission_profile = _session_permission_profile(ctx)
            ctx.announced_permission_profile = permission_profile
            profile_event = PermissionProfile(
                profile=permission_profile)
            await self._emit(ctx, profile_event)
            cached_responses.append(profile_event)
            web_search = _session_web_search(ctx)
            if web_search:
                ctx.announced_web_search = web_search
                search_event = WebSearch(mode=web_search)
                await self._emit(ctx, search_event)
                cached_responses.append(search_event)
            fast_event = Fast(on=_codex_fast_on(ctx.sdk.service_tier))
            await self._emit(ctx, fast_event)
            cached_responses.append(fast_event)
            collaboration_mode = getattr(
                ctx.sdk, "collaboration_mode", "default")
            ctx.announced_collaboration_mode = collaboration_mode
            collaboration_event = CollaborationMode(
                mode=collaboration_mode)
            await self._emit(ctx, collaboration_event)
            cached_responses.append(collaboration_event)
            if ctx.sdk.model:
                ctx.announced_model = ctx.sdk.model
                model_event = Model(model=ctx.sdk.model)
                await self._emit(ctx, model_event)
                cached_responses.append(model_event)
            if ctx.sdk.effort:
                ctx.announced_effort = ctx.sdk.effort
                effort_event = Effort(effort=ctx.sdk.effort)
                await self._emit(ctx, effort_event)
                cached_responses.append(effort_event)
            # Snapshot/SessionFocus has now created the browser runtime. Release
            # initialize-time warnings only after that routing state exists.
            await ctx.sdk.activate_runtime_events()
        return tuple(cached_responses)

    async def _capture_session_id(self, ctx: SessionContext, sid: str) -> None:
        """A brand-new session learned its real cc id (from the first
        ResultMessage/init). Re-key the pool temp-key -> sid, keep ctx.key in
        sync, migrate focus ONLY if this ctx was the focused one, and tell the
        client to re-key its runtime (SessionRekey — NOT SessionFocus, else a
        background session's capture would steal the user's view)."""
        # /btw forks keep their stable `btw-<uuid>` pool key so their events always
        # route to the side panel; they're ephemeral (never resumed/saved/re-keyed).
        # Record the real forked id (cc fork_session persists one) for close-time
        # cleanup, but don't route/save under it.
        if ctx.btw:
            if ctx.btw_real_id == sid:
                return
            if ctx.engine == "codex":
                ctx.btw_real_id = sid
                return
            try:
                # Durably hide the real Claude transcript before publishing it
                # even into the live ctx. A crash after this point remains safe.
                self._remember_private_btw(sid, ctx.cwd)
            except Exception as persist_error:
                # Keep a live guard while we fail-stop and delete the fork. This
                # entry may not be durable, so the private session is not allowed
                # to continue running after the persistence failure.
                ctx.btw_real_id = sid
                self._private_btw_sessions[sid] = {
                    "cwd": ctx.cwd, "created_at": time.time(),
                }
                await self._discard_query_queue(ctx)
                self.sessions.pop(ctx.key, None)
                log.error("private btw persistence failed; terminating fork",
                          error_type=type(persist_error).__name__)
                disconnected = False
                try:
                    await ctx.sdk.disconnect()
                    disconnected = True
                except Exception as disconnect_error:
                    log.warning("failed private btw disconnect failed",
                                error_type=type(disconnect_error).__name__)
                deleted = False
                try:
                    await asyncio.to_thread(
                        delete_session, sid, directory=ctx.cwd)
                except FileNotFoundError:
                    deleted = True
                except Exception as delete_error:
                    log.error("failed private btw transcript could not be deleted",
                              error_type=type(delete_error).__name__)
                    # A transient state-dir failure may have cleared by now. Retry
                    # the tombstone write; if it still fails, RAM remains fail-closed
                    # for the lifetime of this wrapper process.
                    try:
                        self._persist_private_btw_sessions()
                    except RuntimeError:
                        pass
                else:
                    deleted = True
                if disconnected and deleted:
                    # No live writer and no transcript remain. A stale tombstone may
                    # still exist on disk if replace succeeded before fsync failed;
                    # that is harmless and startup cleanup will remove it.
                    self._private_btw_sessions.pop(sid, None)
                raise RuntimeError(
                    "private btw state persistence failed; fork terminated"
                ) from persist_error
            ctx.btw_real_id = sid
            return
        old_key = ctx.key
        ctx.session_id = sid
        old_title = self._notification_titles.pop(old_key, None) if old_key else None
        if old_title:
            self._remember_notification_title(sid, old_title)
        if ctx.space == "work" and ctx.work_id:
            store = self._work.for_engine(ctx.engine)
            await asyncio.to_thread(store.bind_session, ctx.work_id, sid)
            if (ctx.work_context_baseline_pending
                    and ctx.work_context_baseline_tokens is not None):
                ctx.work_context_baseline_tokens = await asyncio.to_thread(
                    store.set_context_baseline,
                    ctx.work_id, ctx.work_context_baseline_tokens,
                )
                ctx.work_context_baseline_pending = False
        if ctx.engine == "codex":
            self._invalidate_codex_session_catalog()
        if ctx.engine != "codex":
            rekey_goal = getattr(ctx.sdk, "rekey_goal", None)
            if rekey_goal is not None:
                rekey_goal(sid)
            save_session_id(self.cfg.state_dir, ctx.cwd, sid)
        if old_key and old_key != sid:
            self._remember_session_alias(old_key, sid, ctx.cwd)
            self.sessions.pop(old_key, None)
            self.sessions[sid] = ctx
            ctx.key = sid
            if self.focused_sid == old_key:
                self.focused_sid = sid
            await self._emit(ctx, SessionRekey(old_key=old_key, session_id=sid, cwd=ctx.cwd))
            self._rekey_cached_create_responses(old_key, sid, ctx.cwd)
        if ctx.engine == "claude":
            # The real id becomes visible before the first turn necessarily ends.
            # Start ownership monitoring at capture so a terminal resume during
            # that first response cannot wait until the next Remote query.
            self._watch_session(sid)
            await self._persist_claude_session_controls(ctx)
        log.info("captured cc session id", sid=sid, focus_followed=(self.focused_sid == sid))

    async def _handle_new_session(self, cmd) -> None:
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message="附件不符合要求，请调整后重试。",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        engine = getattr(cmd, "engine", "claude")
        space = getattr(cmd, "space", "code")
        requested_model = getattr(cmd, "model", None)
        if engine == "claude":
            requested_model = _normalize_claude_new_session_model(
                requested_model)
        work_record = None
        target_cwd = getattr(cmd, "cwd", None)
        if space == "work":
            try:
                work_record = await asyncio.to_thread(
                    self._work.for_engine(engine).create_session,
                    getattr(cmd, "project_id", None))
            except Exception:
                log.exception("Work session directory creation failed",
                              engine=engine)
                error = Error(
                    code=ERR_INTERNAL,
                    message="Work 私有目录创建失败",
                    request_id=getattr(cmd, "request_id", None),
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            target_cwd = work_record.cwd

        try:
            ctx = await self._spawn(
                resume_id=None,
                cwd=target_cwd,
                engine=engine,
                model=requested_model,
                effort=getattr(cmd, "effort", None),
                collaboration_mode=getattr(cmd, "collaboration_mode", None),
                permission_mode=getattr(cmd, "permission_mode", None),
                permission_profile=getattr(cmd, "permission_profile", None),
                web_search=getattr(cmd, "web_search", None),
                service_tier=getattr(cmd, "service_tier", None),
                space=space,
                work_id=(work_record.work_id if work_record else None),
                raise_on_failure=True,
            )
        except _SpawnFailure as exc:
            ctx = None
            failure = exc
        except Exception:
            log.exception("new session spawn failed unexpectedly", engine=engine)
            ctx = None
            failure = _SpawnFailure(
                ERR_CC_CRASH, "新会话暂时无法启动，请稍后重试。")
        else:
            failure = None
        if ctx is None:
            if work_record is not None:
                await asyncio.to_thread(
                    self._work.for_engine(engine).abandon,
                    work_record.work_id)
            error = Error(
                code=(failure.code if failure else ERR_CC_CRASH),
                message=(failure.message if failure else
                         "新会话启动失败，请检查工作目录和引擎配置"),
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        self.focused_sid = ctx.key
        # Establish the temp runtime's wrapper lifetime before any other
        # sequenced frame. Otherwise its cursor has no paired generation and a
        # normal transport reconnect is mistaken for a wrapper restart.
        snap = Snapshot(
            cc_session_id=None,
            state=ctx.state,
            tail_text=ctx.buffer.latest_tail_text(),
            cwd=ctx.cwd,
            generation=self.instance_id,
            control=self._session_control(ctx),
        )
        await self._emit(ctx, snap)
        # session_id is None until captured in _run_turn; use the pool (temp) key
        # as the focus id — the client migrates to the real sid on capture.
        focus = SessionFocus(
            session_id=self.focused_sid,
            cwd=ctx.cwd,
            request_id=getattr(cmd, "request_id", None),
            to=getattr(cmd, "client_id", None),
        )
        await self._emit(ctx, focus)
        # _spawn records explicit picks as already announced. Emit them now that
        # SessionFocus has created the temp-keyed client runtime; otherwise the
        # removed client-side pending-query effect would leave its chips stale.
        cached_responses = [snap, focus]
        initial_model = _session_model(ctx)
        if initial_model:
            ctx.announced_model = initial_model
            model_event = Model(model=initial_model)
            await self._emit(ctx, model_event)
            cached_responses.append(model_event)
        initial_effort = _session_effort(ctx)
        if initial_effort:
            ctx.announced_effort = initial_effort
            effort_event = Effort(effort=initial_effort)
            await self._emit(ctx, effort_event)
            cached_responses.append(effort_event)
        permission_mode = _session_permission_mode(ctx)
        ctx.announced_perm = permission_mode
        permission_event = Perm(mode=permission_mode)
        await self._emit(ctx, permission_event)
        cached_responses.append(permission_event)
        # Collaboration mode is a separate Codex-only control.
        if ctx.engine == "codex":
            permission_profile = _session_permission_profile(ctx)
            ctx.announced_permission_profile = permission_profile
            profile_event = PermissionProfile(
                profile=permission_profile)
            await self._emit(ctx, profile_event)
            cached_responses.append(profile_event)
            web_search = _session_web_search(ctx)
            if web_search:
                ctx.announced_web_search = web_search
                search_event = WebSearch(mode=web_search)
                await self._emit(ctx, search_event)
                cached_responses.append(search_event)
            collaboration_mode = getattr(
                ctx.sdk, "collaboration_mode", "default")
            ctx.announced_collaboration_mode = collaboration_mode
            collaboration_event = CollaborationMode(
                mode=collaboration_mode)
            await self._emit(ctx, collaboration_event)
            cached_responses.append(collaboration_event)
            fast_event = Fast(on=_codex_fast_on(ctx.sdk.service_tier))
            await self._emit(ctx, fast_event)
            cached_responses.append(fast_event)
            # SessionFocus precedes this point, so the temp-keyed browser runtime
            # exists before initialize/config warnings are released.
            await ctx.sdk.activate_runtime_events()

        prompt = getattr(cmd, "prompt", None)
        images = getattr(cmd, "images", None)
        files = getattr(cmd, "files", None)
        if prompt is not None or images or files:
            # Explicit sid is the atomicity guarantee: even if focus changes while
            # the create response is in flight, the first turn targets this ctx.
            query_result = await self._handle_query(Query(
                sid=ctx.key,
                prompt=prompt or "",
                msg_id=cmd.msg_id,
                images=images,
                files=files,
            ))
            if getattr(query_result, "type", None):
                cached_responses.append(query_result)
        if space == "work" and ctx.session_id:
            # A newly durable Work item belongs in the sidebar immediately; do
            # not wait for a later engine/space toggle to request another list.
            await self._handle_list_sessions(ListSessions(
                engine=engine,
                space="work",
                client_id=getattr(cmd, "client_id", None),
            ))
        return tuple(cached_responses)

    async def _handle_rename_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        requested_engine = getattr(cmd, "engine", None)
        if requested_engine is not None and requested_engine != engine:
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的引擎",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        work_record = await asyncio.to_thread(
            self._work.for_engine(engine).get_by_session, sid)
        requested_space = getattr(cmd, "space", "code")
        if (work_record is not None) != (requested_space == "work"):
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的 Work/Code 空间",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if is_codex:
            self._invalidate_codex_session_catalog()
            try:
                await codex_rpc("thread/name/set", {
                    "threadId": sid, "name": cmd.title,
                })
                if work_record is not None:
                    await asyncio.to_thread(
                        self._work.for_engine(engine).update_title,
                        sid, cmd.title)
                self._remember_notification_title(sid, cmd.title)
                log.info("codex session renamed", session_id=sid,
                         title_length=len(cmd.title))
                return await self._list_codex_sessions(cmd)
            except Exception as e:
                log.exception("codex rename_session failed", error=str(e))
                error = Error(
                    code=ERR_INTERNAL, message="会话重命名未完成，请重试。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None))
                await self._emit_to_sid(sid, error)
                listing = await self._list_codex_sessions(cmd)
                return error, listing
        try:
            await asyncio.to_thread(rename_session, sid, cmd.title)
            if work_record is not None:
                await asyncio.to_thread(
                    self._work.for_engine(engine).update_title,
                    sid, cmd.title)
            self._remember_notification_title(sid, cmd.title)
            # our own append -> re-baseline, else the watcher calls it an external write
            self._resync_watch(sid)
            log.info("session renamed", session_id=sid,
                     title_length=len(cmd.title))
            return await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("rename_session failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL, message="会话重命名未完成，请重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error

    async def _handle_archive_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        requested_engine = getattr(cmd, "engine", None)
        if requested_engine is not None and requested_engine != engine:
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的引擎",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        work_record = await asyncio.to_thread(
            self._work.for_engine(engine).get_by_session, sid)
        requested_space = getattr(cmd, "space", "code")
        if (work_record is not None) != (requested_space == "work"):
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的 Work/Code 空间",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if is_codex:
            self._invalidate_codex_session_catalog()
            method = "thread/archive" if cmd.archived else "thread/unarchive"
            try:
                await codex_rpc(method, {"threadId": sid})
                if work_record is not None:
                    await asyncio.to_thread(
                        self._work.for_engine(engine).update_archived,
                        sid, cmd.archived)
                log.info("codex session archive toggled", session_id=sid,
                         archived=cmd.archived)
                return await self._list_codex_sessions(cmd)
            except Exception as e:
                log.exception("codex archive_session failed", error=str(e))
                error = Error(
                    code=ERR_INTERNAL, message="会话归档未完成，请重试。",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None))
                await self._emit_to_sid(sid, error)
                # The UI waits for this authoritative result instead of moving
                # the card optimistically. It also reconciles a timeout where the
                # app-server committed the mutation but its response was lost.
                listing = await self._list_codex_sessions(cmd)
                return error, listing
        try:
            tag = "archived" if cmd.archived else None
            await asyncio.to_thread(tag_session, sid, tag)
            if work_record is not None:
                await asyncio.to_thread(
                    self._work.for_engine(engine).update_archived,
                    sid, cmd.archived)
            # our own append -> re-baseline (see _resync_watch)
            self._resync_watch(sid)
            log.info("session archive toggled", session_id=sid, archived=cmd.archived)
            return await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("archive_session failed", error=str(e))
            error = Error(
                code=ERR_INTERNAL, message="会话归档未完成，请重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error

    async def _handle_pin_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        requested_engine = getattr(cmd, "engine", None)
        if requested_engine is not None and requested_engine != engine:
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的引擎",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        work_record = await asyncio.to_thread(
            self._work.for_engine(engine).get_by_session, sid)
        requested_space = getattr(cmd, "space", "code")
        if (work_record is not None) != (requested_space == "work"):
            error = Error(
                code=ERR_AUTH, message="会话不属于请求的 Work/Code 空间",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            return error
        if (work_record is None and self._ctx_by_sid(sid) is None
                and not is_codex):
            info = await asyncio.to_thread(get_session_info, sid)
            if info is None:
                error = Error(
                    code=ERR_NOT_RUNNING, message="Claude 会话不存在",
                    request_id=getattr(cmd, "cmd_id", None),
                    to=getattr(cmd, "client_id", None))
                await self._emit_to_sid(sid, error)
                listing = await self._handle_list_sessions(cmd)
                return error, listing
        if (self._session_pins is None):
            error = Error(
                code=ERR_INTERNAL, message="置顶状态存储暂不可用",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            listing = await (self._list_codex_sessions(cmd) if is_codex
                             else self._handle_list_sessions(cmd))
            return error, listing
        try:
            await asyncio.to_thread(
                self._session_pins.set_pinned, engine, sid, bool(cmd.pinned))
            log.info("session pin toggled", engine=engine, session_id=sid,
                     pinned=bool(cmd.pinned))
        except SessionPinStoreError as exc:
            log.warning("session pin update failed", engine=engine,
                        session_id=sid, error=str(exc))
            error = Error(
                code=ERR_INTERNAL, message="置顶状态保存失败",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self._emit_to_sid(sid, error)
            listing = await (self._list_codex_sessions(cmd) if is_codex
                             else self._handle_list_sessions(cmd))
            return error, listing
        return await (self._list_codex_sessions(cmd) if is_codex
                      else self._handle_list_sessions(cmd))

    async def _handle_delete_work_session(self, cmd):
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        engine = getattr(cmd, "engine", "claude")
        store = self._work.for_engine(engine)
        record = await asyncio.to_thread(store.get_by_session, sid)
        if record is None:
            error = Error(
                code=ERR_AUTH,
                message="只能删除已注册的 Work 会话",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self._ctx_for(sid)
        if ctx is not None and (
            ctx.state != "idle"
            or ctx.queued_queries
            or self._query_queue_task_active(ctx)
        ):
            error = Error(
                code=ERR_BUSY,
                message="Work 会话仍在运行或有排队消息，请先停止并取消排队后再删除",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx is not None:
            await ctx.sdk.disconnect()
            self.sessions.pop(ctx.key or sid, None)
        try:
            if engine == "codex":
                await codex_rpc("thread/delete", {"threadId": sid})
            else:
                await asyncio.to_thread(
                    delete_session, sid, directory=record.cwd)
            await asyncio.to_thread(store.delete, sid)
        except Exception:
            log.exception("Work session deletion failed", engine=engine,
                          session_id=sid)
            error = Error(
                code=ERR_INTERNAL,
                message="Work 会话删除失败，原始资料未被删除",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if self._session_pins is not None:
            try:
                await asyncio.to_thread(
                    self._session_pins.set_pinned, engine, sid, False)
            except SessionPinStoreError:
                log.warning("stale Work session pin cleanup failed",
                            engine=engine, session_id=sid)
        if self.focused_sid in {sid, getattr(ctx, "key", None)}:
            self.focused_sid = None
        await self._handle_list_sessions(cmd)
        log.info("Work session deleted", engine=engine, session_id=sid)

    async def _handle_delete_session(self, cmd):
        """Delete one native session without confusing Code and Work roots."""
        if getattr(cmd, "space", "code") == "work":
            return await self._handle_delete_work_session(cmd)
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        requested_engine = getattr(cmd, "engine", "claude")
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        if engine != requested_engine:
            error = Error(
                code=ERR_AUTH,
                message="会话不属于请求的引擎",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        work_record = await asyncio.to_thread(
            self._work.for_engine(engine).get_by_session, sid
        )
        if work_record is not None:
            error = Error(
                code=ERR_AUTH,
                message="Work 会话必须从 Work 空间删除",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self._ctx_for(sid)
        if ctx is not None and (
            ctx.state != "idle"
            or ctx.queued_queries
            or self._query_queue_task_active(ctx)
        ):
            error = Error(
                code=ERR_BUSY,
                message="会话仍在运行或有排队消息，请先停止并取消排队后再删除",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        cwd = ctx.cwd if ctx is not None else None
        checkpoint_cleanup_journal = None
        if engine == "codex" and cwd is None:
            cwd = await asyncio.to_thread(codex_session_cwd, sid)
        if engine == "claude" and cwd is None:
            info = await asyncio.to_thread(get_session_info, sid)
            cwd = info.cwd if info is not None else None
            # A metadata-only Claude transcript has no cwd but is still a real
            # exact-SID file in the SDK catalog. delete_session(directory=None)
            # safely searches all project roots for that UUID and deletes only
            # the matching transcript. Preserve the old not-found rejection
            # when no exact transcript exists.
            if not cwd and transcript_path(sid) is None:
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message="Claude 会话不存在",
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        if engine == "codex" and cwd:
            existing_journal = ctx.codex_checkpoint if ctx is not None else None
            if existing_journal is not False:
                try:
                    checkpoint_cleanup_journal = existing_journal or (
                        await asyncio.to_thread(
                            CodexCheckpointJournal,
                            cwd,
                            Path(self.cfg.state_dir),
                            sid,
                        )
                    )
                except (CheckpointError, NotGitWorkspaceError) as exc:
                    log.warning(
                        "Codex checkpoint journal could not be opened for delete cleanup",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
        if ctx is not None:
            try:
                await ctx.sdk.disconnect()
            except Exception:
                log.exception(
                    "session disconnect before delete failed",
                    engine=engine,
                    session_id=sid,
                )
                error = Error(
                    code=ERR_INTERNAL,
                    message="无法安全停止会话，未执行删除",
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            self.sessions.pop(ctx.key or sid, None)
        try:
            if engine == "codex":
                try:
                    await codex_rpc("thread/delete", {"threadId": sid})
                except CodexRpcOutcomeUnknown:
                    remaining = await list_codex_sessions(200)
                    if any(row.get("session_id") == sid for row in remaining):
                        raise
                self._invalidate_codex_session_catalog()
            else:
                await asyncio.to_thread(delete_session, sid, directory=cwd)
        except Exception:
            log.exception("Code session deletion failed", engine=engine, session_id=sid)
            error = Error(
                code=ERR_INTERNAL,
                message="会话删除失败，请刷新后重试",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if engine == "codex" and checkpoint_cleanup_journal is not None:
            try:
                await asyncio.to_thread(
                    checkpoint_cleanup_journal.cleanup, force=True
                )
            except CheckpointError:
                log.warning(
                    "Codex checkpoint cleanup after delete failed", session_id=sid
                )
        if engine == "codex" and self._codex_controls is not None:
            try:
                await asyncio.to_thread(self._codex_controls.delete, sid)
            except CodexControlStoreError:
                log.warning(
                    "stale Codex controls cleanup failed", session_id=sid)
        if self._session_pins is not None:
            try:
                await asyncio.to_thread(
                    self._session_pins.set_pinned, engine, sid, False)
            except SessionPinStoreError:
                log.warning("stale Code session pin cleanup failed",
                            engine=engine, session_id=sid)
        self._watch.pop(sid, None)
        if self.focused_sid in {sid, getattr(ctx, "key", None)}:
            self.focused_sid = None
        await self._handle_list_sessions(cmd)
        log.info("Code session deleted", engine=engine, session_id=sid)

    async def _codex_code_context(self, cmd, action: str) -> SessionContext | Error:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        work_record = await asyncio.to_thread(
            self._work.for_engine("codex").get_by_session, sid
        )
        if work_record is not None or getattr(cmd, "space", "code") != "code":
            error = Error(
                code=ERR_AUTH,
                message=f"{action}仅支持 Codex Code 会话",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self._ctx_for(sid)
        if ctx is None:
            ctx = await self._spawn(resume_id=sid, engine="codex", space="code")
        if ctx is None or ctx.engine != "codex":
            error = Error(
                code=ERR_NOT_RUNNING,
                message="Codex 会话启动失败",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message=f"会话运行中，无法{action}",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if (
            self._codex_shared_affinity(ctx)
            and not await self._ensure_codex_daemon_generation(
                ctx, reason=f"before {action}")
        ):
            error = Error(
                code=ERR_NOT_RUNNING,
                message=f"Codex 共享通道重连失败，无法{action}；请重试",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if await self._prime_codex_ownership(sid):
            error = Error(
                code=ERR_BUSY,
                message=f"会话正由 Codex App 使用，无法{action}",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx.needs_reload:
            if ctx.codex_checkpoint not in (None, False):
                await self._retire_codex_checkpoint(
                    ctx,
                    reason=f"external transcript change before {action}",
                    allow_restart=True,
                )
            try:
                await self._refresh_codex_collaboration_mode(ctx)
                await ctx.sdk.force_reconnect(
                    resume_id=sid,
                    cwd=ctx.cwd,
                    reason=f"external transcript change before {action}",
                )
                ctx.needs_reload = False
            except Exception as exc:
                log.warning(
                    "Codex reload before control mutation failed",
                    session_id=sid,
                    action=action,
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message=f"Codex 会话重载失败，无法{action}；请稍后重试",
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        return ctx

    async def _claude_code_context(self, cmd, action: str) -> SessionContext | Error:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        work_record = await asyncio.to_thread(
            self._work.for_engine("claude").get_by_session, sid
        )
        if work_record is not None or getattr(cmd, "space", "code") != "code":
            error = Error(
                code=ERR_AUTH,
                message=f"{action}仅支持 Claude Code 会话",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = self._ctx_for(sid)
        if ctx is None:
            ctx = await self._spawn(resume_id=sid, engine="claude", space="code")
        if ctx is None or ctx.engine != "claude":
            error = Error(
                code=ERR_NOT_RUNNING,
                message="Claude 会话启动失败",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY,
                message=f"会话运行中，无法{action}",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if await self._prime_claude_ownership(sid):
            error = Error(
                code=ERR_BUSY,
                message=f"会话正由本机终端使用，无法{action}",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        if ctx.needs_reload:
            # The terminal may have appended and exited before this control
            # request arrived.  Rewind must never run against the resident
            # Claude child's stale in-memory conversation/checkpoint map.
            ctx.needs_reload = False
            try:
                await ctx.sdk.force_reconnect(
                    resume_id=sid,
                    cwd=ctx.cwd,
                    reason=f"external transcript change before {action}",
                    preserve_model=False,
                )
            except Exception as exc:
                ctx.needs_reload = True
                log.warning(
                    "Claude reload before control mutation failed",
                    session_id=sid,
                    action=action,
                    error_type=type(exc).__name__,
                )
                error = Error(
                    code=ERR_NOT_RUNNING,
                    message=f"Claude 会话重载失败，无法{action}；请稍后重试",
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
            # Consume a terminal append that raced the reconnect.  A fresh
            # external owner or another observed append invalidates this reload;
            # leave needs_reload set so the next attempt starts over.
            external = await self._prime_claude_ownership(sid)
            if external or ctx.needs_reload:
                message = (
                    f"Claude 会话在重载期间又被本机终端更新，无法{action}；"
                    "请退出终端后重试"
                    if external else
                    f"Claude 会话在重载期间仍有未归属的内容更新，无法{action}；"
                    "请稍后重试"
                )
                error = Error(
                    code=ERR_BUSY,
                    message=message,
                    sid=sid,
                    to=getattr(cmd, "client_id", None),
                )
                await self.transport.send(error)
                return error
        return ctx

    async def _claude_rewind_targets_after_stale(
        self,
        sid: str,
        cwd: str,
        selected_checkpoint: str,
    ) -> list[str]:
        """Return native rewind targets from newest through ``selected_checkpoint``.

        Claude Code's private ``rewind_conversation`` control accepts the latest
        human turn only.  Asking it to rewind directly to an older visible turn
        returns ``stale_target`` without mutating the transcript.  Derive the
        retry order from the same translated history used by the browser so
        slash-command envelopes and other non-conversational rows cannot skew
        the sequence.

        An empty result means the stale response could not be reconciled safely;
        the caller must preserve the original native rejection.
        """

        def read_targets() -> list[str]:
            path = transcript_path(sid)
            if (path
                    and os.path.getsize(path)
                    > self.cfg.history_source_max_bytes):
                return []
            messages = get_session_messages(sid, directory=cwd)
            events = translate_history(
                messages,
                self.cfg.tool_result_max,
                timestamps=transcript_timestamps(sid),
            )
            checkpoints: list[str] = []
            seen: set[str] = set()
            for event in events:
                if not isinstance(event, TurnEnd):
                    continue
                checkpoint = event.checkpoint_id
                if not checkpoint or checkpoint in seen:
                    continue
                checkpoints.append(checkpoint)
                seen.add(checkpoint)

            try:
                selected_index = checkpoints.index(selected_checkpoint)
            except ValueError:
                return []
            targets = list(reversed(checkpoints[selected_index:]))
            # A genuine stale target must have at least one newer human turn.
            # Keep the command's protocol bound as a hard safety limit.
            if len(targets) <= 1 or len(targets) > 1000:
                return []
            return targets

        try:
            return await asyncio.to_thread(read_targets)
        except Exception as exc:
            log.warning(
                "Claude stale rewind target sequence could not be derived",
                session_id=sid,
                error_type=type(exc).__name__,
            )
            return []

    async def _publish_rollback_outcome(
        self,
        ctx: SessionContext,
        result: RollbackResult,
        *,
        invalidate_conversation: bool,
        invalidate_files: bool,
    ):
        """Best-effort UI refresh after the durable mutation boundary."""
        sid = result.session_id
        artifact_invalidated = None
        history_invalidated = None
        reset_history = None

        if invalidate_files:
            artifact_invalidated = ArtifactInvalidated(
                session_id=sid,
                reason="rollback",
            )
            try:
                await self._emit(ctx, artifact_invalidated)
            except Exception as exc:
                log.warning(
                    "artifact invalidation broadcast failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        if invalidate_conversation:
            ctx.active_msg_id = None
            try:
                self._resync_watch(sid)
            except Exception as exc:
                log.warning(
                    "rollback watch resync failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
            history_invalidated = HistoryInvalidated(
                session_id=sid,
                reason="rollback",
                revision=self._history_revision(sid),
            )
            try:
                await self._emit(ctx, history_invalidated)
            except Exception as exc:
                log.warning(
                    "history invalidation broadcast failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
            try:
                history = await self._build_history(
                    sid, limit=self.MIRROR_LIMIT, cwd_hint=ctx.cwd,
                    detail="summary",
                )
                history.reset = True
                history.to = None
                try:
                    await self.transport.send(history)
                except Exception as exc:
                    log.warning(
                        "rollback history replacement broadcast failed",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
                reset_history = history
            except Exception as exc:
                log.warning(
                    "rollback history replacement build failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
            if ctx.engine == "codex":
                self._invalidate_codex_session_catalog()
                try:
                    await self._handle_list_sessions(
                        ListSessions(engine="codex", space="code")
                    )
                except Exception as exc:
                    log.warning(
                        "rollback session list refresh failed",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )

        try:
            await self._emit(ctx, result)
        except Exception as exc:
            # Return the result anyway so the in-memory reliable-command cache
            # suppresses another mutation in this process.
            log.warning(
                "rollback result delivery failed",
                session_id=sid,
                error_type=type(exc).__name__,
            )
        responses = [
            response
            for response in (
                artifact_invalidated,
                history_invalidated,
                reset_history,
                result,
            )
            if response is not None
        ]
        return tuple(responses) if len(responses) > 1 else result

    async def _handle_rollback_session(self, cmd):
        ctx = (
            await self._codex_code_context(cmd, "回滚")
            if cmd.engine == "codex"
            else await self._claude_code_context(cmd, "回滚")
        )
        if isinstance(ctx, Error):
            return ctx
        sid = ctx.session_id or cmd.session_id
        restore = getattr(cmd, "restore", "conversation")
        wants_files = restore in {"files", "both"}
        wants_conversation = restore in {"conversation", "both"}
        client_id = getattr(cmd, "client_id", None)
        cmd_id = getattr(cmd, "cmd_id", None)
        if not client_id or not cmd_id:
            error = Error(
                code=ERR_AUTH,
                message="回滚需要可靠命令标识，请刷新页面后重试",
                sid=sid,
                to=client_id,
            )
            await self.transport.send(error)
            return error
        rollback_journal = self._rollback_commands
        if rollback_journal is None:
            error = Error(
                code=ERR_INTERNAL,
                message="回滚安全日志不可用，已阻止本次操作",
                sid=sid,
                to=client_id,
            )
            await self.transport.send(error)
            return error
        try:
            durable = await asyncio.to_thread(
                rollback_journal.begin,
                client_id,
                cmd_id,
                sid,
                ctx.engine,
                restore,
                cmd.num_turns,
                getattr(cmd, "checkpoint_id", None),
            )
        except RollbackJournalError as exc:
            log.warning(
                "rollback intent could not be persisted",
                session_id=sid,
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="无法安全记录回滚操作，未修改会话或文件",
                sid=sid,
                to=client_id,
            )
            await self.transport.send(error)
            return error

        if durable["status"] == "complete":
            result = RollbackResult(**durable["result"], to=client_id)
            return await self._publish_rollback_outcome(
                ctx,
                result,
                invalidate_conversation=result.conversation == "succeeded",
                invalidate_files=result.files == "succeeded",
            )
        if durable["status"] in {"submitted", "uncertain"}:
            # A previous wrapper crossed the native mutation boundary but did
            # not durably record its response. Never replay a count-based
            # rollback. Refresh both possibly-mutated surfaces and report the
            # uncertainty explicitly; a new user action gets a new command id.
            if wants_conversation:
                self._bump_history_revision(sid)
            if ctx.engine == "codex" and wants_conversation:
                try:
                    await self._prepare_codex_conversation_rollback(ctx)
                except CheckpointError as exc:
                    log.warning(
                        "uncertain Codex rollback checkpoint quarantine failed",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
            result = RollbackResult(
                session_id=sid,
                engine=ctx.engine,
                restore=restore,
                conversation="failed" if wants_conversation else "skipped",
                files="failed" if wants_files else "skipped",
                restored_turns=0,
                detail=("上次回滚结果无法确认；已阻止重复执行，请核对刷新后的"
                        "会话与文件后再操作"),
                to=client_id,
            )
            try:
                await asyncio.to_thread(
                    rollback_journal.mark_uncertain, client_id, cmd_id
                )
                await asyncio.to_thread(
                    rollback_journal.complete, client_id, cmd_id, result
                )
            except RollbackJournalError:
                log.exception(
                    "uncertain rollback result could not be finalized",
                    session_id=sid,
                )
            return await self._publish_rollback_outcome(
                ctx,
                result,
                invalidate_conversation=wants_conversation,
                invalidate_files=wants_files,
            )
        try:
            claimed = await asyncio.to_thread(
                rollback_journal.mark_submitted, client_id, cmd_id
            )
        except RollbackJournalError as exc:
            log.warning(
                "rollback submission boundary could not be persisted",
                session_id=sid,
                error_type=type(exc).__name__,
            )
            error = Error(
                code=ERR_INTERNAL,
                message="无法安全提交回滚操作，未修改会话或文件",
                sid=sid,
                to=client_id,
            )
            await self.transport.send(error)
            return error
        if not claimed:
            # Another wrapper/process won the durable claim after begin().
            # Re-enter once through the now non-intent state without mutation.
            durable = await asyncio.to_thread(
                rollback_journal.get, client_id, cmd_id
            )
            if durable is None:
                raise RollbackJournalError("claimed rollback intent disappeared")
            if wants_conversation:
                self._bump_history_revision(sid)
            result = RollbackResult(
                session_id=sid,
                engine=ctx.engine,
                restore=restore,
                conversation="failed" if wants_conversation else "skipped",
                files="failed" if wants_files else "skipped",
                detail="回滚已由另一进程接管，已阻止重复执行；请刷新后核对结果",
                to=client_id,
            )
            return await self._publish_rollback_outcome(
                ctx,
                result,
                invalidate_conversation=wants_conversation,
                invalidate_files=wants_files,
            )
        if wants_conversation:
            # False-positive invalidation is safe if the later file preflight or
            # native call rejects. It must happen before either engine can
            # mutate history so an older concurrent GetHistory is distinguishable.
            self._bump_history_revision(sid)
        conversation = "skipped"
        files = "skipped"
        conflicts: list[str] = []
        prefill_text = None
        detail_parts: list[str] = []
        codex_journal = None
        codex_checkpoint_retired = False
        conversation_may_have_changed = False
        files_may_have_changed = False
        conversation_blocked = False
        claude_rewound_turns = 0
        claude_combined = ctx.engine == "claude" and restore == "both"

        async def restore_files_now() -> None:
            nonlocal files, files_may_have_changed, codex_journal, conflicts
            try:
                files_may_have_changed = True
                if ctx.engine == "claude":
                    await ctx.sdk.rewind_files(cmd.checkpoint_id)
                else:
                    journal = ctx.codex_checkpoint
                    if journal is False:
                        raise NotGitWorkspaceError(
                            "Code rollback requires a Git repository"
                        )
                    if journal is None:
                        journal = await asyncio.to_thread(
                            CodexCheckpointJournal,
                            ctx.cwd,
                            Path(self.cfg.state_dir),
                            sid,
                        )
                        ctx.codex_checkpoint = journal
                    codex_journal = journal
                    await asyncio.to_thread(
                        journal.rollback, cmd.num_turns, consume=False
                    )
                files = "succeeded"
            except CheckpointConflict as exc:
                files = "failed"
                conflicts = list(dict.fromkeys((*exc.paths, *exc.index_paths)))
                detail_parts.append("检测到回滚点之后的文件或暂存区改动，未覆盖")
            except ClaudeRewindError as exc:
                files = "failed"
                detail_parts.append(
                    f"代码恢复失败：{exc.user_message_zh}（{exc.code}）")
                log.warning(
                    "session file rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    rewind_code=exc.code,
                    retryable=exc.retryable,
                )
            except CheckpointError as exc:
                files = "failed"
                detail_parts.append("当前回滚点没有可安全恢复的代码 checkpoint")
                log.warning(
                    "session file rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                files = "failed"
                detail_parts.append("代码恢复失败")
                log.warning(
                    "session file rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        if ctx.engine == "claude" and wants_conversation:
            try:
                if getattr(ctx.sdk, "is_claude_broker", False):
                    raise ClaudeRewindError(
                        "capability_unavailable", operation="conversation")
                prepare = getattr(ctx.sdk, "prepare_conversation_rewind", None)
                if callable(prepare):
                    await prepare(resume_id=sid, cwd=ctx.cwd)
            except ClaudeRewindError as exc:
                conversation = "failed"
                conversation_blocked = True
                detail_parts.append(
                    f"对话历史恢复失败：{exc.user_message_zh}（{exc.code}）")
                log.warning(
                    "Claude conversation rollback preflight failed",
                    session_id=sid,
                    rewind_code=exc.code,
                    retryable=exc.retryable,
                )
            except Exception as exc:
                conversation = "failed"
                conversation_blocked = True
                detail_parts.append(
                    "对话回滚运行时准备失败，未修改对话或代码")
                log.warning(
                    "Claude conversation rollback runtime refresh failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        # Claude's native conversation target can still reject as stale after
        # capability probing. For a combined restore, do not mutate files until
        # that target has actually been accepted. Codex keeps its existing
        # file-first checkpoint transaction because its native rollback is
        # count-based and the journal is retired at the mutation boundary.
        if wants_files and not conversation_blocked and not claude_combined:
            await restore_files_now()

        # Codex remains file-first: a failed checkpoint restore leaves its
        # conversation untouched. Claude combined restore is conversation-first
        # and reaches this block while ``files`` is still skipped.
        if (wants_conversation and not conversation_blocked
                and not (restore == "both" and files == "failed")):
            try:
                if ctx.engine == "codex":
                    # Retire the count-aligned file journal *before* submitting
                    # native history rollback.  There is no app-server mutation
                    # id with which to reconcile a lost response after restart;
                    # pre-retirement makes every success/error/crash path safe.
                    codex_checkpoint_retired = (
                        await self._prepare_codex_conversation_rollback(ctx)
                    )
                    codex_journal = None
                    conversation_may_have_changed = True
                    native_turns = cmd.num_turns
                    rollout = codex_rollout_path(sid)
                    if rollout:
                        native_turns = await asyncio.to_thread(
                            codex_native_rollback_turns,
                            rollout,
                            cmd.num_turns,
                        )
                    if native_turns > 1000:
                        raise ValueError(
                            "logical rollback expands beyond Codex's native "
                            "1000-turn limit"
                        )
                    if native_turns != cmd.num_turns:
                        log.info(
                            "expanded Codex rollback across internal account "
                            "handoff turns",
                            session_id=sid,
                            logical_turns=cmd.num_turns,
                            native_turns=native_turns,
                        )
                    await ctx.sdk.rollback_thread(native_turns)
                else:
                    conversation_may_have_changed = True
                    try:
                        result = await ctx.sdk.rewind_conversation(
                            cmd.checkpoint_id, interrupt_if_running=False
                        )
                        claude_rewound_turns = 1
                    except ClaudeRewindError as exc:
                        if exc.code != "stale_target":
                            raise
                        targets = await self._claude_rewind_targets_after_stale(
                            sid, ctx.cwd, cmd.checkpoint_id
                        )
                        if not targets:
                            raise
                        log.info(
                            "Claude stale rewind target will be applied sequentially",
                            session_id=sid,
                            turns=len(targets),
                        )
                        for target in targets:
                            result = await ctx.sdk.rewind_conversation(
                                target, interrupt_if_running=False
                            )
                            claude_rewound_turns += 1
                    prefill_text = result.prefill_text
                conversation = "succeeded"
            except ClaudeRewindError as exc:
                conversation = "failed"
                if ctx.engine == "claude" and claude_rewound_turns:
                    detail_parts.append(
                        f"已逐轮恢复 {claude_rewound_turns} 轮，但未能到达所选位置；"
                        "代码未修改"
                    )
                detail_parts.append(
                    f"对话历史恢复失败：{exc.user_message_zh}（{exc.code}）")
                log.warning(
                    "session conversation rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    rewind_code=exc.code,
                    retryable=exc.retryable,
                )
            except CheckpointError as exc:
                conversation = "failed"
                detail_parts.append("对话历史恢复失败")
                log.warning(
                    "session conversation rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                conversation = "failed"
                detail_parts.append("对话历史恢复结果无法确认，已刷新核对")
                log.warning(
                    "session conversation rollback failed",
                    engine=ctx.engine,
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        if claude_combined and conversation == "succeeded":
            await restore_files_now()

        if (ctx.engine == "claude"
                and (conversation == "succeeded" or claude_rewound_turns > 0)):
            try:
                # Keep the original connected checkpoint map through a combined
                # conversation+file restore, then reload the final transcript
                # exactly once after both native mutations have returned.
                await ctx.sdk.force_reconnect(
                    resume_id=sid,
                    cwd=ctx.cwd,
                    reason="conversation rewind",
                )
                ctx.needs_reload = False
            except Exception as exc:
                # rewind_conversation already persisted the destructive
                # transcript mutation. A failed respawn must not report that
                # mutation as failed. Retry before the next query instead.
                ctx.needs_reload = True
                detail_parts.append(
                    ("对话已恢复；运行时重连失败，将在下次发送前重试"
                     if conversation == "succeeded" else
                     "部分对话已恢复；运行时重连失败，将在下次发送前重试")
                )
                log.warning(
                    "Claude reconnect after conversation rewind failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )

        if conversation == "succeeded":
            if ctx.engine == "codex" and not codex_checkpoint_retired:
                try:
                    journal = codex_journal or ctx.codex_checkpoint
                    if journal is None:
                        journal = await asyncio.to_thread(
                            CodexCheckpointJournal,
                            ctx.cwd,
                            Path(self.cfg.state_dir),
                            sid,
                        )
                        ctx.codex_checkpoint = journal
                    if journal is not False:
                        # Keep the exact failing journal attached so retirement
                        # always quarantines it, even if this compatibility path
                        # was reached with a separately opened instance.
                        ctx.codex_checkpoint = journal
                        await asyncio.to_thread(
                            journal.discard, cmd.num_turns, allow_partial=True
                        )
                except NotGitWorkspaceError:
                    ctx.codex_checkpoint = False
                    log.debug(
                        "Codex checkpoint journal absent after conversation rollback",
                        session_id=sid,
                    )
                except Exception as exc:
                    # The native history is already shorter. Keeping records
                    # that still refer to removed turns would make a later
                    # count-based file rollback target the wrong checkpoint.
                    # Drop the private journal and fail closed for this resident
                    # context instead of preserving a silently misaligned tail.
                    await self._retire_codex_checkpoint(
                        ctx, reason="conversation rollback discard failed"
                    )
                    detail_parts.append(
                        "对话已恢复；代码回滚记录同步失败，已安全重置"
                    )
                    log.warning(
                        "Codex checkpoint discard after conversation rollback failed",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
        restored_turns = (
            claude_rewound_turns
            if ctx.engine == "claude" and claude_rewound_turns > 0
            else cmd.num_turns
            if (conversation == "succeeded" or files == "succeeded")
            else 0
        )
        result = RollbackResult(
            session_id=sid,
            engine=ctx.engine,
            restore=restore,
            conversation=conversation,
            files=files,
            restored_turns=restored_turns,
            conflicts=conflicts,
            prefill_text=prefill_text,
            detail="；".join(detail_parts) or None,
            to=client_id,
        )
        try:
            await asyncio.to_thread(
                rollback_journal.complete, client_id, cmd_id, result
            )
        except RollbackJournalError as exc:
            # mark_submitted was durable before any mutation, so even if the
            # structured result cannot be committed a restart still refuses to
            # repeat this command. Surface the exact live result now and let a
            # later retry reconcile as uncertain.
            try:
                await asyncio.to_thread(
                    rollback_journal.mark_uncertain, client_id, cmd_id
                )
            except RollbackJournalError:
                pass
            result.detail = "；".join(filter(None, (
                result.detail,
                "回滚结果安全日志写入失败；重复请求将只核对状态，不会再次执行",
            )))
            log.warning(
                "rollback result could not be persisted",
                session_id=sid,
                error_type=type(exc).__name__,
            )
        log.info(
            "session rollback finished",
            engine=ctx.engine,
            session_id=sid,
            restore=restore,
            conversation=conversation,
            files=files,
            turns=restored_turns,
        )
        return await self._publish_rollback_outcome(
            ctx,
            result,
            invalidate_conversation=(
                conversation == "succeeded"
                or (conversation == "failed" and conversation_may_have_changed)
            ),
            invalidate_files=(
                files == "succeeded"
                or (files == "failed" and files_may_have_changed)
            ),
        )

    async def _handle_compact_session(self, cmd):
        ctx = await self._codex_code_context(cmd, "压缩上下文")
        if isinstance(ctx, Error):
            return ctx
        sid = ctx.session_id or cmd.session_id
        try:
            await ctx.sdk.compact_thread()
            notice = Notice(
                notice_id=f"compact-{uuid4().hex}",
                severity="info",
                category="runtime",
                title="上下文压缩已启动",
                message="Codex 正在使用原生 compact 重写当前线程上下文。",
                thread_id=sid,
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(notice)
            return notice
        except Exception:
            log.exception("Codex compact failed", session_id=sid)
            error = Error(
                code=ERR_INTERNAL,
                message="Codex 原生上下文压缩失败",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error

    async def _handle_start_review(self, cmd):
        ctx = await self._codex_code_context(cmd, "启动 Review")
        if isinstance(ctx, Error):
            return ctx
        value = getattr(cmd, "value", None)
        target: dict[str, object] = {"type": cmd.target}
        if cmd.target == "baseBranch":
            target["branch"] = value
        elif cmd.target == "commit":
            target["sha"] = value
        elif cmd.target == "custom":
            target["instructions"] = value
        sid = ctx.session_id or cmd.session_id
        # Claim before the first await so Query and Interrupt observe the Review
        # as a real turn even while review/start is still waiting for its RPC
        # response. The final launch window is serialized with Interrupt exactly
        # like a normal query.
        ctx.interrupt_event.clear()
        ctx.interrupt_deadline = None
        ctx.active_msg_id = f"review-{uuid4().hex}"
        ctx.state = "running"
        await self._emit(ctx, StateEvent(state="running"))
        try:
            async with ctx.launch_lock:
                # An interrupt can win while the running State frame is being
                # relayed. In that case do not start a turn after the user has
                # already stopped it.
                if (ctx.interrupt_event.is_set()
                        or ctx.state != "running"):
                    ctx.active_msg_id = None
                    ctx.interrupt_deadline = None
                    ctx.interrupt_event.clear()
                    if ctx.state != "idle":
                        await self._set_state(ctx, "idle")
                    return
                result = await ctx.sdk.start_review(target)
                turn_id = result["turn_id"]
                ctx.active_msg_id = turn_id
                ctx.turn_task = asyncio.create_task(
                    self._run_codex_review_turn(ctx, turn_id))
            log.info(
                "Codex review started",
                session_id=sid,
                turn_id=turn_id,
                target=cmd.target,
            )
        except Exception:
            log.exception(
                "Codex review start failed", session_id=sid, target=cmd.target
            )
            error = Error(
                code=ERR_INTERNAL,
                message="Codex 原生 Review 启动失败",
                sid=sid,
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            ctx.active_msg_id = None
            ctx.interrupt_deadline = None
            ctx.interrupt_event.clear()
            if ctx.state != "idle":
                await self._set_state(ctx, "idle")
            return error

    async def _work_dashboard(self, engine: str, client_id: str | None = None):
        data = await asyncio.to_thread(
            self._work.for_engine(engine).dashboard)
        return WorkDashboard(engine=engine, to=client_id, **data)

    async def _handle_get_work_dashboard(self, cmd):
        engine = getattr(cmd, "engine", "claude")
        try:
            dashboard = await self._work_dashboard(
                engine, getattr(cmd, "client_id", None))
        except Exception:
            log.exception("Work dashboard read failed", engine=engine)
            error = Error(
                code=ERR_INTERNAL, message="Work 工作台读取失败",
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        await self.transport.send(dashboard)
        return dashboard

    async def _handle_get_work_artifacts(self, cmd):
        engine = getattr(cmd, "engine", "claude")
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        client_id = getattr(cmd, "client_id", None)
        store = self._work.for_engine(engine)
        try:
            artifacts = await asyncio.to_thread(store.artifacts, sid)
            response = WorkArtifacts(
                engine=engine, session_id=sid, artifacts=artifacts, to=client_id)
        except LookupError:
            response = Error(
                code=ERR_AUTH, message="只能读取当前引擎的 Work 产物",
                sid=sid, to=client_id)
        except Exception:
            log.exception("Work artifact scan failed", engine=engine, session_id=sid)
            response = Error(
                code=ERR_INTERNAL, message="Work 产物读取失败",
                sid=sid, to=client_id)
        await self.transport.send(response)
        return response

    async def _handle_work_mutation(self, cmd):
        """Apply one bounded Work metadata mutation, then return fresh state."""
        engine = getattr(cmd, "engine", "claude")
        store = self._work.for_engine(engine)
        try:
            if cmd.type == "create_work_project":
                await asyncio.to_thread(
                    store.create_project, cmd.name.strip(), cmd.description.strip())
            elif cmd.type == "delete_work_project":
                await asyncio.to_thread(store.delete_project, cmd.project_id)
            elif cmd.type == "add_work_source":
                file = getattr(cmd, "file", None)
                content = None
                filename = None
                if file is not None:
                    attachment_error = validate_attachments(None, [file])
                    if attachment_error:
                        raise ValueError(attachment_error)
                    content = decode_attachment(file["data"])
                    filename = file["filename"]
                elif cmd.kind == "link":
                    filename, content = await capture_public_source(
                        (cmd.uri or "").strip()
                    )
                await asyncio.to_thread(
                    store.add_source, cmd.project_id, cmd.kind,
                    cmd.title.strip(), (cmd.uri or "").strip() or None,
                    filename, content)
            elif cmd.type == "delete_work_source":
                await asyncio.to_thread(store.delete_source, cmd.source_id)
            elif cmd.type == "create_work_plugin":
                await asyncio.to_thread(
                    store.create_plugin, cmd.name.strip(),
                    cmd.instructions.strip(), cmd.project_id)
            elif cmd.type == "delete_work_plugin":
                await asyncio.to_thread(store.delete_plugin, cmd.plugin_id)
            elif cmd.type == "create_work_schedule":
                # A stale browser clock may be a few seconds behind, but never
                # accept a task already far in the past that would run by accident.
                if cmd.next_run_at < time.time() - 60:
                    raise ValueError("schedule time is in the past")
                await asyncio.to_thread(
                    store.create_schedule, cmd.title.strip(), cmd.prompt.strip(),
                    cmd.next_run_at, cmd.repeat_seconds, cmd.project_id)
            elif cmd.type == "delete_work_schedule":
                await asyncio.to_thread(store.delete_schedule, cmd.schedule_id)
            else:
                raise ValueError("unsupported Work mutation")
            dashboard = await self._work_dashboard(
                engine, getattr(cmd, "client_id", None))
        except (LookupError, ValueError) as exc:
            log.warning("Work mutation rejected", type=cmd.type, engine=engine,
                        reason=type(exc).__name__)
            error = Error(
                code=ERR_BAD_PROMPT,
                message="Work 操作未完成，请检查输入后重试。",
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        except Exception:
            log.exception("Work mutation failed", type=cmd.type, engine=engine)
            error = Error(
                code=ERR_INTERNAL, message="Work 操作失败，数据未完整更新",
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        await self.transport.send(dashboard)
        return dashboard

    async def _is_codex_session(self, session_id: str) -> bool:
        """Capability guard for commands whose wire shape predates `engine`.

        Resident contexts are authoritative. For a cold session, a matching Codex
        rollout identifies it without ever handing the id to the Claude SDK.
        """
        ctx = self._ctx_by_sid(session_id)
        if ctx is not None:
            return ctx.engine == "codex"
        try:
            return bool(await asyncio.to_thread(codex_rollout_path, session_id))
        except Exception as exc:
            log.warning("codex session capability check failed",
                        session_id=session_id, error=str(exc))
            return False

    async def _send_session_fork_error(
        self, cmd, code: str, message: str,
    ) -> Error:
        client_id = getattr(cmd, "client_id", None)
        error = Error(
            code=code,
            message=message,
            request_id=getattr(cmd, "request_id", None),
            sid=getattr(cmd, "session_id", None),
            to=client_id,
        )
        if client_id:
            await self.transport.send(error)
        else:
            log.warning("dropping unroutable session fork error", code=code)
        return error

    async def _send_worktree_fork_error(
        self, cmd, code: str, message: str,
    ) -> Error:
        return await self._send_session_fork_error(cmd, code, message)

    def _remember_uncertain_codex_fork(
        self, request_id: str, child_session_id: Optional[str],
    ) -> None:
        existing = self._uncertain_codex_forks.get(request_id)
        if existing and child_session_id and existing != child_session_id:
            raise _ForkOutcomeUncertain(
                "one fork request resolved to multiple child sessions")
        if (request_id not in self._uncertain_codex_forks
                and len(self._uncertain_codex_forks) >= self.UNCERTAIN_FORK_CAP):
            raise _ForkOutcomeUncertain("uncertain fork cache capacity exhausted")
        self._uncertain_codex_forks[request_id] = child_session_id or existing
        self._uncertain_codex_forks.move_to_end(request_id)

    def _ensure_codex_fork_reconciler(
        self, cmd, sid: str, cwd: str, marker: str,
    ) -> None:
        current = self._codex_fork_tasks.get(cmd.request_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._reconcile_codex_fork_command(cmd, sid, cwd, marker),
            name=f"codex-fork-reconcile-{cmd.request_id}",
        )
        self._codex_fork_tasks[cmd.request_id] = task

    async def _reconcile_codex_fork_command(
        self, cmd, sid: str, cwd: str, marker: str,
    ) -> None:
        """Resolve an unknown mutation on the current client connection."""
        request_id = cmd.request_id
        try:
            for attempt in range(self.FORK_BACKGROUND_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(self.FORK_RECONCILE_DELAY)
                lock = self._codex_fork_locks[request_id]
                async with lock:
                    client_id = getattr(cmd, "client_id", None)
                    cmd_id = getattr(cmd, "cmd_id", None)
                    if client_id and cmd_id:
                        seen, _ = self._command_seen(client_id, cmd_id)
                        if seen:
                            return
                    entry = self._codex_forks.get(request_id) or {}
                    child = self._uncertain_codex_forks.get(request_id)
                    if not child and entry.get("status") == "complete":
                        child = entry.get("session_id")
                    if not child:
                        meta = await asyncio.to_thread(
                            find_rollout_fork, marker, sid, cwd)
                        child = (
                            meta.get("session_id")
                            if isinstance(meta, dict) else None)
                    if not isinstance(child, str) or not child:
                        continue
                    try:
                        event = await self._finish_same_cwd_fork(
                            cmd, sid, cwd, child)
                    except _ForkOutcomeUncertain:
                        continue
                    if client_id and cmd_id:
                        self._remember_command(
                            client_id, cmd_id,
                            (event.model_copy(deep=True),),
                        )
                        await self._send_command_ack(client_id, cmd_id)
                    return

            # Keep the durable submitted/uncertain state and outbox command for
            # future reconnect reconciliation, but end the current spinner with
            # a truthful, non-ACKed status. The same request is never re-forked.
            await self._send_session_fork_error(
                cmd,
                ERR_FORK_RECONCILING,
                "派生结果仍无法确认；请求已安全保留，重新连接后会继续核对",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("codex fork background reconcile failed",
                          request_id=request_id, error=str(exc))
        finally:
            current = asyncio.current_task()
            if self._codex_fork_tasks.get(request_id) is current:
                self._codex_fork_tasks.pop(request_id, None)

    async def _finish_same_cwd_fork(
        self, cmd, sid: str, cwd: str, child_session_id: str,
    ) -> SessionForked | Error:
        try:
            await asyncio.to_thread(
                self._codex_forks.complete, cmd.request_id, child_session_id)
        except ForkJournalError as exc:
            self._remember_uncertain_codex_fork(
                cmd.request_id, child_session_id)
            try:
                await asyncio.to_thread(
                    self._codex_forks.mark_uncertain, cmd.request_id)
            except ForkJournalError:
                pass
            self._ensure_codex_fork_reconciler(
                cmd, sid, cwd, fork_thread_source(cmd.request_id))
            log.exception("codex fork result journal failed", error=str(exc))
            # Returning Error would make _process_command ACK a mutation whose
            # durable child correlation was not written. Bubble instead: the
            # reliable command remains pending and retries only reconciliation.
            raise _ForkOutcomeUncertain(
                "fork completed but its durable result is not yet recorded"
            ) from exc
        self._uncertain_codex_forks.pop(cmd.request_id, None)
        event = SessionForked(
            parent_session_id=sid,
            session_id=child_session_id,
            cwd=cwd,
            target="same_cwd",
            last_turn_id=cmd.last_turn_id,
            request_id=cmd.request_id,
            to=cmd.client_id,
        )
        await self.transport.send(event)
        try:
            await self._list_codex_sessions(cmd)
        except Exception as exc:
            # The correlated fork result is already durable and delivered. A
            # sidebar refresh is read-only and must not suppress its ACK.
            log.warning("forked session list refresh failed", error=str(exc))
        return event

    async def _handle_fork_session(self, cmd):
        """Dispatch a message-level persistent fork to the owning engine."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        is_codex = await self._is_codex_session(sid)
        engine = "codex" if is_codex else "claude"
        # Resident contexts already carry the authoritative surface.  Avoid an
        # unnecessary thread hop for Code: reliable Codex fork reconciliation
        # deliberately relies on there being no scheduling point between its
        # journal check and retry path.
        ctx = self._ctx_for(sid)
        is_work = ctx is not None and ctx.space == "work"
        if ctx is None:
            is_work = await asyncio.to_thread(
                self._work.for_engine(engine).get_by_session, sid) is not None
        if is_work:
            return await self._send_session_fork_error(
                cmd, ERR_AUTH,
                "Work 会话不支持派生到 Code；请新建 Work 并选择同一项目")
        if is_codex:
            return await self._handle_codex_fork_session(cmd)
        return await self._handle_claude_fork_session(cmd, sid)

    @staticmethod
    def _claude_fork_title(info) -> str:
        """Match the SDK's human title without sacrificing its recovery marker."""
        base = (
            getattr(info, "custom_title", None)
            or getattr(info, "summary", None)
            or getattr(info, "first_prompt", None)
            or "派生会话"
        ).strip() or "派生会话"
        suffix = " (fork)"
        # Keep parity with RenameSession's public title bound.
        return f"{base[:200 - len(suffix)].rstrip()}{suffix}"

    def _remember_uncertain_claude_fork(
        self, request_id: str, child_session_id: Optional[str],
    ) -> None:
        existing = self._uncertain_claude_forks.get(request_id)
        if existing and child_session_id and existing != child_session_id:
            raise _ForkOutcomeUncertain(
                "one Claude fork request resolved to multiple child sessions")
        if (request_id not in self._uncertain_claude_forks
                and len(self._uncertain_claude_forks) >= self.UNCERTAIN_FORK_CAP):
            raise _ForkOutcomeUncertain(
                "uncertain Claude fork cache capacity exhausted")
        self._uncertain_claude_forks[request_id] = child_session_id or existing
        self._uncertain_claude_forks.move_to_end(request_id)

    def _release_terminal_claude_fork_lock(
        self, request_id: str, expected: Optional[asyncio.Lock] = None,
    ) -> None:
        """Keep only unresolved request locks; terminal journal state is enough."""
        try:
            entry = self._claude_forks.get(request_id)
        except ClaudeForkJournalError as exc:
            log.warning("Claude fork lock cleanup skipped", error=str(exc))
            return
        task = self._claude_fork_tasks.get(request_id)
        lock = self._claude_fork_locks.get(request_id)
        terminal_or_unrecorded = (
            entry is None
            or entry.get("status") in {"complete", "rejected"}
        )
        if (terminal_or_unrecorded
                and (task is None or task.done())
                and lock is not None
                and (expected is None or lock is expected)):
            self._claude_fork_locks.pop(request_id, None)

    def _ensure_claude_fork_reconciler(
        self, cmd, sid: str, cwd: str, title: Optional[str],
    ) -> None:
        current = self._claude_fork_tasks.get(cmd.request_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._reconcile_claude_fork_command(cmd, sid, cwd, title),
            name=f"claude-fork-reconcile-{cmd.request_id}",
        )
        self._claude_fork_tasks[cmd.request_id] = task

    async def _recover_claude_fork(
        self, marker: str, sid: str, cutoff: str, cwd: str,
        attempts: int = 1,
    ) -> Optional[str]:
        for attempt in range(max(1, attempts)):
            meta = await asyncio.to_thread(
                find_claude_fork, marker, sid, cutoff, cwd)
            child = meta.get("session_id") if isinstance(meta, dict) else None
            if isinstance(child, str) and child:
                return child
            if attempt + 1 < attempts:
                await asyncio.sleep(self.FORK_RECONCILE_DELAY)
        return None

    async def _reconcile_claude_fork_command(
        self, cmd, sid: str, cwd: str, title: Optional[str],
    ) -> None:
        """Resolve a submitted SDK fork without ever replaying the mutation."""
        request_id = cmd.request_id
        try:
            for attempt in range(self.FORK_BACKGROUND_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(self.FORK_RECONCILE_DELAY)
                lock = self._claude_fork_locks[request_id]
                async with lock:
                    client_id = getattr(cmd, "client_id", None)
                    cmd_id = getattr(cmd, "cmd_id", None)
                    if client_id and cmd_id:
                        seen, _ = self._command_seen(client_id, cmd_id)
                        if seen:
                            return
                    entry = self._claude_forks.get(request_id) or {}
                    child = self._uncertain_claude_forks.get(request_id)
                    if not child and entry.get("status") == "complete":
                        child = entry.get("session_id")
                    if not child:
                        marker = entry.get("marker") or claude_fork_marker(request_id)
                        try:
                            child = await self._recover_claude_fork(
                                marker, sid, cmd.last_turn_id, cwd)
                        except ClaudeForkJournalError as exc:
                            # list_sessions/transcript reads can fail briefly.
                            # A submitted mutation must keep reconciling and must
                            # never become replayable because of one bad scan.
                            if attempt == 0:
                                log.warning(
                                    "Claude fork recovery scan unavailable",
                                    request_id=request_id, error=str(exc))
                            continue
                    if not isinstance(child, str) or not child:
                        continue
                    try:
                        event = await self._finish_claude_fork(
                            cmd, sid, cwd, child, title)
                    except _ForkOutcomeUncertain:
                        continue
                    if client_id and cmd_id:
                        self._remember_command(
                            client_id, cmd_id, (event.model_copy(deep=True),))
                        await self._send_command_ack(client_id, cmd_id)
                    return
            await self._send_session_fork_error(
                cmd,
                ERR_FORK_RECONCILING,
                "派生结果仍无法确认；请求已安全保留，重新连接后会继续核对",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Claude fork background reconcile failed",
                          request_id=request_id, error=str(exc))
        finally:
            current = asyncio.current_task()
            if self._claude_fork_tasks.get(request_id) is current:
                self._claude_fork_tasks.pop(request_id, None)
            self._release_terminal_claude_fork_lock(request_id)

    async def _finish_claude_fork(
        self, cmd, sid: str, cwd: str, child_session_id: str,
        title: Optional[str],
    ) -> SessionForked:
        try:
            await asyncio.to_thread(
                self._claude_forks.complete, cmd.request_id, child_session_id)
        except ClaudeForkJournalError as exc:
            self._remember_uncertain_claude_fork(
                cmd.request_id, child_session_id)
            try:
                await asyncio.to_thread(
                    self._claude_forks.mark_uncertain, cmd.request_id)
            except ClaudeForkJournalError:
                pass
            self._ensure_claude_fork_reconciler(cmd, sid, cwd, title)
            log.exception("Claude fork result journal failed", error=str(exc))
            raise _ForkOutcomeUncertain(
                "Claude fork completed but its durable result is not recorded"
            ) from exc

        self._uncertain_claude_forks.pop(cmd.request_id, None)
        # The marker must remain list-visible until the child id is durable.
        # Replace only that exact marker: after SessionForked is delivered, an
        # ACK-loss retry must never overwrite a title the user chose meanwhile.
        try:
            fork_entry = self._claude_forks.get(cmd.request_id) or {}
            marker = fork_entry.get("marker")
            child_info = await asyncio.to_thread(
                get_session_info, child_session_id, directory=cwd)
            current_title = getattr(child_info, "custom_title", None)
            if child_info is not None and marker and current_title == marker:
                await asyncio.to_thread(
                    rename_session, child_session_id,
                    title or "派生会话 (fork)", directory=cwd)
        except Exception as exc:
            log.warning("Claude fork title finalization failed",
                        session_id=child_session_id, error=str(exc))

        event = SessionForked(
            parent_session_id=sid,
            session_id=child_session_id,
            cwd=cwd,
            target="same_cwd",
            last_turn_id=cmd.last_turn_id,
            request_id=cmd.request_id,
            to=cmd.client_id,
        )
        await self.transport.send(event)
        try:
            await self._handle_list_sessions(cmd)
        except Exception as exc:
            log.warning("Claude forked session list refresh failed", error=str(exc))
        return event

    async def _handle_claude_fork_session(self, cmd, sid: str):
        request_id = cmd.request_id
        lock = self._claude_fork_locks.get(request_id)
        if lock is None:
            if len(self._claude_fork_locks) >= self.UNCERTAIN_FORK_CAP:
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, "派生请求锁容量已满，请稍后重试")
            lock = asyncio.Lock()
            self._claude_fork_locks[request_id] = lock
        try:
            async with lock:
                client_id = getattr(cmd, "client_id", None)
                cmd_id = getattr(cmd, "cmd_id", None)
                if client_id and cmd_id:
                    seen, cached = self._command_seen(client_id, cmd_id)
                    if seen:
                        return cached
                return await self._handle_claude_fork_session_locked(cmd, sid)
        finally:
            self._release_terminal_claude_fork_lock(request_id, lock)

    async def _handle_claude_fork_session_locked(self, cmd, sid: str):
        if not getattr(cmd, "client_id", None):
            return await self._send_session_fork_error(
                cmd, ERR_AUTH, "派生会话需要已绑定的客户端")
        ctx = self._ctx_by_sid(sid)
        if ctx is not None and ctx.engine != "claude":
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "源会话不属于 Claude")

        try:
            entry = await asyncio.to_thread(
                self._claude_forks.get, cmd.request_id)
            canonical = await asyncio.to_thread(
                self._claude_forks.get_canonical, cmd.request_id)
        except ClaudeForkJournalError as exc:
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"无法读取派生请求状态: {exc}")

        if entry is not None:
            if (entry.get("parent_session_id") != sid
                    or entry.get("cutoff_message_id") != cmd.last_turn_id):
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL,
                    "派生 request_id 已用于另一个源会话或回复")
            source_cwd = entry["cwd"]
            canonical_status = (canonical or entry).get("status")
            title: Optional[str] = None
        else:
            source_cwd = ""
            canonical_status = "intent"
            title = None

        # A submitted/complete journal is authoritative even if the parent was
        # later deleted or temporarily unreadable. Source lookup is needed only
        # before the first SDK mutation, and to derive the initial human title.
        if entry is None or canonical_status == "intent":
            lookup_cwd = (
                ctx.cwd if ctx is not None
                else (source_cwd or None)
            )
            try:
                info = await asyncio.to_thread(
                    get_session_info, sid, directory=lookup_cwd)
            except Exception as exc:
                log.warning("Claude fork source lookup failed", session_id=sid,
                            error=str(exc))
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, f"无法读取 Claude 源会话: {exc}")
            if info is None:
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, "Claude 源会话不存在")
            raw_cwd = source_cwd or (ctx.cwd if ctx is not None else info.cwd)
            if not raw_cwd:
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, "无法确定源会话的工作目录")
            source_cwd = os.path.realpath(raw_cwd)
            title = self._claude_fork_title(info)
            try:
                entry = await asyncio.to_thread(
                    self._claude_forks.begin,
                    cmd.request_id, sid, cmd.last_turn_id, source_cwd)
                canonical = await asyncio.to_thread(
                    self._claude_forks.get_canonical, cmd.request_id)
                canonical_status = (canonical or entry).get("status")
            except ClaudeForkJournalError as exc:
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, f"无法记录派生请求: {exc}")

        assert entry is not None

        if canonical_status == "complete":
            return await self._finish_claude_fork(
                cmd, sid, source_cwd,
                entry.get("session_id") or (canonical or {}).get("session_id"),
                title)
        if canonical_status == "rejected":
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL,
                entry.get("error_message")
                or (canonical or {}).get("error_message")
                or "Claude 会话派生已被拒绝")

        marker = entry["marker"]
        volatile_child = self._uncertain_claude_forks.get(cmd.request_id)
        if (canonical_status in {"submitted", "uncertain"}
                or cmd.request_id in self._uncertain_claude_forks):
            try:
                recovered = volatile_child or await self._recover_claude_fork(
                    marker, sid, cmd.last_turn_id, source_cwd,
                    self.FORK_RECONCILE_ATTEMPTS)
            except ClaudeForkJournalError:
                recovered = None
            if recovered:
                return await self._finish_claude_fork(
                    cmd, sid, source_cwd, recovered, title)
            self._ensure_claude_fork_reconciler(
                cmd, sid, source_cwd, title)
            raise _ForkOutcomeUncertain(
                "Claude fork outcome is waiting for transcript reconciliation")

        try:
            claimed = await asyncio.to_thread(
                self._claude_forks.claim_submission, cmd.request_id)
        except ClaudeForkJournalError as exc:
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"无法持久记录派生提交状态: {exc}")
        if not claimed:
            refreshed = await asyncio.to_thread(
                self._claude_forks.get, cmd.request_id)
            refreshed_canonical = await asyncio.to_thread(
                self._claude_forks.get_canonical, cmd.request_id)
            refreshed_status = (
                refreshed_canonical or refreshed or {}).get("status")
            if refreshed and refreshed_status == "complete":
                return await self._finish_claude_fork(
                    cmd, sid, source_cwd,
                    refreshed.get("session_id")
                    or (refreshed_canonical or {}).get("session_id"),
                    title)
            if refreshed and refreshed_status == "rejected":
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL,
                    refreshed.get("error_message")
                    or (refreshed_canonical or {}).get("error_message")
                    or "Claude 会话派生已被拒绝")
            self._ensure_claude_fork_reconciler(
                cmd, sid, source_cwd, title)
            raise _ForkOutcomeUncertain(
                "canonical Claude fork request is being reconciled")

        try:
            result = await asyncio.to_thread(
                fork_session,
                sid,
                directory=source_cwd,
                up_to_message_id=cmd.last_turn_id,
                title=marker,
            )
        except (ValueError, FileNotFoundError) as exc:
            # SDK validates and builds the complete output before opening the
            # child file, so these failures are proven pre-mutation.
            log.warning(
                "Claude session fork rejected before mutation",
                error_type=type(exc).__name__,
            )
            try:
                await asyncio.to_thread(
                    self._claude_forks.reject, cmd.request_id,
                    "Claude 会话派生未完成，请稍后重试。")
            except ClaudeForkJournalError as journal_exc:
                log.warning("Claude fork rejection journal failed",
                            error=str(journal_exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "Claude 会话派生未完成，请稍后重试。")
        except Exception as exc:
            # os.open/os.write may have crossed the mutation boundary. Never
            # replay after an ambiguous exception; only marker reconciliation
            # may resolve it.
            try:
                await asyncio.to_thread(
                    self._claude_forks.mark_uncertain, cmd.request_id)
            except ClaudeForkJournalError as journal_exc:
                log.warning("Claude uncertain fork journal failed",
                            error=str(journal_exc))
            self._ensure_claude_fork_reconciler(
                cmd, sid, source_cwd, title)
            log.warning("Claude fork outcome unknown", parent=sid,
                        cutoff=cmd.last_turn_id, error=str(exc))
            raise _ForkOutcomeUncertain(
                "Claude fork request outcome is not yet visible") from exc

        child = getattr(result, "session_id", None)
        if not isinstance(child, str) or not child:
            try:
                await asyncio.to_thread(
                    self._claude_forks.mark_uncertain, cmd.request_id)
            except ClaudeForkJournalError:
                pass
            self._ensure_claude_fork_reconciler(
                cmd, sid, source_cwd, title)
            raise _ForkOutcomeUncertain(
                "Claude fork response omitted the child session id")

        event = await self._finish_claude_fork(
            cmd, sid, source_cwd, child, title)
        log.info("Claude session forked", parent=sid, session_id=child,
                 cutoff=cmd.last_turn_id)
        return event

    async def _handle_codex_fork_session(self, cmd):
        request_id = cmd.request_id
        lock = self._codex_fork_locks.get(request_id)
        if lock is None:
            if len(self._codex_fork_locks) >= self.UNCERTAIN_FORK_CAP:
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL, "派生请求锁容量已满，请稍后重试")
            lock = asyncio.Lock()
            self._codex_fork_locks[request_id] = lock
        async with lock:
            # A background reconciler may have resolved this reliable command
            # after _process_command's initial dedupe check but before this lock.
            client_id = getattr(cmd, "client_id", None)
            cmd_id = getattr(cmd, "cmd_id", None)
            if client_id and cmd_id:
                seen, cached = self._command_seen(client_id, cmd_id)
                if seen:
                    # The background resolver published the correlated event and
                    # ACK before releasing this same lock. Return its cached
                    # response so outer _process_command preserves the cache and
                    # emits only its harmless duplicate ACK, not a second event.
                    return cached
            return await self._handle_fork_session_locked(cmd)

    async def _handle_fork_session_locked(self, cmd):
        """Persistently fork Codex after one selected completed turn."""
        if not getattr(cmd, "client_id", None):
            return await self._send_session_fork_error(
                cmd, ERR_AUTH, "派生会话需要已绑定的客户端")
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if not await self._is_codex_session(sid):
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "目前仅支持派生 Codex 会话")

        ctx = self._ctx_by_sid(sid)
        source_cwd = (
            ctx.cwd if ctx is not None
            else await asyncio.to_thread(codex_session_cwd, sid)
        )
        if not source_cwd:
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "无法确定源会话的工作目录")
        source_cwd = os.path.realpath(source_cwd)

        try:
            entry = await asyncio.to_thread(
                self._codex_forks.begin,
                cmd.request_id,
                sid,
                cmd.last_turn_id,
                source_cwd,
            )
        except ForkJournalError as exc:
            log.warning("codex fork intent rejected", error=str(exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"无法记录派生请求: {exc}")

        if entry.get("status") == "complete":
            child = entry.get("session_id")
            return await self._finish_same_cwd_fork(
                cmd, sid, source_cwd, child)
        if entry.get("status") == "rejected":
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL,
                entry.get("error_message") or "Codex 会话派生已被拒绝")

        marker = entry["thread_source"]

        async def recover(attempts: int = 1) -> Optional[str]:
            for attempt in range(max(1, attempts)):
                meta = await asyncio.to_thread(
                    find_rollout_fork, marker, sid, source_cwd)
                child = meta.get("session_id") if isinstance(meta, dict) else None
                if isinstance(child, str):
                    return child
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.FORK_RECONCILE_DELAY)
            return None

        volatile_child = self._uncertain_codex_forks.get(cmd.request_id)
        if (entry.get("status") in {"submitted", "uncertain"}
                or cmd.request_id in self._uncertain_codex_forks):
            recovered = volatile_child or await recover(
                self.FORK_RECONCILE_ATTEMPTS)
            if recovered:
                return await self._finish_same_cwd_fork(
                    cmd, sid, source_cwd, recovered)
            self._ensure_codex_fork_reconciler(
                cmd, sid, source_cwd, marker)
            raise _ForkOutcomeUncertain(
                "fork outcome is still waiting for rollout reconciliation")

        recovered = await recover()
        if recovered:
            log.info("codex fork recovered from rollout marker",
                     parent=sid, session_id=recovered,
                     last_turn_id=cmd.last_turn_id)
            return await self._finish_same_cwd_fork(
                cmd, sid, source_cwd, recovered)

        try:
            claimed_submission = await asyncio.to_thread(
                self._codex_forks.claim_submission, cmd.request_id)
        except ForkJournalError as exc:
            # No request write has happened in this handler, so this is a proven
            # terminal local failure rather than an ambiguous mutation.
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"无法持久记录派生提交状态: {exc}")
        if not claimed_submission:
            # Another handler with the same canonical identity crossed the
            # durable submitted boundary first. Never issue a second RPC; this
            # request becomes another correlated waiter for the same child.
            refreshed = await asyncio.to_thread(
                self._codex_forks.get, cmd.request_id)
            if refreshed and refreshed.get("status") == "complete":
                return await self._finish_same_cwd_fork(
                    cmd, sid, source_cwd, refreshed.get("session_id"))
            if refreshed and refreshed.get("status") == "rejected":
                return await self._send_session_fork_error(
                    cmd, ERR_INTERNAL,
                    refreshed.get("error_message") or "Codex 会话派生已被拒绝")
            recovered = await recover(self.FORK_RECONCILE_ATTEMPTS)
            if recovered:
                return await self._finish_same_cwd_fork(
                    cmd, sid, source_cwd, recovered)
            self._ensure_codex_fork_reconciler(
                cmd, sid, source_cwd, marker)
            raise _ForkOutcomeUncertain(
                "canonical fork request is still being reconciled")

        params = {
            "threadId": sid,
            "lastTurnId": cmd.last_turn_id,
            "ephemeral": False,
            "threadSource": marker,
        }
        try:
            raw_result = await codex_rpc("thread/fork", params)
        except CodexRpcRejected as exc:
            # An explicit JSON-RPC validation/business rejection proves the
            # mutation did not commit and is safe to surface as terminal Error.
            log.warning("codex same-cwd fork rejected", parent=sid,
                        last_turn_id=cmd.last_turn_id, error=str(exc))
            try:
                await asyncio.to_thread(
                    self._codex_forks.reject, cmd.request_id,
                    "Codex 会话派生未完成，请稍后重试。")
            except ForkJournalError as journal_exc:
                log.warning("codex fork rejection journal failed",
                            error=str(journal_exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, "Codex 会话派生未完成，请稍后重试。")
        except CodexRpcOutcomeUnknown as exc:
            # A timeout may happen after app-server committed the fork. Its
            # session_meta marker is the durable authority across processes.
            self._remember_uncertain_codex_fork(cmd.request_id, None)
            try:
                await asyncio.to_thread(
                    self._codex_forks.mark_uncertain, cmd.request_id)
            except ForkJournalError as journal_exc:
                log.warning("codex uncertain fork journal failed",
                            error=str(journal_exc))
            recovered = await recover(self.FORK_RECONCILE_ATTEMPTS)
            if recovered:
                return await self._finish_same_cwd_fork(
                    cmd, sid, source_cwd, recovered)
            self._ensure_codex_fork_reconciler(
                cmd, sid, source_cwd, marker)
            log.warning("codex same-cwd fork outcome unknown", parent=sid,
                        last_turn_id=cmd.last_turn_id, error=str(exc))
            raise _ForkOutcomeUncertain(
                "fork request outcome is not yet visible in the rollout"
            ) from exc
        except Exception as exc:
            # codex_rpc reserves CodexRpcOutcomeUnknown for failures after the
            # request write. Resolve/spawn/initialize failures are pre-submit and
            # therefore proven safe to terminate and ACK.
            log.warning("codex same-cwd fork could not start", parent=sid,
                        last_turn_id=cmd.last_turn_id, error=str(exc))
            try:
                await asyncio.to_thread(
                    self._codex_forks.reject, cmd.request_id,
                    f"无法发起 Codex 会话派生: {exc}")
            except ForkJournalError as journal_exc:
                log.warning("codex pre-submit rejection journal failed",
                            error=str(journal_exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"无法发起 Codex 会话派生: {exc}")

        thread = raw_result.get("thread") if isinstance(raw_result, dict) else None
        child = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(child, str) or not child:
            # A success-shaped response without a child id is malformed, but it
            # does not prove the mutation failed. Reconcile exactly like a
            # response-stream loss and keep the reliable command unacknowledged.
            self._remember_uncertain_codex_fork(cmd.request_id, None)
            try:
                await asyncio.to_thread(
                    self._codex_forks.mark_uncertain, cmd.request_id)
            except ForkJournalError as journal_exc:
                log.warning("codex malformed-result journal failed",
                            error=str(journal_exc))
            recovered = await recover(self.FORK_RECONCILE_ATTEMPTS)
            if not recovered:
                self._ensure_codex_fork_reconciler(
                    cmd, sid, source_cwd, marker)
                raise _ForkOutcomeUncertain(
                    "fork response omitted its child id and is not reconciled")
            child = recovered

        event = await self._finish_same_cwd_fork(
            cmd, sid, source_cwd, child)
        if isinstance(event, SessionForked):
            log.info("codex session forked",
                     parent=sid, session_id=child,
                     last_turn_id=cmd.last_turn_id)
        return event

    async def _find_codex_worktree_fork(
        self, parent_session_id: str, cwd: str,
    ) -> Optional[dict]:
        """Recover a completed fork after an ACK loss or wrapper restart."""
        requests = [
            codex_rpc("thread/list", {
                "archived": archived,
                "cwd": cwd,
                "limit": 20,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            }, cwd=cwd)
            for archived in (False, True)
        ]
        candidates: list[dict] = []
        for result in await asyncio.gather(*requests):
            rows = result.get("data") if isinstance(result, dict) else None
            for thread in rows or ():
                if not isinstance(thread, dict):
                    continue
                thread_cwd = thread.get("cwd")
                if (not isinstance(thread_cwd, str)
                        or os.path.realpath(thread_cwd) != os.path.realpath(cwd)):
                    continue
                if thread.get("forkedFromId") == parent_session_id:
                    return thread
                # 0.144.1's state-DB thread/list currently drops forkedFromId,
                # even though thread/fork and thread/read return it. Keep exact-cwd
                # candidates and verify their rollout metadata through thread/read.
                if isinstance(thread.get("id"), str):
                    candidates.append(thread)
        if not candidates:
            return None
        reads = await asyncio.gather(*[
            codex_rpc("thread/read", {
                "threadId": candidate["id"],
                "includeTurns": False,
            }, cwd=cwd)
            for candidate in candidates[:20]
        ])
        for candidate, result in zip(candidates, reads):
            thread = result.get("thread") if isinstance(result, dict) else None
            if (isinstance(thread, dict)
                    and thread.get("forkedFromId") == parent_session_id
                    and isinstance(thread.get("cwd"), str)
                    and os.path.realpath(thread["cwd"]) == os.path.realpath(cwd)):
                return thread
        return None

    async def _handle_fork_session_worktree(self, cmd):
        """Create a wrapper-owned Git worktree and persistently fork Codex into it."""
        if not getattr(cmd, "client_id", None):
            return await self._send_worktree_fork_error(
                cmd, ERR_AUTH, "派生工作树需要已绑定的客户端")
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if not await self._is_codex_session(sid):
            return await self._send_worktree_fork_error(
                cmd, ERR_INTERNAL, "目前仅支持派生 Codex 会话到新工作树")
        ctx = self._ctx_by_sid(sid)
        # The session menu (no turn id) still means "fork the current complete
        # thread" and must wait for idle. A message action names an already
        # completed historical turn, which remains safe while a newer turn runs.
        if (not getattr(cmd, "last_turn_id", None)
                and ctx is not None and ctx.state != "idle"):
            return await self._send_worktree_fork_error(
                cmd, ERR_BUSY, "会话仍在运行，请等待当前回合结束后再派生")

        source_cwd = (
            ctx.cwd if ctx is not None
            else await asyncio.to_thread(codex_session_cwd, sid)
        )
        if not source_cwd:
            return await self._send_worktree_fork_error(
                cmd, ERR_INTERNAL, "无法确定源会话的工作目录")

        name = (getattr(cmd, "name", None) or "").strip()
        try:
            spec = await asyncio.to_thread(
                prepare_worktree,
                source_cwd,
                name,
                cmd.request_id,
                self.cfg.state_dir,
            )
        except WorktreeError as exc:
            log.warning(
                "Git worktree preparation rejected",
                error_type=type(exc).__name__,
            )
            return await self._send_worktree_fork_error(
                cmd, ERR_INTERNAL, "Git 工作树创建未完成，请稍后重试。")

        existing: Optional[dict] = None
        try:
            existing = await self._find_codex_worktree_fork(sid, spec.cwd)
        except Exception as exc:
            if not spec.created:
                log.warning("worktree fork recovery lookup failed",
                            parent=sid, cwd=spec.cwd, error=str(exc))
                return await self._send_worktree_fork_error(
                    cmd, ERR_INTERNAL,
                    "工作树已存在，但暂时无法确认派生会话；请稍后重试",
                )
            log.warning("fresh worktree pre-fork lookup failed; continuing",
                        parent=sid, cwd=spec.cwd, error=str(exc))

        fork_result: Optional[dict] = None
        if existing is None:
            params: dict = {
                "threadId": sid,
                "cwd": spec.cwd,
                "ephemeral": False,
                "threadSource": fork_thread_source(cmd.request_id),
            }
            if getattr(cmd, "last_turn_id", None):
                params["lastTurnId"] = cmd.last_turn_id
            parent_model = (
                getattr(ctx.sdk, "model", None) if ctx is not None else None
            ) or (await asyncio.to_thread(codex_session_settings, sid)).get("model")
            if parent_model:
                params["model"] = parent_model
            try:
                raw_result = await codex_rpc(
                    "thread/fork", params, cwd=spec.cwd)
                fork_result = raw_result if isinstance(raw_result, dict) else None
            except Exception as exc:
                log.warning(
                    "Codex worktree fork RPC did not complete",
                    parent=sid,
                    cwd=spec.cwd,
                    error_type=type(exc).__name__,
                )
                recovered: Optional[dict] = None
                recovery_failed = False
                try:
                    recovered = await self._find_codex_worktree_fork(sid, spec.cwd)
                except Exception as recovery_exc:
                    recovery_failed = True
                    log.warning("worktree fork post-error recovery failed",
                                parent=sid, cwd=spec.cwd,
                                error=str(recovery_exc))
                if recovered is not None:
                    existing = recovered
                else:
                    if spec.created and not recovery_failed:
                        await asyncio.to_thread(rollback_worktree, spec)
                    detail = (
                        "派生结果暂时无法确认，工作树已保留；请稍后重试"
                        if recovery_failed
                        else "Codex 会话派生未完成，请稍后重试。"
                    )
                    return await self._send_worktree_fork_error(
                        cmd, ERR_INTERNAL, detail)

        thread = existing
        if thread is None and fork_result is not None:
            candidate = fork_result.get("thread")
            thread = candidate if isinstance(candidate, dict) else None
        new_session_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(new_session_id, str) or not new_session_id:
            # A malformed/lost response can still follow a committed fork. Query
            # the state DB once before removing its cwd and potentially orphaning
            # the new thread.
            recovery_failed = False
            try:
                recovered = await self._find_codex_worktree_fork(sid, spec.cwd)
            except Exception as recovery_exc:
                recovered = None
                recovery_failed = True
                log.warning("worktree fork id recovery failed",
                            parent=sid, cwd=spec.cwd, error=str(recovery_exc))
            new_session_id = (
                recovered.get("id") if isinstance(recovered, dict) else None)
            if not isinstance(new_session_id, str) or not new_session_id:
                if spec.created and not recovery_failed:
                    await asyncio.to_thread(rollback_worktree, spec)
                detail = (
                    "派生结果暂时无法确认，工作树已保留；请稍后重试"
                    if recovery_failed
                    else "Codex 会话派生成功但未返回新会话 ID"
                )
                return await self._send_worktree_fork_error(
                    cmd, ERR_INTERNAL, detail)

        if name:
            try:
                await codex_rpc("thread/name/set", {
                    "threadId": new_session_id,
                    "name": name,
                }, cwd=spec.cwd)
            except Exception as exc:
                # The persistent fork and its worktree are already valid. A title
                # can be retried through the normal rename action without risking
                # user work, so never roll the fork back here.
                log.warning("forked thread name set failed",
                            thread_id=new_session_id, error=str(exc))

        event = SessionForked(
            parent_session_id=sid,
            session_id=new_session_id,
            cwd=spec.cwd,
            git_branch=spec.branch,
            target="worktree",
            last_turn_id=getattr(cmd, "last_turn_id", None),
            request_id=cmd.request_id,
            to=cmd.client_id,
        )
        await self.transport.send(event)
        await self._list_codex_sessions(cmd)
        log.info("codex session forked into worktree",
                 parent=sid, session_id=new_session_id,
                 cwd=spec.cwd, branch=spec.branch,
                 recovered=existing is not None)
        return event

    async def _send_session_migration_error(
        self, cmd, code: str, message: str, *, sid: Optional[str] = None,
    ) -> Error:
        error = Error(
            code=code,
            message=message,
            request_id=getattr(cmd, "request_id", None),
            sid=sid or getattr(cmd, "session_id", None),
            to=getattr(cmd, "client_id", None),
        )
        ctx = self._ctx_by_sid(sid or getattr(cmd, "session_id", ""))
        if ctx is not None:
            await self._emit(ctx, error)
        else:
            await self.transport.send(error)
        return error

    async def _handle_migrate_session(self, cmd):
        """Continue one idle Codex Code thread in another cwd."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        requested = os.path.expanduser(cmd.cwd.strip())
        if not os.path.isabs(requested):
            return await self._send_session_migration_error(
                cmd,
                ERR_INVALID_CWD,
                "迁移目录必须是绝对路径",
                sid=sid,
            )
        target_cwd = os.path.realpath(requested)
        if not await asyncio.to_thread(os.path.isdir, target_cwd):
            return await self._send_session_migration_error(
                cmd,
                ERR_INVALID_CWD,
                "迁移目录不存在或不是目录",
                sid=sid,
            )

        ctx = self._ctx_by_sid(sid)
        if ctx is None:
            if not await self._is_codex_session(sid):
                return await self._send_session_migration_error(
                    cmd,
                    ERR_AUTH,
                    "目前仅支持迁移 Codex Code 会话",
                    sid=sid,
                )
            work_record = await asyncio.to_thread(
                self._work.for_engine("codex").get_by_session,
                sid,
            )
            if work_record is not None:
                return await self._send_session_migration_error(
                    cmd,
                    ERR_AUTH,
                    "Codex Work 的隔离目录不能迁移",
                    sid=sid,
                )
            try:
                ctx = await self._spawn(
                    resume_id=sid,
                    engine="codex",
                    space="code",
                    raise_on_failure=True,
                )
            except _SpawnFailure as exc:
                return await self._send_session_migration_error(
                    cmd, exc.code, exc.message, sid=sid)
            except Exception as exc:
                log.exception(
                    "cold Codex session spawn for migration failed",
                    session_id=sid,
                    error_type=type(exc).__name__,
                )
                return await self._send_session_migration_error(
                    cmd,
                    ERR_CC_CRASH,
                    "会话暂时无法加载，迁移未开始",
                    sid=sid,
                )
            if ctx is None:
                return await self._send_session_migration_error(
                    cmd,
                    ERR_NOT_RUNNING,
                    "会话暂时无法加载，迁移未开始",
                    sid=sid,
                )
        if ctx.engine != "codex" or ctx.space != "code" or ctx.btw:
            return await self._send_session_migration_error(
                cmd,
                ERR_AUTH,
                "目前仅支持迁移普通 Codex Code 会话",
                sid=sid,
            )

        async with ctx.query_lock:
            active = any(
                task is not None and not task.done()
                for task in (ctx.turn_task, ctx.codex_spontaneous_task)
            )
            if ctx.state != "idle" or active:
                return await self._send_session_migration_error(
                    cmd,
                    ERR_BUSY,
                    "会话仍在运行，请等待当前回合结束后再迁移",
                    sid=sid,
                )
            control_error = await self._runtime_control_preflight(
                ctx,
                action="迁移工作目录",
                request_id=cmd.request_id,
                client_id=getattr(cmd, "client_id", None),
            )
            if control_error is not None:
                return control_error
            # Shared CLI turns are coordinated by the common app-server and do
            # not block Remote controls, so the generic preflight above only
            # verifies that daemon generation. A private Codex App uses a
            # separate app-server, though, and may be running this same thread
            # while the Remote-owned context remains idle. Refresh that
            # ownership boundary before retargeting the loaded thread.
            if (
                self._codex_shared_affinity(ctx)
                and await self._prime_codex_ownership(sid)
            ):
                return await self._send_session_migration_error(
                    cmd,
                    ERR_BUSY,
                    "会话正由 Codex App 使用，无法迁移工作目录",
                    sid=sid,
                )
            active = any(
                task is not None and not task.done()
                for task in (ctx.turn_task, ctx.codex_spontaneous_task)
            )
            if ctx.state != "idle" or active:
                return await self._send_session_migration_error(
                    cmd,
                    ERR_BUSY,
                    "会话状态已变化，请等待当前回合结束后再迁移",
                    sid=sid,
                )

            previous_cwd = ctx.cwd
            previous_cwd_real = os.path.realpath(previous_cwd)
            if target_cwd != previous_cwd_real or target_cwd != previous_cwd:
                if self._codex_controls is None:
                    return await self._send_session_migration_error(
                        cmd,
                        ERR_INTERNAL,
                        "会话目录迁移状态暂时无法保存，请修复本地状态文件后重试",
                        sid=sid,
                    )
                previous_cwd_override = (
                    self._codex_controls.get(sid).cwd_override
                )
                permission_profile = getattr(
                    ctx.sdk, "permission_profile", None)
                if isinstance(permission_profile, str) and permission_profile:
                    try:
                        profiles = await codex_permission_profiles(target_cwd)
                    except Exception as exc:
                        log.warning(
                            "migration permission profile lookup failed",
                            session_id=sid,
                            error_type=type(exc).__name__,
                        )
                        return await self._send_session_migration_error(
                            cmd,
                            ERR_INTERNAL,
                            "目标目录的执行环境状态无法确认，迁移未开始",
                            sid=sid,
                        )
                    if not any(
                        profile["id"] == permission_profile
                        and profile["allowed"]
                        for profile in profiles
                    ):
                        return await self._send_session_migration_error(
                            cmd,
                            ERR_AUTH,
                            "当前执行环境不允许访问目标目录，请先切换执行环境",
                            sid=sid,
                        )
                try:
                    await ctx.sdk.set_cwd(
                        target_cwd,
                        reason="session cwd migration",
                    )
                except Exception as exc:
                    log.warning(
                        "Codex session cwd migration failed",
                        session_id=sid,
                        target_cwd=target_cwd,
                        error_type=type(exc).__name__,
                    )
                    rollback_ok = False
                    try:
                        await ctx.sdk.set_cwd(
                            previous_cwd,
                            reason="session cwd migration rollback",
                        )
                        rollback_ok = True
                    except Exception as rollback_exc:
                        ctx.needs_reload = True
                        log.warning(
                            "Codex session cwd migration rollback failed",
                            session_id=sid,
                            error_type=type(rollback_exc).__name__,
                        )
                    return await self._send_session_migration_error(
                        cmd,
                        ERR_INTERNAL,
                        (
                            "迁移失败，会话仍保留原工作目录；请稍后重试"
                            if rollback_ok else
                            "迁移失败且原目录连接未恢复，请重新打开会话"
                        ),
                        sid=sid,
                    )

                try:
                    await asyncio.to_thread(
                        self._codex_controls.set_cwd_override,
                        sid,
                        target_cwd,
                    )
                except Exception as exc:
                    log.warning(
                        "Codex session cwd migration persistence failed",
                        session_id=sid,
                        error_type=type(exc).__name__,
                    )
                    persistence_rollback_ok = False
                    try:
                        restored = await asyncio.to_thread(
                            self._codex_controls
                            .restore_cwd_override_after_failed_set,
                            sid,
                            target_cwd,
                            previous_cwd_override,
                        )
                        persistence_rollback_ok = (
                            restored.cwd_override
                            == previous_cwd_override
                        )
                    except Exception as restore_exc:
                        log.warning(
                            "Codex migration persistence cleanup failed",
                            session_id=sid,
                            error_type=type(restore_exc).__name__,
                        )
                    runtime_rollback_ok = False
                    try:
                        await ctx.sdk.set_cwd(
                            previous_cwd,
                            reason="session cwd migration persistence rollback",
                        )
                        runtime_rollback_ok = True
                    except Exception as rollback_exc:
                        ctx.needs_reload = True
                        log.warning(
                            "Codex migration persistence rollback failed",
                            session_id=sid,
                            error_type=type(rollback_exc).__name__,
                        )
                    rollback_ok = (
                        persistence_rollback_ok and runtime_rollback_ok
                    )
                    if not rollback_ok:
                        ctx.needs_reload = True
                    return await self._send_session_migration_error(
                        cmd,
                        ERR_INTERNAL,
                        (
                            "迁移状态无法保存，会话已恢复原工作目录；请稍后重试"
                            if rollback_ok else
                            "迁移状态无法保存且原目录状态未完全恢复，请重新打开会话"
                        ),
                        sid=sid,
                    )

                previous_checkpoint = ctx.codex_checkpoint
                await self._retire_codex_checkpoint(
                    ctx,
                    reason="session cwd migration",
                    allow_restart=True,
                )
                if previous_checkpoint in (None, False):
                    # A different cwd may cross the Git/non-Git boundary, so
                    # re-evaluate checkpoint support on its first managed turn.
                    ctx.codex_checkpoint = None
                ctx.cwd = target_cwd
                # Updating loaded-thread settings does not consume rollout
                # growth written by a separate native app-server. Preserve a
                # pending reload so the queued/next Remote turn resumes the
                # latest native state in this newly confirmed cwd first.
                ctx.preview_write_candidates.clear()
                ctx.preview_external_paths.clear()
                await self._cleanup_codex_steer_attachments(ctx)
                self._invalidate_codex_session_catalog()
                await self._emit(ctx, ArtifactInvalidated(
                    session_id=sid,
                    reason="session_migration",
                ))

            event = SessionMigrated(
                session_id=sid,
                previous_cwd=previous_cwd,
                cwd=target_cwd,
                request_id=cmd.request_id,
            )
            await self._emit(ctx, event)
            await self._list_codex_sessions(cmd)
            ctx.queued_query_wakeup.set()
            self._schedule_query_queue_drain(ctx)
            log.info(
                "Codex session cwd migrated",
                session_id=sid,
                previous_cwd=previous_cwd,
                cwd=target_cwd,
            )
            return event

    # ---- directory picker (arbitrary-cwd session creation) ----

    async def _handle_list_dir(self, cmd) -> None:
        try:
            path, parent, dirs = await asyncio.to_thread(self._scan_dir, cmd.path)
            event = DirList(
                path=path, parent=parent, dirs=dirs,
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None))
            await self.transport.send(event)
            return event
        except Exception as e:
            log.exception("list_dir failed", path=getattr(cmd, "path", None), error=str(e))
            error = Error(
                code=ERR_INTERNAL,
                message="目录暂不可用，请重新选择或稍后重试。",
                request_id=getattr(cmd, "cmd_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error

    @staticmethod
    def _scan_dir(path: Optional[str]) -> tuple[str, Optional[str], list[dict[str, str]]]:
        base = path or os.path.expanduser("~")
        base = os.path.realpath(os.path.expanduser(base))
        if not os.path.isdir(base):
            raise FileNotFoundError(base)
        parent = os.path.dirname(base) or None
        if parent and os.path.realpath(parent) == base:
            parent = None
        dirs: list[dict[str, str]] = []
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=True):
                        dirs.append({"name": entry.name[:255], "path": entry.path[:4096]})
                        if len(dirs) >= 1000:
                            break
            dirs.sort(key=lambda item: item["name"])
        except PermissionError:
            pass
        return base, parent, dirs

    @staticmethod
    def _bg_blocked_session_ids() -> set[str]:
        ids: set[str] = set()
        jobs = str(claude_config_dir() / "jobs")
        if not os.path.isdir(jobs):
            return ids
        try:
            with os.scandir(jobs) as entries:
                for index, entry in enumerate(entries):
                    if index >= WrapperMachine.BG_JOB_SCAN_MAX:
                        log.warning("Claude job scan capped", entries=index)
                        break
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        state_path = os.path.join(entry.path, "state.json")
                        state_info = os.stat(state_path, follow_symlinks=False)
                        if (not stat.S_ISREG(state_info.st_mode)
                                or state_info.st_size
                                > WrapperMachine.BG_JOB_STATE_MAX_BYTES):
                            continue
                        with open(state_path) as stream:
                            raw = stream.read(
                                WrapperMachine.BG_JOB_STATE_MAX_BYTES + 1)
                        if len(raw) > WrapperMachine.BG_JOB_STATE_MAX_BYTES:
                            continue
                        state = json.loads(raw)
                    except Exception:
                        # One malformed/racy job record must not abort the bounded
                        # scan or hide every other active-session marker.
                        continue
                    if (isinstance(state, dict)
                            and state.get("state") != "done"
                            and isinstance(state.get("sessionId"), str)
                            and 1 <= len(state["sessionId"]) <= 128):
                        ids.add(state["sessionId"])
        except OSError:
            pass
        return ids

    # ---- spawn (build a ctx: SdkHandle + connect + history) ----

    async def _spawn(self, resume_id: Optional[str], cwd: Optional[str] = None,
                     bootstrap: bool = False, engine: str = "claude",
                     model: Optional[str] = None, effort: Optional[str] = None,
                     collaboration_mode: Optional[str] = None,
                     permission_mode: Optional[str] = None,
                     permission_profile: Optional[str] = None,
                     web_search: Optional[str] = None,
                     service_tier: Optional[str] = None,
                     space: str = "code",
                     work_id: Optional[str] = None,
                     raise_on_failure: bool = False) -> Optional[SessionContext]:
        """Create a SessionContext, connect its SDK subprocess, load history.
        Returns the ctx (added to the pool under its real or temp key) or None
        on legacy-route failure (an Error has been emitted). NewSession uses
        ``raise_on_failure`` so its handler can send one correlated Error.
        `bootstrap` exempts the cap and
        retries resume→fresh on connect failure. `model`/`effort` (new_session
        only) pre-select those at spawn: effort BEFORE connect so the first turn
        runs at that strength with no respawn; cc model via a live set_model
        after connect; codex model as a per-turn field. An omitted Claude model
        resolves from current settings, then falls back to the curated default;
        omitted Codex controls retain native defaults."""
        explicit_claude_model = engine == "claude" and model is not None
        saved_codex_controls = CodexControls()
        if resume_id and engine == "codex" and space == "code":
            saved_codex_controls = await self._load_codex_session_controls(
                resume_id)

        async def reject(
            code: str,
            message: str,
            *,
            route: str = "focused",
            sid: Optional[str] = None,
            typed_code: Optional[str] = None,
        ) -> None:
            if raise_on_failure:
                raise _SpawnFailure(typed_code or code, message)
            error = Error(code=code, message=message)
            if route == "sid":
                await self._emit_to_sid(sid, error)
            else:
                await self._emit_focused(error)

        if saved_codex_controls.cwd_override is not None:
            try:
                saved_codex_controls = (
                    await self._reconcile_codex_cwd_override(
                        resume_id,
                        saved_codex_controls,
                    )
                )
            except Exception as exc:
                log.warning(
                    "missing Codex cwd override could not be reconciled",
                    session_id=resume_id,
                    error_type=type(exc).__name__,
                )
                await reject(
                    ERR_INTERNAL,
                    "保存的 Codex 迁移目录已失效，且本地状态无法修复",
                    route="sid",
                    sid=resume_id,
                )
                return None

        if resume_id and resume_id in self._private_btw_sessions:
            log.warning("refusing cold resume of private btw transcript",
                        session_id=resume_id)
            await reject(
                ERR_AUTH, "临时 btw 会话不可恢复",
                route="sid", sid=resume_id)
            return None
        broker_handle: Optional[ClaudeBrokerHandle] = None
        if (self._claude_broker_enabled
                and engine == "claude" and space == "code" and resume_id):
            try:
                broker_handle = await ClaudeBrokerHandle.discover(
                    self._claude_broker, resume_id)
            except BrokerClientError as exc:
                # An unsafe/malformed live endpoint is not equivalent to an
                # absent broker. Fail before starting a second Claude writer.
                log.warning(
                    "Claude broker discovery failed closed",
                    session_id=resume_id,
                    error_code=exc.code,
                )
                await reject(
                    ERR_BUSY,
                    "本机 Claude broker 状态无法安全确认，未启动第二个会话进程",
                    route="sid", sid=resume_id)
                return None
        # Claude's CLI/version preflight belongs to Claude spawn, not wrapper
        # process startup.  Keep it before cap eviction: an unavailable optional
        # engine must not evict a healthy resident Codex session and then fail.
        if engine == "claude" and broker_handle is None:
            try:
                SdkHandle.preflight(self.cfg.claude_bin)
            except Exception as exc:
                log.warning("Claude preflight failed; engine unavailable",
                            error=str(exc))
                await reject(
                    ERR_CC_CRASH, "Claude 暂时不可用，请稍后重试。",
                    route="sid", sid=resume_id)
                return None

        # Concurrency cap (bootstrap always allowed). When full, evict an idle,
        # non-focused session (tear down its subprocess; the client keeps its
        # runtime and re-spawns on re-focus). Only reject if ALL are running —
        # so merely browsing between sessions never wedges you.
        if not bootstrap and len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle"
                           and not c.btw and not c.queued_queries
                           and not self._query_queue_task_active(c)), None)
            if victim is None:
                await reject(
                    ERR_BUSY, "所有会话都在运行,先中断一个再切换")
                return None
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for cap", key=victim)
        # Resolve the target cwd.
        if resume_id and engine == "codex":
            # Codex sessions live in ~/.codex/sessions (not the Claude SDK's store),
            # so resolve cwd from the rollout meta, not get_session_info. An
            # explicit Remote migration wins because thread/resume's cwd is a
            # runtime default and does not rewrite thread/list metadata until a
            # later turn materializes the new context.
            cwd_hint = await asyncio.to_thread(codex_session_cwd, resume_id)
            target_cwd = saved_codex_controls.cwd_override
            if target_cwd is None:
                target_cwd = next((
                    candidate
                    for candidate in (cwd_hint, self.cfg.cc_cwd)
                    if isinstance(candidate, str)
                    and os.path.isdir(candidate)
                ), None)
            if target_cwd is None:
                await reject(
                    ERR_INVALID_CWD,
                    "Codex 会话和默认工作目录均已不存在，未连接该会话",
                    route="sid",
                    sid=resume_id,
                )
                return None
            target_cwd = os.path.realpath(target_cwd)
        elif resume_id and broker_handle is not None:
            # A freshly launched broker session has a durable, preassigned
            # Claude session id before Claude writes its first JSONL row.  Its
            # broker metadata is authoritative in that window; requiring
            # get_session_info() here would make the new TUI impossible to open
            # from Remote until somebody had already typed in the terminal.
            target_cwd = broker_handle.cwd
            if not os.path.isdir(target_cwd):
                await reject(
                    ERR_BUSY, "Claude broker 的工作目录已不存在，未连接该会话",
                    route="sid", sid=resume_id)
                return None
        elif resume_id:
            try:
                info = await asyncio.to_thread(get_session_info, resume_id)
            except Exception as e:
                log.warning("get_session_info failed", session_id=resume_id, error=str(e))
                info = None
            if info is None:
                if bootstrap:
                    # A saved bootstrap id that can't be resumed (e.g. it now points
                    # at a codex thread, or the session was deleted) must NOT crash
                    # startup — fall back to a fresh session.
                    log.warning("saved bootstrap session not resumable; starting fresh", session_id=resume_id)
                    resume_id = None
                    target_cwd = self.cfg.cc_cwd
                else:
                    await reject(
                        ERR_INTERNAL, f"session not found: {resume_id}")
                    return None
            else:
                target_cwd = info.cwd or self.cfg.cc_cwd
            # The session's original cwd may be gone (e.g. a deleted /tmp scratch
            # dir). cc can't chdir into a missing dir → "Working directory does not
            # exist" crash on switch. Recreate it (empty) so resume still works —
            # history loads by session id regardless; fall back to the default cwd
            # only if recreation fails.
            if not os.path.isdir(target_cwd):
                try:
                    os.makedirs(target_cwd, exist_ok=True)
                    log.warning("recreated missing session cwd for resume", session_id=resume_id, cwd=target_cwd)
                except Exception as e:
                    log.warning("session cwd missing, using default", session_id=resume_id, cwd=target_cwd, error=str(e))
                    target_cwd = self.cfg.cc_cwd
        elif cwd:
            target_cwd = os.path.realpath(os.path.expanduser(cwd))
            if not os.path.isdir(target_cwd):
                await reject(
                    ERR_INTERNAL, f"目录不存在: {cwd}",
                    typed_code=ERR_INVALID_CWD)
                return None
        else:
            target_cwd = self.cfg.cc_cwd

        work_record = None
        if space == "work":
            store = self._work.for_engine(engine)
            work_record = (await asyncio.to_thread(store.get_by_session, resume_id)
                           if resume_id else
                           await asyncio.to_thread(store.get_by_work_id, work_id or ""))
            if work_record is None:
                await reject(
                    ERR_AUTH, "Work 会话注册信息不存在，已拒绝启动",
                    route="sid", sid=resume_id)
                return None
            registered_cwd = os.path.realpath(work_record.cwd)
            if os.path.realpath(target_cwd) != registered_cwd:
                await reject(
                    ERR_AUTH, "Work 会话目录与原生 Session 不一致，已拒绝启动",
                    route="sid", sid=resume_id)
                return None
            if not store.contains_cwd(registered_cwd):
                await reject(
                    ERR_AUTH, "Work 会话目录越过受控根目录，已拒绝启动",
                    route="sid", sid=resume_id)
                return None
            work_id = work_record.work_id

        codex_profile_catalog: list[dict] = []
        codex_profile_catalog_loaded = False
        codex_profile_catalog_error: Optional[Exception] = None

        async def codex_profile_allowed(profile_id: str) -> bool:
            nonlocal codex_profile_catalog
            nonlocal codex_profile_catalog_loaded
            nonlocal codex_profile_catalog_error
            if not codex_profile_catalog_loaded:
                codex_profile_catalog_loaded = True
                try:
                    codex_profile_catalog = (
                        await codex_permission_profiles(target_cwd))
                except Exception as exc:
                    codex_profile_catalog_error = exc
            if codex_profile_catalog_error is not None:
                raise codex_profile_catalog_error
            return any(
                profile["id"] == profile_id and profile["allowed"]
                for profile in codex_profile_catalog
            )

        if (engine == "codex" and space == "code"
                and isinstance(permission_profile, str)
                and permission_profile):
            try:
                selected_profile_allowed = await codex_profile_allowed(
                    permission_profile)
            except Exception:
                log.exception(
                    "new-session permission profile validation failed",
                    cwd=target_cwd,
                )
                await reject(
                    ERR_INTERNAL,
                    "执行环境状态无法确认，未创建会话。",
                    route="sid",
                    sid=resume_id,
                )
                return None
            if not selected_profile_allowed:
                await reject(
                    ERR_AUTH,
                    "当前目录不允许使用所选执行环境，未创建会话。",
                    route="sid",
                    sid=resume_id,
                )
                return None

        if (resume_id and engine == "claude" and space == "code"
                and broker_handle is None):
            # Explicit command controls win. Otherwise restore only the private
            # session override owned by Remote, never Claude's global settings.
            saved_controls = await self._load_claude_session_controls(resume_id)
            model = model or saved_controls.model
            effort = effort or saved_controls.effort
            permission_mode = (
                permission_mode or saved_controls.permission_mode)
        elif engine == "claude" and resume_id is None and model is None:
            # Resolve the cwd-aware default at spawn time so a fresh session
            # starts on the model shown by Remote. The browser still sends null
            # for an implicit choice; this read is local, current, and cannot be
            # stale across a settings/provider change.
            model, _default_effort = await self._claude_new_session_defaults(
                target_cwd)

        sdk = (
            (CodexHandle(
                self.cfg,
                cwd=target_cwd,
                work_mode=True,
                daemon_mode="off",
                daemon_manager=self._codex_daemon,
            ) if space == "work" else CodexHandle(
                self.cfg,
                cwd=target_cwd,
                daemon_mode=getattr(self.cfg, "codex_daemon_mode", "auto"),
                daemon_manager=self._codex_daemon,
            ))
            if engine == "codex" else (broker_handle or SdkHandle(self.cfg))
        )
        if space == "work":
            if engine == "codex":
                # The named Work permission profile grants autonomous access only
                # inside this registered cwd. Never allow approval escalation to
                # turn an outside-profile denial into broader host access.
                sdk.approval = "never"
            else:
                assert work_record is not None
                sdk.work_mode = True
                sdk.work_settings_path = await asyncio.to_thread(
                    self._work.for_engine("claude").ensure_claude_policy,
                    work_record)
                sdk.permission_mode = "acceptEdits"
        elif (engine == "claude"
              and permission_mode in CLAUDE_PERMISSION_MODES):
            sdk.permission_mode = permission_mode
        # Pre-select effort at spawn (before connect): cc reads it via _options at
        # connect so --effort is baked into the first turn (no respawn); codex uses
        # it as a per-turn param. codex model is also a per-turn field, so set it
        # here; cc's model needs a live set_model AFTER connect (below). Set
        # applied_effort too so _run_turn's "effort != applied" reconnect check
        # sees the first turn as already-applied (cc's connect re-syncs it anyway;
        # this is what keeps codex from a spurious first-turn reconnect).
        if engine == "codex":
            preserve_codex_controls = False
            preserve_codex_permission_profile = False
            if collaboration_mode in CODEX_COLLABORATION_MODES:
                sdk.collaboration_mode = collaboration_mode
            if (space != "work" and permission_mode in CODEX_PERMISSION_MODES):
                sdk.approval = permission_mode
                preserve_codex_controls = True
            if (space != "work" and isinstance(permission_profile, str)
                    and permission_profile):
                sdk.permission_profile = permission_profile
                preserve_codex_controls = True
                preserve_codex_permission_profile = True
            if space != "work" and web_search in CODEX_WEB_SEARCH_MODES:
                sdk.web_search_override = web_search
                sdk.web_search = web_search
            if service_tier in {"default", "fast"}:
                sdk.service_tier = (
                    "fast" if service_tier == "fast" else None)
            # Seed from the session's own bounded rollout tail, never config.toml.
            # CodexHandle.connect then adopts thread/resume's authoritative fields;
            # the rollout remains required for collaboration mode, which 0.144.1's
            # resume response does not expose.
            if resume_id:
                controls = saved_codex_controls
                restored_control_profile = False
                if (space != "work" and permission_mode is None
                        and controls.approval_policy
                        in CODEX_PERMISSION_MODES):
                    sdk.approval = controls.approval_policy
                    preserve_codex_controls = True
                if (space != "work" and permission_profile is None
                        and controls.permission_profile):
                    try:
                        restored_control_profile = (
                            await codex_profile_allowed(
                                controls.permission_profile))
                    except Exception as exc:
                        log.warning(
                            "persisted Codex permission profile could not be "
                            "revalidated; using native default",
                            session_id=resume_id,
                            error_type=type(exc).__name__,
                        )
                    if restored_control_profile:
                        sdk.permission_profile = controls.permission_profile
                        preserve_codex_controls = True
                        preserve_codex_permission_profile = True
                    else:
                        log.warning(
                            "stale or disallowed persisted Codex permission "
                            "profile discarded",
                            session_id=resume_id,
                            permission_profile=controls.permission_profile,
                        )
                if space != "work" and web_search is None:
                    if controls.web_search in CODEX_WEB_SEARCH_MODES:
                        sdk.web_search_override = controls.web_search
                        sdk.web_search = controls.web_search
                prev = await asyncio.to_thread(
                    codex_session_settings, resume_id,
                    self.cfg.history_source_max_bytes)
                model = model or prev.get("model")
                effort = effort or prev.get("effort")
                approval = prev.get("approval_policy")
                if (space != "work" and permission_mode is None
                        and controls.approval_policy is None
                        and approval in CODEX_PERMISSION_MODES):
                    sdk.approval = approval
                    preserve_codex_controls = True
                if (space != "work" and permission_profile is None
                        and not restored_control_profile
                        and "permission_profile" in prev):
                    previous_profile = prev.get("permission_profile")
                    previous_profile_allowed = previous_profile is None
                    if isinstance(previous_profile, str) and previous_profile:
                        try:
                            previous_profile_allowed = (
                                await codex_profile_allowed(previous_profile))
                        except Exception as exc:
                            log.warning(
                                "rollout Codex permission profile could not be "
                                "revalidated; using native default",
                                session_id=resume_id,
                                error_type=type(exc).__name__,
                            )
                    if previous_profile_allowed:
                        sdk.permission_profile = previous_profile
                        preserve_codex_controls = True
                        preserve_codex_permission_profile = True
                    elif previous_profile is not None:
                        log.warning(
                            "stale or disallowed rollout Codex permission "
                            "profile discarded",
                            session_id=resume_id,
                            permission_profile=previous_profile,
                        )
                if service_tier is None and "service_tier" in prev:
                    tier = prev.get("service_tier")
                    if tier is None or isinstance(tier, str):
                        sdk.service_tier = tier
                mode = prev.get("collaboration_mode")
                if mode in CODEX_COLLABORATION_MODES:
                    sdk.collaboration_mode = mode
            if model:
                sdk.model = model
                # A stale client can ask for a level this model doesn't have (it used
                # to offer `minimal`, and `ultra` on luna). codex takes any string here
                # and only fails inside the model API, so clamp against the real catalog.
                effort = await clamp_effort(model, effort)
        if effort:
            sdk.effort = effort
            sdk.applied_effort = effort
        work_context_baseline = (
            work_record.context_baseline_tokens if work_record else None
        )
        if (work_record is not None and work_context_baseline is None
                and resume_id):
            recovered = await asyncio.to_thread(
                recover_work_context_baseline, engine, resume_id)
            if recovered is not None:
                try:
                    work_context_baseline = await asyncio.to_thread(
                        self._work.for_engine(engine).set_context_baseline,
                        work_record.work_id, recovered,
                    )
                except Exception:
                    # History recovery is optional migration metadata. Resume
                    # the native session and keep its honest raw context gauge.
                    log.exception(
                        "migrated Work context baseline persistence failed",
                        engine=engine,
                        work_id=work_record.work_id,
                    )
        ctx = SessionContext(
            session_id=resume_id,
            sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=target_cwd,
            engine=engine,
            space=space,
            work_id=work_id,
            work_context_baseline_tokens=work_context_baseline,
            work_context_baseline_pending=bool(
                work_record is not None
                and work_record.context_baseline_tokens is None
                and work_record.session_id is None
                and resume_id is None
            ),
        )
        # Per-ctx MCP ask server is Claude-only (the cc-remote-ask tools). Codex
        # handles approvals through its own app-server protocol, so skip it.
        if engine != "codex" and broker_handle is None:
            self._configure_claude_sdk_callbacks(ctx, ctx.sdk)
        elif engine == "codex":
            ctx.sdk.approval_callback = (
                lambda method, params: self._on_codex_approval(
                    ctx, method, params))
            ctx.sdk.interaction_callback = (
                lambda method, params: self._on_codex_interaction(
                    ctx, method, params))
            ctx.sdk.goal_callback = (
                lambda goal: self._on_codex_goal(ctx, goal))
            ctx.sdk.turn_lifecycle_callback = (
                lambda phase, turn_id: self._on_codex_turn_lifecycle(
                    ctx, phase, turn_id))
            ctx.sdk.runtime_event_callback = (
                lambda event: self._on_codex_runtime_event(ctx, event))

        try:
            if engine == "codex":
                codex_connect_options = {
                    "resume_id": resume_id,
                    "cwd": target_cwd,
                    "preserve_controls": preserve_codex_controls,
                }
                if (resume_id and preserve_codex_controls
                        and not preserve_codex_permission_profile):
                    codex_connect_options[
                        "preserve_permission_profile"] = False
                await ctx.sdk.connect(
                    **codex_connect_options,
                )
                if (
                    resume_id
                    and space == "code"
                    and (
                        not isinstance(getattr(ctx.sdk, "cwd", None), str)
                        or os.path.realpath(ctx.sdk.cwd) != target_cwd
                    )
                ):
                    await ctx.sdk.set_cwd(
                        target_cwd,
                        reason="resume cwd reconciliation",
                    )
                effective_cwd = getattr(ctx.sdk, "cwd", None)
                if isinstance(effective_cwd, str):
                    effective_cwd = os.path.realpath(effective_cwd)
                    if not os.path.isdir(effective_cwd):
                        raise RuntimeError(
                            "Codex app-server reported a missing cwd"
                        )
                    ctx.cwd = effective_cwd
                    target_cwd = effective_cwd
            else:
                await ctx.sdk.connect(
                    resume_id=resume_id, cwd=target_cwd)
        except Exception as e:
            if bootstrap and resume_id:
                log.warning("resume failed, starting a fresh session", error=str(e))
                ctx.session_id = None
                try:
                    await ctx.sdk.connect(resume_id=None, cwd=target_cwd)
                except Exception as e2:
                    log.exception("fresh connect also failed", error=str(e2))
                    await reject(ERR_CC_CRASH, "会话连接未完成，请稍后重试。")
                    return None
            else:
                log.exception("connect failed", error=str(e))
                await reject(ERR_CC_CRASH, "会话连接未完成，请稍后重试。")
                return None
        await self._stamp_codex_daemon_epoch(ctx)

        if (ctx.space == "work" and ctx.work_context_baseline_pending
                and ctx.work_context_baseline_tokens is None):
            sampled = getattr(ctx.sdk, "work_context_baseline_tokens", None)
            if isinstance(sampled, int) and not isinstance(sampled, bool) and sampled >= 0:
                ctx.work_context_baseline_tokens = sampled

        if broker_handle is not None:
            ctx.claude_broker_generation = broker_handle.generation
            metadata = broker_handle.metadata
            await self._set_session_control(
                ctx,
                control_mode="claude_broker",
                write_state=(
                    "input_busy" if metadata.get("input_busy") else "writable"
                ),
                terminal_attached=bool(metadata.get("attached_count", 0)),
                reason=(
                    "本机终端正在编辑输入，完成或取消后即可从 Remote 发送"
                    if metadata.get("input_busy") else None
                ),
                can_takeover=False,
                emit=False,
            )

        # Unlike Claude, Codex returns its durable thread id from thread/start
        # before the first turn. Bind it now so SessionFocus, artifact reads and
        # the sidebar never observe a temporary id.
        if engine == "codex" and not resume_id:
            native_sid = getattr(ctx.sdk, "thread_id", None)
            if not isinstance(native_sid, str) or not native_sid:
                log.error("fresh Codex session missing thread id")
                try:
                    await ctx.sdk.disconnect()
                except Exception:
                    pass
                await reject(
                    ERR_CC_CRASH, "Codex 未返回新会话 ID，已拒绝创建")
                return None
            ctx.session_id = native_sid
            if space == "work" and work_id:
                try:
                    await asyncio.to_thread(
                        self._work.for_engine("codex").bind_session,
                        work_id, native_sid)
                except Exception:
                    log.exception(
                        "fresh Codex Work session binding failed",
                        work_id=work_id)
                    try:
                        await ctx.sdk.disconnect()
                    except Exception:
                        pass
                    await reject(ERR_INTERNAL, "Codex Work 会话登记失败")
                    return None
            self._invalidate_codex_session_catalog()

        if space == "work" and engine == "codex":
            # thread/resume may restore the Code-time policy recorded in the
            # native rollout. Work always reasserts its non-escalating profile.
            ctx.sdk.approval = "never"
            ctx.sdk.permission_profile = "cc_remote_work"

        if engine == "codex" and space == "code":
            await self._persist_codex_session_controls(ctx)

        if engine == "codex" and space == "code":
            await self._set_session_control(
                ctx,
                control_mode=(
                    "codex_shared"
                    if self._codex_shared_affinity(ctx)
                    else "remote"
                ),
                write_state="writable",
                terminal_attached=False,
                reason=None,
                can_takeover=False,
                emit=False,
            )

        # cc model is a runtime switch on the live subprocess (set_model), so apply
        # a pre-selected model now that we're connected. codex was set pre-connect.
        if model and engine != "codex":
            try:
                await ctx.sdk.set_model(model)
            except Exception as e:
                log.warning("spawn set_model failed", model=model, error=str(e))
                if explicit_claude_model and raise_on_failure:
                    try:
                        await ctx.sdk.disconnect()
                    except Exception:
                        log.warning(
                            "disconnect after explicit model failure failed")
                    await reject(
                        ERR_INTERNAL,
                        "所选 Claude 模型暂不可用，请检查 Provider 配置后重试。",
                    )
                    return None
                # An implicit curated default is a preference, not a reason to
                # discard an otherwise healthy provider connection. Report only
                # the model proven by the connect-time control probe.
                model = getattr(ctx.sdk, "model", None)
            else:
                await self._refresh_pending_claude_work_baseline(ctx)
                model = getattr(ctx.sdk, "model", None) or model
        if (engine == "claude" and space == "code" and resume_id
                and broker_handle is None):
            # Seed a migrated/existing SDK-owned session as soon as its native
            # controls are known; the next claude-remote resume must not depend
            # on the user changing every chip once after an upgrade.
            await self._persist_claude_session_controls(ctx)
        # Record the pre-selected values so _run_turn doesn't redundantly re-announce
        # them (the client already reflects its own pick optimistically).
        if model:
            ctx.announced_model = model
        if effort:
            ctx.announced_effort = effort
        # Codex knows its real id at connect time. Claude still uses a temporary
        # key until its first init/result message exposes the SDK session id.
        key = ctx.session_id or f"tmp-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        if resume_id:
            self._watch_session(resume_id)
            if engine == "codex":
                await self._recover_codex_owned_turn(ctx, resume_id)
                await self._prime_codex_ownership(resume_id)
            elif engine == "claude" and broker_handle is None:
                await self._prime_claude_ownership(resume_id)
            else:
                await self._sync_external_control(
                    ctx, self._watch.get(resume_id))
        if resume_id and engine != "codex":
            save_session_id(self.cfg.state_dir, target_cwd, resume_id)
        await self._load_history(ctx, resume_id)
        if bootstrap:
            ctx.announced_perm = _session_permission_mode(ctx)
            await self._emit(ctx, Perm(mode=ctx.announced_perm))
            if engine == "codex":
                ctx.announced_permission_profile = (
                    _session_permission_profile(ctx))
                await self._emit(ctx, PermissionProfile(
                    profile=ctx.announced_permission_profile))
                web_search = _session_web_search(ctx)
                if web_search:
                    ctx.announced_web_search = web_search
                    await self._emit(ctx, WebSearch(mode=web_search))
                collaboration_mode = getattr(
                    ctx.sdk, "collaboration_mode", "default")
                ctx.announced_collaboration_mode = collaboration_mode
                await self._emit(ctx, CollaborationMode(
                    mode=collaboration_mode))
                await self._emit(ctx, Fast(
                    on=_codex_fast_on(ctx.sdk.service_tier)))
        log.info("session spawned", resume=resume_id, cwd=target_cwd, key=key,
                 resident=len(self.sessions))
        return ctx

    async def _spawn_btw(
        self, parent: SessionContext, owner_client_id: Optional[str] = None,
    ) -> SessionContext:
        """Spawn an ephemeral /btw fork of `parent`: a throwaway side-session that
        inherits the parent's context (codex thread/fork · cc fork_session) and
        streams under a stable `btw-<uuid>` key. Never persisted / listed / focused;
        discarded on close. Its turns reuse the normal _run_turn path."""
        if not owner_client_id:
            raise _BtwSpawnFailure(ERR_AUTH, "btw requires a bound client")
        pending_private_forks = sum(
            1 for resident in self.sessions.values()
            if resident.btw and resident.engine != "codex"
            and not resident.btw_real_id
        )
        if (len(self._private_btw_sessions) + pending_private_forks
                >= self.PRIVATE_BTW_CAP):
            raise _BtwSpawnFailure(
                ERR_BUSY, "临时侧边会话已满，请稍后重试。")
        parent_id = parent.session_id
        if not parent_id:
            raise _BtwSpawnFailure(
                ERR_INTERNAL, "这个会话还没有上下文,先发一条消息再开 btw")
        engine = parent.engine
        if engine != "codex":
            try:
                SdkHandle.preflight(self.cfg.claude_bin)
            except Exception as exc:
                log.warning("Claude preflight failed for btw", error=str(exc))
                raise _BtwSpawnFailure(
                    ERR_CC_CRASH, "Claude 暂时不可用，请稍后重试。") from exc
        # btw counts toward the cap; evict an idle, non-focused, non-btw victim.
        if len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle"
                           and not c.btw and not c.queued_queries
                           and not self._query_queue_task_active(c)), None)
            if victim is None:
                raise _BtwSpawnFailure(ERR_BUSY, "会话已满,先关闭一个再开 btw")
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for btw", key=victim)
        sdk = (CodexHandle(
            self.cfg,
            cwd=parent.cwd,
            daemon_mode=getattr(self.cfg, "codex_daemon_mode", "auto"),
            daemon_manager=self._codex_daemon,
        ) if engine == "codex" else SdkHandle(self.cfg))
        if engine != "codex":
            sdk.permission_mode = getattr(
                parent.sdk, "permission_mode", "bypassPermissions")
        # /btw is a quick side question — run the fork at LOW effort so the first
        # reply is snappy (the parent's own effort can be high/xhigh, which makes a
        # context-inheriting fork slow). Applied at connect (cc) / per-turn (codex).
        sdk.effort = "low"
        ctx = SessionContext(
            session_id=None, sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=parent.cwd, engine=engine, btw=True, parent_sid=parent_id,
            owner_client_id=owner_client_id)
        if engine != "codex":
            self._configure_claude_sdk_callbacks(ctx, ctx.sdk)
        else:
            ctx.sdk.approval = parent.sdk.approval
            ctx.sdk.approval_policy = parent.sdk.approval_policy
            ctx.sdk.permission_profile = parent.sdk.permission_profile
            ctx.sdk.web_search_override = (
                parent.sdk.web_search_override)
            ctx.sdk.web_search = parent.sdk.web_search
            ctx.sdk.approval_callback = (
                lambda method, params: self._on_codex_approval(
                    ctx, method, params))
            ctx.sdk.interaction_callback = (
                lambda method, params: self._on_codex_interaction(
                    ctx, method, params))
            ctx.sdk.goal_callback = (
                lambda goal: self._on_codex_goal(ctx, goal))
            ctx.sdk.turn_lifecycle_callback = (
                lambda phase, turn_id: self._on_codex_turn_lifecycle(
                    ctx, phase, turn_id))
            ctx.sdk.runtime_event_callback = (
                lambda event: self._on_codex_runtime_event(ctx, event))
        try:
            await ctx.sdk.connect(resume_id=parent_id, cwd=parent.cwd, fork=True)
        except Exception as e:
            log.exception("btw fork connect failed", error=str(e))
            raise _BtwSpawnFailure(
                ERR_CC_CRASH, "临时侧边会话暂时无法打开，请稍后重试。"
            ) from e
        await self._stamp_codex_daemon_epoch(ctx)
        key = f"btw-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        log.info("btw fork spawned", parent=parent_id, key=key, engine=engine,
                 fork_thread=getattr(ctx.sdk, "thread_id", None))
        return ctx

    async def _load_history(self, ctx: SessionContext, session_id: Optional[str]) -> None:
        if not session_id or ctx.engine == "codex":
            return  # codex history replay (rollout files) is a later feature
        path = transcript_path(session_id)
        try:
            if path and os.path.getsize(path) > self.cfg.history_source_max_bytes:
                log.warning("history preload skipped: source too large",
                            session_id=session_id)
                return
        except OSError:
            pass
        try:
            msgs = await asyncio.to_thread(
                get_session_messages, session_id, directory=ctx.cwd,
            )
        except Exception as e:
            log.warning("get_session_messages failed", session_id=session_id, error=str(e))
            return
        try:
            events = translate_history(msgs, self.cfg.tool_result_max)
            subagent_events = await asyncio.to_thread(
                translate_subagent_history, session_id, self.cfg.tool_result_max)
            events = merge_subagent_history(events, subagent_events)
            mdl = last_assistant_model(msgs)
        except Exception as e:
            # a single malformed history message must never break the resume — the
            # session still connects; the client just won't get the replayed history.
            log.exception("translate_history failed; resuming without replay", session_id=session_id, error=str(e))
            return
        for event in events:
            if isinstance(event, UserMsg):
                event.prompt, restored_files = self._strip_attachment_paths(event.prompt)
                if restored_files and not event.files:
                    event.files = restored_files
        async with ctx.emit_lock:
            authoritative_model = _session_model(ctx)
            history_model = authoritative_model or (
                mdl if mdl and mdl.startswith("claude-") else None)
            if history_model and history_model != ctx.announced_model:
                ctx.announced_model = history_model
                m = Model(model=history_model)
                m.seq = ctx.next_seq()
                m.sid = ctx.session_id
                ctx.buffer.append(m)
            effort = _session_effort(ctx)
            if effort and effort != ctx.announced_effort:
                ctx.announced_effort = effort
                e = Effort(effort=effort)
                e.seq = ctx.next_seq()
                e.sid = ctx.session_id
                ctx.buffer.append(e)
            for ev in events:
                ev.seq = ctx.next_seq()
                ev.sid = ctx.session_id
                ctx.buffer.append(ev)
        log.info("history loaded", session_id=session_id, events=len(events),
                 model=mdl, head=ctx.buffer.head_seq, tail=ctx.buffer.tail_seq)

    # ---- the per-turn consumer (reader task + queue), per ctx ----

    async def _next_from_queue(self, ctx: SessionContext, queue: asyncio.Queue):
        async def during_drain():
            deadline = ctx.interrupt_deadline
            remaining = (self.cfg.drain_timeout if deadline is None else
                         deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                try:
                    return queue.get_nowait()
                except asyncio.QueueEmpty as exc:
                    raise asyncio.TimeoutError from exc
            return await asyncio.wait_for(queue.get(), timeout=remaining)

        if ctx.state == "interrupting":
            return await during_drain()

        # A plain `await queue.get()` cannot notice a later interrupt. Race it
        # against the per-context event; if the event wins, keep waiting on the
        # queue only until the already-established absolute drain deadline.
        get_task = asyncio.create_task(queue.get())
        interrupt_task = asyncio.create_task(ctx.interrupt_event.wait())
        try:
            done, _ = await asyncio.wait(
                (get_task, interrupt_task), return_when=asyncio.FIRST_COMPLETED)
            if get_task in done:
                return get_task.result()
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            return await during_drain()
        finally:
            for task in (get_task, interrupt_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_task, interrupt_task, return_exceptions=True)

    def _stash_files(self, prompt: str, files: list, temp_dir: str,
                     engine: str = "claude") -> str:
        """Write validated files to a private per-turn directory. cc reads
        the `@path` convention; codex has no `@` layer over the app-server and
        ignores `mention` items (verified), but reads a plain path via its tools —
        so codex gets an explicit 'read these files' block with bare paths."""
        paths = []
        for i, f in enumerate(files):
            fn = f.get("filename") or f"file-{i}"
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(fn))
            safe = safe.strip(".") or f"file-{i}"
            path = os.path.join(temp_dir, f"{i:02d}-{safe}")
            data = decode_attachment(f.get("data", ""))
            with open(path, "xb") as fp:
                fp.write(data)
            os.chmod(path, 0o600)
            paths.append(path)
            log.info("attachment stashed", index=i, bytes=len(data))
        if engine == "codex":
            block = "[用户附件,请用工具读取以下文件]:\n" + "\n".join(paths)
        else:
            block = " ".join(f"@{p}" for p in paths)
        return (prompt + "\n\n" if prompt else "") + block

    @staticmethod
    def _strip_attachment_paths(prompt: str) -> tuple[str, list[dict[str, str]]]:
        """Remove expired private temp paths from transcript-facing history."""
        path_re = re.compile(
            r"(?:/\S*cc-remote-turn-[A-Za-z0-9_-]+|"
            r"/\S*/cc-remote/work/chats/work-[0-9a-f]+/uploads/[A-Za-z0-9._:@-]+)"
            r"/\d{2}-([^\s]+)")
        marker = "\n\n[用户附件,请用工具读取以下文件]:\n"
        if marker in prompt:
            original, block = prompt.rsplit(marker, 1)
            names = [match.group(1) for match in path_re.finditer(block)]
            if names:
                return original, [{"filename": name} for name in names]

        matches = list(re.finditer(
            r"@(?P<path>(?:/\S*cc-remote-turn-[A-Za-z0-9_-]+|"
            r"/\S*/cc-remote/work/chats/work-[0-9a-f]+/uploads/[A-Za-z0-9._:@-]+)"
            r"/\d{2}-(?P<name>[^\s]+))",
            prompt,
        ))
        if matches:
            suffix = prompt[matches[0].start():]
            without_paths = re.sub(r"@/\S+", "", suffix)
            if not without_paths.strip():
                return (
                    prompt[:matches[0].start()].rstrip(),
                    [{"filename": match.group("name")} for match in matches],
                )
        return prompt, []

    def _stash_images(self, images: list, temp_dir: str) -> list:
        """Write validated images to the private turn directory for Codex."""
        _ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/webp": ".webp"}
        paths = []
        for i, img in enumerate(images or []):
            mt = img.get("media_type", "image/png")
            path = os.path.join(temp_dir, f"image-{i:02d}{_ext[mt]}")
            data = decode_attachment(img.get("data", ""))
            with open(path, "xb") as fp:
                fp.write(data)
            os.chmod(path, 0o600)
            paths.append(path)
            log.info("image stashed", index=i, bytes=len(data))
        return paths

    def _cleanup_tmp(self) -> None:
        try:
            cutoff = time.time() - 24 * 3600
            tmp_root = tempfile.gettempdir()
            turn_dir = re.compile(r"^cc-remote-turn-[A-Za-z0-9_-]+$")
            legacy_file = re.compile(r"^cc-remote-\d{10}-[A-Za-z0-9._-]+$")
            with os.scandir(tmp_root) as entries:
                for index, entry in enumerate(entries):
                    if index >= 100_000:
                        log.warning("tmp cleanup scan capped", entries=index)
                        break
                    try:
                        if entry.is_symlink():
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if info.st_uid != os.getuid() or info.st_mtime >= cutoff:
                            continue
                        if turn_dir.fullmatch(entry.name) and entry.is_dir(
                                follow_symlinks=False):
                            shutil.rmtree(entry.path)
                        elif legacy_file.fullmatch(entry.name) and entry.is_file(
                                follow_symlinks=False):
                            os.remove(entry.path)
                    except (FileNotFoundError, PermissionError):
                        continue
        except Exception as e:
            log.warning("tmp cleanup failed", error=str(e))

    async def _run_claude_broker_turn(
        self,
        ctx: SessionContext,
        prompt: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
    ) -> None:
        """Submit one atomic prompt to the broker-owned official Claude TUI.

        The PTY stream is intentionally not parsed: ANSI output is a terminal
        presentation protocol, not a durable conversation API.  Claude remains
        the sole JSONL writer, so the transcript supplies both the mirrored
        history and the authoritative user/end-turn boundaries used here.
        """
        started_at = time.monotonic()
        temp_dir: Optional[str] = None
        path: Optional[str] = None
        offset = 0
        file_id: Optional[tuple[int, int]] = None
        partial = b""
        saw_user_boundary = False
        terminal_id: Optional[str] = None
        last_status_at = 0.0
        last_mirror_at = 0.0
        file_meta = ([{"filename": item.get("filename", "attachment")}
                      for item in (files or [])] or None)

        async def close_turn(*, error: bool, subtype: str) -> None:
            duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
            await self._emit(ctx, TurnEnd(
                result=TurnResult(
                    subtype=subtype,
                    duration_ms=duration_ms,
                    is_error=error,
                ),
                turn_id=terminal_id,
            ))
            await self._set_idle_after_managed_turn(ctx)

        try:
            if not ctx.session_id:
                raise BrokerClientError(
                    "invalid_status", "broker session id is unavailable")

            # Establish the append boundary before the broker accepts input. A
            # very fast Claude response can otherwise finish before send()
            # returns and be mistaken for old history.
            path = transcript_path(ctx.session_id)
            if path:
                try:
                    before = os.stat(path)
                except OSError:
                    path = None
                else:
                    offset = before.st_size
                    file_id = (before.st_dev, before.st_ino)

            async with ctx.launch_lock:
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    await self._emit(ctx, Error(
                        code=ERR_BUSY,
                        message="消息在写入 Claude TUI 前已打断",
                        msg_id=ctx.active_msg_id,
                    ))
                    await close_turn(error=True, subtype="error_during_execution")
                    return

                if files or images:
                    temp_dir = tempfile.mkdtemp(prefix="cc-remote-turn-")
                    os.chmod(temp_dir, 0o700)
                if files:
                    assert temp_dir is not None
                    prompt = self._stash_files(
                        prompt, files, temp_dir, engine="claude")
                if images:
                    assert temp_dir is not None
                    image_paths = self._stash_images(images, temp_dir)
                    image_block = " ".join(f"@{image_path}" for image_path in image_paths)
                    prompt = (prompt + "\n\n" if prompt else "") + image_block

                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    await self._emit(ctx, Error(
                        code=ERR_BUSY,
                        message="消息在写入 Claude TUI 前已打断",
                        msg_id=ctx.active_msg_id,
                    ))
                    await close_turn(error=True, subtype="error_during_execution")
                    return
                await ctx.sdk.submit(prompt)
                # The broker accepted one atomic prompt+Enter. Only now publish
                # the authoritative echo: a terminal half-line can race the
                # earlier status probe and make submit fail with input_busy.
                await self._emit(ctx, UserMsg(
                    msg_id=ctx.active_msg_id or uuid4().hex,
                    prompt=prompt,
                    images=images,
                    files=file_meta,
                ))

            # Keep polling indefinitely for a normal turn.  Long reasoning and
            # tools are valid; only an explicit interrupt gets the existing
            # bounded drain deadline.
            while True:
                now = time.monotonic()
                if now - last_status_at >= 0.5:
                    metadata = await ctx.sdk.refresh_status()
                    last_status_at = now
                    await self._sync_external_control(
                        ctx, self._watch.get(ctx.session_id))
                    if metadata.get("running") is not True:
                        raise BrokerClientError(
                            "session_exited",
                            "official Claude TUI exited before the turn completed",
                        )

                current_path = transcript_path(ctx.session_id)
                if current_path:
                    path = current_path
                    try:
                        current = os.stat(path)
                    except OSError:
                        current = None
                    if current is not None:
                        current_id = (current.st_dev, current.st_ino)
                        if file_id is None:
                            # A new broker session creates its transcript only
                            # after input. Read from byte zero in that case.
                            file_id = current_id
                            offset = 0
                        elif current_id != file_id or current.st_size < offset:
                            file_id = current_id
                            offset = 0
                            partial = b""
                        if current.st_size > offset:
                            data = await asyncio.to_thread(
                                self._read_watch_growth,
                                path,
                                offset,
                                current.st_size - offset,
                            )
                            if data:
                                offset += len(data)
                                lifecycle = parse_claude_broker_lifecycle(
                                    data, partial)
                                partial = lifecycle.partial
                                for kind, event_id in lifecycle.ordered:
                                    if kind == "started":
                                        saw_user_boundary = True
                                    elif saw_user_boundary:
                                        terminal_id = event_id
                                # History is the public content stream for the
                                # official TUI. Bound live refreshes while still
                                # guaranteeing a final authoritative mirror.
                                if now - last_mirror_at >= 0.1:
                                    self._watch_session(ctx.session_id)
                                    await self._push_mirrored_history(ctx.session_id)
                                    last_mirror_at = now
                                if terminal_id is not None:
                                    await self._push_mirrored_history(ctx.session_id)
                                    interrupted = ctx.state == "interrupting"
                                    await close_turn(
                                        error=interrupted,
                                        subtype=("error_during_execution"
                                                 if interrupted else "success"),
                                    )
                                    return

                if ctx.state == "interrupting":
                    deadline = ctx.interrupt_deadline
                    if deadline is not None and now >= deadline:
                        if path:
                            await self._push_mirrored_history(ctx.session_id)
                        await close_turn(
                            error=True, subtype="error_during_execution")
                        return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except BrokerClientError as exc:
            log.warning(
                "Claude broker turn failed",
                session_id=ctx.session_id,
                error_code=exc.code,
            )
            await self._emit(ctx, Error(
                code=(ERR_BUSY if exc.code in {
                    "input_busy", "input_read_only", "stale_generation"
                } else ERR_CC_CRASH),
                message="Claude 本次回复未完成，请稍后重试。",
                msg_id=ctx.active_msg_id,
            ))
            await close_turn(error=True, subtype="error_during_execution")
            # A proven terminal exit should restore Remote write access now,
            # not on a later SwitchSession. Transient socket loss is handled by
            # the same refresh path but remains fail-closed on the broker handle.
            await self._refresh_claude_broker_handle(ctx)
        except Exception as exc:
            log.exception("Claude broker turn failed", error=str(exc))
            await self._emit(ctx, Error(
                code=ERR_CC_CRASH,
                message="Claude 本次回复未完成，请稍后重试。",
                msg_id=ctx.active_msg_id,
            ))
            await close_turn(error=True, subtype="error_during_execution")
        finally:
            if ctx.turn_task is asyncio.current_task():
                ctx.turn_task = None
            ctx.active_msg_id = None
            ctx.interrupt_deadline = None
            ctx.interrupt_event.clear()
            if temp_dir is not None:
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    log.warning(
                        "broker turn attachment cleanup failed", error=str(exc))

    async def _run_turn(self, ctx: SessionContext, prompt: str,
                        images: Optional[list] = None, files: Optional[list] = None) -> None:
        is_codex = ctx.engine == "codex"
        is_codex_shared = self._codex_shared_affinity(ctx)
        ctx.translator = (CodexStreamTranslator(self.cfg.tool_result_max) if is_codex
                          else StreamTranslator(
                              self.cfg.tool_result_max,
                              turn_id=ctx.active_msg_id,
                              item_turns=ctx.claude_item_turns,
                              item_titles=ctx.claude_item_titles,
                              item_meta=ctx.claude_item_meta,
                          ))
        # Backpressure the SDK reader if downstream handling stalls. Without a
        # bound, a slow relay/client could make one verbose model turn grow this
        # process until OOM even though transport queues themselves are bounded.
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=max(1, self.cfg.turn_reader_queue_cap))
        reader_exc: list = []
        reader_task: Optional[asyncio.Task] = None
        temp_dir: Optional[str] = None
        persistent_attachments = False
        notice_active = False
        claude_turn_completed = False
        codex_overflowed = False
        codex_overflow_repair_turn_id: Optional[str] = None
        codex_restart_watch_task: Optional[asyncio.Task] = None
        codex_handoff_to_spontaneous = False
        native_turn_id: Optional[str] = None

        async def reader(
            target_queue: asyncio.Queue, target_reader_exc: list,
        ) -> None:
            cancelled = False
            try:
                async for msg in ctx.sdk.receive_response():
                    await target_queue.put(msg)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as e:
                target_reader_exc.append(e)
            finally:
                if not cancelled:
                    await target_queue.put(None)

        async def next_turn_message():
            nonlocal notice_active
            """Wait for one raw engine event, warning on a silent Codex turn.

            This is deliberately not a hard timeout: ultra reasoning and long
            tools can be valid. A real interrupt still uses the existing bounded
            drain path, and any raw app-server event rearms the warning timer on
            the next loop iteration. A managed bridge overflow has already
            explained why live detail is delayed, so it must not be overwritten
            later by the generic no-progress warning.
            """
            warn = (
                self.cfg.codex_turn_idle_warn_seconds
                if is_codex and not codex_overflowed else 0
            )
            wait_task = asyncio.create_task(self._next_from_queue(ctx, queue))
            try:
                candidates = {wait_task}
                if codex_restart_watch_task is not None:
                    candidates.add(codex_restart_watch_task)
                timeout = (
                    warn if warn > 0 and ctx.state != "interrupting" else None)
                done, _ = await asyncio.wait(
                    candidates,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done and ctx.state == "running":
                    await self._emit(ctx, StateEvent(
                        state="running",
                        phase="waiting",
                        detail=(f"Codex 已 {warn:g} 秒没有收到新进展，仍在等待；"
                                "可点击停止。"),
                        msg_id=ctx.active_msg_id,
                    ))
                    notice_active = True
                    done, _ = await asyncio.wait(
                        candidates,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                # The hook writes its marker before asking the daemon to stop.
                # If a stale stream frame and the marker become ready together,
                # account handoff wins so an old terminal cannot falsely unlock
                # the browser queue.
                if (codex_restart_watch_task is not None
                        and codex_restart_watch_task in done):
                    return (
                        "codex_account_switch",
                        codex_restart_watch_task.result(),
                    )
                if wait_task in done:
                    # A graceful daemon stop may close the proxy in the same
                    # scheduler tick that the 50 ms watcher is polling. Re-read
                    # the bounded marker synchronously before accepting EOF or
                    # an old-generation terminal as authoritative.
                    if is_codex_shared and ctx.codex_daemon_epoch:
                        switch_state = read_restart_state(
                            self._codex_daemon_restart_path)
                        if (
                            switch_state is not None
                            and switch_state.epoch != ctx.codex_daemon_epoch
                        ):
                            return ("codex_account_switch", switch_state)
                    # Preserve a real drain-timeout exception from
                    # _next_from_queue.
                    return wait_task.result()
                return await wait_task
            finally:
                if not wait_task.done():
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)

        async def emit_codex_event(event) -> None:
            nonlocal notice_active
            # Translator errors/progress are turn-local, but the translator is
            # intentionally session-agnostic. Correlate them at the machine edge
            # so the web client renders the detail on the exact optimistic turn.
            if isinstance(event, Error) and event.msg_id is None:
                event.msg_id = ctx.active_msg_id
            if isinstance(event, StateEvent) and event.detail:
                # A retry notification may already be queued when the user
                # interrupts. Never regress interrupting/draining/idle back to
                # running just to display stale retry detail.
                if ctx.state != "running":
                    return
                event.state = ctx.state
                if event.msg_id is None:
                    event.msg_id = ctx.active_msg_id
            await self._emit(ctx, event)
            if isinstance(event, StateEvent) and event.detail:
                notice_active = True
            elif isinstance(event, Error):
                notice_active = False

        async def handoff_codex_account_switch(
            switch_state: CodexDaemonRestartState,
        ) -> str:
            """Move the current logical turn to the replacement daemon.

            Returns ``continued`` for an internal managed continuation,
            ``spontaneous`` when an active goal owns the continuation, or
            ``interrupted`` when the user stopped the task during handoff.
            """
            nonlocal queue, reader_exc, reader_task
            nonlocal codex_overflowed, codex_restart_watch_task
            nonlocal native_turn_id, notice_active

            ctx.codex_account_handoff = True
            await self._emit(ctx, StateEvent(
                state="running",
                phase="waiting",
                detail="Codex 账号已切换，正在把当前任务转移到新账号…",
                msg_id=ctx.active_msg_id,
            ))
            notice_active = True

            # The hook marker precedes daemon restart, so the old proxy should
            # still accept turn/interrupt. A disconnect race is harmless: the
            # generation barrier below remains authoritative.
            try:
                await ctx.sdk.interrupt()
            except Exception as exc:
                log.warning(
                    "old Codex turn could not be interrupted during account handoff",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )

            # Drain the old interrupted terminal briefly so open assistant/tool
            # blocks close in wire order, but suppress its failure boundary: this
            # is still logical turn A and the browser must remain busy.
            old_queue = queue
            old_reader_task = reader_task
            old_terminal_seen = False
            drain_deadline = (
                asyncio.get_running_loop().time()
                + min(2.0, self.cfg.drain_timeout)
            )
            while old_reader_task is not None:
                remaining = drain_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    old_message = await asyncio.wait_for(
                        old_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if old_message is None:
                    break
                if isinstance(old_message, dict):
                    old_terminal = is_turn_terminal(old_message)
                    for event in ctx.translator.feed(old_message):
                        if isinstance(event, (Error, TurnEnd)):
                            continue
                        await emit_codex_event(event)
                    if old_terminal:
                        old_terminal_seen = True
                        break
            if not old_terminal_seen:
                synthetic_old_terminal = {
                    "method": "turn/completed",
                    "params": {"turn": {
                        "id": native_turn_id,
                        "status": "interrupted",
                    }},
                }
                for event in ctx.translator.feed(synthetic_old_terminal):
                    if isinstance(event, (Error, TurnEnd)):
                        continue
                    await emit_codex_event(event)

            if old_reader_task is not None and not old_reader_task.done():
                old_reader_task.cancel()
            if old_reader_task is not None:
                await asyncio.gather(old_reader_task, return_exceptions=True)
            reader_task = None
            if (codex_restart_watch_task is not None
                    and not codex_restart_watch_task.done()):
                codex_restart_watch_task.cancel()
                await asyncio.gather(
                    codex_restart_watch_task, return_exceptions=True)
            codex_restart_watch_task = None

            # The restart worker may still be waiting for the old interrupted
            # turn to release. Do not connect until the exact/newest marker says
            # the official daemon restart completed successfully.
            ready_state = await self._codex_restart_state(
                wait=True,
                interrupt_event=ctx.interrupt_event,
            )
            if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                await self._emit(ctx, TurnEnd(result=TurnResult(
                    subtype="error_during_execution",
                    duration_ms=0,
                    is_error=True,
                ), turn_id=native_turn_id))
                ctx.codex_account_handoff = False
                return "interrupted"
            if ready_state is None or ready_state.phase != "ready":
                phase = ready_state.phase if ready_state is not None else "missing"
                raise RuntimeError(
                    f"Codex account-switch daemon restart did not become ready: {phase}"
                )
            if not await self._ensure_codex_daemon_generation(
                ctx, reason="continue active turn after account switch"
            ):
                raise RuntimeError(
                    "Codex account-switch daemon generation reconnect failed")

            if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                await self._emit(ctx, TurnEnd(result=TurnResult(
                    subtype="error_during_execution",
                    duration_ms=0,
                    is_error=True,
                ), turn_id=native_turn_id))
                ctx.codex_account_handoff = False
                return "interrupted"

            # Resuming an active goal can start its official automatic turn as
            # part of thread/resume. A usage-limited goal needs the same
            # transition as `/goal resume`; do not add a competing managed turn.
            try:
                goal = await ctx.sdk.get_goal()
            except Exception as exc:
                goal = None
                log.warning(
                    "Codex goal state unavailable during account handoff",
                    session_id=ctx.session_id,
                    error_type=type(exc).__name__,
                )
            resumable_goal = bool(
                isinstance(goal, dict)
                and goal.get("status") in {"active", "usageLimited"}
            )
            if resumable_goal:
                if ctx.codex_spontaneous_turn_id is not None:
                    if ctx.codex_checkpoint not in (None, False):
                        # thread/resume launched before a safe post-image could
                        # be captured. Do not snapshot a partially running turn.
                        await self._retire_codex_checkpoint(
                            ctx,
                            reason=(
                                "active goal resumed during Codex account handoff"
                            ),
                        )
                    return "spontaneous"
                # The old native turn has stopped and no replacement has started,
                # so this is the last safe post-image boundary. The automatic
                # Goal turn receives its normal unavailable checkpoint slot.
                await self._finish_codex_checkpoint(ctx)
                resumed = await self._resume_codex_goal_after_account_switch(
                    ctx, goal)
                if resumed:
                    return "spontaneous"
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    await self._emit(ctx, TurnEnd(result=TurnResult(
                        subtype="error_during_execution",
                        duration_ms=0,
                        is_error=True,
                    ), turn_id=native_turn_id))
                    ctx.codex_account_handoff = False
                    return "interrupted"
                raise RuntimeError(
                    "Codex Goal stopped before its account-switch "
                    "continuation started"
                )

            # Ordinary conversations have no resumable-computation RPC. Start a
            # native turn with a private marker that history and rollback
            # projection treat as part of the interrupted logical turn.
            ctx.translator = CodexStreamTranslator(self.cfg.tool_result_max)
            queue = asyncio.Queue(
                maxsize=max(1, self.cfg.turn_reader_queue_cap))
            reader_exc = []
            codex_overflowed = False
            try:
                native_turn_id = await ctx.sdk.query(
                    CODEX_ACCOUNT_SWITCH_CONTINUATION, images=[])
            except Exception:
                # An active goal may win the tiny gap after the bounded wait.
                if ctx.codex_spontaneous_turn_id is not None:
                    return "spontaneous"
                raise
            if native_turn_id and ctx.active_msg_id:
                await self._emit(ctx, TurnBinding(
                    msg_id=ctx.active_msg_id,
                    turn_id=native_turn_id,
                ))
            if native_turn_id:
                self._claim_codex_turn(
                    ctx, native_turn_id, ctx.active_msg_id)
            ctx.codex_account_handoff = False
            if native_turn_id and ctx.codex_daemon_epoch:
                codex_restart_watch_task = asyncio.create_task(
                    self._wait_for_codex_account_switch(
                        starting_epoch=ctx.codex_daemon_epoch,
                    )
                )
            reader_task = asyncio.create_task(reader(queue, reader_exc))
            await self._emit(ctx, StateEvent(
                state="running",
                phase=None,
                detail=None,
                msg_id=ctx.active_msg_id,
            ))
            notice_active = False
            log.info(
                "continued Codex turn after account switch",
                session_id=ctx.session_id,
                requested_epoch=switch_state.epoch,
                new_epoch=ctx.codex_daemon_epoch,
                turn_id=native_turn_id,
            )
            return "continued"

        async def reconnect_claude(reason: str) -> None:
            """Reconnect without hiding transcript changes during the await."""
            await ctx.sdk.force_reconnect(
                resume_id=ctx.session_id, cwd=ctx.cwd, reason=reason,
                preserve_model=not reason.startswith(
                    "external transcript change"))
            model = _session_model(ctx)
            if model and model != ctx.announced_model:
                ctx.announced_model = model
                await self._emit(ctx, Model(model=model))
            effort = _session_effort(ctx)
            if effort and effort != ctx.announced_effort:
                ctx.announced_effort = effort
                await self._emit(ctx, Effort(effort=effort))

        try:
            # An EXTERNAL process (a native `claude`/`codex` in the user's terminal)
            # appended to this session's transcript since we resumed it, so our child's
            # in-memory context is STALE — continuing from it would fork the
            # conversation. Reload by resuming afresh before issuing the turn.
            if ctx.needs_reload and ctx.session_id and is_codex_shared:
                # Rollout growth from another official proxy is already in the
                # shared daemon's authoritative thread state. Reconnecting here
                # only risks falling back to a private stdio process and creating
                # the split-brain this mode exists to avoid.
                ctx.needs_reload = False
            elif ctx.needs_reload and ctx.session_id:
                log.info("reloading session after external transcript change",
                         sid=ctx.session_id)
                if is_codex:
                    if ctx.codex_checkpoint not in (None, False):
                        await self._retire_codex_checkpoint(
                            ctx,
                            reason="external transcript change before query",
                            allow_restart=True,
                        )
                    await self._refresh_codex_collaboration_mode(ctx)
                    await ctx.sdk.force_reconnect(
                        resume_id=ctx.session_id, cwd=ctx.cwd,
                        reason="external transcript change")
                    ctx.needs_reload = False
                else:
                    # Clear first so a watcher that observes a new external write
                    # during reconnect can set it again without being overwritten.
                    ctx.needs_reload = False
                    await reconnect_claude("external transcript change")
            # apply a pending effort change: --effort is spawn-time, so respawn the
            # cc subprocess (resume preserves context) before issuing this turn. Only
            # fires when the level actually changed since the live client was spawned;
            # costs one resume (cold prompt cache) on the first turn after a change.
            if not is_codex and ctx.sdk.effort != ctx.sdk.applied_effort:
                log.info("applying effort change via reconnect", sid=ctx.session_id,
                         effort=ctx.sdk.effort, was=ctx.sdk.applied_effort)
                await reconnect_claude("effort change")
            # Serialize the final launch window against interrupt().  An interrupt
            # may have arrived while one of the reconnects above was in flight; in
            # that case it targeted no live turn and we must not submit the prompt
            # afterwards.  Re-check once more after the UserMsg send because that
            # await is also a scheduling point.
            file_meta = ([{"filename": item.get("filename", "attachment")}
                          for item in (files or [])] or None)
            async with ctx.launch_lock:
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    # Other clients do not have the origin's optimistic turn. Echo
                    # it before the terminal marker so TurnEnd cannot accidentally
                    # close the previous visible turn on those clients.
                    await self._emit(ctx, UserMsg(
                        msg_id=ctx.active_msg_id or uuid4().hex,
                        prompt=prompt,
                        images=images,
                        files=file_meta,
                    ))
                    await self._emit(ctx, TurnEnd(result=TurnResult(
                        subtype="error_during_execution",
                        duration_ms=0,
                        is_error=True,
                    )))
                    await self._set_idle_after_managed_turn(ctx)
                    return

                # Emit the authoritative user echo immediately before sdk.query(),
                # after any slow resume/reload. Its timestamp now matches the
                # transcript's user record closely enough to reconcile optimistic ids
                # without confusing repeated prompts such as "继续".
                await self._emit(ctx, UserMsg(
                    msg_id=ctx.active_msg_id or uuid4().hex,
                    prompt=prompt,
                    images=images,
                    files=file_meta,
                ))
                if files or (is_codex and images):
                    if ctx.space == "work":
                        upload_root = Path(ctx.cwd).parent / "uploads"
                        upload_dir = upload_root / (
                            ctx.active_msg_id or uuid4().hex)
                        upload_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                        temp_dir = str(upload_dir)
                        persistent_attachments = True
                    else:
                        temp_dir = tempfile.mkdtemp(prefix="cc-remote-turn-")
                        os.chmod(temp_dir, 0o700)
                if files:
                    prompt = self._stash_files(prompt, files, temp_dir, ctx.engine)
                if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                    await self._emit(ctx, TurnEnd(result=TurnResult(
                        subtype="error_during_execution",
                        duration_ms=0,
                        is_error=True,
                    )))
                    await self._set_idle_after_managed_turn(ctx)
                    return
                if not is_codex and ctx.session_id:
                    # A terminal can append after _handle_query's probe but before
                    # this task reaches sdk.query(). Consume both process state and
                    # transcript growth again at the final launch boundary.
                    external = await self._prime_claude_ownership(ctx.session_id)
                    if (ctx.interrupt_event.is_set()
                            or ctx.state == "interrupting"):
                        await self._emit(ctx, TurnEnd(result=TurnResult(
                            subtype="error_during_execution",
                            duration_ms=0,
                            is_error=True,
                        )))
                        await self._set_idle_after_managed_turn(ctx)
                        return
                    if external:
                        await self._emit(ctx, Error(
                            code=ERR_BUSY,
                            message=("该 Claude 会话刚被本机终端打开，本次发送已取消；"
                                     "请退出终端或点击『接管』后重试"),
                            msg_id=ctx.active_msg_id,
                        ))
                        await self._set_idle_after_managed_turn(ctx)
                        return
                    if ctx.needs_reload:
                        log.info(
                            "reloading Claude session after transcript change "
                            "found at final preflight",
                            sid=ctx.session_id,
                        )
                        ctx.needs_reload = False
                        await reconnect_claude(
                            "external transcript change at final preflight")
                        if (ctx.interrupt_event.is_set()
                                or ctx.state == "interrupting"):
                            await self._emit(ctx, TurnEnd(result=TurnResult(
                                subtype="error_during_execution",
                                duration_ms=0,
                                is_error=True,
                            )))
                            await self._set_idle_after_managed_turn(ctx)
                            return
                        external = await self._prime_claude_ownership(
                            ctx.session_id)
                        if (ctx.interrupt_event.is_set()
                                or ctx.state == "interrupting"):
                            await self._emit(ctx, TurnEnd(result=TurnResult(
                                subtype="error_during_execution",
                                duration_ms=0,
                                is_error=True,
                            )))
                            await self._set_idle_after_managed_turn(ctx)
                            return
                        if external or ctx.needs_reload:
                            message = (
                                "该 Claude 会话在重载期间又被本机终端更新，"
                                "本次发送已取消；请退出终端后重试"
                                if external else
                                "该 Claude 会话在重载期间仍有未归属的内容更新，"
                                "本次发送已取消；请稍后重试"
                            )
                            await self._emit(ctx, Error(
                                code=ERR_BUSY,
                                message=message,
                                msg_id=ctx.active_msg_id,
                            ))
                            await self._set_idle_after_managed_turn(ctx)
                            return
                if is_codex:
                    # codex: images -> private temp dir -> localImage items; files already
                    # referenced by path in the prompt text above.
                    img_paths = self._stash_images(images, temp_dir) if images else []
                    # Keep the final ownership check adjacent to turn/start. A
                    # short native turn can finish between the earlier reload and
                    # this probe: no holder remains, but consuming its markers sets
                    # needs_reload. Reconnect once, then probe again before sending.
                    if (
                        is_codex_shared
                        and not await self._ensure_codex_daemon_generation(
                            ctx, reason="final query preflight")
                    ):
                        await self._emit(ctx, Error(
                            code=ERR_NOT_RUNNING,
                            message="Codex 共享通道重连失败，本次未发送；请重试",
                            msg_id=ctx.active_msg_id,
                        ))
                        await self._set_idle_after_managed_turn(ctx)
                        return
                    if ctx.session_id and not is_codex_shared:
                        external = await self._prime_codex_ownership(ctx.session_id)
                        if (ctx.interrupt_event.is_set()
                                or ctx.state == "interrupting"):
                            await self._emit(ctx, TurnEnd(result=TurnResult(
                                subtype="error_during_execution",
                                duration_ms=0,
                                is_error=True,
                            )))
                            await self._set_idle_after_managed_turn(ctx)
                            return
                        if external:
                            await self._emit(ctx, Error(
                                code=ERR_BUSY,
                                message=("该 Codex 会话刚被本机终端打开，本次发送已取消；"
                                         "请退出终端或点击『接管』后重试"),
                                msg_id=ctx.active_msg_id,
                            ))
                            await self._set_idle_after_managed_turn(ctx)
                            return
                        if ctx.needs_reload:
                            log.info(
                                "reloading session after external transcript change "
                                "found at final preflight",
                                sid=ctx.session_id,
                            )
                            if ctx.codex_checkpoint not in (None, False):
                                await self._retire_codex_checkpoint(
                                    ctx,
                                    reason=("external transcript change at final "
                                            "query preflight"),
                                    allow_restart=True,
                                )
                            await self._refresh_codex_collaboration_mode(ctx)
                            await ctx.sdk.force_reconnect(
                                resume_id=ctx.session_id, cwd=ctx.cwd,
                                reason="external transcript change at final preflight",
                            )
                            ctx.needs_reload = False
                            if (ctx.interrupt_event.is_set()
                                    or ctx.state == "interrupting"):
                                await self._emit(ctx, TurnEnd(result=TurnResult(
                                    subtype="error_during_execution",
                                    duration_ms=0,
                                    is_error=True,
                                )))
                                await self._set_idle_after_managed_turn(ctx)
                                return
                            external = await self._prime_codex_ownership(ctx.session_id)
                            if (ctx.interrupt_event.is_set()
                                    or ctx.state == "interrupting"):
                                await self._emit(ctx, TurnEnd(result=TurnResult(
                                    subtype="error_during_execution",
                                    duration_ms=0,
                                    is_error=True,
                                )))
                                await self._set_idle_after_managed_turn(ctx)
                                return
                            if external or ctx.needs_reload:
                                await self._emit(ctx, Error(
                                    code=ERR_BUSY,
                                    message=("该 Codex 会话在重载期间又被本机终端更新，"
                                             "本次发送已取消；请退出终端或点击『接管』后重试"),
                                    msg_id=ctx.active_msg_id,
                                ))
                                await self._set_idle_after_managed_turn(ctx)
                                return
                    await self._begin_codex_checkpoint(ctx)
                    if ctx.interrupt_event.is_set() or ctx.state == "interrupting":
                        await self._abort_codex_checkpoint(ctx)
                        await self._emit(ctx, TurnEnd(result=TurnResult(
                            subtype="error_during_execution",
                            duration_ms=0,
                            is_error=True,
                        )))
                        await self._set_idle_after_managed_turn(ctx)
                        return
                    native_turn_id = await ctx.sdk.query(
                        prompt, images=img_paths)
                    # CodexHandle marks turn/start failure by raising with
                    # turn_active=False. Reaching here is the authoritative
                    # acceptance boundary, including an ultra-fast turn that
                    # already completed before the RPC coroutine resumed.
                    if native_turn_id and ctx.active_msg_id:
                        await self._emit(ctx, TurnBinding(
                            msg_id=ctx.active_msg_id,
                            turn_id=native_turn_id,
                        ))
                    if native_turn_id:
                        self._claim_codex_turn(
                            ctx, native_turn_id, ctx.active_msg_id)
                    if (
                        is_codex_shared
                        and native_turn_id
                        and ctx.codex_daemon_epoch
                    ):
                        codex_restart_watch_task = asyncio.create_task(
                            self._wait_for_codex_account_switch(
                                starting_epoch=ctx.codex_daemon_epoch,
                            )
                        )
                    await self._accept_codex_checkpoint(ctx)
                elif images:
                    content: list = []
                    if prompt:
                        content.append({"type": "text", "text": prompt})
                    for img in images:
                        content.append({"type": "image", "source": {
                            "type": "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data": img.get("data", ""),
                        }})

                    async def msg_stream():
                        yield {"type": "user", "message": {"role": "user", "content": content},
                               "parent_tool_use_id": None}

                    ctx.sdk.next_turn_id = ctx.active_msg_id
                    ctx.claude_write_active = True
                    await ctx.sdk.query(msg_stream())
                else:
                    ctx.sdk.next_turn_id = ctx.active_msg_id
                    ctx.claude_write_active = True
                    await ctx.sdk.query(prompt)
            # Codex sessions don't emit a Model event like cc's init SystemMessage,
            # so announce the configured codex model (gpt-*) once — else the header
            # would keep showing a stale Claude model.
            if is_codex:
                collaboration_mode = getattr(
                    ctx.sdk, "collaboration_mode", "default")
                if ctx.announced_model != ctx.sdk.model:
                    ctx.announced_model = ctx.sdk.model
                    await self._emit(ctx, Model(model=ctx.announced_model))
                if ctx.announced_effort != ctx.sdk.effort:
                    ctx.announced_effort = ctx.sdk.effort
                    await self._emit(ctx, Effort(effort=ctx.sdk.effort))
                if ctx.announced_collaboration_mode != collaboration_mode:
                    ctx.announced_collaboration_mode = collaboration_mode
                    await self._emit(ctx, CollaborationMode(
                        mode=collaboration_mode))
                await self._emit(ctx, Fast(
                    on=_codex_fast_on(ctx.sdk.service_tier)))
            reader_task = asyncio.create_task(reader(queue, reader_exc))
            while True:
                msg = await next_turn_message()
                if isinstance(msg, CodexSteerFence):
                    msg.reached.set()
                    await msg.release.wait()
                    continue
                if notice_active:
                    # Any raw app-server frame is fresh activity, even when the
                    # translator intentionally skips it (reasoning/token usage).
                    # Clear the stale wait/retry label before handling the frame;
                    # a new retry notification below can immediately replace it.
                    await self._emit(ctx, StateEvent(
                        state=ctx.state,
                        phase=None,
                        detail=None,
                        msg_id=ctx.active_msg_id,
                    ))
                    notice_active = False
                if msg is None:
                    if reader_exc:
                        raise reader_exc[0]
                    raise RuntimeError("cc stream ended without a ResultMessage")

                if is_codex:
                    if (
                        isinstance(msg, tuple)
                        and len(msg) == 2
                        and msg[0] == "codex_account_switch"
                        and isinstance(msg[1], CodexDaemonRestartState)
                    ):
                        outcome = await handoff_codex_account_switch(msg[1])
                        if outcome == "continued":
                            continue
                        if outcome == "spontaneous":
                            codex_handoff_to_spontaneous = True
                        break
                    if isinstance(msg, CodexManagedOverflow):
                        codex_overflowed = True
                        await emit_codex_event(StateEvent(
                            state="running",
                            phase="waiting",
                            detail=(
                                "Codex 仍在执行；实时过程暂时延迟，"
                                "完成后会自动同步。"
                            ),
                            msg_id=ctx.active_msg_id,
                        ))
                        continue
                    await ctx.codex_steer_gate.wait()
                    await self._confirm_uncertain_codex_steer(ctx, msg)
                    sid = codex_session_id(msg)
                    if sid and not ctx.session_id:
                        await self._capture_session_id(ctx, sid)
                    terminal = is_turn_terminal(msg)
                    events = ctx.translator.feed(msg)
                    completed_after_overflow = (
                        codex_overflowed and terminal
                        and _codex_terminal_status(msg) == "completed"
                    )
                    if completed_after_overflow:
                        events = [event for event in events
                                  if not isinstance(event, (Error, TurnEnd))]
                    for ev in events:
                        await emit_codex_event(ev)
                    if terminal:
                        if ctx.work_context_baseline_pending:
                            await self._persist_fresh_work_context_baseline(
                                ctx, await ctx.sdk.get_context_usage())
                        if completed_after_overflow:
                            success = _codex_success_terminal(
                                msg, native_turn_id or ctx.active_msg_id or ctx.key)
                            await self._emit(ctx, success)
                            codex_overflow_repair_turn_id = success.turn_id
                        break
                    continue

                log.debug("sdk msg", sid=ctx.session_id, msg_type=type(msg).__name__)

                sid = extract_session_id(msg)
                if sid and not ctx.session_id:
                    await self._capture_session_id(ctx, sid)

                # cc-only path (the codex branch continues above). Only announce
                # Claude-branded models so a cc-switch proxy's raw upstream name
                # (e.g. glm-5.2) never replaces the user's Claude alias in the chip.
                mdl = extract_model(msg)
                if (mdl and not getattr(ctx.sdk, "model", None)
                        and mdl.startswith("claude-")):
                    # get_context_usage owns the selected alias. Init/transcript
                    # metadata can expose a gateway's Claude upstream model; use
                    # it only as a fallback when the control-plane read failed.
                    ctx.sdk.model = mdl
                if (mdl and mdl == getattr(ctx.sdk, "model", None)
                        and mdl != ctx.announced_model):
                    ctx.announced_model = mdl
                    await self._emit(ctx, Model(model=mdl))

                goal_changed, goal = ctx.sdk.observe_goal_message(
                    msg, ctx.session_id or ctx.key)
                if goal_changed and ctx.goal_visible:
                    await self._emit(ctx, GoalState(goal=goal))

                for ev in ctx.translator.feed(msg):
                    await self._emit(ctx, ev)

                if isinstance(msg, ResultMessage):
                    break

            if not is_codex:
                # ResultMessage closes the SDK response iterator; wait for its
                # reader task to release the turn queue, then allow the sole
                # session-long SDK reader to deliver messages that followed the
                # processed Result. This preserves raw ordering: TurnEnd reaches
                # the UI before a delayed task/hook update from the same queue.
                if reader_task is not None:
                    await reader_task
                # Only an authoritative Result permits a clean re-baseline in
                # finally. Query/send or reader failures remain ambiguous and
                # their growth must be consumed as a reload on the next probe.
                claude_turn_completed = True

            if not is_codex and ctx.session_id:
                goal = await ctx.sdk.refresh_goal(ctx.session_id)
                if ctx.goal_visible:
                    await self._emit(ctx, GoalState(goal=goal))

            if not is_codex:
                release_background = getattr(
                    ctx.sdk, "release_background_messages", None)
                if release_background is not None:
                    release_background()

            if codex_handoff_to_spontaneous:
                return
            await self._set_idle_after_managed_turn(ctx)
            if codex_overflow_repair_turn_id is not None:
                await self._repair_codex_projection_after_overflow(
                    ctx, codex_overflow_repair_turn_id)
        except asyncio.TimeoutError:
            log.error("drain timeout — interrupt did not yield a ResultMessage",
                      prompt_length=len(prompt))
            await self._emit(ctx, Error(
                code=ERR_DRAIN_TIMEOUT,
                message="interrupt drain timed out; reconnecting cc",
                msg_id=ctx.active_msg_id))
            timed_out_spontaneous_turn = ctx.codex_spontaneous_turn_id
            try:
                await ctx.sdk.force_reconnect(ctx.session_id, ctx.cwd)
            except Exception as e:
                log.exception("force reconnect failed", error=str(e))
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message="会话恢复未完成，请重新进入后重试。",
                    msg_id=ctx.active_msg_id))
            # Reconnect terminates the old app-server turn. Preserve only a
            # different id delivered by the new generation while it connected.
            if ctx.codex_spontaneous_turn_id == timed_out_spontaneous_turn:
                ctx.codex_spontaneous_turn_id = None
            await self._set_idle_after_managed_turn(ctx)
        except Exception as e:
            log.exception("turn failed", error=str(e))
            await self._emit(ctx, Error(
                code=ERR_CC_CRASH,
                message="本次回复未完成，请重试。",
                msg_id=ctx.active_msg_id))
            await self._set_idle_after_managed_turn(ctx)
        finally:
            if not is_codex:
                release_background = getattr(
                    ctx.sdk, "release_background_messages", None)
                if release_background is not None:
                    release_background()
            ctx.codex_account_handoff = False
            ctx.translator = None
            ctx.turn_task = None
            if ctx.codex_spontaneous_turn_id is None:
                ctx.active_msg_id = None
                ctx.interrupt_deadline = None
                ctx.interrupt_event.clear()
            # cc keeps flushing this turn's lines to the transcript for a moment after
            # the result. Re-baseline only after its authoritative Result arrived;
            # preflight/query/reader failures must not hide an external append.
            ctx.claude_write_active = False
            if not is_codex and claude_turn_completed and ctx.session_id:
                self._resync_watch(ctx.session_id)
            if temp_dir is not None and not persistent_attachments:
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    log.warning("turn attachment cleanup failed", error=str(e))
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
            if (
                codex_restart_watch_task is not None
                and not codex_restart_watch_task.done()
            ):
                codex_restart_watch_task.cancel()
                await asyncio.gather(
                    codex_restart_watch_task, return_exceptions=True)
