"""Codex app-server lifecycle: connect / query / interrupt / receive / disconnect.

The Codex analog of SdkHandle (sdk.py). Drives either a private ``app-server
--stdio`` process (Work/compatibility) or one short-lived ``app-server proxy``
connection to the official shared Code daemon. Both present the SAME async
JSON-RPC surface the machine's per-turn consumer expects:

  connect(resume_id, cwd) -> initialize/initialized handshake + thread/start|resume
  query(prompt)           -> turn/start (opens a fresh per-turn queue)
  receive_response()      -> async-gen of raw notification dicts until turn/completed
  interrupt()             -> turn/interrupt {threadId, turnId}
  disconnect()            -> terminate only the owned stdio/proxy subprocess

Model-agnostic: whatever backend Codex is pointed at (user's cc-switch) is Codex's
concern. The sole exception is a process-local HTTP transport alias for an
oversized Codex Desktop/OpenAI resume whose native WebSocket transport is known
to fail before Codex can perform its own HTTPS fallback. It never mutates the
user's config or changes third-party providers.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import signal
from collections import OrderedDict, deque
from typing import Any, Awaitable, Callable, Optional

from cc_remote import __version__
from cc_remote.log import logger
from cc_remote.protocol import Notice, RateLimitUpdate, ThreadGoal
from cc_remote.wrapper.codex_daemon import (
    CodexDaemonUpgradeRequired,
    CodexDaemonManager,
    codex_daemon_mode,
    default_codex_daemon_manager,
)
from cc_remote.wrapper.codex_sessions import (
    codex_approval,
    codex_context_window,
    codex_effort,
    codex_fast_enabled,
    codex_model,
    codex_rollout_path,
    codex_web_search,
)
from cc_remote.wrapper.codex_provider_repair import (
    HTTP_COMPAT_PROVIDER_ID,
    canonical_thread_provider_is_restored,
    repair_http_provider_records,
)
from cc_remote.wrapper.codex_runtime import (
    codex_env as _runtime_codex_env,
    codex_version as _runtime_codex_version,
    resolve_codex_bin as _runtime_resolve_codex_bin,
)
from cc_remote.wrapper.codex_permissions import normalize_permission_profiles
from cc_remote.wrapper.work_prompt import (
    WORK_BASE_INSTRUCTIONS,
    WORK_DEVELOPER_INSTRUCTIONS,
)

log = logger("cc_remote.wrapper.codex_handle")

_REQ_TIMEOUT = 60.0
_APPROVAL_TIMEOUT = 5 * 60.0
_MAX_SERVER_REQUEST_TASKS = 32
_THREAD_SETTINGS_NOTIFY_TIMEOUT = 1.0
_OWNED_TURN_IDS_MAX = 512
_STATUS_RATE_LIMIT_MAX = 16
_RUNTIME_EVENT_PENDING_MAX = 32
_RUNTIME_EVENT_SEEN_MAX = 128
_NOTICE_MESSAGE_MAX = 2 * 1024
_NOTICE_DETAIL_MAX = 4 * 1024
_NOTICE_PATH_MAX = 1024
_NOTICE_PATH_SAMPLE_MAX = 3
_MANAGED_QUEUE_MIN_ITEMS = 64
_MANAGED_QUEUE_MAX_ITEMS = 256
_MANAGED_QUEUE_MIN_BYTES = 4 * 1024 * 1024
_SPONTANEOUS_QUEUE_MIN_ITEMS = 64
_SPONTANEOUS_QUEUE_MAX_ITEMS = 256
_SPONTANEOUS_QUEUE_MIN_BYTES = 4 * 1024 * 1024
_WORK_SKILL_LIMIT = 512
_WORK_MCP_SERVER_LIMIT = 128
_WORK_PATH_MAX = 4096
_WORK_NAME_MAX = 256
_PROXY_HANDSHAKE_MAX = 16 * 1024
_PROXY_HANDSHAKE_TIMEOUT = 5.0
_PROXY_MESSAGE_MAX = 16 * 1024 * 1024
_LIGHTWEIGHT_RESUME_MIN_VERSION = (0, 144, 6)
# The managed shared daemon intentionally follows Codex's standalone release
# channel.  The desktop app can temporarily bundle a newer official app-server
# core.  Only very large rollouts opt into that private core; normal Code
# sessions keep the shared daemon and its CLI <-> Remote live channel.
_OVERSIZED_RESUME_PRIVATE_CORE_MIN_BYTES = 256 * 1024 * 1024
_ROLLOUT_SESSION_META_MAX_BYTES = 1024 * 1024
_OPENAI_HTTP_RESUME_PROVIDER_ID = HTTP_COMPAT_PROVIDER_ID
_OPENAI_HTTP_RESUME_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_DESKTOP_BIN_CANDIDATES = (
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
)
_WEBSOCKET_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WORK_DISABLED_FEATURES = (
    "apps",
    "hooks",
    "memories",
    "multi_agent",
    "personality",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "tool_suggest",
)
_HTTP_PROVIDER_PERSISTING_METHODS = frozenset({
    "review/start",
    "thread/compact/start",
    "thread/goal/clear",
    "thread/goal/set",
    "thread/rollback",
    "thread/settings/update",
    "turn/start",
    "turn/steer",
})
_HTTP_PROVIDER_PERSISTING_NOTIFICATIONS = frozenset({
    "thread/settings/updated",
    "thread/status/changed",
    "turn/completed",
    "turn/started",
})

ApprovalCallback = Callable[[str, dict], Awaitable[str]]
InteractionCallback = Callable[[str, dict], Awaitable[dict[str, Any]]]
GoalCallback = Callable[[Optional[dict[str, Any]]], Awaitable[None]]
TurnLifecycleCallback = Callable[[str, str], Awaitable[None]]
RuntimeEvent = Notice | RateLimitUpdate
RuntimeEventCallback = Callable[[RuntimeEvent], Awaitable[None]]


class CodexProxyProtocolError(RuntimeError):
    """The local proxy stream violated its RFC 6455 boundary."""


class CodexAppServerError(RuntimeError):
    """Typed JSON-RPC error returned by the official Codex app-server."""

    def __init__(self, error: Any):
        self.error = error
        self.code = error.get("code") if isinstance(error, dict) else None
        data = error.get("data") if isinstance(error, dict) else None
        self.codex_error_info = (
            data.get("codexErrorInfo")
            if isinstance(data, dict)
            and isinstance(data.get("codexErrorInfo"), dict)
            else {}
        )
        fragments: list[str] = []
        if isinstance(error, dict):
            for value in (error.get("message"), error.get("data")):
                if isinstance(value, str):
                    fragments.append(value)
                elif isinstance(value, dict):
                    nested = value.get("message")
                    if isinstance(nested, str):
                        fragments.append(nested)
        elif isinstance(error, str):
            fragments.append(error)
        self.message = " ".join(fragments)[:4096]
        # Preserve the previous RuntimeError text for callers/tests which match
        # native app-server diagnostics, while exposing structured fields to
        # control paths that need an authoritative classification.
        super().__init__(str(error))

    @property
    def no_active_turn(self) -> bool:
        return "no active turn to interrupt" in self.message.lower()

    @property
    def active_turn_not_steerable(self) -> bool:
        if isinstance(
            self.codex_error_info.get("activeTurnNotSteerable"), dict
        ):
            return True
        compact = re.sub(r"[^a-z]", "", self.message.lower())
        return "activeturnnotsteerable" in compact

    @property
    def unsteerable_turn_kind(self) -> Optional[str]:
        value = self.codex_error_info.get("activeTurnNotSteerable")
        if not isinstance(value, dict):
            return None
        kind = value.get("turnKind")
        return kind if isinstance(kind, str) else None

    @property
    def steer_turn_changed(self) -> bool:
        text = self.message.lower()
        return (
            "expectedturnid" in re.sub(r"[^a-z]", "", text)
            or ("expected turn" in text and "mismatch" in text)
            or ("expected active turn id" in text and "but found" in text)
            or "no active turn" in text
        )


class CodexNoActiveTurnError(RuntimeError):
    """A strict interrupt target was authoritatively reported inactive."""

    def __init__(self, thread_id: str, turn_id: str):
        self.thread_id = thread_id
        self.turn_id = turn_id
        super().__init__("Codex app-server reports no active turn")


class CodexSteerOutcomeUnknown(RuntimeError):
    """turn/steer was written but no authoritative response was observed."""


def _websocket_client_frame(
    payload: bytes, *, opcode: int = 0x1, fin: bool = True,
) -> bytes:
    """Build one masked client frame for the local app-server proxy."""
    if opcode not in {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}:
        raise CodexProxyProtocolError("invalid WebSocket opcode")
    if len(payload) > _PROXY_MESSAGE_MAX:
        raise CodexProxyProtocolError("WebSocket payload exceeds limit")
    if opcode >= 0x8 and (not fin or len(payload) > 125):
        raise CodexProxyProtocolError("invalid WebSocket control frame")
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length <= 125:
        header = bytes((first, 0x80 | length))
    elif length <= 0xFFFF:
        header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
    else:
        header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
    mask = os.urandom(4)
    if len(mask) != 4:
        raise CodexProxyProtocolError("invalid WebSocket mask")
    masked = bytes(value ^ mask[index & 3]
                   for index, value in enumerate(payload))
    return header + mask + masked


class CodexSpontaneousOverflow:
    """Internal bridge signal: live detail was shed to protect stdout reading."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: str):
        self.turn_id = turn_id


class CodexManagedOverflow:
    """Internal signal: managed live detail was shed before its consumer ran."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: Optional[str]):
        self.turn_id = turn_id


class CodexSpontaneousClosed:
    """Internal bridge signal: app-server ended before a terminal notification."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: str):
        self.turn_id = turn_id


class CodexSteerFence:
    """In-order barrier between pre-steer and post-steer notifications."""

    __slots__ = ("reached", "release")

    def __init__(self):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def release_now(self) -> None:
        self.release.set()


class CodexNoActiveTurnFence:
    """In-order barrier after an authoritative inactive thread/read response."""

    __slots__ = ("reached", "release")

    def __init__(self):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    def release_now(self) -> None:
        self.release.set()


class CodexNoActiveTurnConfirmation:
    """Exact inactive-thread proof plus its raw notification boundary."""

    __slots__ = ("fence", "_queue", "authoritative_terminal")

    def __init__(
        self,
        *,
        fence: Optional[CodexNoActiveTurnFence],
        queue: Optional["_SpontaneousNotificationQueue"],
        authoritative_terminal: bool,
    ):
        self.fence = fence
        self._queue = queue
        self.authoritative_terminal = authoritative_terminal

    def terminal_pending(self) -> bool:
        return bool(
            self._queue is not None
            and self._queue.has_turn_completed()
        )


class CodexSteerAcceptance(str):
    """String-compatible turn id carrying its raw-notification fence."""

    fence: Optional[CodexSteerFence]

    def __new__(
        cls,
        turn_id: str,
        fence: Optional[CodexSteerFence] = None,
    ):
        value = super().__new__(cls, turn_id)
        value.fence = fence
        return value


class _CodexSteerResponseBoundary:
    """Mutable response-dispatch handoff for one exact turn/steer RPC."""

    __slots__ = ("thread_id", "turn_id", "fence")

    def __init__(self, thread_id: str, turn_id: str):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.fence: Optional[CodexSteerFence] = None

    def release(self) -> None:
        if self.fence is not None:
            self.fence.release_now()


class _SpontaneousNotificationQueue:
    """Single-loop FIFO bounded by both parsed frames and original wire bytes.

    The app-server stdout reader must keep draining even if the relay is slow.  A
    regular ``asyncio.Queue.put`` would transfer relay backpressure all the way to
    stdout and can deadlock JSON-RPC responses/approvals.  This queue therefore has
    a synchronous, fail-fast producer and one asynchronous consumer. One item and
    a small byte allowance are reserved for the authoritative terminal/close
    frame, so a saturated live-detail queue can never erase turn completion.
    """

    def __init__(self, max_items: int, max_bytes: int):
        self.max_items = max(2, max_items)
        self.max_bytes = max(1024, max_bytes)
        self.max_end_bytes = min(
            4 * 1024,
            max(512, self.max_bytes // 8),
        )
        self._live_max_items = self.max_items - 1
        self._live_max_bytes = self.max_bytes - self.max_end_bytes
        self._items: deque[tuple[object, int]] = deque()
        self._end: Optional[tuple[object, int]] = None
        self._bytes = 0
        self._ready = asyncio.Event()
        self._lossy = False
        self._post_gap_items: set[str] = set()
        self._end_delivered = False

    @property
    def byte_size(self) -> int:
        return self._bytes

    def qsize(self) -> int:
        return len(self._items) + (1 if self._end is not None else 0)

    def has_turn_completed(self) -> bool:
        return bool(
            self._end is not None
            and isinstance(self._end[0], dict)
            and self._end[0].get("method") == "turn/completed"
        )

    @property
    def end_delivered(self) -> bool:
        """Whether this bridge consumer has removed its reserved end item."""
        return self._end_delivered

    def put_nowait(self, item: object, size: int = 0) -> bool:
        size = max(0, size)
        if not self._can_put_live(size):
            return False
        self._items.append((item, size))
        self._bytes += size
        self._ready.set()
        return True

    def put_control_nowait(self, item: object) -> None:
        """Insert one zero-byte ordering control outside live-frame capacity."""
        self._items.append((item, 0))
        self._ready.set()

    def _can_put_live(self, size: int) -> bool:
        return not (
            self._end is not None
            or size > self._live_max_bytes
            or len(self._items) >= self._live_max_items
            or self._bytes + size > self._live_max_bytes
        )

    def put_terminal_nowait(self, item: dict, size: int) -> bool:
        """Retain exactly one terminal in the queue's reserved end slot."""
        size = max(0, size)
        if self.has_turn_completed():
            return True
        if size > self.max_end_bytes:
            return False
        if self._end is not None:
            _old_item, old_size = self._end
            self._bytes = max(0, self._bytes - old_size)
        self._end = (item, size)
        self._bytes += size
        self._ready.set()
        return True

    def put_end_nowait(self, item: object, size: int = 0) -> bool:
        """Retain one EOF/close sentinel without competing with live detail."""
        size = max(0, size)
        if self._end is not None or size > self.max_end_bytes:
            return False
        self._end = (item, size)
        self._bytes += size
        self._ready.set()
        return True

    def begin_gap(self, marker: object) -> None:
        """Drop one stale live tail and open a bounded loss epoch."""
        # Control-plane ordering boundaries are not lossy live detail. Preserve
        # them even when later output overflows before the consumer reaches the
        # barrier.
        controls = [
            entry for entry in self._items
            if isinstance(
                entry[0], (CodexSteerFence, CodexNoActiveTurnFence)
            )
        ]
        self._items.clear()
        if self._end is None:
            self._bytes = 0
        else:
            self._bytes = self._end[1]
        self._lossy = True
        self._post_gap_items.clear()
        # A zero-byte marker always fits in the live reservation. It precedes
        # the preserved fence because this gap discarded at least part of the
        # backlog that existed before the fence was consumed; attributing the
        # loss warning to the new user segment would be misleading.
        self._items.append((marker, 0))
        self._items.extend(controls)
        self._ready.set()

    def retry_after_gap_nowait(self, message: dict, size: int) -> bool:
        """Retain the overflow-triggering frame when it is safe and now fits."""
        size = max(0, size)
        # Check capacity before lifecycle admission mutates the tracker. A single
        # oversized start must not authorize its later orphan deltas.
        if not self._can_put_live(size):
            return False
        if not self.accepts_after_gap(message):
            return False
        return self.put_nowait(message, size)

    def accepts_after_gap(self, message: dict) -> bool:
        """Admit only post-gap frames whose lifecycle boundary is intact.

        Normal turns retain app-server compatibility with delta-only and
        completion-only providers. Once frames were shed, however, forwarding an
        orphan incremental update lets the translator resurrect a tool or message
        whose start was lost. Keep strict delta admission for the remainder of
        that turn while allowing authoritative completion snapshots.
        """
        if not self._lossy:
            return True

        method = message.get("method")
        params = (
            message.get("params")
            if isinstance(message.get("params"), dict)
            else {}
        )
        if not isinstance(method, str):
            return False
        if not method.startswith(("item/", "hook/")):
            # Turn/model/error/thread notifications are self-contained snapshots.
            return True

        if method == "item/started":
            item = params.get("item")
            item_id = item.get("id") if isinstance(item, dict) else None
            return self._admit_post_gap_start("item", item_id)
        if method == "item/reasoning/summaryPartAdded":
            return self._admit_post_gap_start(
                "item", params.get("itemId"))
        if method == "item/completed":
            item = params.get("item")
            item_id = item.get("id") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("type"), str)
                or not item["type"]
            ):
                return False
            return self._admit_post_gap_completion("item", item_id)
        if method == "item/autoApprovalReview/started":
            return self._admit_post_gap_start(
                "review", params.get("reviewId"))
        if method == "item/autoApprovalReview/completed":
            if not isinstance(params.get("review"), dict):
                return False
            return self._admit_post_gap_completion(
                "review", params.get("reviewId"))
        if method == "hook/started":
            run = params.get("run")
            run_id = run.get("id") if isinstance(run, dict) else None
            return self._admit_post_gap_start("hook", run_id)
        if method == "hook/completed":
            run = params.get("run")
            run_id = run.get("id") if isinstance(run, dict) else None
            if not isinstance(run, dict):
                return False
            return self._admit_post_gap_completion("hook", run_id)

        # Every other item notification is an incremental update to itemId.
        item_id = params.get("itemId")
        return self._post_gap_key("item", item_id) in self._post_gap_items

    def _admit_post_gap_start(self, kind: str, value: Any) -> bool:
        key = self._post_gap_key(kind, value)
        if key is None:
            return False
        if key in self._post_gap_items:
            return True
        if len(self._post_gap_items) >= self.max_items:
            return False
        self._post_gap_items.add(key)
        return True

    def _admit_post_gap_completion(self, kind: str, value: Any) -> bool:
        """Admit one authoritative, self-contained completion snapshot.

        Completion-only items are part of the official app-server contract. The
        start may have reached the consumer before the gap, been shed inside the
        gap, or never have been emitted by this provider revision. A valid
        completion closes a tracked lifecycle when present, but never depends on
        that tracker for admission. Incremental deltas remain strict above.
        """
        key = self._post_gap_key(kind, value)
        if key is None:
            return False
        self._post_gap_items.discard(key)
        return True

    @staticmethod
    def _post_gap_key(kind: str, value: Any) -> Optional[str]:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
        ):
            return None
        digest = hashlib.sha256(
            value.encode("utf-8", errors="surrogatepass"),
        ).hexdigest()
        return f"{kind}:{digest}"

    async def get(self) -> object:
        while not self._items and self._end is None:
            self._ready.clear()
            if not self._items and self._end is None:
                await self._ready.wait()
        if self._items:
            item, size = self._items.popleft()
        else:
            assert self._end is not None
            item, size = self._end
            self._end = None
            self._end_delivered = True
        self._bytes = max(0, self._bytes - size)
        if not self._items and self._end is None:
            self._ready.clear()
        return item

_NEW_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
})
_LEGACY_APPROVAL_METHODS = frozenset({
    "execCommandApproval",
    "applyPatchApproval",
})
_INTERACTION_METHODS = frozenset({
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
})
_APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
_LEGACY_DECISIONS = {
    "accept": "approved",
    "acceptForSession": "approved_for_session",
    "decline": "denied",
    "cancel": "abort",
}

_TURN_NOTIFICATION_PREFIXES = ("item/", "turn/", "hook/")
_TURN_QUEUE_PREFIXES = ("item/", "turn/", "hook/")
_MODEL_TURN_METHODS = frozenset({
    "model/rerouted",
    "model/safetyBuffering/updated",
    "model/verification",
})
_TURN_QUEUE_METHODS = frozenset({
    "error", "thread/compacted", *_MODEL_TURN_METHODS,
})


def _notification_thread_id(message: dict) -> Optional[str]:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    value = params.get("threadId")
    if isinstance(value, str) and value:
        return value
    thread = params.get("thread")
    if isinstance(thread, dict):
        value = thread.get("id") or thread.get("sessionId")
        if isinstance(value, str) and value:
            return value
    return None


def _notification_turn_id(message: dict) -> Optional[str]:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    value = params.get("turnId")
    if isinstance(value, str) and value:
        return value
    turn = params.get("turn")
    if isinstance(turn, dict):
        value = turn.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _server_request_key(value: Any) -> Optional[object]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _is_turn_notification(method: Any) -> bool:
    return (
        isinstance(method, str)
        and (method in _MODEL_TURN_METHODS
             or method.startswith(_TURN_NOTIFICATION_PREFIXES))
    )


def _is_turn_queue_notification(method: Any) -> bool:
    return (
        isinstance(method, str)
        and (method in _TURN_QUEUE_METHODS
             or method.startswith(_TURN_QUEUE_PREFIXES))
    )


def _codex_version(path: str) -> tuple[int, ...]:
    """Compatibility seam for private-core selection and existing tests."""
    return _runtime_codex_version(path)


def _newer_private_core_for_oversized_resume(
    managed_bin: str, resume_id: Optional[str],
) -> Optional[str]:
    """Select a newer official desktop app-server for one oversized thread.

    ``app-server daemon`` always executes the managed standalone Codex binary;
    it cannot be pointed at the desktop app's bundled core.  During a staggered
    rollout the desktop core may contain large-history/compaction fixes that the
    managed daemon does not yet have.  Starting that official core over stdio is
    therefore a narrow compatibility fallback, not a second history engine:
    thread/resume still owns all native context and uses ``excludeTurns``.

    Explicit ``CODEX_BIN`` remains authoritative.  Small/ordinary sessions stay
    on the shared daemon so terminal CLI bidirectional updates are unaffected.
    """
    if (not resume_id or os.environ.get("CODEX_BIN")
            or managed_bin in _CODEX_DESKTOP_BIN_CANDIDATES):
        return None
    rollout_path = codex_rollout_path(resume_id)
    if not rollout_path:
        return None
    try:
        rollout_size = os.path.getsize(rollout_path)
    except OSError:
        return None
    if rollout_size < _OVERSIZED_RESUME_PRIVATE_CORE_MIN_BYTES:
        return None

    managed_version = _codex_version(managed_bin)
    if managed_version == (-1,):
        return None
    best_path: Optional[str] = None
    best_version = managed_version
    for candidate in _CODEX_DESKTOP_BIN_CANDIDATES:
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        version = _codex_version(candidate)
        if version > best_version:
            best_path = candidate
            best_version = version
    if best_path is not None:
        log.info(
            "oversized Codex resume uses newer official private core",
            rollout_bytes=rollout_size,
            managed_version=".".join(map(str, managed_version)),
            private_version=".".join(map(str, best_version)),
        )
    return best_path


def _oversized_desktop_openai_resume_requires_http(
    resume_id: Optional[str],
) -> bool:
    """Use Codex's official Responses HTTP path for one pathological resume.

    Some very large Codex Desktop rollouts contain many compact records whose
    effective replacement history still includes embedded images.  The native
    OpenAI Responses WebSocket can close before ``response.completed`` for
    these requests; Codex retries the same WebSocket five times before falling
    back to HTTPS.  That turns a valid request into minutes of apparent Remote
    failure.

    Keep the workaround deliberately narrow: only an oversized rollout whose
    immutable first ``session_meta`` says it was created by Codex Desktop and
    uses the built-in ``openai`` provider.  CLI/shared-daemon sessions and every
    custom provider retain their native transport and live-channel semantics.
    """
    if not resume_id:
        return False
    rollout_path = codex_rollout_path(resume_id)
    if not rollout_path:
        return False
    try:
        if os.path.getsize(
                rollout_path) < _OVERSIZED_RESUME_PRIVATE_CORE_MIN_BYTES:
            return False
        with open(rollout_path, "rb") as stream:
            first_line = stream.readline(_ROLLOUT_SESSION_META_MAX_BYTES + 1)
    except OSError:
        return False
    if (not first_line or len(first_line) > _ROLLOUT_SESSION_META_MAX_BYTES
            or not first_line.endswith(b"\n")):
        return False
    try:
        record = json.loads(first_line)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("model_provider") == "openai"
        and payload.get("originator") == "Codex Desktop"
    )


def _append_openai_http_resume_provider(argv: list[str]) -> None:
    """Register a private, process-local alias for official OpenAI HTTP."""
    provider = f"model_providers.{_OPENAI_HTTP_RESUME_PROVIDER_ID}"
    argv.extend([
        "-c", f'{provider}.name="cc-remote OpenAI HTTP"',
        "-c", f'{provider}.base_url={json.dumps(_OPENAI_HTTP_RESUME_BASE_URL)}',
        "-c", f'{provider}.wire_api="responses"',
        "-c", f"{provider}.requires_openai_auth=true",
        "-c", f"{provider}.supports_websockets=false",
    ])


def _semantic_version(value: Optional[str]) -> tuple[int, ...]:
    """Return the numeric release prefix used for app-server feature gates."""
    if not isinstance(value, str):
        return (-1,)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(int(group) for group in match.groups()) if match else (-1,)


def _supports_lightweight_resume(value: Optional[str]) -> bool:
    """Whether app-server supports excludeTurns on thread/resume/fork."""
    return _semantic_version(value) >= _LIGHTWEIGHT_RESUME_MIN_VERSION


def _resolve_codex_bin() -> str:
    """Compatibility seam for resident handle call sites."""
    return _runtime_resolve_codex_bin()


def _codex_env(bin_path: str) -> dict[str, str]:
    """Compatibility seam for wrapper-owned Codex child environments."""
    return _runtime_codex_env(bin_path)


def _codex_runtime_tmp() -> str:
    """Return the private runtime directory Codex uses for sandbox launchers.

    Work leaves unspecified paths ungranted and opens only its own workspace.
    Codex stages ``codex-linux-sandbox`` below ``$CODEX_HOME/tmp`` before every
    tool call, so that narrow runtime path must remain readable as well.
    """
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    codex_home = os.path.abspath(os.path.expanduser(codex_home))
    return os.path.join(codex_home, "tmp")


def _initialize_params() -> dict[str, Any]:
    """Declare the capability required by collaborationMode/list and turn/start."""
    return {
        "clientInfo": {"name": "cc-remote", "version": __version__},
        "capabilities": {"experimentalApi": True},
    }


def _work_thread_config(
    skills_response: Any,
    config_response: Any,
) -> dict[str, Any]:
    """Build a fail-closed Work-only app-server config overlay.

    Work intentionally keeps the user's account, provider, model catalog and
    thread store in the normal ``CODEX_HOME``.  Passing this overlay on the
    thread RPC is therefore narrower than creating a second home, while still
    preventing personal Skills, plugins, MCP servers and collaboration agents
    from being inserted into the model context.  Code threads never call this
    helper and continue to inherit the native Codex configuration unchanged.
    """
    if not isinstance(skills_response, dict):
        raise RuntimeError("codex skills/list returned an invalid response")
    entries = skills_response.get("data")
    if not isinstance(entries, list):
        raise RuntimeError("codex skills/list returned an invalid response")

    skill_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("codex skills/list returned an invalid entry")
        skills = entry.get("skills")
        if not isinstance(skills, list):
            raise RuntimeError("codex skills/list returned an invalid entry")
        for skill in skills:
            if not isinstance(skill, dict):
                raise RuntimeError("codex skills/list returned an invalid skill")
            # Missing ``enabled`` meant enabled in older app-server responses.
            if skill.get("enabled") is False:
                continue
            path = skill.get("path")
            if (not isinstance(path, str) or not path
                    or len(path) > _WORK_PATH_MAX):
                raise RuntimeError("codex skills/list returned an invalid path")
            skill_paths.add(path)
            if len(skill_paths) > _WORK_SKILL_LIMIT:
                raise RuntimeError("codex Work skill inventory exceeds limit")

    if not isinstance(config_response, dict):
        raise RuntimeError("codex config/read returned an invalid response")
    effective = config_response.get("config")
    if not isinstance(effective, dict):
        raise RuntimeError("codex config/read returned an invalid response")
    raw_mcp = effective.get("mcp_servers", {})
    if not isinstance(raw_mcp, dict):
        raise RuntimeError("codex config/read returned invalid MCP settings")
    if len(raw_mcp) > _WORK_MCP_SERVER_LIMIT:
        raise RuntimeError("codex Work MCP inventory exceeds limit")
    mcp_servers: dict[str, dict[str, bool]] = {}
    for name in raw_mcp:
        if (not isinstance(name, str) or not name
                or len(name) > _WORK_NAME_MAX):
            raise RuntimeError("codex config/read returned an invalid MCP name")
        mcp_servers[name] = {"enabled": False}

    return {
        "features": {name: False for name in _WORK_DISABLED_FEATURES},
        # Work artifacts may legitimately be called AGENTS.md.  They are data,
        # not a route for re-introducing Code/project instructions.
        "project_doc_max_bytes": 0,
        "project_doc_fallback_filenames": [],
        # Keep the native indexed research tool needed by general Work tasks,
        # without inheriting a user's Code-time live-search preference.
        "web_search": "cached",
        "skills": {
            "config": [
                {"path": path, "enabled": False}
                for path in sorted(skill_paths)
            ],
        },
        "mcp_servers": mcp_servers,
    }


class CodexHandle:
    def __init__(self, cfg, cwd: Optional[str] = None,
                 work_mode: bool = False,
                 approval_callback: Optional[ApprovalCallback] = None,
                 interaction_callback: Optional[InteractionCallback] = None,
                 goal_callback: Optional[GoalCallback] = None,
                 turn_lifecycle_callback: Optional[TurnLifecycleCallback] = None,
                 runtime_event_callback: Optional[RuntimeEventCallback] = None,
                 daemon_mode: Optional[str] = None,
                 daemon_manager: Optional[CodexDaemonManager] = None):
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.thread_id: Optional[str] = None
        # A shared daemon can publish every subscribed thread immediately after
        # initialize, before thread/resume returns and assigns ``thread_id``.
        # Freeze the requested resume id across that bind window so only the
        # intended thread can claim turn lifecycle state.
        self._shared_resume_binding_thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.turn_start_pending = False
        self.turn_active = False
        # Inline Review has two different app-server turn ids.  The response and
        # visible lifecycle use the outer id, while a nested reviewer turn is the
        # thread's actual interrupt target.  Keep them separate: collapsing both
        # into ``turn_id`` makes cancel target the outer id and makes the rollout
        # watcher mistake the nested task for a native terminal turn.
        self._review_active = False
        self._review_outer_turn_id: Optional[str] = None
        self._review_execution_turn_id: Optional[str] = None
        self._review_execution_ready = asyncio.Event()
        # turn/start ids produced by this wrapper. Codex can flush a rollout for
        # tens of seconds after turn/completed; retaining ids lets the transcript
        # watcher attribute those late records to us instead of to a terminal.
        self._owned_turn_ids: OrderedDict[str, None] = OrderedDict()
        self._cwd = cwd
        self.work_mode = work_mode
        requested_daemon_mode = (
            daemon_mode if daemon_mode is not None
            else getattr(daemon_manager, "mode", None)
        )
        self.daemon_mode = (
            "off" if work_mode else codex_daemon_mode(requested_daemon_mode))
        self.daemon_manager = (
            daemon_manager
            if daemon_manager is not None
            else default_codex_daemon_manager(self.daemon_mode)
        )
        self._using_daemon_proxy = False
        # Once a Code session has joined the official shared app-server, a
        # transport interruption must not silently turn it into a private stdio
        # session.  Keep this affinity across proxy reconnects so Machine can
        # preserve bidirectional ownership while the short-lived proxy is down.
        self._daemon_proxy_established = False
        self._proxy_read_buffer = bytearray()
        self._proxy_close_sent = False
        self._send_lock = asyncio.Lock()
        self._work_config: Optional[dict[str, Any]] = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._pending_response_boundaries: dict[
            int, _CodexSteerResponseBoundary
        ] = {}
        self._turn_q: Optional[Any] = None
        self._managed_overflow = False
        self._reader: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._thread_settings_updated = asyncio.Event()
        # Human approval can take minutes.  It must not block the sole stdout
        # reader, which still has to consume turn/interrupt and other RPC replies.
        # Keep detached request handlers generation-owned and cancel them on
        # disconnect so an old approval cannot reply to a new app-server process.
        self._server_request_tasks: set[asyncio.Task] = set()
        self._server_request_tasks_by_id: dict[object, asyncio.Task] = {}
        self._pending_server_request_ids: set[object] = set()
        # POSIX transport children get their own group. For stdio this includes
        # tool descendants; in daemon mode it contains only this proxy chain.
        self._process_group: Optional[int] = None
        self._generation = 0
        self._dead = False
        self.approval_callback = approval_callback
        self.interaction_callback = interaction_callback
        self.goal_callback = goal_callback
        self.turn_lifecycle_callback = turn_lifecycle_callback
        # App-server can emit initialize/config warnings before thread/start has
        # returned.  Machine binds this callback immediately but activates it only
        # after the SessionContext has a non-null routing key.  Until then, keep a
        # bounded, deduplicated queue so no notice can be broadcast with sid=None.
        self.runtime_event_callback = runtime_event_callback
        self._runtime_events_active = False
        self._runtime_event_pending: OrderedDict[str, RuntimeEvent] = OrderedDict()
        self._runtime_event_seen: OrderedDict[str, None] = OrderedDict()
        self._runtime_rate_keys: OrderedDict[str, str] = OrderedDict()
        self._runtime_event_lock = asyncio.Lock()
        # The process-local HTTP alias is a transport implementation detail.
        # Codex persists it after several otherwise unrelated thread mutations;
        # serialize the narrow durable-state repair so concurrent controls
        # cannot race each other or expose the alias to ordinary App/CLI clients.
        self._http_provider_root_id: Optional[str] = None
        self._http_provider_repair_lock = asyncio.Lock()
        self._http_provider_repair_tasks: set[asyncio.Task] = set()
        self._http_provider_repair_stop = asyncio.Event()
        self.last_token_usage: Optional[dict] = None
        self.context_window: Optional[int] = None
        self.app_server_version: Optional[str] = None
        self.last_thread_status: Optional[dict] = None
        self.last_rate_limits: Optional[dict] = None
        self.last_rate_limits_by_id: dict[str, dict] = {}
        self.last_goal: Optional[dict[str, Any]] = None
        self.goal_revision = 0
        self.last_goal_turn_id: Optional[str] = None
        # A goal/automatic continuation can start without query(), hence without
        # a response queue owned by Machine._run_turn.  Track that one turn
        # separately so the machine can lock the session, expose interrupt, and
        # return to idle at its authoritative turn/completed notification.
        self._spontaneous_turn_id: Optional[str] = None
        self._spontaneous_q: Optional[_SpontaneousNotificationQueue] = None
        self._spontaneous_queue_turn_id: Optional[str] = None
        self._spontaneous_overflow = False
        # Per-session Codex settings, persisted through the official
        # thread/settings/update API and repeated on turn/start for an atomic first
        # turn. Config.toml is read-only here and supplies fresh-thread defaults.
        # Codex equivalents of cc's model / effort / permission-mode. Defaults come
        # from ~/.codex/config.toml; the client overrides them via set_* .
        self.model: Optional[str] = codex_model()
        self.effort: Optional[str] = codex_effort()         # low | medium | high | xhigh
        self.applied_effort = self.effort                   # keep machine's spawn-time check a no-op
        # Work is governed by its per-process named permission profile. It must
        # never fall back to interactive escalation outside that profile, even
        # when a resumed native thread persisted a Code-time approval policy.
        self.approval: str = (
            "never" if self.work_mode else codex_approval())  # UI/callback projection
        # Official named profile id (for example ``:workspace`` or
        # ``:danger-full-access``).  It is independent from approvalPolicy:
        # the profile says what the sandbox may access; approvalPolicy says
        # whether Codex may ask to exceed that access.
        self.permission_profile: Optional[str] = (
            "cc_remote_work" if self.work_mode else None
        )
        # app-server has no thread/settings field for search. A Code override is
        # applied through config.web_search on start/resume/fork and retained
        # locally so controlled reconnects preserve it.
        self.web_search_override: Optional[str] = None
        self.web_search: str = (
            "cached" if self.work_mode else codex_web_search()
        )
        self.collaboration_mode: str = "default"            # default | plan; independent of approval
        self.service_tier: Optional[str] = (
            "fast" if codex_fast_enabled() else None
        )                                                    # thread-scoped; None = standard

    async def activate_runtime_events(self) -> None:
        """Release initialization-time notices after Machine can route them.

        Activation is deliberately separate from assigning the callback: a new
        SessionContext does not receive its temp/real key until connect() has
        completed.  The pending queue stays bounded even if activation never
        happens because connect failed.
        """
        async with self._runtime_event_lock:
            callback = self.runtime_event_callback
            if callback is None:
                return
            self._runtime_events_active = True
            pending = list(self._runtime_event_pending.values())
            self._runtime_event_pending.clear()
        for event in pending:
            if (isinstance(event, Notice) and event.thread_id is not None
                    and self.thread_id is not None
                    and event.thread_id != self.thread_id):
                log.warning(
                    "foreign pending codex notice dropped",
                    event_type=event.type,
                    category=event.category,
                )
                continue
            try:
                await callback(event)
            except Exception as exc:
                log.warning(
                    "codex runtime event callback failed",
                    event_type=event.type,
                    error_type=type(exc).__name__,
                )

    @property
    def using_daemon_proxy(self) -> bool:
        """Whether this live handle is attached to the shared Codex daemon."""
        return bool(
            self._using_daemon_proxy
            and self.proc is not None
            and not self._dead
            and getattr(self.proc, "returncode", None) is None
        )

    @property
    def shared_daemon_affinity(self) -> bool:
        """Whether this Code session belongs to the shared app-server.

        Unlike ``using_daemon_proxy``, this remains true while the per-client
        proxy reconnects.  It is deliberately sticky for the handle lifetime:
        falling back to a private stdio app-server after joining the shared
        daemon would split one thread into two independently writable owners.
        """
        return bool(
            not self.work_mode
            and self.daemon_mode == "auto"
            and (self._daemon_proxy_established or self._using_daemon_proxy)
        )

    async def _publish_runtime_event(self, event: RuntimeEvent) -> None:
        key = _runtime_event_key(event)
        async with self._runtime_event_lock:
            if isinstance(event, RateLimitUpdate):
                # Rate values can legitimately cycle (99 -> 100 -> 99). Dedup
                # only a consecutive identical snapshot for the same public
                # limit, not every value observed in the global LRU window.
                identity = event.limit_id or "__default__"
                previous = self._runtime_rate_keys.get(identity)
                if previous == key and key in self._runtime_event_seen:
                    self._runtime_event_seen.move_to_end(key)
                    self._runtime_rate_keys.move_to_end(identity)
                    return
                if previous is not None:
                    self._runtime_event_seen.pop(previous, None)
                self._runtime_rate_keys[identity] = key
                self._runtime_rate_keys.move_to_end(identity)
                while len(self._runtime_rate_keys) > _STATUS_RATE_LIMIT_MAX:
                    _, evicted_key = self._runtime_rate_keys.popitem(last=False)
                    self._runtime_event_seen.pop(evicted_key, None)
            if key in self._runtime_event_seen:
                self._runtime_event_seen.move_to_end(key)
                return
            self._runtime_event_seen[key] = None
            while len(self._runtime_event_seen) > _RUNTIME_EVENT_SEEN_MAX:
                self._runtime_event_seen.popitem(last=False)
            callback = self.runtime_event_callback
            if not self._runtime_events_active or callback is None:
                self._runtime_event_pending[key] = event
                while len(self._runtime_event_pending) > _RUNTIME_EVENT_PENDING_MAX:
                    dropped, _ = self._runtime_event_pending.popitem(last=False)
                    # A capacity drop was never delivered; allow a later repeat
                    # to re-enter after Machine has activated the route.
                    self._runtime_event_seen.pop(dropped, None)
                return
        try:
            await callback(event)
        except Exception as exc:
            # Never include exception text: transports may echo payload details.
            log.warning(
                "codex runtime event callback failed",
                event_type=event.type,
                error_type=type(exc).__name__,
            )

    async def _proxy_handshake(
        self, proc: asyncio.subprocess.Process,
    ) -> None:
        """Upgrade the proxy's raw stdio stream to a local WebSocket."""
        if proc.stdin is None or proc.stdout is None:
            raise CodexProxyProtocolError("proxy stdio unavailable")
        nonce_bytes = os.urandom(16)
        if len(nonce_bytes) != 16:
            raise CodexProxyProtocolError("invalid WebSocket nonce")
        nonce = base64.b64encode(nonce_bytes).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        async with self._send_lock:
            proc.stdin.write(request)
            await proc.stdin.drain()

        received = bytearray()
        marker = b"\r\n\r\n"
        while marker not in received:
            remaining = _PROXY_HANDSHAKE_MAX - len(received)
            if remaining <= 0:
                raise CodexProxyProtocolError("proxy handshake exceeds limit")
            chunk = await proc.stdout.read(min(4096, remaining))
            if not chunk:
                raise CodexProxyProtocolError("proxy closed during handshake")
            received.extend(chunk)
        boundary = received.find(marker) + len(marker)
        if boundary > _PROXY_HANDSHAKE_MAX:
            raise CodexProxyProtocolError("proxy handshake exceeds limit")
        header_bytes = bytes(received[:boundary - len(marker)])
        trailing = received[boundary:]
        try:
            lines = header_bytes.decode("ascii").split("\r\n")
        except UnicodeDecodeError as exc:
            raise CodexProxyProtocolError("non-ASCII proxy handshake") from exc
        if not lines or re.fullmatch(r"HTTP/1\.[01] 101(?: .*)?", lines[0]) is None:
            raise CodexProxyProtocolError("proxy handshake was not HTTP 101")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line or line[:1] in {" ", "\t"} or ":" not in line:
                raise CodexProxyProtocolError("malformed proxy handshake header")
            name, value = line.split(":", 1)
            if not name or any(ord(char) <= 32 or ord(char) >= 127 for char in name):
                raise CodexProxyProtocolError("malformed proxy handshake header")
            headers.setdefault(name.lower(), []).append(value.strip())
        upgrades = headers.get("upgrade", [])
        connections = headers.get("connection", [])
        accepts = headers.get("sec-websocket-accept", [])
        connection_tokens = {
            token.strip().lower()
            for value in connections for token in value.split(",")
        }
        expected = base64.b64encode(hashlib.sha1(
            nonce.encode("ascii") + _WEBSOCKET_GUID,
        ).digest()).decode("ascii")
        if (len(upgrades) != 1 or upgrades[0].lower() != "websocket"
                or "upgrade" not in connection_tokens
                or len(accepts) != 1
                or not hmac.compare_digest(accepts[0], expected)):
            raise CodexProxyProtocolError("proxy handshake validation failed")
        self._proxy_read_buffer = bytearray(trailing)

    async def _open_process(
        self, argv: list[str], codex_bin: str, *, daemon_proxy: bool,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=_codex_env(codex_bin),
            # JSONL and reassembled WebSocket messages share this hard ceiling.
            limit=_PROXY_MESSAGE_MAX,
            start_new_session=(os.name == "posix"),
        )
        self.proc = proc
        self._using_daemon_proxy = daemon_proxy
        self._proxy_read_buffer.clear()
        self._proxy_close_sent = False
        self._process_group = proc.pid if os.name == "posix" else None
        self._generation += 1
        generation = self._generation
        self._dead = False
        # A new app-server/account generation must not inherit a stale runtime
        # snapshot if its first status refresh partially fails.
        self.app_server_version = None
        self.last_thread_status = None
        self.last_rate_limits = None
        self.last_rate_limits_by_id = {}
        self.last_goal = None
        self.goal_revision = 0
        self.last_goal_turn_id = None
        self._spontaneous_turn_id = None
        self.last_token_usage = None
        self.context_window = None
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(proc, generation))
        try:
            if daemon_proxy:
                await asyncio.wait_for(
                    self._proxy_handshake(proc),
                    timeout=_PROXY_HANDSHAKE_TIMEOUT,
                )
        except BaseException:
            await self.disconnect()
            raise
        self._reader = asyncio.create_task(self._read_loop(proc, generation))

    async def connect(
        self,
        resume_id: Optional[str] = None,
        cwd: Optional[str] = None,
        fork: bool = False,
        preserve_controls: bool = False,
        preserve_permission_profile: bool = True,
    ) -> None:
        if self.proc is not None:
            await self.disconnect()
        self._shared_resume_binding_thread_id = None
        self._cwd = cwd or self._cwd or getattr(self.cfg, "cc_cwd", None) or os.getcwd()
        # version-probes subprocesses on first call; keep it off the event loop.
        codex_bin = await asyncio.to_thread(_resolve_codex_bin)
        private_core = None
        http_only_resume = False
        if not self.work_mode and not self._daemon_proxy_established:
            http_only_resume = await asyncio.to_thread(
                _oversized_desktop_openai_resume_requires_http,
                resume_id,
            )
            private_core = await asyncio.to_thread(
                _newer_private_core_for_oversized_resume,
                codex_bin,
                resume_id,
            )
            if private_core is not None:
                codex_bin = private_core
        self._http_provider_root_id = resume_id if http_only_resume else None
        if http_only_resume:
            self._http_provider_repair_stop.clear()
        child_env = _codex_env(codex_bin)
        stdio_argv = [codex_bin, "app-server", "--stdio"]
        if http_only_resume:
            _append_openai_http_resume_provider(stdio_argv)
            log.info(
                "oversized Codex Desktop resume uses official HTTP transport",
                thread_id=resume_id,
            )
        if self.work_mode:
            # One app-server process belongs to one resident session, so a
            # per-process permission profile can enforce this Work cwd without
            # mutating the user's global ~/.codex/config.toml.
            # The arg0 sandbox helper is a symlink to this exact executable. A
            # grant for CODEX_HOME/tmp alone leaves that target invisible inside
            # bwrap and every tool fails with ENOENT before its command starts.
            filesystem_entries = [
                '":minimal" = "read"',
                f'{json.dumps(_codex_runtime_tmp())} = "read"',
                f'{json.dumps(os.path.realpath(codex_bin))} = "read"',
                f'{json.dumps(self._cwd)} = "write"',
            ]
            filesystem = "{ " + ", ".join(filesystem_entries) + " }"
            stdio_argv.extend([
                "-c", 'default_permissions="cc_remote_work"',
                "-c", f"permissions.cc_remote_work.filesystem={filesystem}",
                "-c", "permissions.cc_remote_work.network.enabled=false",
            ])
        proxy_argv: Optional[list[str]] = None
        strict_shared = False
        if (private_core is None and not http_only_resume and not self.work_mode
                and self.daemon_mode == "auto"):
            try:
                proxy_argv = await self.daemon_manager.proxy_args(
                    codex_bin, child_env)
                strict_shared = bool(
                    getattr(
                        self.daemon_manager,
                        "strict_shared_affinity",
                        False,
                    )
                )
            except CodexDaemonUpgradeRequired:
                # Starting private stdio here would appear healthy while
                # silently severing the terminal CLI <-> Remote live channel.
                raise
            except Exception as exc:
                log.warning(
                    "Codex daemon preparation failed; using stdio",
                    error_type=type(exc).__name__,
                )
        attempts = (
            [(proxy_argv, True), (stdio_argv, False)]
            if proxy_argv is not None else [(stdio_argv, False)]
        )
        if strict_shared and proxy_argv is not None:
            attempts = [(proxy_argv, True)]
        if self._daemon_proxy_established:
            if proxy_argv is None:
                raise RuntimeError(
                    "shared Codex app-server proxy is unavailable")
            # A previously shared thread must never reconnect through private
            # stdio.  Leave the handle disconnected and let Machine retry the
            # shared proxy instead of manufacturing a false external-CLI lock.
            attempts = [(proxy_argv, True)]
        initialized: Any = None
        for argv, daemon_proxy in attempts:
            self._shared_resume_binding_thread_id = (
                resume_id
                if daemon_proxy and resume_id and not fork
                else None
            )
            try:
                await self._open_process(
                    argv, codex_bin, daemon_proxy=daemon_proxy)
                initialized = await self._request(
                    "initialize", _initialize_params())
                self.app_server_version = _app_server_version(initialized)
                await self._notify("initialized")
                if daemon_proxy:
                    self._daemon_proxy_established = True
                break
            except asyncio.CancelledError:
                await self.disconnect()
                raise
            except Exception as exc:
                await self.disconnect()
                if not daemon_proxy:
                    raise
                self.daemon_manager.invalidate()
                if strict_shared or self._daemon_proxy_established:
                    log.warning(
                        "Codex shared daemon proxy unavailable; reconnect required",
                        error_type=type(exc).__name__,
                    )
                    raise
                log.warning(
                    "Codex daemon proxy unavailable; using stdio",
                    error_type=type(exc).__name__,
                )
        else:  # pragma: no cover - the attempt list is never empty
            raise RuntimeError("unable to start Codex app-server transport")
        try:
            if self.work_mode:
                # Inspect the effective native runtime rather than guessing at
                # user-configured skill and MCP names.  Failure is fatal: silently
                # falling back would leak Code's global context into Work again.
                skills_response, config_response = await asyncio.gather(
                    self._request("skills/list", {
                        "cwds": [self._cwd],
                        # Work must not miss a skill installed since the last
                        # native cache snapshot; a partial inventory would make
                        # the supposedly isolated context depend on timing.
                        "forceReload": True,
                    }),
                    self._request("config/read", {
                        "cwd": self._cwd,
                        "includeLayers": False,
                    }),
                )
                self._work_config = _work_thread_config(
                    skills_response, config_response)

            if fork and resume_id:
                # ephemeral /btw fork: inherits resume_id's context into a throwaway
                # thread; the parent thread is never touched (verified: fork answers
                # from parent context, parent stays coherent).
                fork_params: dict[str, Any] = {
                    "threadId": resume_id, "ephemeral": True,
                    "cwd": self._cwd,
                    "approvalPolicy": self.approval_policy,
                }
                if self.permission_profile:
                    fork_params["permissions"] = self.permission_profile
                if not self.work_mode and self.web_search_override:
                    fork_params["config"] = {
                        "web_search": self.web_search_override,
                    }
                if http_only_resume:
                    fork_params["modelProvider"] = (
                        _OPENAI_HTTP_RESUME_PROVIDER_ID)
                if self.work_mode:
                    fork_params.update({
                        "baseInstructions": WORK_BASE_INSTRUCTIONS,
                        "developerInstructions": WORK_DEVELOPER_INSTRUCTIONS,
                        "personality": "none",
                        "config": self._work_config,
                    })
                if _supports_lightweight_resume(self.app_server_version):
                    # Official app-server pagination contract: fork the durable
                    # context without serializing its complete turn history over
                    # this control connection. History remains available through
                    # thread/turns/list (and cc-remote's bounded projection).
                    fork_params["excludeTurns"] = True
                res = await self._request("thread/fork", fork_params)
                self.thread_id = _thread_id_of(res)
            elif resume_id:
                # A replacement daemon reconstructs approval/profile from
                # config defaults rather than the last live thread settings.
                # Controlled reconnects repeat the exact settings that the old
                # generation had already accepted.
                resume_params: dict[str, Any] = {
                    "threadId": resume_id, "cwd": self._cwd,
                }
                preserved_approval = (
                    self.approval_policy if preserve_controls else None)
                preserved_profile = (
                    self.permission_profile
                    if preserve_controls and preserve_permission_profile
                    else None
                )
                if preserve_controls:
                    resume_params["approvalPolicy"] = preserved_approval
                    if preserve_permission_profile and preserved_profile:
                        resume_params["permissions"] = preserved_profile
                if http_only_resume:
                    resume_params["modelProvider"] = (
                        _OPENAI_HTTP_RESUME_PROVIDER_ID)
                if self.work_mode:
                    resume_params.update({
                        "baseInstructions": WORK_BASE_INSTRUCTIONS,
                        "developerInstructions": WORK_DEVELOPER_INSTRUCTIONS,
                        "personality": "none",
                        "config": self._work_config,
                        "permissions": "cc_remote_work",
                    })
                elif self.web_search_override:
                    resume_params["config"] = {
                        "web_search": self.web_search_override,
                    }
                if _supports_lightweight_resume(self.app_server_version):
                    # Since Codex 0.144.6, excludeTurns is the official way for
                    # clients with a paged history UI to resume a live thread.
                    # It prevents a multi-hundred-MiB rollout from becoming one
                    # oversized JSON-RPC response while preserving native context.
                    resume_params["excludeTurns"] = True
                else:
                    # Older app-servers reject excludeTurns. Preserve legacy
                    # compatibility only while the rollout can fit within the
                    # transport ceiling; otherwise fail before the stdout reader
                    # is destroyed by an oversized thread/resume response.
                    rollout_path = await asyncio.to_thread(
                        codex_rollout_path, resume_id)
                    try:
                        rollout_size = (
                            await asyncio.to_thread(os.path.getsize, rollout_path)
                            if rollout_path else 0
                        )
                    except OSError:
                        rollout_size = 0
                    if rollout_size > _PROXY_MESSAGE_MAX:
                        version = self.app_server_version or "unknown"
                        minimum = ".".join(
                            str(part) for part in _LIGHTWEIGHT_RESUME_MIN_VERSION)
                        raise RuntimeError(
                            "Codex app-server " + version
                            + " 不支持超长会话的轻量恢复；请升级 Codex 至 "
                            + minimum + " 或更高版本"
                        )
                res = await self._request("thread/resume", resume_params)
                self.thread_id = _thread_id_of(res) or resume_id
                self._shared_resume_binding_thread_id = None
            else:
                params: dict[str, Any] = {
                    "cwd": self._cwd,
                    "approvalPolicy": self.approval_policy,
                    "serviceTier": self.service_tier,
                }
                if self.model:
                    params["model"] = self.model
                if self.permission_profile:
                    params["permissions"] = self.permission_profile
                if not self.work_mode and self.web_search_override:
                    params["config"] = {
                        "web_search": self.web_search_override,
                    }
                if self.work_mode:
                    params.update({
                        "baseInstructions": WORK_BASE_INSTRUCTIONS,
                        "developerInstructions": WORK_DEVELOPER_INSTRUCTIONS,
                        "personality": "none",
                        "config": self._work_config,
                    })
                res = await self._request("thread/start", params)
                self.thread_id = _thread_id_of(res)
            if not self.thread_id:
                raise RuntimeError("codex app-server did not return a thread id")
            if http_only_resume:
                await self._restore_http_provider_state(strict=True)
            if isinstance(res, dict):
                authoritative = res
                if not resume_id:
                    # thread/start has no effort/collaboration params in 0.144.1.
                    # Preserve an explicit new-session first-turn selection instead
                    # of replacing it with the response's config-derived default.
                    authoritative = dict(res)
                    authoritative.pop("reasoningEffort", None)
                self._apply_thread_settings(authoritative)
                if resume_id and not fork and preserve_controls:
                    # Some generations echo config-derived defaults even when
                    # resume overrides were accepted. Keep the next turn pinned
                    # to the controls carried across the generation boundary.
                    if self.work_mode:
                        self.approval = "never"
                        self.permission_profile = "cc_remote_work"
                    else:
                        if isinstance(preserved_approval, str):
                            self.approval = preserved_approval
                        else:
                            granular = _copy_granular_approval(
                                preserved_approval)
                            if granular is not None:
                                self.approval_policy = granular
                                self._approval = "on-request"
                        if preserve_permission_profile:
                            self.permission_profile = preserved_profile
            if not resume_id:
                # thread/start cannot carry effort or collaborationMode in 0.144.1.
                # Persist both before the new-session command can return, so even a
                # blank session that is evicted before its first query retains the
                # complete selection. turn/start still repeats the same values.
                sticky: dict[str, Any] = {
                    "collaborationMode": self._collaboration_setting(
                        self.collaboration_mode),
                }
                if self.effort:
                    sticky["effort"] = self.effort
                if self.permission_profile:
                    sticky["permissions"] = self.permission_profile
                await self._update_thread_settings(
                    wait_for_notification=True, **sticky)
        except BaseException:
            await self.disconnect()
            raise
        log.info("codex connected", thread_id=self.thread_id, cwd=self._cwd,
                 resume=bool(resume_id), fork=fork)

    async def query(self, prompt, images=None) -> Optional[str]:
        if self.thread_id and (
            self.proc is None or self._dead or self.proc.returncode is not None
        ):
            await self.force_reconnect(self.thread_id, self._cwd, reason="app-server unavailable")
        assert self.proc is not None and self.thread_id, "connect() first"
        self._open_managed_stream()
        queue = self._turn_q
        params = {
            "threadId": self.thread_id,
            "input": _to_input(prompt, images),
            "approvalPolicy": self.approval_policy,
        }
        if self.permission_profile:
            params["permissions"] = self.permission_profile
        if self.work_mode:
            params["cwd"] = self._cwd
            # Do not add the legacy sandboxPolicy here. Codex gives legacy
            # sandbox settings precedence over named permission profiles; the
            # old workspaceWrite policy therefore re-protected this cwd merely
            # because Work lives below ~/.codex.
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        # Codex Plan mode is a collaboration-mode override, not an approval
        # policy. The app-server schema requires settings.model. Code selects the
        # built-in mode instructions with null; Work repeats its isolated policy.
        collaboration_model = self.model or codex_model()
        if collaboration_model:
            params["collaborationMode"] = self._collaboration_setting(
                self.collaboration_mode)
        elif self.collaboration_mode == "plan":
            raise RuntimeError("Codex Plan mode requires an active model")
        # null is intentional: in app-server 0.144.1 it clears a persisted Fast
        # override, while omission would leave the previous tier unchanged.
        params["serviceTier"] = self.service_tier
        # Mark the turn active before awaiting the RPC.  app-server may dispatch
        # turn/completed immediately after the response, before this coroutine is
        # scheduled again; setting this afterwards would resurrect a completed
        # turn and leave ownership attribution stuck on "ours".
        # The previous completed turn id is never a valid owner for notifications
        # emitted while this turn/start is pending. Clear it before the RPC so an
        # early turn/started (or even turn/completed) can claim the new id without
        # being mistaken for a stale cross-turn frame.
        self.turn_id = None
        self.turn_active = True
        self.turn_start_pending = True
        try:
            res = await self._request("turn/start", params)
        except BaseException:
            self.turn_active = False
            self.turn_id = None
            if self._turn_q is queue:
                self._turn_q = None
            self._managed_overflow = False
            raise
        finally:
            self.turn_start_pending = False
        turn = (res or {}).get("turn") or {}
        returned_turn_id = turn.get("id")
        if isinstance(returned_turn_id, str) and returned_turn_id:
            self.remember_owned_turn_id(returned_turn_id)
            # A very fast turn can complete before the turn/start response. Do
            # not resurrect it as interruptible after its authoritative terminal
            # notification already cleared turn_active.
            if self.turn_active:
                self.turn_id = returned_turn_id
            return returned_turn_id
        return None

    async def steer(
        self,
        prompt,
        images=None,
        *,
        client_user_message_id: Optional[str] = None,
    ) -> str:
        """Append input to the exact active turn without changing lifecycle."""
        if self.thread_id and (
            self.proc is None or self._dead or self.proc.returncode is not None
        ):
            raise RuntimeError("codex app-server unavailable during steer")
        if not (
            self.proc and self.thread_id and self.turn_active and self.turn_id
        ):
            raise CodexAppServerError({
                "code": -32600,
                "message": "active turn not steerable",
            })
        thread_id = self.thread_id
        turn_id = self.turn_id
        params: dict[str, Any] = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": _to_input(prompt, images),
        }
        if client_user_message_id:
            params["clientUserMessageId"] = client_user_message_id
        boundary = _CodexSteerResponseBoundary(thread_id, turn_id)
        try:
            result = await self._request(
                "turn/steer",
                params,
                response_boundary=boundary,
            )
        except CodexAppServerError:
            raise
        except Exception as exc:
            raise CodexSteerOutcomeUnknown(
                "Codex app-server did not confirm turn/steer") from exc
        returned_turn_id = (
            result.get("turnId") if isinstance(result, dict) else None
        )
        if returned_turn_id != turn_id:
            boundary.release()
            raise CodexAppServerError({
                "code": -32600,
                "message": "expected turn mismatch after turn/steer",
            })
        return CodexSteerAcceptance(turn_id, boundary.fence)

    def remember_owned_turn_id(self, turn_id: str) -> None:
        self._owned_turn_ids[turn_id] = None
        self._owned_turn_ids.move_to_end(turn_id)
        while len(self._owned_turn_ids) > _OWNED_TURN_IDS_MAX:
            self._owned_turn_ids.popitem(last=False)

    @property
    def owned_turn_ids(self) -> frozenset[str]:
        return frozenset(self._owned_turn_ids)

    @property
    def turn_attribution_pending(self) -> bool:
        """Whether a rollout task may still belong to the current local launch.

        Ordinary turns learn their id from ``turn/start``. Inline Review is
        different: ``review/start`` returns the visible outer id first, then
        app-server announces a second nested id which is the rollout writer and
        interrupt target. Keep the ownership watcher in its attribution grace
        until that second notification has arrived.
        """
        return bool(
            self.turn_start_pending
            or (self._review_active
                and self._review_execution_turn_id is None)
        )

    def _begin_review_tracking(self) -> None:
        self._review_active = True
        self._review_outer_turn_id = None
        self._review_execution_turn_id = None
        self._review_execution_ready = asyncio.Event()

    def _clear_review_tracking(self) -> None:
        # Wake an interrupt which is waiting for the nested reviewer id.  It will
        # re-check the ids/active flag and avoid issuing a stale RPC.
        self._review_execution_ready.set()
        self._review_active = False
        self._review_outer_turn_id = None
        self._review_execution_turn_id = None

    async def receive_response(self):
        """Async-gen of this turn's raw notification dicts, ending at turn/completed."""
        q = self._turn_q
        if q is None:
            return
        terminal_seen = False
        try:
            while True:
                msg = await q.get()
                if msg is None:      # sentinel pushed by the reader on turn/completed
                    break
                yield msg
                # The fail-fast managed bridge preserves terminal frames without
                # spending a third queue slot on a sentinel. Legacy asyncio.Queue
                # tests may still append one; it is harmlessly abandoned below.
                if isinstance(msg, dict) and msg.get("method") == "turn/completed":
                    terminal_seen = True
                    break
        finally:
            if self._turn_q is q:
                self._turn_q = None
            self._managed_overflow = False
            # An automatic continuation may have started after this managed
            # queue received its terminal sentinel but before its consumer
            # unwound. Do not let the old generator clear the new turn's active
            # ownership (thread-scoped hooks depend on this flag).
            if self._spontaneous_turn_id is None:
                self.turn_active = False
            if terminal_seen:
                await self._restore_http_provider_state(
                    include_descendants=True,
                )
                self._schedule_http_provider_descendant_repair()

    def _notification_queue_limits(
        self, *, managed: bool,
    ) -> tuple[int, int]:
        reader_cap = max(1, int(getattr(self.cfg, "turn_reader_queue_cap", 4)))
        item_cap = (
            min(
                _MANAGED_QUEUE_MAX_ITEMS,
                max(_MANAGED_QUEUE_MIN_ITEMS, reader_cap * 16),
            )
            if managed
            else min(
                _SPONTANEOUS_QUEUE_MAX_ITEMS,
                max(_SPONTANEOUS_QUEUE_MIN_ITEMS, reader_cap * 16),
            )
        )
        ws_cap = max(1024, int(getattr(
            self.cfg, "ws_max_size_bytes", 16 * 1024 * 1024)))
        tool_cap = max(1024, int(getattr(self.cfg, "tool_result_max", 65536)))
        byte_cap = min(
            ws_cap,
            max(
                (_MANAGED_QUEUE_MIN_BYTES if managed
                 else _SPONTANEOUS_QUEUE_MIN_BYTES),
                tool_cap * 16,
            ),
        )
        return item_cap, byte_cap

    def _open_managed_stream(self) -> None:
        """Create a bounded producer that can never block JSON-RPC stdout."""
        item_cap, byte_cap = self._notification_queue_limits(managed=True)
        self._turn_q = _SpontaneousNotificationQueue(item_cap, byte_cap)
        self._managed_overflow = False

    def _open_spontaneous_stream(self, turn_id: str) -> None:
        """Create the bounded raw-notification bridge before announcing a turn."""
        if self._spontaneous_q is not None:
            self._close_spontaneous_stream(self._spontaneous_queue_turn_id)
        item_cap, byte_cap = self._notification_queue_limits(managed=False)
        self._spontaneous_q = _SpontaneousNotificationQueue(item_cap, byte_cap)
        self._spontaneous_queue_turn_id = turn_id
        self._spontaneous_overflow = False

    @staticmethod
    def _notification_wire_size(message: dict) -> int:
        try:
            return len(json.dumps(
                message, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8", errors="surrogatepass"))
        except Exception:
            # Invalid/non-JSON values are already unusable as app-server frames.
            # Charging the full single-frame allowance fails closed without
            # copying arbitrary object representations into logs.
            return 16 * 1024 * 1024

    @staticmethod
    def _minimal_turn_completed(message: dict) -> dict:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        raw_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn: dict[str, Any] = {}
        turn_id = raw_turn.get("id") or params.get("turnId")
        if isinstance(turn_id, str) and 0 < len(turn_id) <= 512:
            turn["id"] = turn_id
        status = raw_turn.get("status")
        if status in {"completed", "interrupted", "failed"}:
            turn["status"] = status
        duration = raw_turn.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            try:
                turn["durationMs"] = max(0, int(duration))
            except (OverflowError, ValueError):
                pass
        out_params: dict[str, Any] = {"turn": turn}
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and 0 < len(thread_id) <= 512:
            out_params["threadId"] = thread_id
        if isinstance(turn_id, str) and 0 < len(turn_id) <= 512:
            out_params["turnId"] = turn_id
        return {"method": "turn/completed", "params": out_params}

    def _retain_terminal_notification(
        self,
        q: _SpontaneousNotificationQueue,
        message: dict,
        size: int,
    ) -> None:
        terminal_message = message
        terminal_size = size
        if terminal_size > q.max_end_bytes:
            terminal_message = self._minimal_turn_completed(message)
            terminal_size = self._notification_wire_size(terminal_message)
        if q.put_terminal_nowait(terminal_message, terminal_size):
            return
        # The bounded minimal form above is normally below 2 KiB. Keep one
        # schema-valid status-only terminal even if untrusted ids were extreme.
        params = (
            message.get("params")
            if isinstance(message.get("params"), dict)
            else {}
        )
        raw_turn = (
            params.get("turn")
            if isinstance(params.get("turn"), dict)
            else {}
        )
        status = raw_turn.get("status")
        tiny = {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "status": (
                        status
                        if status in {"completed", "interrupted", "failed"}
                        else "failed"
                    ),
                },
            },
        }
        q.put_terminal_nowait(tiny, self._notification_wire_size(tiny))

    def _queue_spontaneous_notification(
        self, message: dict, raw_size: Optional[int] = None,
    ) -> bool:
        """Offer one current-turn frame without ever awaiting relay backpressure."""
        q = self._spontaneous_q
        turn_id = self._spontaneous_queue_turn_id
        if q is None or turn_id is None:
            return False
        method = message.get("method")
        terminal = method == "turn/completed"
        size = (
            raw_size if isinstance(raw_size, int) and raw_size >= 0
            else self._notification_wire_size(message)
        )
        if terminal:
            self._retain_terminal_notification(q, message, size)
            return True
        if not q.accepts_after_gap(message):
            return True
        if q.put_nowait(message, size):
            return True

        if not self._spontaneous_overflow:
            log.warning(
                "codex spontaneous notification bridge overflow",
                turn_id=turn_id,
                queued=q.qsize(),
                queued_bytes=q.byte_size,
            )
        self._spontaneous_overflow = True
        q.begin_gap(CodexSpontaneousOverflow(turn_id))
        q.retry_after_gap_nowait(message, size)
        return True

    def _queue_managed_notification(
        self, message: dict, raw_size: Optional[int] = None,
    ) -> bool:
        """Offer a managed-turn frame without blocking the sole stdout reader.

        review/start can emit multiple item notifications before its RPC response.
        A regular bounded ``asyncio.Queue.put`` deadlocks once full because the
        response which starts the consumer is waiting behind those notifications.
        On overflow retain one gap signal, then admit only intact new item
        lifecycles; the authoritative terminal has its own reserved end slot.
        """
        q = self._turn_q
        if not isinstance(q, _SpontaneousNotificationQueue):
            return False
        method = message.get("method")
        terminal = method == "turn/completed"
        size = (
            raw_size if isinstance(raw_size, int) and raw_size >= 0
            else self._notification_wire_size(message)
        )
        if terminal:
            self._retain_terminal_notification(q, message, size)
            return True
        if not q.accepts_after_gap(message):
            return True
        if q.put_nowait(message, size):
            return True

        turn_id = self.turn_id or _notification_turn_id(message)
        if not self._managed_overflow:
            log.warning(
                "codex managed notification bridge overflow",
                turn_id=turn_id,
                queued=q.qsize(),
                queued_bytes=q.byte_size,
            )
            self._managed_overflow = True
        q.begin_gap(CodexManagedOverflow(turn_id))
        q.retry_after_gap_nowait(message, size)
        return True

    def _close_spontaneous_stream(self, turn_id: Optional[str]) -> None:
        """Wake the bridge consumer after disconnect/EOF, without blocking stdout."""
        q = self._spontaneous_q
        current = self._spontaneous_queue_turn_id
        if q is None or current is None or (turn_id and turn_id != current):
            return
        if q.has_turn_completed():
            return
        closed = CodexSpontaneousClosed(current)
        if q.put_end_nowait(closed):
            return

    def _discard_spontaneous_stream(self, turn_id: str) -> None:
        """Drop a recovery bridge that was never exposed to a consumer."""
        if self._spontaneous_queue_turn_id != turn_id:
            return
        self._spontaneous_q = None
        self._spontaneous_queue_turn_id = None
        self._spontaneous_overflow = False

    async def receive_spontaneous_response(self, turn_id: str):
        """Yield exactly one spontaneous turn's raw frames and internal signals."""
        q = self._spontaneous_q
        if q is None or self._spontaneous_queue_turn_id != turn_id:
            return
        terminal_seen = False
        try:
            while True:
                item = await q.get()
                yield item
                if isinstance(item, CodexSpontaneousClosed):
                    break
                if isinstance(item, dict) and item.get("method") == "turn/completed":
                    terminal_seen = True
                    break
        finally:
            if self._spontaneous_q is q:
                self._spontaneous_q = None
                self._spontaneous_queue_turn_id = None
                self._spontaneous_overflow = False
            if terminal_seen:
                await self._restore_http_provider_state(
                    include_descendants=True,
                )
                self._schedule_http_provider_descendant_repair()

    async def interrupt(self) -> None:
        if not (self.proc and self.thread_id and self.turn_id):
            raise RuntimeError("codex turn is not running")
        target_thread_id = self.thread_id
        target_turn_id = self._review_execution_turn_id or self.turn_id
        try:
            await self._request(
                "turn/interrupt", {
                    "threadId": target_thread_id,
                    "turnId": target_turn_id,
                },
            )
            return
        except Exception as first_error:
            # A click can race review/start's outer response and the nested
            # turn/started notification.  app-server serializes both on stdout,
            # so a rejected outer interrupt is normally followed immediately by
            # the authoritative nested id.  Wait briefly and retry only when the
            # target actually changed; unrelated interrupt failures still surface.
            if self._review_active and self._review_execution_turn_id is None:
                ready = self._review_execution_ready
                try:
                    await asyncio.wait_for(ready.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            retry_turn_id = self._review_execution_turn_id
            if (self._review_active and retry_turn_id
                    and retry_turn_id != target_turn_id):
                try:
                    await self._request(
                        "turn/interrupt", {
                            "threadId": target_thread_id,
                            "turnId": retry_turn_id,
                        },
                    )
                except CodexAppServerError as retry_error:
                    if retry_error.no_active_turn:
                        raise CodexNoActiveTurnError(
                            target_thread_id, retry_turn_id) from retry_error
                    raise
                return
            if (isinstance(first_error, CodexAppServerError)
                    and first_error.no_active_turn):
                raise CodexNoActiveTurnError(
                    target_thread_id, target_turn_id) from first_error
            raise

    async def confirm_no_active_turn(
        self, thread_id: str, turn_id: str,
    ) -> Optional[CodexNoActiveTurnConfirmation]:
        """Prove one spontaneous turn inactive at a raw-stream boundary.

        ``turn/interrupt`` can report no active turn before a nearby terminal
        notification reaches the bridge consumer. ``thread/read`` is issued on
        the same app-server transport and its response gives the stdout reader a
        deterministic ordering point. A zero-byte fence inserted immediately
        after that response lets the machine drain every preceding live frame
        before deciding whether a synthetic terminal is still necessary.
        """
        if self.thread_id != thread_id:
            return None
        queue = (
            self._spontaneous_q
            if (
                self._spontaneous_queue_turn_id == turn_id
                and isinstance(
                    self._spontaneous_q, _SpontaneousNotificationQueue
                )
            )
            else None
        )
        terminal_already_observed = bool(
            self.turn_id is None
            and not self.turn_active
            and turn_id in self._owned_turn_ids
        )
        if not (
            self.turn_id == turn_id
            and self.turn_active
        ) and queue is None and not terminal_already_observed:
            return None

        result = await self._request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        raw_thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(raw_thread, dict) or raw_thread.get("id") != thread_id:
            return None
        raw_status = raw_thread.get("status")
        status_type = (
            raw_status.get("type") if isinstance(raw_status, dict) else None
        )
        if status_type not in {"idle", "notLoaded", "systemError"}:
            return None

        current_queue = (
            self._spontaneous_q
            if (
                self._spontaneous_q is queue
                and self._spontaneous_queue_turn_id == turn_id
            )
            else None
        )
        authoritative_terminal = bool(
            (current_queue is not None
             and current_queue.has_turn_completed())
            or terminal_already_observed
            or (
                self.turn_id is None
                and not self.turn_active
                and self._spontaneous_turn_id != turn_id
            )
        )
        if authoritative_terminal:
            return CodexNoActiveTurnConfirmation(
                fence=None,
                queue=current_queue,
                authoritative_terminal=True,
            )

        # A different active turn may have begun while thread/read was in flight.
        # Never use the old interrupt miss to clear that newer lifecycle.
        if (
            self.thread_id != thread_id
            or self.turn_id != turn_id
            or not self.turn_active
        ):
            return None
        fence = (
            CodexNoActiveTurnFence()
            if current_queue is not None
            else None
        )
        if current_queue is not None and fence is not None:
            current_queue.put_control_nowait(fence)
        return CodexNoActiveTurnConfirmation(
            fence=fence,
            queue=current_queue,
            authoritative_terminal=False,
        )

    def reconcile_no_active_turn(
        self, thread_id: str, turn_id: str,
    ) -> bool:
        """Commit one previously confirmed inactive local turn."""
        if self.thread_id != thread_id:
            return False
        if self.turn_id not in {None, turn_id}:
            return False
        if self.turn_id is None and self.turn_active:
            return False
        self.turn_active = False
        self.turn_start_pending = False
        if self.turn_id == turn_id:
            self.turn_id = None
        if self._spontaneous_turn_id == turn_id:
            self._close_spontaneous_stream(turn_id)
            self._spontaneous_turn_id = None
        return True

    async def disconnect(self) -> None:
        self._http_provider_repair_stop.set()
        proc = self.proc
        process_group = self._process_group
        daemon_proxy = self._using_daemon_proxy
        spontaneous_turn_id = self._spontaneous_turn_id
        self._close_spontaneous_stream(spontaneous_turn_id)
        self._spontaneous_turn_id = None
        self._clear_review_tracking()
        tasks = [t for t in (self._reader, self._stderr_task)
                 if t is not None and t is not asyncio.current_task()]
        server_tasks = [
            task for task in self._server_request_tasks
            if task is not asyncio.current_task()
        ]
        self._server_request_tasks.clear()
        self._server_request_tasks_by_id.clear()
        self._pending_server_request_ids.clear()
        for boundary in self._pending_response_boundaries.values():
            boundary.release()
        self._pending_response_boundaries.clear()
        self.proc = None
        self._process_group = None
        self._reader = None
        self._stderr_task = None
        self._generation += 1  # invalidate callbacks from the old process
        self._dead = True
        for t in tasks + server_tasks:
            t.cancel()
        if tasks or server_tasks:
            await asyncio.gather(*tasks, *server_tasks, return_exceptions=True)
        if proc is not None:
            def stop(sig: signal.Signals, *, force: bool = False) -> None:
                try:
                    if process_group is not None:
                        os.killpg(process_group, sig)
                    elif proc.returncode is None:
                        (proc.kill() if force else proc.terminate())
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    log.warning("codex process stop failed",
                                signal=sig.name, error=str(exc))

            if proc.returncode is None:
                stop(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    stop(getattr(signal, "SIGKILL", signal.SIGTERM), force=True)
                    await proc.wait()
            # For stdio this also cleans tool descendants.  For daemon mode the
            # process group contains only this connection's proxy; the shared
            # app-server was started independently by the official manager.
            if process_group is not None:
                stop(signal.SIGKILL, force=True)
        self._using_daemon_proxy = False
        self._shared_resume_binding_thread_id = None
        self._proxy_read_buffer.clear()
        self._proxy_close_sent = False
        if self._turn_q is not None:
            self._force_turn_sentinel(self._turn_q)
            self._turn_q = None
        self._managed_overflow = False
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server disconnected"))
        self._pending.clear()
        self.turn_id = None
        self.turn_start_pending = False
        self.turn_active = False
        if spontaneous_turn_id is not None:
            await self._publish_turn_lifecycle(
                "completed", spontaneous_turn_id)
        await self._restore_http_provider_state(include_descendants=True)
        self._http_provider_root_id = None
        repair_tasks = [
            task for task in self._http_provider_repair_tasks
            if task is not asyncio.current_task()
        ]
        if repair_tasks:
            await asyncio.gather(*repair_tasks, return_exceptions=True)
        self._http_provider_repair_tasks.clear()
        if daemon_proxy:
            log.debug("codex daemon proxy disconnected")

    async def force_reconnect(self, resume_id: Optional[str], cwd: Optional[str] = None,
                              reason: str = "reconnect") -> None:
        log.warning("codex force-reconnect", reason=reason)
        target = resume_id or self.thread_id
        await self.disconnect()
        await self.connect(
            resume_id=target,
            cwd=cwd or self._cwd,
            preserve_controls=True,
        )

    # --- live controls (persisted for this thread by app-server 0.144.1) ---
    @property
    def cwd(self) -> Optional[str]:
        """The effective cwd last confirmed by app-server."""
        return self._cwd

    @property
    def approval(self) -> str:
        return self._approval

    @approval.setter
    def approval(self, value: str) -> None:
        # Existing machine/tests assign this projection directly. Keep the raw
        # policy in lockstep for named policies; granular snapshots set _approval
        # directly so turn/start can preserve their full official object.
        if self.work_mode:
            value = "never"
        self._approval = value
        self.approval_policy: Any = value

    async def _update_thread_settings(
        self, *, wait_for_notification: bool = False, **settings: Any,
    ) -> bool:
        if not self.thread_id:
            raise RuntimeError("connect() first")
        if wait_for_notification:
            self._thread_settings_updated.clear()
        await self._request(
            "thread/settings/update",
            {"threadId": self.thread_id, **settings},
        )
        # The response is intentionally empty in 0.144.1. Its full authoritative
        # snapshot arrives on thread/settings/updated and may include a model-driven
        # effort adjustment. Wait only on a real connected reader; isolated unit
        # fakes and older servers fall back to the requested value without hanging.
        if wait_for_notification and self._reader is not None:
            try:
                await asyncio.wait_for(
                    self._thread_settings_updated.wait(),
                    timeout=_THREAD_SETTINGS_NOTIFY_TIMEOUT,
                )
                await self._restore_http_provider_state()
                return True
            except asyncio.TimeoutError:
                log.warning("codex thread settings notification timed out")
        return False

    def _collaboration_setting(self, mode: str) -> dict[str, Any]:
        model = self.model or codex_model()
        if not model:
            raise RuntimeError("Codex collaboration mode requires an active model")
        settings: dict[str, Any] = {
            "model": model,
            "developer_instructions": (
                WORK_DEVELOPER_INSTRUCTIONS if self.work_mode else None),
        }
        if self.effort:
            settings["reasoning_effort"] = self.effort
        return {"mode": mode, "settings": settings}

    def _apply_thread_settings(self, settings: dict[str, Any]) -> None:
        """Adopt a resume response or thread/settings/updated snapshot.

        Resume responses use ``reasoningEffort`` while notification snapshots
        use ``effort``. Both expose the remaining settings with the same names.
        Granular approval objects are preserved in ``approval_policy`` while the
        current UI receives their lossless-compatible ``on-request`` projection.
        """
        cwd = settings.get("cwd")
        if (isinstance(cwd, str) and os.path.isabs(cwd)
                and 0 < len(cwd) <= 4096):
            self._cwd = os.path.realpath(cwd)

        model = settings.get("model")
        if isinstance(model, str) and model:
            self.model = model[:256]

        effort_key = "effort" if "effort" in settings else "reasoningEffort"
        if effort_key in settings:
            effort = settings.get(effort_key)
            if effort is None:
                self.effort = None
                self.applied_effort = None
            elif isinstance(effort, str) and effort:
                self.effort = effort[:64]
                self.applied_effort = self.effort

        approval = settings.get("approvalPolicy")
        if self.work_mode:
            # A resume response may carry the thread's previous Code policy.
            # Work's filesystem profile is non-escalating by construction.
            self.approval = "never"
        elif isinstance(approval, str) and approval in {
            "untrusted",
            "on-request",
            "never",
        }:
            self.approval = approval
        else:
            granular = _copy_granular_approval(approval)
            if granular is not None:
                self.approval_policy = granular
                # Remote's current wire has only named modes. Project granular to
                # the interactive behavior without destroying the raw policy.
                self._approval = "on-request"

        if "serviceTier" in settings:
            tier = settings.get("serviceTier")
            if tier is None:
                self.service_tier = None
            elif isinstance(tier, str) and tier:
                self.service_tier = tier[:64]

        collaboration = settings.get("collaborationMode")
        if isinstance(collaboration, dict):
            mode = collaboration.get("mode")
            if mode in {"default", "plan"}:
                self.collaboration_mode = mode

        active_key = (
            "activePermissionProfile"
            if "activePermissionProfile" in settings
            else "active_permission_profile"
        )
        active_profile = settings.get(active_key)
        if self.work_mode:
            self.permission_profile = "cc_remote_work"
        elif isinstance(active_profile, dict):
            profile_id = active_profile.get("id")
            if isinstance(profile_id, str) and 0 < len(profile_id) <= 256:
                self.permission_profile = profile_id
        elif active_key in settings:
            self.permission_profile = None

    async def set_model(self, model: str) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("Codex model must be non-empty")
        authoritative = await self._update_thread_settings(
            model=model, wait_for_notification=True)
        if not authoritative:
            self.model = model
        log.info("codex thread model set", requested=model, applied=self.model)

    async def set_cwd(
        self, cwd: str, *, reason: str = "thread cwd update",
    ) -> str:
        """Retarget subsequent turns and require an authoritative snapshot."""
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("Codex cwd must be non-empty")
        target = os.path.realpath(os.path.expanduser(cwd))
        if not os.path.isabs(target) or not await asyncio.to_thread(
            os.path.isdir, target
        ):
            raise ValueError("Codex cwd must be an existing absolute directory")
        authoritative = await self._update_thread_settings(
            cwd=target,
            wait_for_notification=True,
        )
        effective = self._cwd
        if (
            not authoritative
            or not isinstance(effective, str)
            or os.path.realpath(effective) != target
        ):
            raise RuntimeError(
                "Codex app-server did not confirm the requested cwd"
            )
        log.info(
            "codex thread cwd set",
            requested=target,
            applied=effective,
            reason=reason,
        )
        return effective

    async def set_effort(self, effort: str) -> None:
        if not isinstance(effort, str) or not effort:
            raise ValueError("Codex effort must be non-empty")
        authoritative = await self._update_thread_settings(
            effort=effort, wait_for_notification=True)
        if not authoritative:
            self.effort = effort
            self.applied_effort = effort
        log.info("codex thread effort set", requested=effort,
                 applied=self.effort)

    async def set_service_tier(self, tier: Optional[str]) -> None:
        normalized = tier if tier and tier != "default" else None
        if normalized not in {None, "fast"}:
            raise ValueError(f"unsupported Codex service tier: {tier}")
        authoritative = await self._update_thread_settings(
            serviceTier=normalized, wait_for_notification=True)
        if not authoritative:
            self.service_tier = normalized
        log.info("codex thread service tier set", requested=normalized,
                 applied=self.service_tier)

    async def set_permission_mode(self, mode: str) -> None:
        # Codex "mode" = approval policy (untrusted | on-request | never).
        if mode not in ("untrusted", "on-request", "never"):
            raise ValueError(f"unsupported codex approval policy: {mode}")
        if self.work_mode and mode != "never":
            raise ValueError(
                "Codex Work approval is fixed to never; its named permission "
                "profile controls access")
        authoritative = await self._update_thread_settings(
            approvalPolicy=mode, wait_for_notification=True)
        if not authoritative:
            self.approval = mode
        log.info("codex thread approval set", requested=mode,
                 applied=self.approval)

    async def list_permission_profiles(self) -> list[dict[str, Any]]:
        """Return the bounded, cwd-aware profile catalog from app-server."""
        return normalize_permission_profiles(await self._request(
            "permissionProfile/list",
            {"cwd": self._cwd, "limit": 128},
        ))

    async def set_permission_profile(self, profile: str) -> None:
        if (not isinstance(profile, str) or not profile
                or len(profile) > 256):
            raise ValueError("Codex permission profile must be non-empty")
        if self.work_mode:
            raise ValueError(
                "Codex Work permission profile is fixed to cc_remote_work")
        catalog = await self.list_permission_profiles()
        selected = next(
            (item for item in catalog if item["id"] == profile), None)
        if selected is None or not selected["allowed"]:
            raise ValueError(
                "Codex permission profile is unavailable for this cwd")
        authoritative = await self._update_thread_settings(
            permissions=profile, wait_for_notification=True)
        if not authoritative:
            self.permission_profile = profile
        log.info(
            "codex permission profile set",
            requested=profile,
            applied=self.permission_profile,
        )

    async def set_web_search(self, mode: str) -> None:
        """Apply Code search mode by resuming the same thread with config."""
        if mode not in {"cached", "live"}:
            raise ValueError(f"unsupported Codex web search mode: {mode}")
        if self.work_mode:
            raise ValueError("Codex Work web search is fixed to cached")
        if not self.thread_id:
            raise RuntimeError("connect() first")
        if self.turn_active or self.turn_start_pending:
            raise RuntimeError("Codex turn is active")
        if (self.web_search_override == mode and self.web_search == mode):
            return
        thread_id = self.thread_id
        previous_approval = self._approval
        previous_approval_policy = self.approval_policy
        granular_approval = _copy_granular_approval(
            previous_approval_policy)
        if granular_approval is not None:
            previous_approval_policy = granular_approval
        previous_profile = self.permission_profile
        previous_override = self.web_search_override
        previous_mode = self.web_search
        self.web_search_override = mode
        self.web_search = mode
        try:
            await self.force_reconnect(
                thread_id,
                self._cwd,
                reason="web search changed",
            )
        except BaseException:
            # The failed replacement process may have reported its config
            # defaults before failing later in resume. Roll back the complete
            # execution boundary, not just search, before reconnecting.
            self._approval = previous_approval
            self.approval_policy = previous_approval_policy
            self.permission_profile = previous_profile
            self.web_search_override = previous_override
            self.web_search = previous_mode
            if self.proc is None:
                try:
                    await self.connect(
                        resume_id=thread_id,
                        cwd=self._cwd,
                        preserve_controls=True,
                    )
                except Exception:
                    log.exception(
                        "codex web search rollback reconnect failed",
                        thread_id=thread_id,
                    )
            raise
        log.info("codex web search set", mode=mode, thread_id=thread_id)

    async def set_collaboration_mode(self, mode: str) -> None:
        if mode not in ("default", "plan"):
            raise ValueError(f"unsupported codex collaboration mode: {mode}")
        authoritative = await self._update_thread_settings(
            collaborationMode=self._collaboration_setting(mode),
            wait_for_notification=True,
        )
        if not authoritative:
            self.collaboration_mode = mode
        log.info("codex thread collaboration mode set", requested=mode,
                 applied=self.collaboration_mode)

    async def get_goal(self) -> Optional[dict]:
        assert self.thread_id, "connect() first"
        result = await self._request("thread/goal/get", {"threadId": self.thread_id})
        goal = (result or {}).get("goal")
        if goal is None:
            self.last_goal = None
            return None
        self.last_goal = _sanitize_thread_goal(goal, self.thread_id)
        return self.last_goal

    async def set_goal(self, *, objective: Optional[str] = None,
                       status: Optional[str] = None,
                       token_budget: Optional[int] = None) -> dict:
        assert self.thread_id, "connect() first"
        params = {"threadId": self.thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        if token_budget is not None:
            params["tokenBudget"] = token_budget
        result = await self._request("thread/goal/set", params)
        goal = (result or {}).get("goal")
        if not isinstance(goal, dict):
            raise RuntimeError("codex app-server did not return a goal")
        self.last_goal = _sanitize_thread_goal(goal, self.thread_id)
        return self.last_goal

    async def clear_goal(self) -> bool:
        assert self.thread_id, "connect() first"
        result = await self._request("thread/goal/clear", {"threadId": self.thread_id})
        cleared = bool((result or {}).get("cleared"))
        if cleared:
            self.last_goal = None
        return cleared

    async def start_review(self, target: dict[str, Any]) -> dict[str, str]:
        """Start an official inline review on this resident app-server.

        The resident process is required: review/start creates a real turn and
        its reasoning/tool notifications must flow through the same stdout
        reader as ordinary turns instead of disappearing in a one-shot RPC.
        """
        assert self.thread_id, "connect() first"
        if self.turn_active:
            raise RuntimeError("codex thread is busy")
        self._open_managed_stream()
        queue = self._turn_q
        assert queue is not None
        # review/start creates a normal inline turn. Claim it before awaiting the
        # RPC exactly like turn/start: app-server can emit enteredReviewMode and
        # even turn/completed before the response coroutine is scheduled again.
        # Without this queue those frames are classified as orphan/spontaneous,
        # and the UI can never observe the review's terminal event.
        self.turn_id = None
        self.turn_active = True
        self.turn_start_pending = True
        self._begin_review_tracking()
        try:
            result = await self._request("review/start", {
                "threadId": self.thread_id,
                "target": target,
                "delivery": "inline",
            })
            review_thread_id = (result or {}).get("reviewThreadId")
            turn = (result or {}).get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if not isinstance(review_thread_id, str) or not review_thread_id:
                raise RuntimeError(
                    "codex app-server did not return a review thread")
            if not isinstance(turn_id, str) or not turn_id:
                raise RuntimeError(
                    "codex app-server did not return a review turn")
            self.remember_owned_turn_id(turn_id)
            if self._review_active:
                if (self._review_outer_turn_id is not None
                        and self._review_outer_turn_id != turn_id):
                    raise RuntimeError(
                        "codex app-server returned mismatched review turns")
                self._review_outer_turn_id = turn_id
                # A revision which reports turn/started for the outer lifecycle
                # before answering review/start can provisionally look like the
                # nested executor. The RPC response is authoritative; reopen the
                # nested-id attribution window for the actual reviewer turn.
                if self._review_execution_turn_id == turn_id:
                    self._review_execution_turn_id = None
                    self._review_execution_ready = asyncio.Event()
            # A fast review may already have queued its terminal event. Do not
            # resurrect it as interruptible after turn/completed cleared the flag.
            if self.turn_active:
                self.turn_id = turn_id
            else:
                # turn/completed can precede the RPC response, when the outer id
                # is not known yet and _dispatch therefore cannot clear Review
                # tracking. The completed state is authoritative here.
                self._clear_review_tracking()
            return {"thread_id": review_thread_id, "turn_id": turn_id}
        except BaseException:
            self.turn_active = False
            self.turn_id = None
            self._clear_review_tracking()
            if self._turn_q is queue:
                self._turn_q = None
            raise
        finally:
            self.turn_start_pending = False

    async def compact_thread(self) -> None:
        assert self.thread_id, "connect() first"
        if self.turn_active:
            raise RuntimeError("codex thread is busy")
        await self._request(
            "thread/compact/start", {"threadId": self.thread_id})

    async def rollback_thread(self, num_turns: int) -> dict[str, Any]:
        assert self.thread_id, "connect() first"
        if self.turn_active:
            raise RuntimeError("codex thread is busy")
        if not isinstance(num_turns, int) or isinstance(num_turns, bool) \
                or not 1 <= num_turns <= 1000:
            raise ValueError("num_turns must be between 1 and 1000")
        result = await self._request("thread/rollback", {
            "threadId": self.thread_id,
            "numTurns": num_turns,
        })
        thread = (result or {}).get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("codex app-server did not return the rolled back thread")
        return thread

    async def _publish_goal(self, goal: Optional[dict[str, Any]]) -> None:
        """Forward one sanitized goal notification without killing the reader."""
        callback = self.goal_callback
        if callback is None:
            return
        try:
            await callback(goal)
        except Exception as exc:
            # The app-server stdout reader owns every outstanding RPC response.
            # A UI transport failure must not terminate it, and raw exception text
            # may contain relay/provider details.
            log.warning(
                "codex goal callback failed",
                error_type=type(exc).__name__,
            )

    async def _publish_turn_lifecycle(self, phase: str, turn_id: str) -> None:
        """Forward a spontaneous app-server turn without killing stdout reader."""
        callback = self.turn_lifecycle_callback
        if callback is None:
            return
        try:
            await callback(phase, turn_id)
        except Exception as exc:
            log.warning(
                "codex spontaneous turn callback failed",
                phase=phase,
                error_type=type(exc).__name__,
            )

    async def get_context_usage(self) -> dict:
        # Real shape (verified, gpt-5.5): tokenUsage = {last:{totalTokens,…},
        # total:{totalTokens,…}, modelContextWindow}. `last.totalTokens` is the most
        # recent turn's full token count ≈ current context depth (what the codex TUI
        # gauges); `total` is the cumulative session sum (over-counts context). Use
        # `last` for the "context full?" reading, falling back to `total`.
        u = self.last_token_usage if isinstance(self.last_token_usage, dict) else {}
        last = u.get("last") if isinstance(u.get("last"), dict) else {}
        total = u.get("total") if isinstance(u.get("total"), dict) else {}
        used = last.get("totalTokens")
        if used is None:
            used = total.get("totalTokens")
        # server value (captured in _dispatch) wins; else the config-declared window.
        win = self.context_window or u.get("modelContextWindow") or codex_context_window()
        return {"used_tokens": used, "context_window": win, "raw": u}

    async def recover_owned_turn(self, turn_id: str) -> bool:
        """Reattach one durably attributed turn after a wrapper restart.

        Arm the bounded stream before the status reads so a terminal notification
        racing either RPC is captured rather than leaving a false running state.
        Machine has already matched this id against its private lease and rollout
        tail; the bounded latest-turn page supplies the missing native-id proof.
        """
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("invalid Codex turn id")
        if self.turn_active or self._spontaneous_turn_id is not None:
            return (
                self.turn_id == turn_id
                and self._spontaneous_turn_id == turn_id
            )

        self.turn_id = turn_id
        self.turn_active = True
        self.remember_owned_turn_id(turn_id)
        self._spontaneous_turn_id = turn_id
        self._open_spontaneous_stream(turn_id)
        try:
            response = await self._request("thread/read", {
                "threadId": self.thread_id,
                "includeTurns": False,
            })
            thread = (
                response.get("thread")
                if isinstance(response, dict) else None
            )
            raw_status = (
                thread.get("status")
                if isinstance(thread, dict) else None
            )
            if (
                not isinstance(raw_status, dict)
                or raw_status.get("type") not in _THREAD_STATUSES
            ):
                raise RuntimeError(
                    "codex thread/read returned an invalid thread status")
            self.last_thread_status = _copy_thread_status(raw_status)
            active = raw_status.get("type") == "active"
            latest_turn = None
            if (
                active
                and self.turn_id == turn_id
                and self._spontaneous_turn_id == turn_id
            ):
                page = await self._request("thread/turns/list", {
                    "threadId": self.thread_id,
                    "cursor": None,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                })
                turns = page.get("data") if isinstance(page, dict) else None
                if not isinstance(turns, list) or len(turns) != 1:
                    raise RuntimeError(
                        "codex latest-turn read returned an invalid page")
                latest_turn = turns[0]
        except BaseException:
            if self._spontaneous_turn_id == turn_id:
                self._spontaneous_turn_id = None
            self._discard_spontaneous_stream(turn_id)
            if self.turn_id == turn_id:
                self.turn_id = None
            self.turn_active = False
            raise

        # turn/completed may have been dispatched while the read response was
        # waking this coroutine.  Its cleared id overrides a stale active result.
        if (
            active
            and isinstance(latest_turn, dict)
            and latest_turn.get("id") == turn_id
            and latest_turn.get("status") == "inProgress"
            and self.turn_id == turn_id
            and self._spontaneous_turn_id == turn_id
        ):
            await self._publish_turn_lifecycle("started", turn_id)
            return True

        if self._spontaneous_turn_id == turn_id:
            self._spontaneous_turn_id = None
        self._discard_spontaneous_stream(turn_id)
        if self.turn_id == turn_id:
            self.turn_id = None
        self.turn_active = False
        return False

    async def get_status(self) -> dict:
        """Return a sanitized status composed from official app-server RPCs.

        Thread, config and account are read concurrently first. ChatGPT-only
        account statistics are then skipped for explicit API key/Bedrock auth;
        unknown or failed account reads still attempt them for forward
        compatibility. A missing or unsupported account endpoint must not
        suppress thread/config/context state. Raw responses never leave this
        method: every returned field is copied through a small allow-list and
        raw errors become generic labels.
        """
        assert self.thread_id, "connect() first"
        config_params: dict[str, Any] = {"includeLayers": False}
        if self._cwd:
            config_params["cwd"] = self._cwd
        specs = (
            ("thread", "thread/read", {
                "threadId": self.thread_id, "includeTurns": False}),
            ("config", "config/read", config_params),
            ("account", "account/read", {"refreshToken": False}),
        )
        results = await asyncio.gather(*(
            self._request(method, params) if params is not None
            else self._request(method)
            for _component, method, params in specs
        ), return_exceptions=True)

        responses: dict[str, dict] = {}
        errors: list[str] = []
        for (component, _method, _params), result in zip(specs, results):
            if isinstance(result, BaseException):
                errors.append(f"{component}: {_status_error_message(result)}")
            elif isinstance(result, dict):
                responses[component] = result
            else:
                errors.append(f"{component}: app-server returned an invalid response")

        account_response = responses.get("account")
        raw_account_for_auth = (
            account_response.get("account")
            if isinstance(account_response, dict) else None
        )
        account_auth_type = (
            raw_account_for_auth.get("type")
            if isinstance(raw_account_for_auth, dict) else None
        )
        skip_chatgpt_stats = account_auth_type in {"apiKey", "amazonBedrock"}
        if not skip_chatgpt_stats:
            stats_specs = (
                ("rate_limits", "account/rateLimits/read", None),
                ("usage", "account/usage/read", None),
            )
            stats_results = await asyncio.gather(*(
                self._request(method, params) if params is not None
                else self._request(method)
                for _component, method, params in stats_specs
            ), return_exceptions=True)
            for (component, _method, _params), result in zip(
                    stats_specs, stats_results):
                if isinstance(result, BaseException):
                    errors.append(f"{component}: {_status_error_message(result)}")
                elif isinstance(result, dict):
                    responses[component] = result
                else:
                    errors.append(
                        f"{component}: app-server returned an invalid response")

        thread_response = responses.get("thread") or {}
        raw_thread = thread_response.get("thread")
        if not isinstance(raw_thread, dict):
            raw_thread = {}
            _append_status_error(errors, "thread", "app-server returned an invalid response")
        raw_status = raw_thread.get("status")
        if isinstance(raw_status, dict):
            self.last_thread_status = _copy_thread_status(raw_status)
        thread = _sanitize_thread(
            raw_thread,
            fallback_id=self.thread_id,
            fallback_cwd=self._cwd,
            fallback_status=self.last_thread_status,
        )

        config_response = responses.get("config") or {}
        raw_config = config_response.get("config")
        if not isinstance(raw_config, dict):
            raw_config = {}
            _append_status_error(errors, "config", "app-server returned an invalid response")

        context_usage = await self.get_context_usage()
        used_tokens = _nonnegative_int(context_usage.get("used_tokens"))
        max_tokens = _nonnegative_int(context_usage.get("context_window"))
        context = {
            "used_tokens": used_tokens,
            "max_tokens": max_tokens,
            "percentage": (
                used_tokens / max_tokens * 100.0
                if used_tokens is not None and max_tokens else None
            ),
        }

        runtime = {
            "app_server_version": _bounded_string(self.app_server_version, 128),
            "model": _bounded_string(self.model or raw_config.get("model"), 256),
            "model_provider": _bounded_string(
                raw_thread.get("modelProvider") or raw_config.get("model_provider"), 256),
            "reasoning_effort": _bounded_string(
                self.effort or raw_config.get("model_reasoning_effort"), 64),
            "service_tier": _bounded_string(
                self.service_tier, 64),
            "approval_policy": _approval_policy_name(
                self.approval_policy if self.approval_policy
                else raw_config.get("approval_policy")),
            "permission_profile": _bounded_string(
                self.permission_profile, 256),
            "sandbox_mode": _bounded_string(raw_config.get("sandbox_mode"), 64),
            "web_search": _bounded_string(self.web_search, 64),
        }

        account = None
        if account_response is not None:
            raw_account = account_response.get("account")
            if raw_account is not None and not isinstance(raw_account, dict):
                _append_status_error(errors, "account", "app-server returned an invalid response")
            else:
                raw_account = raw_account or {}
                auth_type = raw_account.get("type")
                if auth_type not in {"apiKey", "chatgpt", "amazonBedrock"}:
                    auth_type = "unknown"
                account = {
                    "auth_type": auth_type,
                    "plan_type": _bounded_string(raw_account.get("planType"), 128),
                    "requires_openai_auth": bool(account_response.get("requiresOpenaiAuth")),
                }

        rate_response = responses.get("rate_limits")
        if rate_response is not None:
            if isinstance(rate_response.get("rateLimits"), dict):
                self._remember_rate_limits(rate_response)
            else:
                _append_status_error(
                    errors, "rate_limits", "app-server returned an invalid response")
        rate_limits = [] if skip_chatgpt_stats else _sanitize_rate_limits(
            rate_response if rate_response is not None else {
                "rateLimits": self.last_rate_limits,
                "rateLimitsByLimitId": self.last_rate_limits_by_id,
            })

        usage = None
        usage_response = responses.get("usage")
        if usage_response is not None:
            summary = usage_response.get("summary")
            if not isinstance(summary, dict):
                _append_status_error(errors, "usage", "app-server returned an invalid response")
            else:
                usage = {
                    "lifetime_tokens": _nonnegative_int(summary.get("lifetimeTokens")),
                    "peak_daily_tokens": _nonnegative_int(summary.get("peakDailyTokens")),
                    "current_streak_days": _nonnegative_int(summary.get("currentStreakDays")),
                    "longest_streak_days": _nonnegative_int(summary.get("longestStreakDays")),
                    "longest_running_turn_sec": _nonnegative_int(
                        summary.get("longestRunningTurnSec")),
                }

        return {
            "thread": thread,
            "runtime": runtime,
            "context": context,
            "account": account,
            "rate_limits": rate_limits,
            "usage": usage,
            "component_errors": errors[:5],
        }

    def _remember_rate_limits(self, response: dict) -> None:
        single = response.get("rateLimits")
        self.last_rate_limits = (
            _copy_rate_limit_snapshot(single) if isinstance(single, dict) else None)
        raw_by_id = response.get("rateLimitsByLimitId")
        remembered: dict[str, dict] = {}
        if isinstance(raw_by_id, dict):
            for key, value in list(raw_by_id.items())[:_STATUS_RATE_LIMIT_MAX]:
                if isinstance(key, str) and isinstance(value, dict):
                    remembered[key[:128]] = _copy_rate_limit_snapshot(value)
        self.last_rate_limits_by_id = remembered

    # ---- internals ----
    async def _request(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        response_boundary: Optional[_CodexSteerResponseBoundary] = None,
    ):
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        if response_boundary is not None:
            self._pending_response_boundaries[rid] = response_boundary
        obj = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            obj["params"] = params
        completed = False
        try:
            await self._send(obj)
            result = await asyncio.wait_for(fut, timeout=_REQ_TIMEOUT)
            if method in _HTTP_PROVIDER_PERSISTING_METHODS:
                await self._restore_http_provider_state()
            completed = True
            return result
        finally:
            self._pending.pop(rid, None)
            boundary = self._pending_response_boundaries.pop(rid, None)
            if boundary is not None and not completed:
                boundary.release()

    async def _restore_http_provider_state(
        self,
        *,
        include_descendants: bool = False,
        strict: bool = False,
    ) -> None:
        root_id = self._http_provider_root_id
        if root_id is None:
            return
        include_ids = {
            value for value in (root_id, self.thread_id)
            if isinstance(value, str) and value
        }
        roots = {root_id} if include_descendants else set()
        async with self._http_provider_repair_lock:
            try:
                report = await asyncio.to_thread(
                    repair_http_provider_records,
                    apply=True,
                    roots=roots,
                    include_thread_ids=include_ids,
                )
                restored = await asyncio.to_thread(
                    canonical_thread_provider_is_restored,
                    root_id,
                )
                if strict and not restored:
                    raise RuntimeError(
                        "Codex HTTP compatibility provider was not restored")
                if report.changed_db_thread_ids or report.changed_rollout_thread_ids:
                    log.info(
                        "Codex HTTP provider durable state canonicalized",
                        db_rows=len(report.changed_db_thread_ids),
                        rollout_rows=len(report.changed_rollout_thread_ids),
                    )
                if report.deferred_thread_ids:
                    log.info(
                        "Codex HTTP provider child repair deferred",
                        rows=len(report.deferred_thread_ids),
                    )
            except Exception as exc:
                log.warning(
                    "Codex HTTP provider durable state repair failed",
                    error_type=type(exc).__name__,
                )
                if strict:
                    raise

    def _schedule_http_provider_descendant_repair(self) -> None:
        root_id = self._http_provider_root_id
        if root_id is None:
            return
        # Codex can flush/archive subagent rollouts after the parent terminal.
        # Retry in the background without delaying the visible TurnEnd.  A later
        # disconnect performs one final synchronous pass before clearing root_id.
        live = {
            task for task in self._http_provider_repair_tasks
            if not task.done()
        }
        self._http_provider_repair_tasks = live
        if live:
            return
        for delay in (1.0, 5.0, 20.0):
            task = asyncio.create_task(
                self._delayed_http_provider_descendant_repair(
                    root_id, delay),
            )
            self._http_provider_repair_tasks.add(task)
            task.add_done_callback(self._http_provider_repair_done)

    async def _delayed_http_provider_descendant_repair(
        self,
        root_id: str,
        delay: float,
    ) -> None:
        try:
            await asyncio.wait_for(
                self._http_provider_repair_stop.wait(),
                timeout=delay,
            )
            return
        except asyncio.TimeoutError:
            pass
        if self._http_provider_root_id != root_id:
            return
        await self._restore_http_provider_state(include_descendants=True)

    def _http_provider_repair_done(self, task: asyncio.Task) -> None:
        self._http_provider_repair_tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.warning(
                "delayed Codex HTTP provider repair failed",
                error_type=type(error).__name__,
            )

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        await self._send(obj)

    async def _respond(self, rid, result) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _respond_error(self, rid, code: int, message: str) -> None:
        await self._send({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": code, "message": message},
        })

    async def _approval_decision(
        self, method: str, params: dict,
    ) -> Optional[str]:
        """Return a decision, or stay silent for a shared pending request."""
        if self.approval == "never":
            return "decline"
        if self.approval not in {"on-request", "untrusted"}:
            log.warning("invalid codex approval policy; denying", approval=self.approval)
            return None if self._using_daemon_proxy else "decline"
        callback = self.approval_callback
        if callback is None:
            log.warning("codex approval requested without a client callback", method=method)
            return None if self._using_daemon_proxy else "decline"
        try:
            decision = await asyncio.wait_for(
                callback(method, params), timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("codex approval timed out", method=method)
            return None if self._using_daemon_proxy else "decline"
        except Exception as exc:
            log.warning(
                "codex approval callback failed",
                method=method,
                error_type=type(exc).__name__,
            )
            return None if self._using_daemon_proxy else "decline"
        if decision not in _APPROVAL_DECISIONS:
            log.warning("invalid codex approval decision",
                        method=method, decision=decision)
            return None if self._using_daemon_proxy else "decline"
        return decision

    async def _handle_server_request(self, message: dict) -> None:
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str):
            await self._respond_error(rid, -32600, "invalid server request")
            return
        if method not in _NEW_APPROVAL_METHODS | _LEGACY_APPROVAL_METHODS | _INTERACTION_METHODS:
            log.warning("unsupported codex server request; rejecting", method=method)
            await self._respond_error(rid, -32601, f"unsupported server request: {method}")
            return
        if not isinstance(params, dict):
            await self._respond_error(rid, -32602, f"invalid params for {method}")
            return

        if method in _INTERACTION_METHODS:
            callback = self.interaction_callback
            if callback is None:
                if self._using_daemon_proxy:
                    return
                await self._respond_error(rid, -32000, "remote interaction callback unavailable")
                return
            try:
                result = await asyncio.wait_for(
                    callback(method, params), timeout=_APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                if self._using_daemon_proxy:
                    return
                await self._respond_error(rid, -32001, "remote user input timed out")
                return
            except Exception as exc:
                log.warning(
                    "codex interaction callback failed",
                    method=method,
                    error_type=type(exc).__name__,
                )
                if self._using_daemon_proxy:
                    return
                await self._respond_error(rid, -32000, "remote user input failed")
                return
            await self._respond(rid, result)
            return

        decision = await self._approval_decision(method, params)
        if decision is None:
            return
        if method in _LEGACY_APPROVAL_METHODS:
            decision = _LEGACY_DECISIONS[decision]
        await self._respond(rid, {"decision": decision})

    def _server_request_done(self, task: asyncio.Task) -> None:
        """Observe a detached server-request task and keep the set bounded."""
        self._server_request_tasks.discard(task)
        for request_id, owner in list(self._server_request_tasks_by_id.items()):
            if owner is task:
                self._server_request_tasks_by_id.pop(request_id, None)
                self._pending_server_request_ids.discard(request_id)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.warning(
                "codex server request handler failed",
                error_type=type(error).__name__,
            )

    async def _proxy_read_exact(
        self, proc: asyncio.subprocess.Process, size: int,
    ) -> bytes:
        if proc.stdout is None:
            raise CodexProxyProtocolError("proxy stdout unavailable")
        while len(self._proxy_read_buffer) < size:
            chunk = await proc.stdout.read(size - len(self._proxy_read_buffer))
            if not chunk:
                raise EOFError("Codex daemon proxy closed")
            self._proxy_read_buffer.extend(chunk)
        data = bytes(self._proxy_read_buffer[:size])
        del self._proxy_read_buffer[:size]
        return data

    async def _proxy_read_frame(
        self, proc: asyncio.subprocess.Process,
    ) -> tuple[bool, int, bytes]:
        header = await self._proxy_read_exact(proc, 2)
        first, second = header
        fin = bool(first & 0x80)
        if first & 0x70:
            raise CodexProxyProtocolError("unsupported WebSocket extension")
        opcode = first & 0x0F
        if opcode not in {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}:
            raise CodexProxyProtocolError("invalid WebSocket opcode")
        # Servers never mask frames.  Accepting a masked local stream would hide
        # a direction/protocol mixup and risks feeding arbitrary bytes to JSON.
        if second & 0x80:
            raise CodexProxyProtocolError("masked WebSocket server frame")
        length_marker = second & 0x7F
        if length_marker == 126:
            length = int.from_bytes(
                await self._proxy_read_exact(proc, 2), "big")
            if length <= 125:
                raise CodexProxyProtocolError("non-canonical WebSocket length")
        elif length_marker == 127:
            raw_length = await self._proxy_read_exact(proc, 8)
            if raw_length[0] & 0x80:
                raise CodexProxyProtocolError("invalid WebSocket length")
            length = int.from_bytes(raw_length, "big")
            if length <= 0xFFFF:
                raise CodexProxyProtocolError("non-canonical WebSocket length")
        else:
            length = length_marker
        if length > _PROXY_MESSAGE_MAX:
            raise CodexProxyProtocolError("WebSocket frame exceeds limit")
        if opcode >= 0x8 and (not fin or length > 125):
            raise CodexProxyProtocolError("invalid WebSocket control frame")
        return fin, opcode, await self._proxy_read_exact(proc, length)

    async def _send_proxy_frame(self, payload: bytes, opcode: int) -> None:
        if not self._using_daemon_proxy:
            raise CodexProxyProtocolError("proxy transport is not active")
        frame = _websocket_client_frame(payload, opcode=opcode)
        async with self._send_lock:
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("codex daemon proxy disconnected")
            proc.stdin.write(frame)
            await proc.stdin.drain()

    async def _proxy_read_message(
        self, proc: asyncio.subprocess.Process,
    ) -> Optional[bytes]:
        fragments: Optional[bytearray] = None
        while True:
            fin, opcode, payload = await self._proxy_read_frame(proc)
            if opcode == 0x8:  # close
                if len(payload) == 1:
                    raise CodexProxyProtocolError("invalid WebSocket close frame")
                if len(payload) >= 2:
                    code = int.from_bytes(payload[:2], "big")
                    defined = code in {
                        1000, 1001, 1002, 1003, 1007, 1008,
                        1009, 1010, 1011, 1012, 1013, 1014,
                    }
                    if not defined and not 3000 <= code < 5000:
                        raise CodexProxyProtocolError("invalid WebSocket close code")
                    try:
                        payload[2:].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise CodexProxyProtocolError(
                            "invalid WebSocket close reason") from exc
                if not self._proxy_close_sent:
                    self._proxy_close_sent = True
                    await self._send_proxy_frame(payload, 0x8)
                return None
            if opcode == 0x9:  # ping
                await self._send_proxy_frame(payload, 0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x2:
                raise CodexProxyProtocolError(
                    "binary app-server WebSocket message")
            if opcode == 0x1:
                if fragments is not None:
                    raise CodexProxyProtocolError(
                        "interleaved WebSocket data messages")
                if fin:
                    try:
                        payload.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise CodexProxyProtocolError(
                            "invalid WebSocket text") from exc
                    return payload
                fragments = bytearray(payload)
                continue
            # continuation
            if fragments is None:
                raise CodexProxyProtocolError(
                    "unexpected WebSocket continuation")
            if len(fragments) + len(payload) > _PROXY_MESSAGE_MAX:
                raise CodexProxyProtocolError(
                    "WebSocket message exceeds limit")
            fragments.extend(payload)
            if fin:
                complete = bytes(fragments)
                try:
                    complete.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CodexProxyProtocolError(
                        "invalid fragmented WebSocket text") from exc
                return complete

    async def _send(self, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        if self._using_daemon_proxy:
            await self._send_proxy_frame(payload, 0x1)
            return
        async with self._send_lock:
            proc = self.proc
            assert proc and proc.stdin
            proc.stdin.write(payload + b"\n")
            await proc.stdin.drain()

    async def _read_loop(self, proc: asyncio.subprocess.Process,
                         generation: int) -> None:
        assert proc.stdout
        daemon_proxy = self._using_daemon_proxy
        try:
            while True:
                if daemon_proxy:
                    line = await self._proxy_read_message(proc)
                    if line is None:
                        break
                else:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                try:
                    m = json.loads(line)
                except Exception as exc:
                    if daemon_proxy:
                        raise CodexProxyProtocolError(
                            "invalid app-server JSON message") from exc
                    continue
                if not isinstance(m, dict):
                    if daemon_proxy:
                        raise CodexProxyProtocolError(
                            "non-object app-server WebSocket message")
                    continue
                if generation != self._generation:
                    return
                await self._dispatch(m, raw_size=len(line))
                if self._turn_q is not None or self._spontaneous_q is not None:
                    # StreamReader can satisfy many buffered readline() calls
                    # without yielding. Give the independent bridge consumer one
                    # scheduling opportunity per frame; never wait for its relay
                    # I/O or for queue capacity. Managed bridges are fail-fast too,
                    # so they need the same fairness once their consumer exists.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("codex read loop ended", error=str(e))
            if daemon_proxy and generation == self._generation:
                self.daemon_manager.invalidate()
                await self.disconnect()
        finally:
            # A stale reader belongs to an app-server generation already replaced
            # by disconnect/reconnect.  Leave the new generation alone, but do not
            # `return` from finally: that would suppress cancellation or an active
            # exception from this reader task.
            if generation == self._generation:
                self._dead = True
                if daemon_proxy:
                    self.daemon_manager.invalidate()
                    self._using_daemon_proxy = False
                    self._proxy_read_buffer.clear()
                self._clear_review_tracking()
                spontaneous_turn_id = self._spontaneous_turn_id
                self._spontaneous_turn_id = None
                self._close_spontaneous_stream(spontaneous_turn_id)
                self.turn_active = False
                # The process can no longer receive approval responses.  Release
                # any AskUser callbacks tied to this generation immediately.
                for task in list(self._server_request_tasks):
                    task.cancel()
                # unblock any waiting turn/request
                if self._turn_q is not None:
                    self._force_turn_sentinel(self._turn_q)
                self._managed_overflow = False
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("codex app-server closed"))
                if spontaneous_turn_id is not None:
                    await self._publish_turn_lifecycle(
                        "completed", spontaneous_turn_id)

    def _notification_is_current(self, message: dict) -> bool:
        """Fail closed for app-server notifications owned by another turn.

        One app-server process can report delayed activity for a thread/turn that
        is not the response currently consumed by ``Machine._run_turn``. Filter
        before state updates and queueing so those frames cannot reset the idle
        watchdog, close the active queue, or appear in the wrong conversation.
        """
        method = message.get("method")
        target_thread_id = _notification_thread_id(message)
        binding_thread_id = (
            self._shared_resume_binding_thread_id
            if self._using_daemon_proxy else None
        )
        current_thread_id = binding_thread_id or self.thread_id
        if (method in _MODEL_TURN_METHODS
                and (target_thread_id is None or current_thread_id is None)):
            # These 0.144.1 notifications require both threadId and turnId.
            # Unlike legacy error/hook frames, there is no valid thread-scoped
            # form, so a partial payload must never be guessed into this session.
            log.warning("unattributed codex model notification dropped",
                        method=method)
            return False
        if (self._using_daemon_proxy
                and (_is_turn_notification(method)
                     or method == "thread/compacted")
                and target_thread_id is None):
            # Private one-session app-servers historically omit threadId on
            # legitimate spontaneous turns. A shared daemon has no such safe
            # inference at any point in its connection lifetime: after resume
            # binds, another subscribed thread can still emit an unattributed
            # lifecycle frame.
            log.warning(
                "unattributed shared Codex notification dropped",
                method=method,
            )
            return False
        if (target_thread_id is not None and current_thread_id is not None
                and target_thread_id != current_thread_id):
            log.warning("foreign codex thread notification dropped", method=method)
            return False
        target_turn_id = _notification_turn_id(message)
        if (not _is_turn_notification(method)
                and method not in {"error", "thread/compacted"}):
            return True

        if self._review_active and target_turn_id is not None:
            # ``review/start`` owns an outer visible turn plus one nested reviewer
            # execution turn.  The nested turn is authoritative for interrupt and
            # rollout attribution, while outer items/completion remain the public
            # managed stream. There is exactly one nested executor; a third id is
            # foreign and must not be allowed to hijack cancellation.
            if method == "turn/started":
                if target_turn_id != self._review_outer_turn_id:
                    if (self._review_execution_turn_id is not None
                            and self._review_execution_turn_id != target_turn_id):
                        log.warning(
                            "foreign codex review execution notification dropped",
                            method=method,
                        )
                        return False
                    self._review_execution_turn_id = target_turn_id
                    self.remember_owned_turn_id(target_turn_id)
                    self._review_execution_ready.set()
                return True
            if target_turn_id in {
                self._review_outer_turn_id,
                self._review_execution_turn_id,
                self.turn_id,
            }:
                return True

        if method == "turn/started":
            if target_turn_id is None:
                log.warning("unattributed codex turn notification dropped",
                            method=method)
                return False
            if (self.turn_active and self.turn_id is not None
                    and self.turn_id != target_turn_id):
                log.warning("foreign codex turn notification dropped",
                            method=method)
                return False
            self.turn_id = target_turn_id
            self.remember_owned_turn_id(target_turn_id)
            return True

        if target_turn_id is not None:
            if self.turn_id is None:
                if not (self.turn_active or self.turn_start_pending):
                    log.warning("orphan codex turn notification dropped",
                                method=method)
                    return False
                # app-server may deliver item/completed before the turn/start RPC
                # response is scheduled. The first attributed frame claims it.
                self.turn_id = target_turn_id
                self.remember_owned_turn_id(target_turn_id)
            if target_turn_id != self.turn_id:
                log.warning("foreign codex turn notification dropped",
                            method=method)
                return False
            return True

        # Thread-scoped hooks legitimately use turnId=null. All other item/turn
        # notifications in the v2 schema are attributable; dropping a malformed
        # one is safer than guessing which active reply owns it.
        if method == "error":
            if self._using_daemon_proxy:
                # Shared app-server provider errors currently carry neither a
                # thread nor a turn id.  The daemon can emit one while another
                # subscribed thread is active; accepting it merely because this
                # handle also has a live turn renders a foreign retry/failure in
                # the wrong conversation.  Terminal turn/completed remains the
                # authoritative, attributable result for this handle.
                log.warning(
                    "unattributed shared Codex provider error dropped",
                    active_thread_id=self.thread_id,
                    active_turn_id=self.turn_id,
                )
                return False
            return self.turn_active
        if isinstance(method, str) and method.startswith("hook/"):
            return self.turn_active
        log.warning("unattributed codex turn notification dropped", method=method)
        return False

    def _install_steer_response_boundary(
        self,
        boundary: _CodexSteerResponseBoundary,
        response: dict,
    ) -> None:
        """Insert the steer fence at the exact JSON-RPC response boundary."""
        if "error" in response or boundary.fence is not None:
            return
        result = response.get("result")
        returned_turn_id = (
            result.get("turnId") if isinstance(result, dict) else None
        )
        if returned_turn_id != boundary.turn_id:
            return
        if (
            self.thread_id != boundary.thread_id
            or self.turn_id != boundary.turn_id
            or not self.turn_active
            or boundary.turn_id not in self._owned_turn_ids
        ):
            return
        queue = (
            self._turn_q
            if isinstance(self._turn_q, _SpontaneousNotificationQueue)
            else self._spontaneous_q
            if (
                self._spontaneous_queue_turn_id == boundary.turn_id
                and isinstance(
                    self._spontaneous_q, _SpontaneousNotificationQueue
                )
            )
            else None
        )
        if queue is None or queue.end_delivered:
            return
        fence = CodexSteerFence()
        queue.put_control_nowait(fence)
        boundary.fence = fence

    async def _dispatch(self, m: dict, raw_size: Optional[int] = None) -> None:
        has_id = "id" in m
        has_method = "method" in m
        if has_id and not has_method:                       # response to our request
            fut = self._pending.get(m["id"])
            if fut and not fut.done():
                boundary = self._pending_response_boundaries.get(m["id"])
                if boundary is not None:
                    self._install_steer_response_boundary(boundary, m)
                if "error" in m:
                    fut.set_exception(CodexAppServerError(m["error"]))
                else:
                    fut.set_result(m.get("result"))
            return
        if has_id and has_method:                            # server -> client request
            method = m.get("method")
            request_id = _server_request_key(m.get("id"))
            missing_callback = isinstance(method, str) and ((
                method in _NEW_APPROVAL_METHODS | _LEGACY_APPROVAL_METHODS
                and self.approval != "never"
                and self.approval_callback is None
            ) or (
                method in _INTERACTION_METHODS
                and self.interaction_callback is None
            ))
            if self._using_daemon_proxy and missing_callback:
                # A shared app-server can deliver the same prompt to another
                # subscribed client.  Silence means "still pending"; replying
                # decline/error here would win the race and reject it globally.
                if (request_id is not None
                        and len(self._pending_server_request_ids)
                        < _MAX_SERVER_REQUEST_TASKS):
                    self._pending_server_request_ids.add(request_id)
                log.warning(
                    "Codex shared server request left pending without callback",
                    method=method,
                )
                return
            if len(self._server_request_tasks) >= _MAX_SERVER_REQUEST_TASKS:
                rid = m.get("id")
                supported = isinstance(method, str) and method in (
                    _NEW_APPROVAL_METHODS
                    | _LEGACY_APPROVAL_METHODS
                    | _INTERACTION_METHODS
                )
                if self._using_daemon_proxy and supported:
                    # Do not let this saturated connection reject a prompt which
                    # another subscribed official client can still resolve.
                    log.warning(
                        "Codex shared server request cap reached; left pending",
                        method=method,
                    )
                    return
                log.warning(
                    "codex server request cap reached; rejecting", method=method)
                if method in _NEW_APPROVAL_METHODS:
                    await self._respond(rid, {"decision": "decline"})
                elif method in _LEGACY_APPROVAL_METHODS:
                    await self._respond(rid, {"decision": "denied"})
                else:
                    await self._respond_error(
                        rid, -32000, "too many pending server requests")
                return
            task = asyncio.create_task(self._handle_server_request(m))
            self._server_request_tasks.add(task)
            if request_id is not None:
                self._server_request_tasks_by_id[request_id] = task
                self._pending_server_request_ids.add(request_id)
            task.add_done_callback(self._server_request_done)
            return
        # notification
        method = m.get("method")
        if method == "serverRequest/resolved":
            params = m.get("params")
            request_id = _server_request_key(
                params.get("requestId") if isinstance(params, dict) else None)
            if request_id is not None:
                self._pending_server_request_ids.discard(request_id)
                task = self._server_request_tasks_by_id.pop(request_id, None)
                if task is not None and not task.done():
                    task.cancel()
        if not self._notification_is_current(m):
            return
        if method == "error":
            diagnostic = _provider_error_diagnostic(m.get("params"))
            logger_method = log.info if diagnostic["will_retry"] else log.warning
            logger_method("codex provider error", **diagnostic)
        target_turn_id = _notification_turn_id(m)
        review_execution_frame = bool(
            self._review_active
            and target_turn_id is not None
            and target_turn_id == self._review_execution_turn_id
            and target_turn_id != self._review_outer_turn_id
        )
        # Some app-server revisions may report a nested terminal before the
        # outer Review finishes its exitedReviewMode/agentMessage lifecycle.  It
        # is not the managed turn's terminal and must not close the queue.
        if method == "turn/completed" and review_execution_frame:
            self._review_execution_turn_id = None
            return
        completed_spontaneous_turn_id: Optional[str] = None
        if method == "thread/started" and not self.thread_id:
            self.thread_id = _thread_id_of_notif(m)
        elif method in _NOTICE_METHODS:
            notice = _notice_from_notification(
                method, m.get("params"), self.thread_id)
            if notice is not None:
                await self._publish_runtime_event(notice)
        elif method == "turn/started":
            # Automatic continuations can start without a new local turn/start
            # response.  Remember the authoritative notification id so delayed
            # rollout flushes are still attributed to this app-server.
            was_active = self.turn_active
            turn = (m.get("params") or {}).get("turn") or {}
            turn_id = turn.get("id")
            if (isinstance(turn_id, str) and turn_id
                    and not review_execution_frame):
                self.turn_id = turn_id
                self.remember_owned_turn_id(turn_id)
            self.turn_active = True
            if (not was_active and isinstance(turn_id, str) and turn_id
                    and not review_execution_frame):
                # A goal loop/automatic continuation is not backed by query().
                # The previous managed turn's receive_response() may still hold
                # its local queue after consuming completed+sentinel. Detach the
                # handle reference so this new turn cannot fill that orphaned
                # bounded queue and deadlock the sole stdout reader.
                self._turn_q = None
                self._spontaneous_turn_id = turn_id
                self._open_spontaneous_stream(turn_id)
                # The machine callback only claims state and schedules its raw
                # consumer; it deliberately performs no relay I/O on this reader.
                await self._publish_turn_lifecycle("started", turn_id)
        elif method == "thread/tokenUsage/updated":
            tu = (m.get("params") or {}).get("tokenUsage")
            if isinstance(tu, dict):
                self.last_token_usage = tu
                # modelContextWindow is the SERVER-authoritative window (e.g. 258400);
                # keep the last non-null value so get_context_usage always has it.
                mcw = tu.get("modelContextWindow")
                if mcw:
                    self.context_window = mcw
        elif method == "thread/settings/updated":
            params = m.get("params") or {}
            settings = params.get("threadSettings")
            if (params.get("threadId") == self.thread_id
                    and isinstance(settings, dict)):
                self._apply_thread_settings(settings)
                self._thread_settings_updated.set()
        elif method == "thread/status/changed":
            params = m.get("params") or {}
            status = params.get("status")
            if params.get("threadId") == self.thread_id and isinstance(status, dict):
                self.last_thread_status = _copy_thread_status(status)
        elif method == "account/rateLimits/updated":
            snapshot = (m.get("params") or {}).get("rateLimits")
            if isinstance(snapshot, dict):
                update = _copy_rate_limit_snapshot(snapshot)
                limit_id = update.get("limitId")
                merged = update
                if isinstance(limit_id, str) and limit_id:
                    previous = self.last_rate_limits_by_id.get(limit_id, {})
                    merged = _merge_rate_limit_snapshot(previous, update)
                    self.last_rate_limits_by_id[limit_id] = merged
                    while len(self.last_rate_limits_by_id) > _STATUS_RATE_LIMIT_MAX:
                        self.last_rate_limits_by_id.pop(next(iter(self.last_rate_limits_by_id)))
                current_id = (
                    self.last_rate_limits.get("limitId")
                    if isinstance(self.last_rate_limits, dict) else None)
                if self.last_rate_limits is None or not limit_id or current_id == limit_id:
                    self.last_rate_limits = _merge_rate_limit_snapshot(
                        self.last_rate_limits or {}, update)
                    merged = self.last_rate_limits
                event = _rate_limit_update_from_snapshot(merged)
                if event is not None:
                    await self._publish_runtime_event(event)
                    reached = _rate_limit_notice(event, self.thread_id)
                    if reached is not None:
                        await self._publish_runtime_event(reached)
        elif method == "thread/goal/updated":
            params = m.get("params") or {}
            if params.get("threadId") == self.thread_id:
                try:
                    goal = _sanitize_thread_goal(params.get("goal"), self.thread_id)
                except Exception as exc:
                    log.warning(
                        "invalid codex goal notification dropped",
                        error_type=type(exc).__name__,
                    )
                else:
                    self.last_goal = goal
                    self.goal_revision += 1
                    turn_id = params.get("turnId")
                    self.last_goal_turn_id = (
                        turn_id if isinstance(turn_id, str) and turn_id else None
                    )
                    await self._publish_goal(goal)
        elif method == "thread/goal/cleared":
            params = m.get("params") or {}
            if params.get("threadId") == self.thread_id:
                self.last_goal = None
                self.goal_revision += 1
                self.last_goal_turn_id = None
                await self._publish_goal(None)
        if method == "turn/completed":
            self.turn_active = False
            turn = (m.get("params") or {}).get("turn") or {}
            completed_turn_id = turn.get("id")
            spontaneous_turn_id = self._spontaneous_turn_id
            if (
                spontaneous_turn_id is not None
                and (not completed_turn_id
                     or completed_turn_id == spontaneous_turn_id)
            ):
                completed_spontaneous_turn_id = spontaneous_turn_id
        if method in _HTTP_PROVIDER_PERSISTING_NOTIFICATIONS:
            await self._restore_http_provider_state()
        if self._turn_q is not None and _is_turn_queue_notification(method):
            queue = self._turn_q
            if not self._queue_managed_notification(m, raw_size):
                # Compatibility for narrow unit tests which inject an
                # asyncio.Queue directly instead of opening the live bridge.
                await queue.put(m)
                if method == "turn/completed":
                    await queue.put(None)
        elif (_is_turn_queue_notification(method)
              and self._spontaneous_turn_id is not None):
            self._queue_spontaneous_notification(m, raw_size)
        if completed_spontaneous_turn_id is not None:
            self._spontaneous_turn_id = None
            await self._publish_turn_lifecycle(
                "completed", completed_spontaneous_turn_id)
        if method == "turn/completed":
            completed_turn_id = _notification_turn_id(m)
            if completed_turn_id == self.turn_id:
                self.turn_id = None
            if (self._review_active
                    and completed_turn_id == self._review_outer_turn_id):
                self._clear_review_tracking()

    @staticmethod
    def _force_turn_sentinel(queue: Any) -> None:
        """Wake a consumer during disconnect even when the bounded queue is full."""
        if isinstance(queue, _SpontaneousNotificationQueue):
            # The reserved end slot never competes with live frames. An existing
            # authoritative terminal wins over EOF; otherwise one sentinel wakes
            # the consumer after its retained live tail drains.
            if queue.has_turn_completed():
                return
            queue.put_end_nowait(None)
            return
        try:
            offered = queue.put_nowait(None)
            if offered is False:
                clear = getattr(queue, "clear", None)
                if clear is not None:
                    clear()
                    queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _drain_stderr(self, proc: asyncio.subprocess.Process,
                            generation: int) -> None:
        try:
            while generation == self._generation and proc.stderr:
                line = await proc.stderr.readline()
                if not line:
                    break
                # Stderr can contain one frame-sized line. Its content is already
                # intentionally suppressed by the formatter, so avoid a redundant
                # multi-megabyte decode/copy just to redact it again.
                log.debug("codex stderr", bytes=len(line))
        except asyncio.CancelledError:
            pass


def _to_input(prompt, images=None) -> list:
    """Build codex turn input: a text item + one localImage item per attached
    image path (verified codex reads localImage). `images` is a list of /tmp paths."""
    out = [{"type": "text", "text": prompt if isinstance(prompt, str) else str(prompt)}]
    for path in (images or []):
        out.append({"type": "localImage", "path": path})
    return out


def _thread_id_of(res) -> Optional[str]:
    if isinstance(res, dict):
        th = res.get("thread")
        if isinstance(th, dict):
            return th.get("id") or th.get("sessionId")
        return res.get("threadId") or res.get("thread_id")
    return None


def _thread_id_of_notif(m: dict) -> Optional[str]:
    th = (m.get("params") or {}).get("thread")
    if isinstance(th, dict):
        return th.get("id") or th.get("sessionId")
    return None


_STATUS_WIRE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})
_THREAD_ACTIVE_FLAGS = frozenset({"waitingOnApproval", "waitingOnUserInput"})
_SESSION_SOURCES = frozenset({"cli", "vscode", "exec", "appServer", "unknown"})
_RATE_SCALAR_KEYS = (
    "limitId", "limitName", "planType", "rateLimitReachedType",
)
_RATE_WINDOW_KEYS = ("usedPercent", "resetsAt", "windowDurationMins")
_NOTICE_METHODS = frozenset({
    "warning",
    "guardianWarning",
    "configWarning",
    "deprecationNotice",
    "windows/worldWritableWarning",
})


def _sanitize_thread_goal(raw: Any, fallback_thread_id: Optional[str]) -> dict:
    """Copy the stable public goal contract and reject every unknown field.

    The app-server's experimental response may grow over time.  Constructing a
    fresh payload here (instead of returning ``raw``) ensures new fields never
    cross the remote boundary accidentally; ``ThreadGoal`` then enforces types,
    bounds, and the shared Claude/Codex contract.
    """
    if not isinstance(raw, dict):
        raise ValueError("goal must be an object")
    raw_thread_id = raw.get("threadId")
    thread_id = raw_thread_id if raw_thread_id is not None else fallback_thread_id
    if (not isinstance(thread_id, str)
            or not _STATUS_WIRE_ID.fullmatch(thread_id)
            or (fallback_thread_id is not None and thread_id != fallback_thread_id)):
        raise ValueError("goal thread id does not match the active thread")

    payload: dict[str, Any] = {
        "threadId": thread_id,
        "objective": raw.get("objective"),
        "status": raw.get("status"),
        "engine": "codex",
        "tokensUsed": raw.get("tokensUsed"),
        "timeUsedSeconds": raw.get("timeUsedSeconds"),
    }
    for key in ("tokenBudget", "createdAt", "updatedAt"):
        if key in raw:
            payload[key] = raw[key]
    return ThreadGoal.model_validate(payload).model_dump()


def _app_server_version(initialized: Any) -> Optional[str]:
    """Extract only the semantic version, never the full user-agent/codexHome."""
    if not isinstance(initialized, dict):
        return None
    user_agent = initialized.get("userAgent")
    if not isinstance(user_agent, str):
        return None
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)", user_agent)
    return match.group(1)[:128] if match else None


def _bounded_string(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _safe_notice_thread_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and _STATUS_WIRE_ID.fullmatch(value):
        return value
    return None


def _stable_notice_id(method: str, payload: dict[str, Any]) -> str:
    # Routing metadata may be unknown during initialize and appear on the same
    # warning later. Keep identity tied to user-visible content so that timing
    # difference cannot create a duplicate bar after thread/start.
    identity_payload = {
        key: value for key, value in payload.items() if key != "thread_id"
    }
    canonical = json.dumps(
        {"method": method, **identity_payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"codex-notice-{hashlib.sha256(canonical).hexdigest()[:24]}"


def _notice_from_notification(
    method: Any, params: Any, fallback_thread_id: Optional[str],
) -> Optional[Notice]:
    """Convert only the five official user-notice notifications.

    Each branch constructs a fresh allow-listed payload.  In particular,
    configWarning.details/range never leave this function, and the Windows
    warning samples only three bounded paths.
    """
    if method not in _NOTICE_METHODS or not isinstance(params, dict):
        return None
    raw_thread_id = params.get("threadId")
    thread_id = _safe_notice_thread_id(
        fallback_thread_id if raw_thread_id is None else raw_thread_id)
    severity = "warning"
    detail: Optional[str] = None

    if method == "warning":
        category = "runtime"
        title = "Codex 运行警告"
        message = _bounded_string(params.get("message"), _NOTICE_MESSAGE_MAX)
    elif method == "guardianWarning":
        category = "guardian"
        title = "Codex 安全守护提示"
        message = _bounded_string(params.get("message"), _NOTICE_MESSAGE_MAX)
    elif method == "configWarning":
        category = "config"
        title = "Codex 配置警告"
        message = _bounded_string(params.get("summary"), _NOTICE_MESSAGE_MAX)
        # Deliberately ignore details and range.  Only the bounded path joins the
        # public summary, as required by the remote privacy boundary.
        detail = _bounded_string(params.get("path"), _NOTICE_PATH_MAX)
    elif method == "deprecationNotice":
        category = "deprecation"
        severity = "info"
        title = "Codex 兼容性提示"
        message = _bounded_string(params.get("summary"), _NOTICE_MESSAGE_MAX)
        detail = _bounded_string(params.get("details"), _NOTICE_DETAIL_MAX)
    else:
        category = "security"
        title = "Codex 目录权限警告"
        samples = params.get("samplePaths")
        safe_paths: list[str] = []
        if isinstance(samples, list):
            for path in samples[:_NOTICE_PATH_SAMPLE_MAX]:
                safe_path = _bounded_string(path, _NOTICE_PATH_MAX)
                if safe_path is not None:
                    safe_paths.append(safe_path)
        extra = _nonnegative_int(params.get("extraCount")) or 0
        failed = params.get("failedScan") is True
        total = len(safe_paths) + extra
        if failed:
            message = "Codex 未能完整扫描可被所有用户写入的目录"
        elif total:
            message = f"Codex 检测到 {total} 个可被所有用户写入的目录"
        else:
            message = "Codex 检测到可被所有用户写入的目录"
        if safe_paths:
            detail = "\n".join(safe_paths)[:_NOTICE_DETAIL_MAX]

    if message is None:
        return None
    public = {
        "severity": severity,
        "category": category,
        "title": title,
        "message": message,
        "detail": detail,
        "thread_id": thread_id,
    }
    return Notice(
        notice_id=_stable_notice_id(str(method), public),
        **public,
    )


def _rate_limit_update_from_snapshot(snapshot: Any) -> Optional[RateLimitUpdate]:
    if not isinstance(snapshot, dict):
        return None
    sanitized = _sanitize_rate_limits({"rateLimits": snapshot})
    if not sanitized:
        return None
    limit = sanitized[0]
    payload = {
        "limit_id": limit.get("limit_id"),
        "name": limit.get("limit_name"),
        "plan_type": limit.get("plan_type"),
        "reached_type": limit.get("rate_limit_reached_type"),
        "primary": limit.get("primary"),
        "secondary": limit.get("secondary"),
    }
    if all(value is None for value in payload.values()):
        return None
    return RateLimitUpdate(**payload)


def _rate_limit_notice(
    update: RateLimitUpdate, thread_id: Optional[str],
) -> Optional[Notice]:
    if not update.reached_type:
        return None
    name = update.name or update.limit_id or "Codex"
    message = _bounded_string(f"{name} 已达到使用限额", _NOTICE_MESSAGE_MAX)
    assert message is not None
    public = {
        "severity": "warning",
        "category": "rate_limit",
        "title": "Codex 使用限额已达到",
        "message": message,
        "detail": update.reached_type,
        "thread_id": _safe_notice_thread_id(thread_id),
    }
    return Notice(
        notice_id=_stable_notice_id("rate_limit", public),
        **public,
    )


def _runtime_event_key(event: RuntimeEvent) -> str:
    if isinstance(event, Notice):
        return f"notice:{event.notice_id}"
    payload = event.model_dump(exclude={
        "v", "ts", "sid", "seq", "to", "route_id",
    })
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return f"rate:{hashlib.sha256(canonical).hexdigest()[:32]}"


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _status_error_message(error: BaseException) -> str:
    """Map arbitrary provider/app-server errors to a bounded non-sensitive label."""
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "app-server request timed out"
    text = str(error)[:1024].lower()
    if "-32601" in text or "method not found" in text or "unsupported" in text:
        return "unsupported by this Codex app-server"
    if (
        "chatgpt authentication required" in text
        or any(token in text for token in ("401", "403", "unauthorized", "not logged"))
    ):
        return "unavailable for the current account"
    return "app-server request failed"


def _provider_error_diagnostic(params: Any) -> dict[str, Any]:
    """Extract non-sensitive provider failure fields for operator logs.

    The full app-server error can contain account, endpoint or connector data.
    Keep only retry state, an HTTP status and a coarse failure class so future
    upstream incidents are attributable without copying provider text to disk.
    """
    public = params if isinstance(params, dict) else {}
    error = public.get("error") if isinstance(public.get("error"), dict) else {}
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    details = (error.get("additionalDetails")
               if isinstance(error.get("additionalDetails"), str) else "")
    combined = (message + " " + details)[:8192].lower()
    status_match = re.search(r"\b([45]\d\d)\b", combined)
    status = int(status_match.group(1)) if status_match else None
    info = error.get("codexErrorInfo")
    if isinstance(info, dict):
        disconnected = info.get("responseStreamDisconnected")
        if isinstance(disconnected, dict):
            candidate = disconnected.get("httpStatusCode")
            if isinstance(candidate, int) and 400 <= candidate <= 599:
                status = candidate
    if status in {401, 403}:
        category = "authentication"
    elif status == 429:
        category = "rate_limit"
    elif isinstance(status, int) and status >= 500:
        category = "upstream_server"
    elif "timeout" in combined or "timed out" in combined:
        category = "timeout"
    elif any(token in combined for token in (
        "connect", "network", "dns", "tls", "stream disconnected",
    )):
        category = "connection"
    else:
        category = "provider"
    return {
        "will_retry": bool(public.get("willRetry")),
        "category": category,
        "http_status": status,
    }


def _append_status_error(errors: list[str], component: str,
                         message: str) -> None:
    prefix = f"{component}:"
    if not any(item.startswith(prefix) for item in errors):
        errors.append(f"{component}: {message}")


def _copy_thread_status(status: dict) -> dict:
    status_type = status.get("type")
    if status_type not in _THREAD_STATUSES:
        return {"type": "unknown", "activeFlags": []}
    flags = status.get("activeFlags") if status_type == "active" else []
    return {
        "type": status_type,
        "activeFlags": [
            flag for flag in (flags if isinstance(flags, list) else [])
            if flag in _THREAD_ACTIVE_FLAGS
        ][:8],
    }


def _sanitize_thread(raw: dict, *, fallback_id: str,
                     fallback_cwd: Optional[str], fallback_status: Optional[dict]) -> dict:
    raw_id = raw.get("id")
    thread_id = raw_id if (
        isinstance(raw_id, str) and _STATUS_WIRE_ID.fullmatch(raw_id)
    ) else fallback_id
    raw_session_id = raw.get("sessionId")
    session_id = raw_session_id if (
        isinstance(raw_session_id, str) and _STATUS_WIRE_ID.fullmatch(raw_session_id)
    ) else None
    raw_status = raw.get("status")
    status = _copy_thread_status(raw_status) if isinstance(raw_status, dict) else (
        fallback_status or {"type": "unknown", "activeFlags": []})

    source = raw.get("source")
    if isinstance(source, str):
        source = source if source in _SESSION_SOURCES else "unknown"
    elif isinstance(source, dict) and "custom" in source:
        source = "custom"
    elif isinstance(source, dict) and "subAgent" in source:
        source = "subAgent"
    else:
        source = None

    cwd = _bounded_string(raw.get("cwd") or fallback_cwd, 4096)
    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "cwd": cwd,
        "source": source,
        "cli_version": _bounded_string(raw.get("cliVersion"), 128),
        "status": status.get("type", "unknown"),
        "active_flags": status.get("activeFlags", []),
        "ephemeral": raw.get("ephemeral") if isinstance(raw.get("ephemeral"), bool) else None,
        "created_at": _nonnegative_int(raw.get("createdAt")),
        "updated_at": _nonnegative_int(raw.get("updatedAt")),
    }


def _approval_policy_name(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return _bounded_string(value, 64)
    if isinstance(value, dict) and isinstance(value.get("granular"), dict):
        return "granular"
    return None


def _copy_granular_approval(value: Any) -> Optional[dict[str, Any]]:
    """Copy the bounded 0.144.1 granular approval shape, or reject it."""
    if not isinstance(value, dict) or not isinstance(value.get("granular"), dict):
        return None
    raw = value["granular"]
    required = ("mcp_elicitations", "rules", "sandbox_approval")
    if any(not isinstance(raw.get(key), bool) for key in required):
        return None
    copied = {key: raw[key] for key in required}
    for key in ("request_permissions", "skill_approval"):
        if isinstance(raw.get(key), bool):
            copied[key] = raw[key]
    return {"granular": copied}


def _copy_rate_limit_snapshot(snapshot: dict) -> dict:
    """Copy only display-safe rate fields used by StatusReport."""
    copied: dict[str, Any] = {}
    for key in _RATE_SCALAR_KEYS:
        value = snapshot.get(key)
        if isinstance(value, str):
            copied[key] = value[:256]
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        safe_window: dict[str, int] = {}
        for window_key in _RATE_WINDOW_KEYS:
            value = _nonnegative_int(window.get(window_key))
            if value is not None:
                safe_window[window_key] = value
        if safe_window:
            copied[key] = safe_window
    return copied


def _merge_rate_limit_snapshot(previous: dict, update: dict) -> dict:
    """Merge a sparse notification without treating null as field deletion."""
    merged = _copy_rate_limit_snapshot(previous)
    for key, value in _copy_rate_limit_snapshot(update).items():
        if key in {"primary", "secondary"} and isinstance(value, dict):
            window = merged.get(key)
            merged[key] = {
                **(window if isinstance(window, dict) else {}),
                **value,
            }
        elif value is not None:
            merged[key] = value
    return merged


def _sanitize_rate_window(window: Any) -> Optional[dict]:
    if not isinstance(window, dict):
        return None
    out = {
        "used_percent": _nonnegative_int(window.get("usedPercent")),
        "resets_at": _nonnegative_int(window.get("resetsAt")),
        "window_duration_mins": _nonnegative_int(window.get("windowDurationMins")),
    }
    return out if any(value is not None for value in out.values()) else None


def _sanitize_rate_limits(response: dict) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    raw_by_id = response.get("rateLimitsByLimitId")
    if isinstance(raw_by_id, dict):
        for map_id, snapshot in list(raw_by_id.items())[:_STATUS_RATE_LIMIT_MAX]:
            if not isinstance(snapshot, dict):
                continue
            copied = _copy_rate_limit_snapshot(snapshot)
            if not copied.get("limitId") and isinstance(map_id, str):
                copied["limitId"] = map_id[:128]
            limit_id = copied.get("limitId")
            if isinstance(limit_id, str):
                seen.add(limit_id)
            entries.append(copied)
    single = response.get("rateLimits")
    if isinstance(single, dict):
        copied = _copy_rate_limit_snapshot(single)
        limit_id = copied.get("limitId")
        if not isinstance(limit_id, str) or limit_id not in seen:
            entries.append(copied)

    out: list[dict] = []
    for snapshot in entries[:_STATUS_RATE_LIMIT_MAX]:
        out.append({
            "limit_id": _bounded_string(snapshot.get("limitId"), 128),
            "limit_name": _bounded_string(snapshot.get("limitName"), 256),
            "plan_type": _bounded_string(snapshot.get("planType"), 128),
            "rate_limit_reached_type": _bounded_string(
                snapshot.get("rateLimitReachedType"), 128),
            "primary": _sanitize_rate_window(snapshot.get("primary")),
            "secondary": _sanitize_rate_window(snapshot.get("secondary")),
        })
    return out
