"""Wire protocol between client <-> relay <-> wrapper.

One JSON object per WebSocket text frame. Common envelope fields on every
message: `v` (protocol version), `type`, `ts`, optional `sid` (cc session id
once known), optional `seq` (monotonic per-session int assigned by the wrapper
to every downstream event; absent on command messages). Auth is NOT in the
envelope: wrappers use a Bearer header and browser clients use an HttpOnly
cookie at WS upgrade; neither credential is logged.

Discriminated by `type`. `extra="forbid"` so unknown fields fail fast.
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, Literal, Optional, Union

from typing_extensions import NotRequired, TypedDict

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError,
    PrivateAttr, model_validator,
)

from cc_remote.attachments import (
    MAX_ATTACHMENT_COUNT,
    MAX_FILENAME_BYTES,
    MAX_SINGLE_ATTACHMENT_BYTES,
)

PROTOCOL_VERSION = 61

# Codex Desktop renders a 53-week daily token-activity calendar. Keep the wire
# payload to that same bounded window so an account response can never turn a
# one-shot status frame into an unbounded relay/browser allocation.
MAX_STATUS_USAGE_BUCKETS = 53 * 7
MAX_STATUS_RESET_CREDITS = 32
MAX_SAFE_WIRE_INTEGER = 9_007_199_254_740_991
MAX_SAFE_WIRE_TIMESTAMP_SECONDS = MAX_SAFE_WIRE_INTEGER // 1000
MAX_BACKGROUND_PROCESS_ITEMS = 64
MAX_BACKGROUND_PROCESS_SUMMARY_CHARS = 8 * 1024
MAX_BACKGROUND_PROCESS_COMMAND_CHARS = 16 * 1024
MAX_BACKGROUND_PROCESS_CWD_CHARS = 4 * 1024

State = Literal["idle", "running", "interrupting", "draining"]
Engine = Literal["claude", "codex", "dsh"]
Space = Literal["code", "work"]
RestoreMode = Literal["conversation", "files", "both"]
RestoreOutcome = Literal["succeeded", "failed", "skipped"]
AssistantChannel = Literal["unknown", "thinking", "commentary", "final"]
ToolCategory = Literal[
    "tool", "command", "file", "mcp", "agent", "server_tool", "web_search",
]
ProcessKind = Literal[
    "reasoning", "plan", "command", "file_change", "mcp", "agent", "hook",
    "server_tool", "web_search", "task", "terminal", "model", "safety",
    "diff", "compaction",
]
ProcessPhase = Literal["start", "update", "end", "snapshot"]
ProcessStatus = Literal[
    "pending", "running", "succeeded", "failed", "declined", "cancelled",
    "interrupted", "unknown",
]
ProcessAppendTarget = Literal["summary", "detail", "output", "diff", "progress"]
CodexThreadStatus = Literal["notLoaded", "idle", "systemError", "active"]
EffortLevel = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
]
AutoCompactMode = Literal["inherit", "auto", "custom"]
AutoCompactPhase = Literal[
    "stable", "waiting_terminal", "compacting", "reconnecting", "blocked",
]
PermissionMode = Literal[
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
    "never", "on-request", "untrusted",
]
CollaborationModeName = Literal["default", "plan"]
WebSearchMode = Literal["cached", "live"]
ControlMode = Literal[
    "remote", "codex_shared", "claude_broker", "external_cli",
    "agent_view", "desktop",
]
WriteState = Literal[
    "writable", "read_only", "takeover_pending", "input_busy",
]
ModelName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
PermissionProfileId = Annotated[
    str, StringConstraints(min_length=1, max_length=256),
]
WireId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
ResetCreditId = Annotated[
    str, StringConstraints(min_length=1, max_length=512),
]
RateLimitResetOutcome = Literal[
    "reset", "nothingToReset", "noCredit", "alreadyRedeemed", "unknown",
]

MAX_ENCODED_ATTACHMENT_CHARS = ((MAX_SINGLE_ATTACHMENT_BYTES + 2) // 3) * 4
MAX_QUERY_QUEUE_ITEMS = 32
MAX_QUERY_QUEUE_BYTES = 64 * 1024 * 1024
MAX_QUERY_QUEUE_PREVIEW_CHARS = 512
MIN_AUTO_COMPACT_TOKENS = 100_000
MAX_AUTO_COMPACT_TOKENS = 1_000_000
ASK_QUESTION_MAX_CHARS = 16 * 1024
ASK_OPTION_LABEL_MAX_CHARS = 512
ASK_OPTION_DESCRIPTION_MAX_CHARS = 2 * 1024
ASK_ANSWER_MAX_CHARS = 4 * 1024
ASK_OPTION_MIN_COUNT = 2
ASK_OPTION_MAX_COUNT = 5
FILE_PREVIEW_MAX_BYTES = 512 * 1024
PREVIEW_ASSET_MAX_BYTES = 4 * 1024 * 1024
MAX_ENCODED_PREVIEW_ASSET_CHARS = ((PREVIEW_ASSET_MAX_BYTES + 2) // 3) * 4
ARTIFACT_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
MAX_ENCODED_ARTIFACT_PREVIEW_CHARS = ((ARTIFACT_PREVIEW_MAX_BYTES + 2) // 3) * 4


def _valid_attachment_filename(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("attachment filename must be valid UTF-8") from exc
    if len(encoded) > MAX_FILENAME_BYTES:
        raise ValueError(
            f"attachment filename exceeds {MAX_FILENAME_BYTES} UTF-8 bytes")
    return value


def _valid_preview_content(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("file content must be valid UTF-8") from exc
    if len(encoded) > FILE_PREVIEW_MAX_BYTES:
        raise ValueError(
            f"file content exceeds {FILE_PREVIEW_MAX_BYTES} UTF-8 bytes")
    return value


AttachmentFilename = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_FILENAME_BYTES),
    AfterValidator(_valid_attachment_filename),
]
AttachmentData = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_ENCODED_ATTACHMENT_CHARS),
]
AskQuestionText = Annotated[
    str, StringConstraints(min_length=1, max_length=ASK_QUESTION_MAX_CHARS),
]
AskOptionLabel = Annotated[
    str, StringConstraints(min_length=1, max_length=ASK_OPTION_LABEL_MAX_CHARS),
]
AskOptionDescription = Annotated[
    str, StringConstraints(max_length=ASK_OPTION_DESCRIPTION_MAX_CHARS),
]
AskAnswerText = Annotated[
    str, StringConstraints(min_length=1, max_length=ASK_ANSWER_MAX_CHARS),
]
AskAnswer = Union[
    AskAnswerText,
    Annotated[
        list[AskAnswerText],
        Field(min_length=1, max_length=ASK_OPTION_MAX_COUNT),
    ],
]
PreviewPath = Annotated[
    str, StringConstraints(min_length=1, max_length=4096),
]
PreviewContent = Annotated[
    str,
    StringConstraints(max_length=FILE_PREVIEW_MAX_BYTES),
    AfterValidator(_valid_preview_content),
]
FileRevision = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
FileMtimeNs = Annotated[
    str, StringConstraints(min_length=1, max_length=20, pattern=r"^[0-9]+$"),
]
PreviewAssetData = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_ENCODED_PREVIEW_ASSET_CHARS),
]
ArtifactPreviewData = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_ENCODED_ARTIFACT_PREVIEW_CHARS),
]
StatusErrorText = Annotated[
    str, StringConstraints(min_length=1, max_length=384),
]
NoticeTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=256),
]
NoticeMessage = Annotated[
    str, StringConstraints(min_length=1, max_length=2 * 1024),
]
NoticeDetail = Annotated[
    str, StringConstraints(min_length=1, max_length=4 * 1024),
]
NoticeSeverity = Literal["info", "warning"]
NoticeCategory = Literal[
    "runtime", "guardian", "config", "deprecation", "security", "rate_limit",
]
GoalStatus = Literal[
    "active", "paused", "blocked", "usageLimited", "budgetLimited", "complete",
]


class QueryImage(TypedDict):
    """Strict image attachment shape; validation returns a plain ``dict``."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    media_type: Literal["image/png", "image/jpeg", "image/jpg", "image/webp"]
    data: AttachmentData


class ConversationImageRef(TypedDict):
    """Payload-free locator for one user image in materialized history."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    image_id: WireId
    media_type: Literal["image/png", "image/jpeg", "image/jpg", "image/webp"]
    width: int
    height: int
    byte_size: int


class QueryFile(TypedDict):
    """Strict uploaded-file shape; validation returns a plain ``dict``."""

    __pydantic_config__ = ConfigDict(extra="forbid")
    filename: AttachmentFilename
    data: AttachmentData


class UserFileMeta(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    filename: AttachmentFilename


class AskOption(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    label: AskOptionLabel
    ds: NotRequired[AskOptionDescription]


class PlanEntry(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    step: Annotated[str, StringConstraints(min_length=1, max_length=16 * 1024)]
    status: Literal["pending", "inProgress", "completed"]


def _attachment_count(images, files) -> int:
    return len(images or ()) + len(files or ())

# Error codes
ERR_BUSY = "busy"
ERR_NOT_RUNNING = "not_running"
ERR_DRAIN_TIMEOUT = "drain_timeout"
ERR_CC_CRASH = "cc_crash"
ERR_BAD_PROMPT = "bad_prompt"
ERR_PROTOCOL = "protocol"
ERR_INTERNAL = "internal"
ERR_INVALID_CWD = "invalid_cwd"
ERR_WRAPPER_OFFLINE = "wrapper_offline"
ERR_WRAPPER_ALREADY_CONNECTED = "wrapper_already_connected"
ERR_AUTH = "auth"
ERR_FORK_RECONCILING = "fork_reconciling"
ERR_NOT_STEERABLE = "not_steerable"
ERR_STEER_UNKNOWN = "steer_outcome_unknown"
ERR_QUEUE_FULL = "queue_full"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: int = PROTOCOL_VERSION
    ts: float = Field(default_factory=time.time)
    sid: Optional[WireId] = None
    seq: Optional[int] = None
    # `to` routes a frame to a single client (by client_id) instead of
    # broadcasting. Used for per-client replay frames; null = broadcast.
    to: Optional[WireId] = None
    # Relay-generated WebSocket generation. Only Hello replay/catch-up frames
    # echo this value; the relay drops them if `client_id` has since reconnected
    # onto another socket. Clients must not treat it as a durable identity.
    route_id: Optional[WireId] = None
    # Relay-authenticated browser-account identity. Unlike ``client_id`` this
    # survives page reloads and is shared by independent tabs signed in as the
    # same relay user. The relay always replaces client-supplied values.
    owner_id: Optional[WireId] = None


class _Command(_Base):
    """Reliable client command envelope (hello and heartbeat are excluded).

    The relay overwrites ``client_id`` with the identity bound by client hello;
    ``cmd_id`` remains stable across WebSocket reconnect retries.
    """
    cmd_id: Optional[WireId] = None
    client_id: Optional[WireId] = None


# ---- client -> wrapper (via relay); no seq ----

class Hello(_Base):
    """First frame after upgrade. `role` distinguishes client vs wrapper.

    Client role sends `client_id` (for routed frames). A session with no supplied
    cursor gets one lightweight Snapshot; a named `cursors[sid]` receives only
    the missing live ring tail (`seq > cursor`). `last_seq` is the legacy focused-
    session form. Authoritative history remains an on-demand GetHistory transcript
    read. Wrapper role announces its cc session id, current state, and ring bounds.
    """
    type: Literal["hello"] = "hello"
    role: Literal["client", "wrapper"]
    client_id: Optional[WireId] = None  # client
    machine_id: Optional[WireId] = None  # relay route; wrapper identity
    last_seq: Optional[int] = None  # client (legacy: focused session only)
    cursors: Optional[dict[WireId, int]] = None  # client: per-session last_seq for multi-session catch-up
    generations: Optional[dict[WireId, WireId]] = None  # client: wrapper generation paired with each cursor
    cc_session_id: Optional[WireId] = None  # wrapper
    wrapper_generation: Optional[WireId] = None  # wrapper process lifetime id
    state: Optional[State] = None  # wrapper
    buffer_head_seq: Optional[int] = None  # wrapper
    buffer_tail_seq: Optional[int] = None  # wrapper

    @model_validator(mode="after")
    def bounded_cursors(self):
        if self.cursors is not None:
            if len(self.cursors) > 128:
                raise ValueError("hello cursors exceed 128 sessions")
            if any(seq < 0 for seq in self.cursors.values()):
                raise ValueError("hello cursors must be non-negative")
        if self.generations is not None and len(self.generations) > 128:
            raise ValueError("hello generations exceed 128 sessions")
        return self


class Query(_Command):
    type: Literal["query"] = "query"
    prompt: str = Field(max_length=2 * 1024 * 1024)
    msg_id: WireId
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[QueryFile]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    # `queue` and `replace` transfer ownership to the always-on wrapper
    # immediately. The wrapper starts them after the active turn reaches its
    # authoritative terminal boundary, even if every browser has disconnected.
    delivery: Literal["immediate", "queue", "replace"] = "immediate"

    @model_validator(mode="after")
    def bounded_attachment_count(self):
        if _attachment_count(self.images, self.files) > MAX_ATTACHMENT_COUNT:
            raise ValueError(
                f"query attachments exceed {MAX_ATTACHMENT_COUNT} items")
        if self.delivery != "immediate" and (
            not self.sid or not self.cmd_id or not self.client_id
        ):
            raise ValueError(
                "deferred query requires sid, cmd_id, and client_id")
        return self


class CancelQueuedQuery(_Command):
    """Cancel one wrapper-owned query before it starts."""

    type: Literal["cancel_queued_query"] = "cancel_queued_query"
    sid: WireId
    msg_id: WireId
    cmd_id: WireId
    client_id: WireId


class GetQueuedQuery(_Command):
    """Read one wrapper-owned query without putting its payload in the ring."""

    type: Literal["get_queued_query"] = "get_queued_query"
    sid: WireId
    msg_id: WireId
    cmd_id: WireId
    client_id: WireId


class QueuedQueryDetail(_Base):
    """Private, on-demand full prompt for one queued query."""

    type: Literal["queued_query_detail"] = "queued_query_detail"
    sid: WireId
    msg_id: WireId
    request_id: WireId
    prompt: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    kind: Optional[Literal["queue", "replace"]] = None
    image_count: int = Field(default=0, ge=0, le=MAX_ATTACHMENT_COUNT)
    file_count: int = Field(default=0, ge=0, le=MAX_ATTACHMENT_COUNT)
    error: Optional[str] = Field(default=None, max_length=4096)


class UpdateQueuedQuery(_Command):
    """Replace only the prompt of one queued query, preserving attachments."""

    type: Literal["update_queued_query"] = "update_queued_query"
    sid: WireId
    msg_id: WireId
    prompt: str = Field(max_length=2 * 1024 * 1024)
    cmd_id: WireId
    client_id: WireId


class QueuedQueryUpdated(_Base):
    """Private result for one atomic queued-query edit."""

    type: Literal["queued_query_updated"] = "queued_query_updated"
    sid: WireId
    msg_id: WireId
    request_id: WireId
    updated: bool
    error: Optional[str] = Field(default=None, max_length=4096)


class QueuedQueryInfo(BaseModel):
    """Payload-bounded queue projection safe to replay to browsers."""

    model_config = ConfigDict(extra="forbid")
    msg_id: WireId
    kind: Literal["queue", "replace"]
    prompt_preview: str = Field(max_length=MAX_QUERY_QUEUE_PREVIEW_CHARS)
    image_count: int = Field(ge=0, le=MAX_ATTACHMENT_COUNT)
    file_count: int = Field(ge=0, le=MAX_ATTACHMENT_COUNT)
    retained_bytes: int = Field(ge=0, le=MAX_QUERY_QUEUE_BYTES)
    error: Optional[str] = Field(default=None, max_length=4096)


class QueryQueueState(_Base):
    """Authoritative per-session wrapper queue, newest replacement first."""

    type: Literal["query_queue"] = "query_queue"
    items: list[QueuedQueryInfo] = Field(
        default_factory=list, max_length=MAX_QUERY_QUEUE_ITEMS)
    # The queue bound is wrapper-global, not per session.  Browsers need the
    # authoritative aggregate because prompt previews intentionally omit almost
    # all retained bytes (especially base64 attachment bodies).
    total_count: int = Field(ge=0, le=MAX_QUERY_QUEUE_ITEMS)
    total_bytes: int = Field(ge=0, le=MAX_QUERY_QUEUE_BYTES)


class Steer(_Command):
    """Append input to the active Codex turn without interrupting it."""
    type: Literal["steer"] = "steer"
    # Steer has no pre-v21 compatibility form. Requiring the reliable identity
    # prevents an ACK-lost retry from appending the same instruction twice.
    cmd_id: WireId
    client_id: WireId
    # Steer is new in v21 and has no legacy focused-session form. Requiring the
    # target prevents a delayed command from falling through to a newer focus.
    sid: WireId
    prompt: str = Field(max_length=2 * 1024 * 1024)
    msg_id: WireId
    images: Optional[list[QueryImage]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[QueryFile]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)

    @model_validator(mode="after")
    def bounded_attachment_count(self):
        if _attachment_count(self.images, self.files) > MAX_ATTACHMENT_COUNT:
            raise ValueError(
                f"steer attachments exceed {MAX_ATTACHMENT_COUNT} items")
        return self


class Interrupt(_Command):
    type: Literal["interrupt"] = "interrupt"


class Takeover(_Command):
    """client -> wrapper: explicitly release a terminal read-only lock."""
    type: Literal["takeover"] = "takeover"
    sid: WireId
    # v5 introduced takeover as an at-most-once ownership mutation. Unlike
    # legacy commands, it has no compatibility reason to allow an unreliable
    # envelope that can bypass wrapper deduplication.
    cmd_id: WireId


class TakeoverState(_Base):
    """wrapper -> clients: transient status for an active-turn takeover intent.

    This is a control frame, not transcript narrative, so it is never seq'd or
    replayed after the intent has completed or been cancelled.
    """
    type: Literal["takeover_state"] = "takeover_state"
    pending: bool
    message: Optional[str] = Field(default=None, max_length=4096)


class SessionControl(_Base):
    """Authoritative, revisioned control state for one session.

    ``control_mode`` says which surface currently coordinates the session;
    ``write_state`` independently says whether the Web composer may write.
    Consumers must accept only increasing revisions. An equal revision is
    idempotent only when every control field is unchanged.

    ``can_takeover`` advertises that a migration action can be shown. It is a
    capability hint, not proof that a takeover command has already been
    authorized or completed.
    """

    type: Literal["session_control"] = "session_control"
    control_mode: ControlMode
    write_state: WriteState
    terminal_attached: bool
    reason: Optional[str] = Field(default=None, max_length=4096)
    # Wrapper lifetime that owns the numeric revision. A new generation starts
    # a fresh revision epoch; generation-less values exist only for the short
    # v15 migration window and never overwrite generation-bound control.
    generation: Optional[WireId] = None
    revision: int = Field(ge=0, le=9_007_199_254_740_991)
    can_takeover: Optional[bool] = None


class SetModel(_Command):
    type: Literal["set_model"] = "set_model"
    model: ModelName


class SetEffort(_Command):
    """client -> wrapper: set the session's reasoning effort (thinking strength).
    Unlike set_model, effort is a spawn-time CLI flag (--effort), so applying it
    respawns the cc subprocess with resume — done lazily at the next turn."""
    type: Literal["set_effort"] = "set_effort"
    effort: EffortLevel


class SetCodexContext(_Command):
    """Set usable context capacity for one Codex session; derive compaction."""

    type: Literal["set_codex_context"] = "set_codex_context"
    max_context_tokens: Optional[int] = Field(default=None, ge=1, le=100_000_000, strict=True)


class CodexContext(_Base):
    type: Literal["codex_context"] = "codex_context"
    model: str = ""
    max_context_tokens: Optional[int] = None
    applied_max_context_tokens: Optional[int] = None
    applied_threshold_tokens: Optional[int] = None
    model_max_tokens: Optional[int] = None
    limit_tokens: Optional[int] = None
    context_window_tokens: Optional[int] = None
    pending: bool = False
    mutable: bool = True
    error: Optional[str] = None


class SetAutoCompact(_Command):
    """Set one Claude session's spawn-time automatic compaction threshold.

    ``inherit`` omits Claude's CLI flag, ``auto`` asks Claude to choose its
    recommended threshold, and ``custom`` carries an exact bounded token count.
    The wrapper may defer the reconnect until the active turn reaches its real
    terminal boundary; it never interrupts a turn to apply this command.
    """

    type: Literal["set_auto_compact"] = "set_auto_compact"
    mode: AutoCompactMode
    threshold_tokens: Optional[int] = Field(
        default=None,
        ge=MIN_AUTO_COMPACT_TOKENS,
        le=MAX_AUTO_COMPACT_TOKENS,
    )

    @model_validator(mode="after")
    def threshold_matches_mode(self):
        if self.mode == "custom" and self.threshold_tokens is None:
            raise ValueError("custom autocompact requires threshold_tokens")
        if self.mode != "custom" and self.threshold_tokens is not None:
            raise ValueError("threshold_tokens is only valid for custom autocompact")
        return self


class SetServiceTier(_Command):
    """client -> wrapper: set the Codex service tier (codex only). "fast" maps to
    app-server's persisted per-thread service tier; "" / "default" clears the
    override. 0.144.1 reports the applied Fast tier as ``priority``."""
    type: Literal["set_service_tier"] = "set_service_tier"
    service_tier: Literal["", "default", "fast", "toggle"]


class SetCollaborationMode(_Command):
    """client -> wrapper: select Codex's real collaboration mode.

    This is deliberately separate from SetPerm: collaboration mode controls how
    Codex approaches the next turn (default vs plan), while approvalPolicy controls
    whether tools require confirmation.
    """
    type: Literal["set_collaboration_mode"] = "set_collaboration_mode"
    mode: CollaborationModeName


class OpenBtw(_Command):
    """client -> wrapper: open a /btw ephemeral side-fork of `sid` (the parent
    session). The wrapper forks it (inherits context) and replies BtwOpened
    routed to=<client_id> (only the requester opens the panel)."""
    type: Literal["open_btw"] = "open_btw"
    request_id: WireId
    client_id: Optional[WireId] = None  # requester, for routing BtwOpened back
    # `sid` (inherited) = the parent session to fork.


class CloseBtw(_Command):
    """client -> wrapper: discard the /btw fork `sid` (tear down, never persisted)."""
    type: Literal["close_btw"] = "close_btw"
    # `sid` (inherited) = the btw fork to close.


class SyncBtw(_Command):
    """Hydrate one visible resident BTW from its bounded replay ring."""

    type: Literal["sync_btw"] = "sync_btw"
    cursor: Optional[int] = Field(default=None, ge=0)
    generation: Optional[WireId] = None


class Ping(_Base):
    type: Literal["ping"] = "ping"
    n: int = Field(ge=0, le=2_147_483_647)


class Pong(_Base):
    type: Literal["pong"] = "pong"
    n: int = Field(ge=0, le=2_147_483_647)


class CommandAck(_Base):
    """Wrapper -> originating client after a reliable handler returns.

    Routed with ``to=client_id`` and intentionally not sequenced or buffered.
    Duplicate processed commands receive this again without re-execution.
    """
    type: Literal["command_ack"] = "command_ack"
    cmd_id: WireId
    client_id: WireId

    @model_validator(mode="after")
    def must_be_routed_to_origin(self):
        if self.to != self.client_id:
            raise ValueError("command_ack.to must equal client_id")
        return self


# ---- wrapper -> client (via relay); all carry seq ----

class ReplayStart(_Base):
    type: Literal["replay_start"] = "replay_start"
    from_seq: int
    to_seq: int
    truncated: bool
    # rebuild=True: the client's last_seq was from a previous wrapper lifetime
    # (seq reset on restart), so it must discard its IndexedDB cache and rebuild
    # from this full-buffer replay. Distinct from `truncated` (which means the
    # buffer evicted events the client wanted -> data may be lost -> show banner).
    rebuild: bool = False
    generation: Optional[WireId] = None


class ReplayEnd(_Base):
    type: Literal["replay_end"] = "replay_end"
    to_seq: int
    truncated: bool


class Snapshot(_Base):
    type: Literal["snapshot"] = "snapshot"
    cc_session_id: Optional[WireId] = None
    state: State
    tail_text: str = ""
    cwd: Optional[str] = None  # active cc cwd, so the client knows the current project
    generation: Optional[WireId] = None
    # Latest authoritative control value. It is intentionally also available as
    # a live SessionControl event; embedding it here closes reconnect races when
    # the corresponding control event has already fallen out of the ring.
    control: Optional[SessionControl] = None


class StateEvent(_Base):
    type: Literal["state"] = "state"
    state: State
    # Optional non-terminal activity for the current turn. Additive on protocol
    # v4: older clients still consume `state`, while newer clients replace the
    # generic spinner with retry/wait detail without treating it as an Error.
    phase: Optional[Literal["retrying", "waiting"]] = None
    detail: Optional[str] = Field(default=None, max_length=4096)
    msg_id: Optional[WireId] = None


class Model(_Base):
    """The cc session's current model (from SystemMessage init / after set_model).
    Downstream so a reconnecting client restores the model readout."""
    type: Literal["model"] = "model"
    model: str


class Effort(_Base):
    """The session's current reasoning effort. Downstream so a reconnecting
    client restores the effort readout."""
    type: Literal["effort"] = "effort"
    effort: str


class AutoCompact(_Base):
    """Claude session-level automatic compaction control state.

    Desired and applied values are separate because this is a process-start
    option. ``pending`` remains true until a safe resume reconnect succeeds.
    ``mutable`` is false while an official terminal/broker owns the session;
    those surfaces are observed but never controlled through Web commands.
    """

    type: Literal["auto_compact"] = "auto_compact"
    mode: AutoCompactMode = "inherit"
    threshold_tokens: Optional[int] = Field(
        default=None,
        ge=MIN_AUTO_COMPACT_TOKENS,
        le=MAX_AUTO_COMPACT_TOKENS,
    )
    applied_mode: Optional[AutoCompactMode] = None
    applied_threshold_tokens: Optional[int] = Field(
        default=None,
        ge=MIN_AUTO_COMPACT_TOKENS,
        le=MAX_AUTO_COMPACT_TOKENS,
    )
    pending: bool = False
    phase: AutoCompactPhase = "stable"
    mutable: bool = True
    error: Optional[str] = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def thresholds_match_modes(self):
        if self.mode == "custom" and self.threshold_tokens is None:
            raise ValueError("custom autocompact requires threshold_tokens")
        if self.mode != "custom" and self.threshold_tokens is not None:
            raise ValueError("threshold_tokens is only valid for custom autocompact")
        if self.applied_mode == "custom" and self.applied_threshold_tokens is None:
            raise ValueError(
                "applied custom autocompact requires applied_threshold_tokens")
        if (self.applied_mode != "custom"
                and self.applied_threshold_tokens is not None):
            raise ValueError(
                "applied_threshold_tokens is only valid for applied custom autocompact")
        return self


class Fast(_Base):
    """Codex Fast-mode (service_tier) state, downstream. Emitted after a /fast
    toggle and on each codex turn so the client shows whether the next reply is on
    the fast tier or standard — not just that it was 'toggled'."""
    type: Literal["fast"] = "fast"
    on: bool


class CollaborationMode(_Base):
    """The Codex session's collaboration mode, restored across reconnects."""
    type: Literal["collaboration_mode"] = "collaboration_mode"
    mode: CollaborationModeName


class BtwOpened(_Base):
    """wrapper -> client: a /btw fork is ready. `btw_sid` is the stable routing key
    for the side panel (send Query{sid=btw_sid} for its turns, CloseBtw to end)."""
    type: Literal["btw_opened"] = "btw_opened"
    request_id: WireId
    btw_sid: WireId
    parent_sid: WireId
    engine: Engine
    created_at: float = Field(ge=0)
    revision: int = Field(ge=0, le=9_007_199_254_740_991)


class BtwSessionInfo(BaseModel):
    """One owner-scoped resident side chat in an authoritative BTW catalog."""

    model_config = ConfigDict(extra="forbid")
    btw_sid: WireId
    parent_sid: WireId
    engine: Literal["claude", "codex", "dsh"]
    created_at: float = Field(ge=0)
    state: State = "idle"


class BtwSync(_Base):
    """Wrapper -> client: complete resident BTW catalog for this client.

    This reconnect baseline is intentionally independent of the narrative ring:
    an idle fork still exists even when no recent event mentions it.
    """

    type: Literal["btw_sync"] = "btw_sync"
    generation: WireId
    revision: int = Field(ge=0, le=9_007_199_254_740_991)
    sessions: list[BtwSessionInfo] = Field(max_length=64)


class BtwClosed(_Base):
    """Wrapper -> owner: one resident side chat was explicitly discarded."""

    type: Literal["btw_closed"] = "btw_closed"
    btw_sid: WireId
    parent_sid: WireId
    revision: int = Field(ge=0, le=9_007_199_254_740_991)


class UserMsg(_Base):
    """A user's query, broadcast to all clients so every device sees the full
    conversation (prompt + response). The originating client dedups by msg_id
    (it already created the turn optimistically on send). Carries images so
    other devices / fresh replays render the attachment."""
    type: Literal["user_msg"] = "user_msg"
    msg_id: WireId
    # Codex persists turn/steer's clientUserMessageId on the legacy
    # event_msg/user_message record, while its history pagination cursor remains
    # a source-derived id. Carry both so a history-first race can deduplicate
    # the later live echo.
    client_msg_id: Optional[WireId] = None
    prompt: str
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    # Metadata only: file bodies stay out of replay/cache, while names remain
    # visible across devices and after transcript history reload.
    files: Optional[list[UserFileMeta]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)


class TurnSteered(_Base):
    """A user message appended to the active Codex turn."""
    type: Literal["turn_steered"] = "turn_steered"
    msg_id: WireId
    turn_id: WireId
    prompt: str
    images: Optional[list[QueryImage]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[UserFileMeta]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)


class AssistantMsgStart(_Base):
    type: Literal["assistant_msg_start"] = "assistant_msg_start"
    message_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    channel: AssistantChannel = "unknown"


class Delta(_Base):
    # Replace a provisional DSH attempt at its authoritative settlement.
    replace: bool = False
    type: Literal["delta"] = "delta"
    message_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    text: str
    # Some engines only reveal whether text is commentary or final on the
    # assembled message. Repeating the channel on deltas/end lets the client
    # promote a provisional block without duplicating it.
    channel: AssistantChannel = "unknown"


class ToolUse(_Base):
    # Native file evidence is consumed locally before entering the replay ring.
    _turn_change_source: Optional[dict] = PrivateAttr(default=None)
    type: Literal["tool_use"] = "tool_use"
    message_id: WireId
    tool_use_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    tool: str
    input: dict[str, Any]
    category: ToolCategory = "tool"
    title: Optional[str] = Field(default=None, max_length=1024)
    parent_id: Optional[WireId] = None
    server: Optional[str] = Field(default=None, max_length=1024)


class ToolDelta(_Base):
    """Incremental progress/output for a previously-started tool call."""
    type: Literal["tool_delta"] = "tool_delta"
    tool_use_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    stream: Literal["progress", "output", "diff", "summary", "terminal"]
    delta: str = Field(max_length=512 * 1024)


class ToolResult(_Base):
    _turn_change_source: Optional[dict] = PrivateAttr(default=None)
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    content: str
    is_error: bool
    truncated: Optional[bool] = None
    status: Optional[ProcessStatus] = None
    summary: Optional[str] = Field(default=None, max_length=64 * 1024)
    diff: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    diff_source: Optional[Literal["native", "fragment"]] = None
    diff_truncated: Optional[bool] = None
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)


class AsyncQuestionSpec(BaseModel):
    """Native non-blocking question; a reply is ordinary user input, not approval."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=8192)
    options: Optional[list[Annotated[str, Field(min_length=1, max_length=1024)]]] = Field(
        default=None, max_length=16)


class AssistantMsgEnd(_Base):
    type: Literal["assistant_msg_end"] = "assistant_msg_end"
    message_id: WireId
    turn_id: Optional[WireId] = None
    background: Optional[bool] = None
    channel: AssistantChannel = "unknown"
    # These are message content, not pending AskUser/Future state. They survive
    # history/reconnect and may be answered while the native turn keeps running.
    delivery: Optional[Literal["async"]] = None
    questions: Optional[list[AsyncQuestionSpec]] = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def bounded_async_questions(self):
        if self.questions is not None:
            if self.delivery != "async":
                raise ValueError("questions require async delivery")
            if len(json.dumps([q.model_dump() for q in self.questions],
                              ensure_ascii=False)) > 16 * 1024:
                raise ValueError("async question metadata exceeds 16 KiB")
        return self


class ProcessEvent(_Base):
    """Engine-neutral lifecycle event rendered inside one turn's process UI.

    Tool calls retain ToolUse/ToolDelta/ToolResult for compatibility. This event
    carries non-tool rich-client activities such as plans, hooks, collaboration,
    compaction, and structured app-server lifecycle updates.
    """
    type: Literal["process"] = "process"
    item_id: WireId
    kind: ProcessKind
    phase: ProcessPhase
    status: ProcessStatus = "unknown"
    turn_id: Optional[WireId] = None
    parent_id: Optional[WireId] = None
    title: str = Field(min_length=1, max_length=1024)
    summary: Optional[str] = Field(default=None, max_length=64 * 1024)
    detail: Optional[str] = Field(default=None, max_length=256 * 1024)
    input: Optional[dict[str, Any]] = None
    output: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    diff: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    progress: Optional[str] = Field(default=None, max_length=64 * 1024)
    append_to: Optional[ProcessAppendTarget] = None
    delta: Optional[str] = Field(default=None, max_length=512 * 1024)
    server: Optional[str] = Field(default=None, max_length=1024)
    tool: Optional[str] = Field(default=None, max_length=1024)
    command: Optional[str] = Field(default=None, max_length=256 * 1024)
    cwd: Optional[str] = Field(default=None, max_length=16 * 1024)
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    truncated: Optional[bool] = None
    # Claude Agent work may outlive the parent ResultMessage. It remains visible
    # in that turn's collaboration card, but must not re-open the session-wide
    # running indicator after the parent turn has authoritatively completed.
    background: Optional[bool] = None


class BackgroundProcessItem(BaseModel):
    """Bounded public snapshot of one currently-live detached process.

    The native engine owns membership.  This shape intentionally mirrors the
    presentation subset of ``ProcessEvent`` without carrying routing/sequence
    fields, raw task output, or private environment data.
    """

    model_config = ConfigDict(extra="forbid")
    item_id: WireId
    kind: Literal["agent", "task"]
    status: ProcessStatus = "running"
    turn_id: Optional[WireId] = None
    parent_id: Optional[WireId] = None
    title: str = Field(min_length=1, max_length=1024)
    summary: Optional[str] = Field(
        default=None, max_length=MAX_BACKGROUND_PROCESS_SUMMARY_CHARS)
    progress: Optional[str] = Field(
        default=None, max_length=MAX_BACKGROUND_PROCESS_SUMMARY_CHARS)
    command: Optional[str] = Field(
        default=None, max_length=MAX_BACKGROUND_PROCESS_COMMAND_CHARS)
    cwd: Optional[str] = Field(
        default=None, max_length=MAX_BACKGROUND_PROCESS_CWD_CHARS)
    started_at: Optional[float] = Field(
        default=None, ge=0, le=MAX_SAFE_WIRE_TIMESTAMP_SECONDS)
    updated_at: Optional[float] = Field(
        default=None, ge=0, le=MAX_SAFE_WIRE_TIMESTAMP_SECONDS)


class BackgroundProcessSync(_Base):
    """Authoritative full replacement for one session's live background work.

    Unlike replayable edge events, an empty snapshot is meaningful: it clears
    stale browser/IndexedDB cards after a missed terminal or process restart.
    """

    type: Literal["background_process_sync"] = "background_process_sync"
    generation: Optional[WireId] = None
    items: list[BackgroundProcessItem] = Field(
        default_factory=list, max_length=MAX_BACKGROUND_PROCESS_ITEMS)


class TurnPlan(_Base):
    type: Literal["turn_plan"] = "turn_plan"
    item_id: WireId
    turn_id: Optional[WireId] = None
    explanation: Optional[str] = Field(default=None, max_length=64 * 1024)
    plan: list[PlanEntry] = Field(max_length=128)


class TurnDiff(_Base):
    type: Literal["turn_diff"] = "turn_diff"
    item_id: WireId
    turn_id: Optional[WireId] = None
    diff: str = Field(max_length=2 * 1024 * 1024)
    truncated: Optional[bool] = None


class TurnFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    state: Literal["pending", "available", "unavailable"]
    additions: Optional[int] = Field(default=None, ge=0)
    deletions: Optional[int] = Field(default=None, ge=0)
    reason: Optional[str] = Field(default=None, max_length=256)


class TurnChangeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=1, max_length=64)
    files: list[TurnFileChange] = Field(max_length=64)
    total_files: Optional[int] = Field(default=None, ge=0, le=4096)
    total_additions: Optional[int] = Field(default=None, ge=0)
    total_deletions: Optional[int] = Field(default=None, ge=0)
    next_offset: Optional[int] = Field(default=None, ge=1, le=4096)
    truncated: Optional[bool] = None


class TurnFileChanges(_Base):
    type: Literal["turn_file_changes"] = "turn_file_changes"
    turn_id: WireId
    changes: TurnChangeSummary


class TurnBinding(_Base):
    """Bind one browser optimistic message id to Codex's native turn id.

    Codex persists the native id in rollout history, while the browser creates
    ``msg_id`` before ``turn/start``.  The mapping is authoritative and lets a
    history refresh reconcile the same turn without timestamp heuristics.
    """
    type: Literal["turn_binding"] = "turn_binding"
    msg_id: WireId
    turn_id: WireId
    # A native autonomous round has no human/optimistic prompt row.
    autonomous: bool = False


class TurnResult(BaseModel):
    """Subset of ResultMessage forwarded to clients."""
    model_config = ConfigDict(extra="allow")
    subtype: str
    duration_ms: int
    is_error: bool
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None


class TurnNotificationContext(BaseModel):
    """Realtime-only presentation metadata for completion notifications.

    The wrapper strips this field before buffering a TurnEnd, so reconnect and
    history replay cannot create a second OS notification for an old turn.
    """
    model_config = ConfigDict(extra="forbid")
    engine: Engine
    space: Space
    display_name: Optional[str] = Field(default=None, max_length=160)
    # A private /btw runtime routes under its ephemeral sid, but a notification
    # opens the durable parent conversation that owns the side panel.
    parent_session_id: Optional[WireId] = None


class TurnEnd(_Base):
    type: Literal["turn_end"] = "turn_end"
    result: TurnResult
    # Engine-specific authoritative fork point. Codex sends app-server's turn
    # id; Claude sends the final assistant transcript UUID in this user turn.
    # Synthetic/legacy boundaries without a real engine id leave it unset.
    # The legacy wire name stays stable for protocol-v5 browser compatibility.
    turn_id: Optional[WireId] = None
    # Claude's file-checkpoint API targets the top-level user transcript UUID,
    # not the assistant UUID above or the browser's optimistic message id.
    checkpoint_id: Optional[WireId] = None
    notification_context: Optional[TurnNotificationContext] = None
    # Wrapper-internal provenance.  This is deliberately a Pydantic private
    # attribute so it never crosses the wire or changes protocol validation.
    # Only a real Codex app-server ``turn/completed`` may set it; locally
    # synthesized TurnEnd frames must not become durable lifecycle facts.
    _codex_authoritative_terminal: bool = PrivateAttr(default=False)
    # Claude's final assistant UUID is a fork point, not the logical owner of
    # its tool events. Keep their exact translator owner off the wire.
    _changes_turn_id: Optional[str] = PrivateAttr(default=None)


class Error(_Base):
    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(max_length=64 * 1024)
    # Optional correlation for terminal command rejection. NewSession uses
    # request_id so the draft stays open on failure; Query uses msg_id so the
    # exact optimistic turn can be marked done instead of leaving a spinner.
    request_id: Optional[WireId] = None
    msg_id: Optional[WireId] = None


# ---- relay -> client (control); no seq ----

class WrapperDisconnected(_Base):
    type: Literal["wrapper_disconnected"] = "wrapper_disconnected"


class WrapperReconnected(_Base):
    type: Literal["wrapper_reconnected"] = "wrapper_reconnected"
    cc_session_id: Optional[WireId] = None
    state: State
    generation: Optional[WireId] = None


# ---- sessions (list / switch / new) ----

class CodexProfileInfo(BaseModel):
    """Public account metadata. CODEX_HOME paths never cross the wire."""
    model_config = ConfigDict(extra="forbid")
    id: WireId
    label: str = Field(min_length=1, max_length=48)
    error: Optional[str] = Field(default=None, min_length=1, max_length=384)


class ClaudeProfileInfo(BaseModel):
    """Public account metadata. CLAUDE_CONFIG_DIR never crosses the wire."""
    model_config = ConfigDict(extra="forbid")
    id: WireId
    label: str = Field(min_length=1, max_length=48)
    error: Optional[str] = Field(default=None, min_length=1, max_length=384)


class SessionInfo(BaseModel):
    """A row in the sessions sidebar (subset of SDK SDKSessionInfo)."""
    model_config = ConfigDict(extra="forbid")
    session_id: WireId
    summary: Optional[str] = None
    last_modified: Optional[str] = None
    first_prompt: Optional[str] = None
    git_branch: Optional[str] = None
    cwd: Optional[str] = None
    tag: Optional[str] = None  # SDK session tag; "archived" hides the card in the sidebar
    pinned: bool = False  # cc-remote sidebar preference; engine transcripts stay untouched
    state: Optional[State] = None  # if resident: this session's idle/running/... (sidebar status dot)
    engine: Optional[str] = None  # "claude" | "codex"; None = claude (legacy sidebar badge)
    forked_from_id: Optional[WireId] = None  # Codex thread/fork parent, when present
    codex_status: Optional[CodexThreadStatus] = None  # authoritative app-server status
    space: Space = "code"
    work_id: Optional[WireId] = None
    # ``session_id`` is the routing id. Multi-profile engines namespace it;
    # copy/resume surfaces should use the native id below.
    native_session_id: Optional[WireId] = None
    claude_profile_id: Optional[WireId] = None
    claude_profile_label: Optional[str] = Field(default=None, max_length=48)
    codex_profile_id: Optional[WireId] = None
    codex_profile_label: Optional[str] = Field(default=None, max_length=48)
    # A catalog read also repairs completion receipts for cold/evicted sessions
    # which have no resident Snapshot on reconnect.
    completion_id: Optional[WireId] = None
    completion_unread: Optional[bool] = None
    completion_revision: Optional[int] = Field(default=None, ge=0)


class ListSessions(_Command):
    """client -> wrapper: request the session list. `engine` picks the backend's
    session store (Claude ~/.claude/projects vs Codex ~/.codex/sessions);
    optional, default claude."""
    type: Literal["list_sessions"] = "list_sessions"
    engine: Literal["claude", "codex", "dsh"] = "claude"
    space: Space = "code"


class SessionList(_Base):
    """wrapper -> client: the sessions (downstream so a reconnect restores it)."""
    type: Literal["session_list"] = "session_list"
    engine: Literal["claude", "codex", "dsh"]
    space: Space = "code"
    # Exact ListSessions.cmd_id. A single Codex read may paint a cached list and
    # then a refreshed list, so both responses intentionally carry the same id.
    request_id: Optional[WireId] = None
    sessions: list[SessionInfo]
    claude_profiles: list[ClaudeProfileInfo] = Field(
        default_factory=list, max_length=32)
    default_claude_profile_id: Optional[WireId] = None
    codex_profiles: list[CodexProfileInfo] = Field(default_factory=list, max_length=32)
    default_codex_profile_id: Optional[WireId] = None


class SessionListInvalidated(_Base):
    """wrapper -> clients: request a fresh, correlated catalog read.

    This is intentionally an unbuffered control hint. Each visible browser
    freezes its own socket/surface ownership when it answers the hint with
    ListSessions; the wrapper never broadcasts an uncorrelated SessionList.
    """
    type: Literal["session_list_invalidated"] = "session_list_invalidated"
    engine: Literal["claude", "codex", "dsh"]
    space: Space = "code"


class SessionActivity(_Base):
    """Lightweight sidebar lifecycle for a session that need not be resident.

    Native Codex App turns can update a rollout owned by another app-server, so
    the wrapper may know that a cold session is running without having a live
    conversation runtime for it.  Keep this separate from ``StateEvent``: the
    latter controls the composer/runtime, while this event only updates catalog
    presentation.
    """
    type: Literal["session_activity"] = "session_activity"
    engine: Literal["claude", "codex", "dsh"]
    session_id: WireId
    state: State


class SwitchSession(_Command):
    """client -> wrapper: resume a different existing session. `engine` tells the
    wrapper which backend to resume it with (codex threads resume differently)."""
    type: Literal["switch_session"] = "switch_session"
    session_id: WireId
    engine: Optional[Engine] = None
    space: Space = "code"


class NewSession(_Command):
    """client -> wrapper: start a fresh session (no resume). Optional `cwd`
    spawns it in that directory (default = the wrapper's current cc_cwd).
    `engine` selects the backend — Claude Code (default) or Codex. Optional
    `model`/`effort` pre-select the model and reasoning strength AT SPAWN — so
    the very first turn already uses them (effort especially: applying it at
    spawn avoids the respawn-with-resume that a post-spawn set_effort forces).
    The command can carry the first query atomically. The wrapper starts it on
    the newly-created context rather than waiting for a later SessionFocus and
    a separately-routed Query. Omitting query fields still creates a blank
    session, preserving the existing new-session command."""
    type: Literal["new_session"] = "new_session"
    request_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    engine: Engine = "claude"
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None
    dsh_agent_preset: Optional[str] = Field(default=None, min_length=1, max_length=256)
    dsh_effort: Optional[str] = Field(default=None, min_length=1, max_length=64)
    space: Space = "code"
    project_id: Optional[WireId] = None
    model: Optional[ModelName] = None    # None -> engine default (settings.json / codex config)
    effort: Optional[EffortLevel] = None  # None -> engine default
    # Claude only. None delegates to Claude Code's model/provider default.
    auto_compact_mode: Optional[AutoCompactMode] = None
    auto_compact_threshold_tokens: Optional[int] = Field(
        default=None,
        ge=MIN_AUTO_COMPACT_TOKENS,
        le=MAX_AUTO_COMPACT_TOKENS,
    )
    collaboration_mode: Optional[CollaborationModeName] = None  # Codex only; first turn included
    permission_mode: Optional[
        Literal["never", "on-request", "untrusted"]
    ] = None  # Codex only; persisted before the first turn
    permission_profile: Optional[PermissionProfileId] = None  # Codex only
    web_search: Optional[WebSearchMode] = None  # Codex Code only
    service_tier: Optional[Literal["default", "fast"]] = None  # Codex only
    prompt: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    msg_id: Optional[WireId] = None
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[QueryFile]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)

    @model_validator(mode="after")
    def initial_query_requires_message_id(self):
        if self.engine == "dsh" and self.space != "code":
            raise ValueError("DSH supports Code sessions")
        if self.engine != "dsh" and (self.dsh_agent_preset or self.dsh_effort):
            raise ValueError("DSH controls require the DSH engine")
        if _attachment_count(self.images, self.files) > MAX_ATTACHMENT_COUNT:
            raise ValueError(
                f"new_session attachments exceed {MAX_ATTACHMENT_COUNT} items")
        if (self.prompt is not None or self.images or self.files) and not self.msg_id:
            raise ValueError("msg_id is required when new_session carries a query")
        codex_only = {
            "codex_profile_id": self.codex_profile_id,
            "collaboration_mode": self.collaboration_mode,
            "permission_mode": self.permission_mode,
            "permission_profile": self.permission_profile,
            "web_search": self.web_search,
            "service_tier": self.service_tier,
        }
        invalid = [name for name, value in codex_only.items()
                   if value is not None and self.engine != "codex"]
        if invalid:
            raise ValueError(
                f"{', '.join(invalid)} only supported for Codex sessions")
        if self.claude_profile_id is not None and self.engine != "claude":
            raise ValueError(
                "claude_profile_id only supported for Claude sessions")
        if (self.auto_compact_mode is not None
                or self.auto_compact_threshold_tokens is not None):
            if self.engine != "claude":
                raise ValueError("autocompact only supported for Claude sessions")
            if (self.auto_compact_mode == "custom"
                    and self.auto_compact_threshold_tokens is None):
                raise ValueError(
                    "custom autocompact requires auto_compact_threshold_tokens")
            if (self.auto_compact_mode != "custom"
                    and self.auto_compact_threshold_tokens is not None):
                raise ValueError(
                    "auto_compact_threshold_tokens is only valid for custom autocompact")
        if self.space == "work" and self.cwd is not None:
            raise ValueError("Work session cwd is assigned by the wrapper")
        if self.space == "work" and self.web_search is not None:
            raise ValueError("Work web_search is fixed by the wrapper")
        if self.space == "code" and self.project_id is not None:
            raise ValueError("project_id is only supported for Work sessions")
        return self


class DeleteWorkSession(_Command):
    """Permanently delete one registered Work chat and its owned files."""
    type: Literal["delete_work_session"] = "delete_work_session"
    session_id: WireId
    engine: Engine
    space: Literal["work"] = "work"


class DeleteSession(_Command):
    """Permanently delete a native Code session or a registered Work session."""
    type: Literal["delete_session"] = "delete_session"
    session_id: WireId
    engine: Engine
    space: Space = "code"


class RollbackSession(_Command):
    """Restore conversation state, files, or both for one Code session.

    Claude targets an authoritative user-message checkpoint. Codex targets the
    latest ``num_turns`` because app-server's rollback RPC is count-based.
    Conversation and file restore deliberately report separate outcomes: the
    two engines do not expose an atomic transaction spanning both operations.
    """
    type: Literal["rollback_session"] = "rollback_session"
    session_id: WireId
    engine: Engine
    space: Literal["code"] = "code"
    restore: RestoreMode = "conversation"
    num_turns: int = Field(default=1, ge=1, le=1000)
    checkpoint_id: Optional[WireId] = None

    @model_validator(mode="after")
    def target_matches_engine(self):
        if self.engine == "claude" and self.checkpoint_id is None:
            raise ValueError("Claude rewind requires checkpoint_id")
        if self.engine == "codex" and self.checkpoint_id is not None:
            raise ValueError("Codex rollback is count-based")
        return self


class RollbackResult(_Base):
    """Structured, non-atomic restore result for the confirmation UI."""
    type: Literal["rollback_result"] = "rollback_result"
    session_id: WireId
    engine: Engine
    restore: RestoreMode
    conversation: RestoreOutcome
    files: RestoreOutcome
    restored_turns: int = Field(default=0, ge=0, le=1000)
    conflicts: list[str] = Field(default_factory=list, max_length=128)
    prefill_text: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    detail: Optional[str] = Field(default=None, max_length=4 * 1024)


class CompactSession(_Command):
    type: Literal["compact_session"] = "compact_session"
    session_id: WireId
    engine: Literal["claude", "codex", "dsh"] = "codex"
    space: Literal["code"] = "code"


class StartReview(_Command):
    type: Literal["start_review"] = "start_review"
    session_id: WireId
    engine: Literal["codex"] = "codex"
    space: Literal["code"] = "code"
    target: Literal["uncommittedChanges", "baseBranch", "commit", "custom"]
    value: Optional[str] = Field(default=None, max_length=16 * 1024)

    @model_validator(mode="after")
    def target_value_matches(self):
        value = (self.value or "").strip()
        if self.target == "uncommittedChanges":
            if value:
                raise ValueError("uncommittedChanges does not accept a value")
            self.value = None
        elif not value:
            raise ValueError(f"{self.target} requires a value")
        else:
            self.value = value
        return self


class WorkProjectInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: WireId
    name: str = Field(max_length=200)
    description: str = Field(max_length=16 * 1024)
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)


class WorkSourceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: WireId
    project_id: WireId
    kind: Literal["file", "link", "note"]
    title: str = Field(max_length=500)
    uri: Optional[str] = Field(default=None, max_length=16 * 1024)
    created_at: float = Field(ge=0)


class WorkPluginInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plugin_id: WireId
    project_id: Optional[WireId] = None
    name: str = Field(max_length=200)
    instructions: str = Field(max_length=64 * 1024)
    enabled: bool
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)


class WorkScheduleInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schedule_id: WireId
    project_id: Optional[WireId] = None
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None
    title: str = Field(max_length=200)
    prompt: str = Field(max_length=64 * 1024)
    next_run_at: float = Field(ge=0)
    repeat_seconds: Optional[int] = Field(default=None, ge=60, le=31_536_000)
    enabled: bool
    last_run_at: Optional[float] = Field(default=None, ge=0)
    last_session_id: Optional[WireId] = None
    last_error: Optional[str] = Field(default=None, max_length=2000)
    last_run_id: Optional[WireId] = None
    last_run_status: Optional[
        Literal["queued", "claimed", "running", "succeeded", "failed"]
    ] = None
    last_run_attempt: Optional[int] = Field(default=None, ge=0, le=100)
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)


class WorkDashboard(_Base):
    type: Literal["work_dashboard"] = "work_dashboard"
    engine: Engine
    projects: list[WorkProjectInfo] = Field(max_length=500)
    sources: list[WorkSourceInfo] = Field(max_length=5000)
    plugins: list[WorkPluginInfo] = Field(max_length=500)
    schedules: list[WorkScheduleInfo] = Field(max_length=500)


class GetWorkDashboard(_Command):
    type: Literal["get_work_dashboard"] = "get_work_dashboard"
    engine: Engine = "claude"


class CreateWorkProject(_Command):
    type: Literal["create_work_project"] = "create_work_project"
    engine: Engine = "claude"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=16 * 1024)


class DeleteWorkProject(_Command):
    type: Literal["delete_work_project"] = "delete_work_project"
    engine: Engine = "claude"
    project_id: WireId


class AddWorkSource(_Command):
    type: Literal["add_work_source"] = "add_work_source"
    engine: Engine = "claude"
    project_id: WireId
    kind: Literal["file", "link", "note"]
    title: str = Field(min_length=1, max_length=500)
    uri: Optional[str] = Field(default=None, max_length=16 * 1024)
    file: Optional[QueryFile] = None

    @model_validator(mode="after")
    def source_payload_matches_kind(self):
        if self.kind == "file" and self.file is None:
            raise ValueError("file Work source requires file")
        if self.kind != "file" and self.file is not None:
            raise ValueError("non-file Work source cannot contain file")
        if self.kind in {"link", "note"} and not (self.uri or "").strip():
            raise ValueError("Work source requires uri or note content")
        return self


class DeleteWorkSource(_Command):
    type: Literal["delete_work_source"] = "delete_work_source"
    engine: Engine = "claude"
    source_id: WireId


class CreateWorkPlugin(_Command):
    type: Literal["create_work_plugin"] = "create_work_plugin"
    engine: Engine = "claude"
    project_id: Optional[WireId] = None
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=64 * 1024)


class DeleteWorkPlugin(_Command):
    type: Literal["delete_work_plugin"] = "delete_work_plugin"
    engine: Engine = "claude"
    plugin_id: WireId


class CreateWorkSchedule(_Command):
    type: Literal["create_work_schedule"] = "create_work_schedule"
    engine: Engine = "claude"
    project_id: Optional[WireId] = None
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=64 * 1024)
    next_run_at: float = Field(ge=0)
    repeat_seconds: Optional[int] = Field(default=None, ge=60, le=31_536_000)

    @model_validator(mode="after")
    def profile_matches_engine(self):
        if self.engine != "codex" and self.codex_profile_id is not None:
            raise ValueError("Codex profile is only valid for Codex schedules")
        if self.engine != "claude" and self.claude_profile_id is not None:
            raise ValueError(
                "Claude profile is only valid for Claude schedules")
        return self


class DeleteWorkSchedule(_Command):
    type: Literal["delete_work_schedule"] = "delete_work_schedule"
    engine: Engine = "claude"
    schedule_id: WireId


class GetWorkArtifacts(_Command):
    type: Literal["get_work_artifacts"] = "get_work_artifacts"
    engine: Engine = "claude"
    session_id: WireId


class WorkArtifactInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: PreviewPath
    size: int = Field(ge=0)
    modified_at: float = Field(ge=0)
    kind: Literal["document", "spreadsheet", "presentation", "image", "pdf", "file"]
    previewable: bool = False


class WorkArtifacts(_Base):
    type: Literal["work_artifacts"] = "work_artifacts"
    engine: Engine
    session_id: WireId
    artifacts: list[WorkArtifactInfo] = Field(max_length=200)


class SessionFocus(_Base):
    """wrapper -> client: NON-destructive view change — the user switched which
    resident session they're viewing. The client just swaps its view to this
    session; turns are NOT cleared (they're already in memory).

    Focus-moving ONLY: sending this switches the client's view. A brand-new
    session captures its real cc id mid-turn — that is a re-key, NOT a focus
    change, so it uses SessionRekey (below), else a background session capturing
    its id would yank the user's view (focus-steal)."""
    type: Literal["session_focus"] = "session_focus"
    session_id: WireId
    cwd: Optional[str] = None
    # Echoed from NewSession so clients accept only the focus belonging to the
    # create request they initiated; ordinary switch confirmations leave it null.
    request_id: Optional[WireId] = None


class SessionRekey(_Base):
    """wrapper -> client: a resident session's pool key changed from a temp key
    (`tmp-<uuid>`, assigned to a brand-new session before its id is known) to
    its real cc session id, captured from the first ResultMessage/init. The
    client renames its runtime entry old_key -> session_id and migrates that
    session's replay cursor. This does NOT move focus — focus only follows if
    the client was already viewing old_key. Splitting this out from
    SessionFocus is what prevents focus-steal when a *background* new session
    captures its id."""
    type: Literal["session_rekey"] = "session_rekey"
    old_key: WireId
    session_id: WireId
    cwd: Optional[str] = None


class RenameSession(_Command):
    """client -> wrapper: set a session's custom title (appended to its jsonl)."""
    type: Literal["rename_session"] = "rename_session"
    session_id: WireId
    title: str = Field(min_length=1, max_length=200)
    engine: Optional[Engine] = None
    space: Space = "code"


class ArchiveSession(_Command):
    """client -> wrapper: toggle the "archived" tag on a session."""
    type: Literal["archive_session"] = "archive_session"
    session_id: WireId
    archived: bool
    engine: Optional[Engine] = None
    space: Space = "code"


class PinSession(_Command):
    """client -> wrapper: persist a cross-client sidebar pin preference."""
    type: Literal["pin_session"] = "pin_session"
    session_id: WireId
    pinned: bool
    engine: Optional[Engine] = None
    space: Space = "code"


class ForkSession(_Command):
    """client -> wrapper: persistently fork a session at one completed turn.

    The child inherits the source working directory. ``last_turn_id`` is the
    selected message's engine-specific ``TurnEnd.turn_id``: Codex turn id or
    Claude assistant transcript UUID. The field name remains unchanged on the
    v5 wire for backward compatibility.
    """
    type: Literal["fork_session"] = "fork_session"
    session_id: WireId
    request_id: WireId
    last_turn_id: WireId


class ForkSessionWorktree(_Command):
    """client -> wrapper: persistently fork a Codex thread into a new Git worktree.

    The wrapper chooses the target path and branch. ``request_id`` is stable
    across transport retries and therefore also keys deterministic worktree
    recovery.
    """
    type: Literal["fork_session_worktree"] = "fork_session_worktree"
    session_id: WireId
    request_id: WireId
    name: Optional[str] = Field(default=None, max_length=80)
    # Present for a message-level action. Omitted by the session menu, whose
    # historical behavior remains "fork the complete current thread".
    last_turn_id: Optional[WireId] = None


class SessionForked(_Base):
    """wrapper -> requesting client: a persistent engine fork is ready."""
    type: Literal["session_forked"] = "session_forked"
    parent_session_id: WireId
    session_id: WireId
    cwd: str = Field(min_length=1, max_length=4096)
    target: Literal["same_cwd", "worktree"]
    git_branch: Optional[str] = Field(default=None, min_length=1, max_length=500)
    last_turn_id: Optional[WireId] = None
    request_id: WireId


class MigrateSession(_Command):
    """client -> wrapper: continue a Codex thread in another cwd."""
    type: Literal["migrate_session"] = "migrate_session"
    session_id: WireId
    cwd: str = Field(min_length=1, max_length=4096)
    request_id: WireId


class SessionMigrated(_Base):
    """wrapper -> clients: the same session now uses a different cwd."""
    type: Literal["session_migrated"] = "session_migrated"
    session_id: WireId
    previous_cwd: str = Field(min_length=1, max_length=4096)
    cwd: str = Field(min_length=1, max_length=4096)
    request_id: WireId


# ---- directory picker (for creating a session in an arbitrary cwd) ----

class ListDir(_Command):
    """client -> wrapper: list subdirectories of a path on the wrapper host.
    Used by the directory picker when creating a session in an arbitrary cwd.
    path=None starts at $HOME."""
    type: Literal["list_dir"] = "list_dir"
    path: Optional[str] = Field(default=None, max_length=4096)


class DirList(_Base):
    """wrapper -> client: subdirectories of the requested path. One-shot like
    SessionList (not buffered/replayed). `parent` is the parent dir for the
    "go up" button; null at filesystem root. Hidden dirs (dotfiles) are omitted."""
    type: Literal["dir_list"] = "dir_list"
    path: str
    parent: Optional[str] = None
    dirs: list[dict[str, str]] = []  # each: {name, path}
    request_id: Optional[WireId] = None


# ---- model catalog (the engine is the source of truth, not the client) ----

class GetModels(_Command):
    """client -> wrapper: what models does this engine actually offer?

    `codex` answers with app-server's real catalog. Claude has no equivalent
    catalog RPC, but can resolve explicit no-override settings for a cwd;
    its model list therefore remains empty and the client keeps the static table.
    """
    type: Literal["get_models"] = "get_models"
    engine: Optional[Literal["cc", "claude", "codex", "dsh"]] = None
    client_id: Optional[WireId] = None  # requester, so the wrapper routes Models back to=<client_id>
    # Legacy Claude defaults can depend on project/local settings, so resolve
    # them in the prospective cwd. Explicit account profiles remain user-scoped.
    cwd: Optional[str] = Field(default=None, max_length=4096)
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class DshPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=256)
    description: str = Field(default="", max_length=4096)
    is_default: bool = False
    available: bool = True


class DshCommandInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4096)
    input_hint: Optional[str] = Field(default=None, max_length=1024)
    attachments: bool = False


class DshPermissionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=256)
    name: str = Field(max_length=256)
    description: str = Field(default="", max_length=4096)


class DshGoal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    objective: str = Field(max_length=65536)
    phase: Literal["active", "paused", "blocked", "complete"]
    rounds: int = Field(ge=0)
    max_rounds: int = Field(ge=1)
    blocked_reason: Optional[str] = Field(default=None, max_length=4096)
    activation: Optional[Literal["armed", "disarmed"]] = None


class DshState(_Base):
    """Native DSH controls. Replaced as a whole, never merged across sessions."""
    type: Literal["dsh_state"] = "dsh_state"
    connected: bool = True
    error: Optional[str] = Field(default=None, max_length=4096)
    agent_preset: Optional[str] = Field(default=None, max_length=256)
    commands: list[DshCommandInfo] = Field(default_factory=list, max_length=256)
    permissions: list[DshPermissionOption] = Field(default_factory=list, max_length=64)
    permission: Optional[str] = Field(default=None, max_length=256)
    goal: Optional[DshGoal] = None


class DshCommandResult(_Base):
    """Requester-only native command result; not a model answer or replay item."""
    type: Literal["dsh_command_result"] = "dsh_command_result"
    request_id: WireId
    status: Literal["success", "error", "unknown"]
    text: str = Field(default="", max_length=16384)


class SetDshControl(_Command):
    type: Literal["set_dsh_control"] = "set_dsh_control"
    sid: WireId
    kind: Literal["permission", "effort", "command"]
    value: str = Field(min_length=1, max_length=65536)
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[QueryFile]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)

    @model_validator(mode="after")
    def command_attachments(self):
        count = _attachment_count(self.images, self.files)
        if count > MAX_ATTACHMENT_COUNT or (count and self.kind != "command"):
            raise ValueError("DSH attachments require a bounded native command")
        return self


class Models(_Base):
    """wrapper -> client: the engine's model catalog (one-shot, like DirList — not
    seq'd/buffered). Each entry: {id, display_name, description, efforts,
    default_effort, is_default}. `efforts` is authoritative: turn/start does NOT
    validate the level (it accepts `bogus-zzz`), so offering one the model lacks
    only fails later inside the model API. Empty list = we couldn't read it; the
    client falls back to its static table rather than rendering nothing.

    `default_model`/`default_effort` are what a NEW no-override session starts on.
    They are NOT the focused session's controls (those are per-session events)."""
    type: Literal["models"] = "models"
    dsh_presets: list[DshPreset] = Field(default_factory=list, max_length=256)
    error: Optional[str] = Field(default=None, max_length=4096)
    engine: str
    models: list[dict[str, Any]] = []
    default_model: Optional[str] = Field(default=None, max_length=256)
    default_effort: Optional[str] = Field(default=None, max_length=64)
    # Echoes GetModels.cwd for cwd-sensitive Claude defaults so a late response
    # can never be rendered against a different directory in the new-chat form.
    cwd: Optional[str] = Field(default=None, max_length=4096)
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class GetEngineCapabilities(_Command):
    """Read the engine's real skill/plugin/app/MCP inventory on demand."""
    type: Literal["get_engine_capabilities"] = "get_engine_capabilities"
    engine: Engine
    space: Space = "code"
    client_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    skills_only: bool = False
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class ManageEnginePlugin(_Command):
    """Install or uninstall one plugin through the engine's native manager."""
    type: Literal["manage_engine_plugin"] = "manage_engine_plugin"
    engine: Engine
    action: Literal["install", "uninstall"]
    plugin_id: str = Field(min_length=1, max_length=512)
    space: Space = "code"
    client_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class ManageEngineSkill(_Command):
    """Create/remove a local skill, or toggle it through a native engine API."""
    type: Literal["manage_engine_skill"] = "manage_engine_skill"
    engine: Engine
    action: Literal["create", "remove", "enable", "disable"]
    skill_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=4096)
    instructions: Optional[str] = Field(default=None, max_length=128 * 1024)
    scope: Literal["user", "project"] = "user"
    space: Space = "code"
    client_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class ManageEngineHook(_Command):
    """Create/remove a Claude command hook. Codex hooks are read-only today."""
    type: Literal["manage_engine_hook"] = "manage_engine_hook"
    engine: Engine
    action: Literal["create", "remove"]
    hook_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    event: Optional[str] = Field(default=None, min_length=1, max_length=128)
    matcher: Optional[str] = Field(default=None, max_length=2048)
    command: Optional[str] = Field(default=None, max_length=16 * 1024)
    timeout: Optional[int] = Field(default=None, ge=1, le=3600)
    scope: Literal["user", "project"] = "user"
    space: Space = "code"
    client_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class EngineCapabilityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["skill", "plugin", "app", "mcp", "hook"]
    id: str = Field(max_length=512)
    name: str = Field(max_length=512)
    description: Optional[str] = Field(default=None, max_length=16 * 1024)
    enabled: Optional[bool] = None
    installed: Optional[bool] = None
    status: Optional[str] = Field(default=None, max_length=256)
    scope: Optional[str] = Field(default=None, max_length=256)
    source: Optional[str] = Field(default=None, max_length=256)
    tool_count: Optional[int] = Field(default=None, ge=0, le=100_000)
    resource_count: Optional[int] = Field(default=None, ge=0, le=100_000)
    install_url: Optional[str] = Field(default=None, max_length=4096)
    actions: list[Literal["install", "uninstall", "enable", "disable", "remove"]] = Field(
        default_factory=list, max_length=8)
    event: Optional[str] = Field(default=None, max_length=128)
    matcher: Optional[str] = Field(default=None, max_length=2048)
    handler_type: Optional[str] = Field(default=None, max_length=128)
    detail: Optional[str] = Field(default=None, max_length=4096)


class EngineCapabilities(_Base):
    type: Literal["engine_capabilities"] = "engine_capabilities"
    engine: Engine
    space: Space
    request_id: Optional[WireId] = None
    cwd: str = Field(max_length=4096)
    items: list[EngineCapabilityItem] = Field(max_length=2000)
    errors: list[str] = Field(default_factory=list, max_length=32)
    notes: list[str] = Field(default_factory=list, max_length=32)
    skills_only: bool = False
    claude_profile_id: Optional[WireId] = None
    codex_profile_id: Optional[WireId] = None


class SetPerm(_Command):
    """client -> wrapper: switch the cc session's permission mode (runtime, no reconnect)."""
    type: Literal["set_perm"] = "set_perm"
    mode: PermissionMode


class Perm(_Base):
    """The cc session's current permission mode. Downstream so a reconnecting
    client restores the readout."""
    type: Literal["perm"] = "perm"
    mode: str


class PermissionProfileInfo(BaseModel):
    """One cwd-aware named Codex permission profile from app-server."""
    model_config = ConfigDict(extra="forbid")

    id: PermissionProfileId
    description: Optional[str] = Field(default=None, max_length=2048)
    allowed: bool


class GetPermissionProfiles(_Command):
    """List profiles for a session, or for a prospective new-session cwd."""
    type: Literal["get_permission_profiles"] = "get_permission_profiles"
    client_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    codex_profile_id: Optional[WireId] = None


class PermissionProfiles(_Base):
    """wrapper -> requesting client: bounded cwd-aware profile catalog."""
    type: Literal["permission_profiles"] = "permission_profiles"
    profiles: list[PermissionProfileInfo] = Field(max_length=128)
    request_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    codex_profile_id: Optional[WireId] = None


class SetPermissionProfile(_Command):
    """client -> wrapper: select a named Codex permission profile."""
    type: Literal["set_permission_profile"] = "set_permission_profile"
    profile: PermissionProfileId


class PermissionProfile(_Base):
    """The Codex session's active named permission profile."""
    type: Literal["permission_profile"] = "permission_profile"
    profile: Optional[PermissionProfileId] = None


class SetWebSearch(_Command):
    """Select cached or live Codex search for the next turn."""
    type: Literal["set_web_search"] = "set_web_search"
    mode: WebSearchMode


class WebSearch(_Base):
    """The effective per-session Codex search mode."""
    type: Literal["web_search"] = "web_search"
    mode: WebSearchMode


class GetContext(_Command):
    """client -> wrapper: request current context window usage."""
    type: Literal["get_context"] = "get_context"
    # Automatic focus/TurnEnd reads stay cache-only so an optional Claude
    # control request can never block the next prompt. User-opened /context
    # explicitly asks for a fresh native breakdown.
    refresh: bool = False


class ContextReport(_Base):
    """wrapper -> client: context window usage (one-shot response to GetContext,
    like SessionList — not buffered)."""
    type: Literal["context_report"] = "context_report"
    # Correlate one-shot reports with their GetContext command. Internal
    # wrapper-owned refreshes omit this field; browsers may still consume their
    # value but must not let them settle a newer explicit request.
    request_id: Optional[WireId] = None
    total_tokens: int
    max_tokens: int
    percentage: float
    # Codex does not expose tokenUsage immediately after an excludeTurns resume.
    # Keep the numeric fields for wire compatibility, but mark the reading as
    # unavailable instead of presenting a fabricated 0% to the user.  ``None``
    # is omitted so older Code reports retain their exact historical shape.
    available: Optional[bool] = None
    # Claude may fall back to the most recent exact control response or the
    # newest main-chain assistant usage. Omitted reports retain the historical
    # exact/control meaning (including all Codex reports).
    source: Optional[
        Literal["control", "cached_control", "recent_turn"]
    ] = None
    # Work reports keep the engine's real context usage above for honest
    # remaining-capacity calculations, while exposing the fresh-session startup
    # zero point separately so Work shows later conversation growth.
    # Code omits these fields and retains the historical wire contract.
    session_tokens: Optional[int] = None
    fixed_tokens: Optional[int] = None
    session_percentage: Optional[float] = None
    model: Optional[str] = None
    is_auto_compact_enabled: Optional[bool] = None
    # Claude's effective automatic-compaction boundary and physical context
    # capacity. They are observations from get_context_usage(), not substitutes
    # for the desired/applied session control carried by AutoCompact.
    auto_compact_threshold_tokens: Optional[int] = Field(default=None, ge=0)
    raw_max_tokens: Optional[int] = Field(default=None, ge=0)
    categories: list[dict[str, Any]] = []


class GetStatus(_Command):
    """client -> wrapper: read the authoritative Codex app-server status.

    Unlike GetContext this is a composed, one-shot control response. It never
    enters transcript history and is safe for the reliable command layer to
    re-run when a response/ACK is lost.
    """
    type: Literal["get_status"] = "get_status"


class ConsumeRateLimitResetCredit(_Command):
    """client -> wrapper: redeem one Codex account reset credit.

    ``cmd_id`` is also the native app-server idempotency key, so a transport
    retry or wrapper restart cannot redeem the same logical action twice.
    Omitting ``credit_id`` lets the account backend select the next available
    reset credit.
    """
    type: Literal[
        "consume_rate_limit_reset_credit"
    ] = "consume_rate_limit_reset_credit"
    sid: WireId
    cmd_id: WireId
    client_id: WireId
    credit_id: Optional[ResetCreditId] = None


class _StatusPart(BaseModel):
    """Strict nested payload shared by StatusReport's allow-listed sections."""
    model_config = ConfigDict(extra="forbid")


class StatusThread(_StatusPart):
    thread_id: WireId
    session_id: Optional[WireId] = None
    cwd: Optional[str] = Field(default=None, max_length=4096)
    source: Optional[Literal[
        "cli", "vscode", "exec", "appServer", "unknown", "custom", "subAgent",
    ]] = None
    cli_version: Optional[str] = Field(default=None, max_length=128)
    status: Literal["notLoaded", "idle", "systemError", "active", "unknown"] = "unknown"
    active_flags: list[Literal[
        "waitingOnApproval", "waitingOnUserInput",
    ]] = Field(default_factory=list, max_length=8)
    ephemeral: Optional[bool] = None
    created_at: Optional[int] = Field(default=None, ge=0)
    updated_at: Optional[int] = Field(default=None, ge=0)


class StatusRuntime(_StatusPart):
    app_server_version: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=256)
    model_provider: Optional[str] = Field(default=None, max_length=256)
    reasoning_effort: Optional[str] = Field(default=None, max_length=64)
    service_tier: Optional[str] = Field(default=None, max_length=64)
    approval_policy: Optional[str] = Field(default=None, max_length=64)
    permission_profile: Optional[str] = Field(default=None, max_length=256)
    sandbox_mode: Optional[str] = Field(default=None, max_length=64)
    web_search: Optional[str] = Field(default=None, max_length=64)


class StatusContext(_StatusPart):
    used_tokens: Optional[int] = Field(default=None, ge=0)
    max_tokens: Optional[int] = Field(default=None, ge=0)
    percentage: Optional[float] = Field(default=None, ge=0)


class StatusAccount(_StatusPart):
    auth_type: Literal["apiKey", "chatgpt", "amazonBedrock", "unknown"]
    plan_type: Optional[str] = Field(default=None, max_length=128)
    requires_openai_auth: bool


class StatusRateLimitWindow(_StatusPart):
    used_percent: Optional[int] = Field(default=None, ge=0)
    resets_at: Optional[int] = Field(default=None, ge=0)
    window_duration_mins: Optional[int] = Field(default=None, ge=0)


class StatusRateLimit(_StatusPart):
    limit_id: Optional[str] = Field(default=None, max_length=128)
    limit_name: Optional[str] = Field(default=None, max_length=256)
    plan_type: Optional[str] = Field(default=None, max_length=128)
    rate_limit_reached_type: Optional[str] = Field(default=None, max_length=128)
    primary: Optional[StatusRateLimitWindow] = None
    secondary: Optional[StatusRateLimitWindow] = None


class StatusRateLimitResetCredit(_StatusPart):
    id: ResetCreditId
    granted_at: int = Field(ge=0, le=MAX_SAFE_WIRE_TIMESTAMP_SECONDS)
    expires_at: Optional[int] = Field(
        default=None, ge=0, le=MAX_SAFE_WIRE_TIMESTAMP_SECONDS,
    )
    reset_type: Literal["codexRateLimits", "unknown"]
    status: Literal["available", "redeeming", "redeemed", "unknown"]
    title: Optional[str] = Field(default=None, max_length=256)
    description: Optional[str] = Field(default=None, max_length=2048)


class StatusRateLimitResetCredits(_StatusPart):
    available_count: int = Field(ge=0, le=MAX_SAFE_WIRE_INTEGER)
    # ``None`` means the backend exposed only the count. An empty list means it
    # did return detail rows and none were available. The list may be capped.
    credits: Optional[list[StatusRateLimitResetCredit]] = Field(
        default=None, max_length=MAX_STATUS_RESET_CREDITS,
    )


class StatusDailyUsageBucket(_StatusPart):
    start_date: Annotated[
        str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ]
    tokens: int = Field(ge=0, le=MAX_SAFE_WIRE_INTEGER)


class StatusUsage(_StatusPart):
    lifetime_tokens: Optional[int] = Field(default=None, ge=0)
    peak_daily_tokens: Optional[int] = Field(default=None, ge=0)
    current_streak_days: Optional[int] = Field(default=None, ge=0)
    longest_streak_days: Optional[int] = Field(default=None, ge=0)
    longest_running_turn_sec: Optional[int] = Field(default=None, ge=0)
    daily_usage_buckets: list[StatusDailyUsageBucket] = Field(
        default_factory=list, max_length=MAX_STATUS_USAGE_BUCKETS,
    )


class StatusReport(_Base):
    """wrapper -> client: sanitized Codex app-server status snapshot.

    Every nested model forbids extras. The wrapper copies only explicitly
    approved display fields; account email, credentials, config instructions,
    rollout paths/previews and paid credit balances never cross the wire.
    Earned rate-limit reset credits are a separate, explicitly allow-listed
    control surface. Daily activity is limited to validated date/token pairs
    for the latest 53 weeks.
    ``component_errors`` makes partial RPC failure visible without hiding the
    successful sections.
    """
    type: Literal["status_report"] = "status_report"
    # Correlates this snapshot with the reliable GetStatus command that
    # requested it.  Optional for unsolicited/legacy reports.
    request_id: Optional[WireId] = None
    thread: StatusThread
    runtime: StatusRuntime
    context: StatusContext
    account: Optional[StatusAccount] = None
    rate_limits: list[StatusRateLimit] = Field(default_factory=list, max_length=16)
    reset_credits: Optional[StatusRateLimitResetCredits] = None
    usage: Optional[StatusUsage] = None
    # Entries are ``<component>: <generic reason>``. Raw RPC error text is never
    # allowed, because it may contain provider or account details.
    component_errors: list[StatusErrorText] = Field(default_factory=list, max_length=5)


class RateLimitResetResult(_Base):
    """Private result of one idempotent reset-credit redemption attempt."""
    type: Literal["rate_limit_reset_result"] = "rate_limit_reset_result"
    sid: WireId
    to: WireId
    request_id: WireId
    outcome: RateLimitResetOutcome
    credit_id: Optional[ResetCreditId] = None


class Notice(_Base):
    """Ephemeral, per-session app-server notice.

    Notices are control-plane UI state rather than transcript narrative: the
    wrapper routes them to one resident session but never puts them in history
    or its replay ring.  Every text field is deliberately bounded before it can
    reach a browser.
    """
    type: Literal["notice"] = "notice"
    notice_id: WireId
    severity: NoticeSeverity
    category: NoticeCategory
    title: NoticeTitle
    message: NoticeMessage
    detail: Optional[NoticeDetail] = None
    thread_id: Optional[WireId] = None


class RateLimitUpdate(_Base):
    """Sparse-safe, sanitized rolling engine rate-limit state.

    Codex app-server credits/spend controls and Claude SDK raw/account fields
    are intentionally absent. Names differ slightly from StatusReport so the
    live event remains concise; the Web reducer projects both engines into the
    shared StatusRateLimit view model.
    """
    type: Literal["rate_limit_update"] = "rate_limit_update"
    limit_id: Optional[str] = Field(default=None, max_length=128)
    name: Optional[str] = Field(default=None, max_length=256)
    plan_type: Optional[str] = Field(default=None, max_length=128)
    reached_type: Optional[str] = Field(default=None, max_length=128)
    primary: Optional[StatusRateLimitWindow] = None
    secondary: Optional[StatusRateLimitWindow] = None

    @model_validator(mode="after")
    def has_public_field(self):
        if all(getattr(self, field) is None for field in (
            "limit_id", "name", "plan_type", "reached_type", "primary", "secondary",
        )):
            raise ValueError("rate_limit_update requires a public field")
        return self


class GetDiff(_Command):
    """client -> wrapper: request a git diff (context + line numbers) for a file.
    `theme` picks delta's light/dark rendering so the panel matches the app."""
    type: Literal["get_diff"] = "get_diff"
    file: str = Field(max_length=4096)
    theme: Literal["light", "dark"] = "light"
    turn_id: Optional[WireId] = None
    revision: Optional[str] = Field(default=None, min_length=1, max_length=64)
    engine: Optional[Literal["claude", "codex", "dsh"]] = None

    @model_validator(mode="after")
    def require_complete_archive_identity(self):
        fields = (self.turn_id, self.revision, self.engine)
        if any(value is not None for value in fields) and not all(value is not None for value in fields):
            raise ValueError("historical diff requires turn_id, revision and engine together")
        return self


class GetTurnFileChanges(_Command):
    """Read an immutable file-index page without resuming the engine."""
    type: Literal["get_turn_file_changes"] = "get_turn_file_changes"
    sid: WireId
    engine: Literal["claude", "codex", "dsh"]
    turn_id: WireId
    revision: str = Field(min_length=1, max_length=64)
    offset: int = Field(default=0, ge=0, le=4096, strict=True)
    limit: int = Field(default=64, ge=1, le=64, strict=True)


class TurnFileChangesPage(_Base):
    """Private one-shot response; never retained in the live replay ring."""
    type: Literal["turn_file_changes_page"] = "turn_file_changes_page"
    engine: Literal["claude", "codex", "dsh"]
    turn_id: WireId
    revision: str = Field(min_length=1, max_length=64)
    offset: int = Field(ge=0, le=4096)
    files: list[TurnFileChange] = Field(max_length=64)
    total_files: int = Field(ge=0, le=4096)
    next_offset: Optional[int] = Field(default=None, ge=1, le=4096)
    request_id: Optional[WireId] = None


class DiffReport(_Base):
    """wrapper -> client: git diff text (one-shot, like ContextReport)."""
    type: Literal["diff_report"] = "diff_report"
    file: str
    diff: str
    request_id: Optional[WireId] = None


class BrowseFiles(_Command):
    """List one session directory without starting or resuming an engine."""

    type: Literal["browse_files"] = "browse_files"
    path: str = Field(default=".", max_length=4096)
    request_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0, le=20_000)
    limit: int = Field(default=100, ge=1, le=100)
    hidden: bool = False
    revision: Optional[str] = Field(default=None, max_length=128)


class WorkspaceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=1024)
    path: str = Field(max_length=4096)
    kind: Literal["directory", "file", "unsupported"]


class FilesListed(_Base):
    type: Literal["files_listed"] = "files_listed"
    request_id: str
    root: str = ""
    path: str = ""
    kind: Literal["directory", "file"] = "directory"
    parent: Optional[str] = None
    entries: list[WorkspaceEntry] = Field(default_factory=list, max_length=100)
    revision: Optional[str] = None
    next_offset: Optional[int] = None
    error: Optional[str] = None


class GetFilePreview(_Command):
    """client -> wrapper: read one authorized, bounded file preview."""
    type: Literal["get_file_preview"] = "get_file_preview"
    path: PreviewPath
    request_id: WireId


class FilePreview(_Base):
    """wrapper -> requesting client: bounded source or locally-rendered artifact.

    Binary previews are transported directly through the authenticated relay;
    the relay never writes them to disk. Office files are converted by the
    wrapper host and report their original extension in ``converted_from``.
    """
    type: Literal["file_preview"] = "file_preview"
    path: PreviewPath
    request_id: WireId
    format: Literal["markdown", "text", "html", "image", "pdf", "audio"] = "text"
    content: PreviewContent = ""
    media_type: Optional[Literal[
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
        "image/svg+xml", "application/pdf",
        "audio/wav", "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac",
        "audio/ogg", "audio/webm",
    ]] = None
    data: Optional[ArtifactPreviewData] = None
    converted_from: Optional[str] = Field(default=None, max_length=16)
    size: int = Field(default=0, ge=0)
    truncated: bool = False
    mtime_ns: FileMtimeNs = "0"
    revision: Optional[FileRevision] = None
    writable: bool = True
    error: Optional[str] = Field(default=None, max_length=512)


class SaveMarkdown(_Command):
    """client -> wrapper: atomically replace one existing Markdown file.

    The three expected fields form an optimistic concurrency guard. A stale
    editor receives ``conflict`` and never overwrites the newer file.
    """
    type: Literal["save_markdown"] = "save_markdown"
    path: PreviewPath
    request_id: WireId
    content: PreviewContent
    expected_size: int = Field(ge=0, le=FILE_PREVIEW_MAX_BYTES)
    expected_mtime_ns: FileMtimeNs
    expected_revision: FileRevision


class FileSaveResult(_Base):
    """wrapper -> requesting client: correlated Markdown save outcome."""
    type: Literal["file_save_result"] = "file_save_result"
    path: PreviewPath
    request_id: WireId
    status: Literal["saved", "conflict", "error"]
    size: int = Field(default=0, ge=0)
    mtime_ns: FileMtimeNs = "0"
    revision: Optional[FileRevision] = None
    error: Optional[str] = Field(default=None, max_length=512)


class GetPreviewAsset(_Command):
    """client -> wrapper: load one image referenced by an open Markdown preview."""
    type: Literal["get_preview_asset"] = "get_preview_asset"
    path: PreviewPath
    preview_id: WireId
    request_id: WireId


class PreviewAsset(_Base):
    """wrapper -> requesting client: bounded base64 image for one preview."""
    type: Literal["preview_asset"] = "preview_asset"
    path: PreviewPath
    preview_id: WireId
    request_id: WireId
    media_type: Optional[Literal[
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
        "image/svg+xml",
    ]] = None
    data: Optional[PreviewAssetData] = None
    error: Optional[str] = Field(default=None, max_length=512)


class PreviewAuthorizationRequired(_Base):
    """One external file needs an explicit requester-local user gesture."""
    type: Literal[
        "preview_authorization_required"
    ] = "preview_authorization_required"
    authorization_id: WireId
    request_id: WireId
    operation: Literal["file_preview", "preview_asset"]
    path: PreviewPath
    resolved_path: PreviewPath
    format: Literal["markdown", "text", "html", "image", "pdf", "audio"] = "text"
    preview_id: Optional[WireId] = None


class AuthorizePreview(_Command):
    """Answer one exact, short-lived external preview challenge."""
    type: Literal["authorize_preview"] = "authorize_preview"
    authorization_id: WireId
    request_id: WireId
    decision: Literal["allow", "deny"]


class PreviewAuthorizationResult(_Base):
    """Small result; after a grant the browser recreates the bounded read."""
    type: Literal[
        "preview_authorization_result"
    ] = "preview_authorization_result"
    authorization_id: WireId
    request_id: WireId
    operation: Optional[Literal["file_preview", "preview_asset"]] = None
    path: Optional[PreviewPath] = None
    status: Literal["granted", "denied", "expired", "changed", "error"]
    preview_id: Optional[WireId] = None
    error: Optional[str] = Field(default=None, max_length=512)


class GetHistory(_Command):
    """client -> wrapper: request a source-bound materialized history page.

    This never uses the live ring and never requires a resident engine. Web
    clients request the lightweight canonical summary; compatibility clients
    may still request the translated full event page. ``before``/``limit`` page
    older turns.
    """
    type: Literal["get_history"] = "get_history"
    session_id: WireId
    client_id: Optional[WireId] = None  # requester, so the wrapper routes History back to=<client_id>
    cwd: Optional[str] = Field(default=None, max_length=4096)
    # Stable cursor for the oldest loaded turn: a user msg_id normally, or the
    # authoritative engine turn_id for an assistant-only automatic continuation.
    before: Optional[WireId] = None
    limit: Optional[int] = Field(default=None, ge=1, le=200)
    detail: Literal["summary", "full"] = "full"


class ConversationTurn(BaseModel):
    """Canonical lightweight turn rendered without replaying raw events."""
    model_config = ConfigDict(extra="forbid")
    id: WireId
    clientMsgId: Optional[WireId] = None
    prompt: str = Field(default="", max_length=128 * 1024)
    blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    done: bool = False
    forkPointId: Optional[WireId] = None
    checkpointId: Optional[WireId] = None
    interrupted: Optional[bool] = None
    error: Optional[str] = Field(default=None, max_length=64 * 1024)
    images: Optional[list[QueryImage]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)
    imageRefs: Optional[list[ConversationImageRef]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[UserFileMeta]] = Field(
        default=None, max_length=MAX_ATTACHMENT_COUNT)
    ts: Optional[int] = Field(default=None, ge=0)
    doneTs: Optional[int] = Field(default=None, ge=0)
    durationMs: Optional[int] = Field(default=None, ge=0)
    # Detail size and process visibility are deliberately separate. A deferred
    # payload may contain only a truncated prompt/final answer (or an image),
    # while an official Codex summary may not reveal whether process items
    # exist at all. The browser must never turn either case into a fake
    # "processed" row.
    processDetailState: Literal["none", "present", "unknown"] = "none"
    detailReasons: list[Literal[
        "process", "prompt_truncated", "answer_truncated", "image_deferred",
    ]] = Field(default_factory=list, max_length=4)
    processStartedTs: Optional[int] = Field(default=None, ge=0)
    processDoneTs: Optional[int] = Field(default=None, ge=0)
    detailEventCount: int = Field(default=0, ge=0)
    detailLoaded: bool = False
    fileChanges: Optional[TurnChangeSummary] = None


class CodexTerminalFence(BaseModel):
    """Source-bound Codex lifecycle fact independent of History content.

    App-server's terminal notification is authoritative, but a large rollout's
    materialized History page can briefly lag behind it.  A newest-page History
    carries these small exact-turn fences so a reconnect can close the already
    painted row without rescanning or guessing from the last open turn.
    """
    model_config = ConfigDict(extra="forbid")
    turn_id: WireId
    status: Literal["completed", "interrupted", "failed"]
    duration_ms: Optional[int] = Field(
        default=None, ge=0, le=MAX_SAFE_WIRE_INTEGER)
    completed_at: Optional[float] = Field(
        default=None, ge=0, le=MAX_SAFE_WIRE_TIMESTAMP_SECONDS)


class History(_Base):
    """wrapper -> client: one summary or compatibility event page.

    The frame is one-shot and requester-routed, not seq'd/buffered. Summary
    pages carry canonical ``turns`` plus small control rows; full pages carry
    translated ``events``. Live incomplete turns are reconciled client-side.
    """
    type: Literal["history"] = "history"
    session_id: WireId
    # Boot-scoped authoritative transcript revision.  Browsers persist this
    # beside cached turns. A change replaces completed cache state unless an
    # explicit same-generation continuity boundary proves only aliases changed.
    # Wrapper restart and destructive rewind never preserve that continuity.
    revision: WireId
    # Wrapper lifetime that owns build_seq. It lets clients reject a pre-
    # rollback response across revision epochs while still accepting build_seq
    # restarting from one after a real wrapper restart.
    generation: Optional[WireId] = None
    # Stable only across additive message-ID alias updates. A source-family
    # switch, rollback or wrapper restart starts a new continuity boundary.
    # This is not a pagination/detail revision: those still use `revision`.
    continuity_revision: Optional[WireId] = None
    # Monotonic per-session sequence for newest-page builds. Pagination echoes
    # the sequence of the newest page it belongs to, so a browser can reject an
    # older first page without rejecting a valid older page from the same view.
    build_seq: int = Field(default=0, ge=0)
    # Resident-session downstream sequence captured before transcript I/O.
    # When the browser has already consumed a newer live event, this History is
    # still useful for merging older rows but cannot delete the newer live tail.
    live_seq: Optional[int] = Field(default=None, ge=0)
    # False means this frame cannot replace the canonical transcript projection:
    # either `error` describes a parse/read failure, or its populated page is a
    # sampled/changed-during-read preview while an exact refresh runs in background.
    authoritative: bool = True
    error: Optional[str] = Field(default=None, max_length=4096)
    events: list[dict[str, Any]] = Field(default_factory=list)
    turns: list[ConversationTurn] = []
    detail: Literal["summary", "full"] = "full"
    has_more: bool = False            # older turns exist beyond what's returned (pagination)
    oldest_id: Optional[str] = None   # first returned stable turn cursor
    newest_id: Optional[str] = None   # last returned stable turn cursor
    before: Optional[str] = None      # echoes the request's `before`: set => this is an OLDER page (client prepends)
    # Control is revisioned independently from transcript history/build_seq.
    # Browsers may accept a newer control snapshot even when this History's
    # narrative page is stale or non-authoritative.
    control: Optional[SessionControl] = None
    # True => this session's transcript is being appended to by an EXTERNAL process
    # (a native `claude`/`codex` in the user's terminal), not by us. The wrapper
    # mirrors those appends by broadcasting a fresh History; the client renders the
    # session READ-ONLY, since a cc session has a single owner and typing here would
    # fork the conversation.
    external: bool = False
    # Authoritative current takeover intent. Unlike TakeoverState this survives
    # a GetHistory refresh without becoming replayable transcript narrative.
    takeover_pending: bool = False
    # True while the resident wrapper context is running/interrupting. Claude's
    # transcript has no ResultMessage, so the final History TurnEnd is synthetic;
    # clients must not let it close their matching live tail while this is true.
    in_progress: bool = False
    # Exact native/logical Codex ids whose persisted interrupted terminal is a
    # context-compaction boundary for the currently resident managed turn. This
    # is deliberately narrower than ``in_progress``/active turn ownership: a
    # real user interrupt or crash must stay terminal on a cold browser.
    compaction_continuation_turn_ids: list[WireId] = Field(
        default_factory=list, max_length=4)
    # Exact native terminal facts are a separate lifecycle projection.  They
    # may close a stale/incomplete narrative page, but never identify a target
    # by array position and never replace live TurnEnd notifications.
    terminal_fences: list[CodexTerminalFence] = Field(
        default_factory=list, max_length=16)
    # Authoritative replacement after a destructive history mutation such as
    # Codex rollback. Ordinary loads merge with a live tail; reset loads must
    # discard turns that the engine has just removed.
    reset: bool = False


class GetTurnDetail(_Command):
    """client -> wrapper: fetch heavyweight records for one visible turn."""
    type: Literal["get_turn_detail"] = "get_turn_detail"
    session_id: WireId
    turn_id: WireId
    client_id: Optional[WireId] = None
    revision: Optional[WireId] = None
    # Opaque, revision-bound cursor returned by a newer TurnDetail page.
    before: Optional[WireId] = None
    limit: int = Field(default=192, ge=1, le=256)


class TurnDetail(_Base):
    """wrapper -> requester: one translated event page for a visible turn."""
    type: Literal["turn_detail"] = "turn_detail"
    session_id: WireId
    turn_id: WireId
    revision: WireId
    authoritative: bool = True
    error: Optional[str] = Field(default=None, max_length=4096)
    # The requested opaque page no longer has its immutable backing snapshot.
    # Browsers must restart this turn at before=None rather than retrying the
    # same cursor or treating a silently substituted newest page as older.
    reset_required: bool = False
    events: list[dict[str, Any]] = Field(default_factory=list)
    has_more: bool = False
    oldest_cursor: Optional[WireId] = None
    has_newer: bool = False
    newer_cursor: Optional[WireId] = None
    before: Optional[WireId] = None


class GetAgentDetail(_Command):
    """client -> wrapper: fetch one Claude subagent's public process projection."""
    type: Literal["get_agent_detail"] = "get_agent_detail"
    session_id: WireId
    run_id: WireId
    request_id: WireId
    client_id: Optional[WireId] = None
    revision: Optional[WireId] = None
    detail_revision: Optional[WireId] = None
    before: Optional[WireId] = None
    limit: int = Field(default=192, ge=1, le=256)


class AgentDetail(_Base):
    """wrapper -> browser: one read-only page from a Claude subagent run.

    ``run_id`` is a stable public hash of the spawning Agent tool call. Raw
    Claude agent ids, delegated prompts and output-file paths never cross this
    boundary. Live batches are unbuffered hints; a normal response is always a
    source-backed or resident authoritative snapshot.
    """
    type: Literal["agent_detail"] = "agent_detail"
    session_id: WireId
    run_id: WireId
    request_id: Optional[WireId] = None
    revision: WireId
    detail_revision: WireId
    authoritative: bool = True
    live: bool = False
    title: str = Field(default="协作代理", min_length=1, max_length=1024)
    parent_run_id: Optional[WireId] = None
    status: ProcessStatus = "unknown"
    error: Optional[str] = Field(default=None, max_length=4096)
    events: list[dict[str, Any]] = Field(default_factory=list)
    through_seq: int = Field(default=0, ge=0)
    has_more: bool = False
    oldest_cursor: Optional[WireId] = None
    has_newer: bool = False
    newer_cursor: Optional[WireId] = None
    before: Optional[WireId] = None


class GetHistoryImage(_Command):
    """client -> wrapper: fetch one indexed historical user image."""
    type: Literal["get_history_image"] = "get_history_image"
    session_id: WireId
    turn_id: WireId
    image_id: WireId
    variant: Literal["thumbnail", "full"]
    request_id: WireId
    client_id: Optional[WireId] = None
    revision: Optional[WireId] = None


class HistoryImage(_Base):
    """wrapper -> requester: correlated thumbnail or full historical image."""
    type: Literal["history_image"] = "history_image"
    session_id: WireId
    turn_id: WireId
    image_id: WireId
    variant: Literal["thumbnail", "full"]
    request_id: WireId
    revision: WireId
    media_type: Optional[Literal[
        "image/png", "image/jpeg", "image/jpg", "image/webp",
    ]] = None
    width: Optional[int] = Field(default=None, ge=1, le=8192)
    height: Optional[int] = Field(default=None, ge=1, le=8192)
    data: Optional[AttachmentData] = None
    error: Optional[str] = Field(default=None, max_length=512)


class HistoryInvalidated(_Base):
    """Small replayable barrier emitted before destructive history replacement.

    A complete History frame can exceed the bounded ring and is intentionally
    one-shot. This marker remains replayable, so an offline client always drops
    turns removed by rollback before its next transcript refresh is merged.
    """

    type: Literal["history_invalidated"] = "history_invalidated"
    session_id: WireId
    revision: WireId
    reason: Literal["rollback"] = "rollback"


class ArtifactInvalidated(_Base):
    """Replayable barrier for previews made stale by workspace mutations."""

    type: Literal["artifact_invalidated"] = "artifact_invalidated"
    session_id: WireId
    reason: Literal["rollback", "session_migration"] = "rollback"


class AskUser(_Base):
    """wrapper -> client: the agent called the `ask_user` MCP tool and is
    blocked awaiting the user's choice. The client renders a question card;
    the user's pick is returned via AnswerQuestion. ask_id correlates the two.
    The wrapper's MCP handler awaits a Future keyed by ask_id."""
    type: Literal["ask_user"] = "ask_user"
    ask_id: WireId
    question: AskQuestionText
    header: Optional[str] = Field(default=None, max_length=512)
    options: list[AskOption] = Field(default_factory=list, max_length=ASK_OPTION_MAX_COUNT)
    allow_text: bool = False
    secret: bool = False
    multi_select: bool = False

    @model_validator(mode="after")
    def choices_or_text(self):
        if not self.allow_text and len(self.options) < ASK_OPTION_MIN_COUNT:
            raise ValueError("ask_user requires 2-5 options unless text input is enabled")
        if self.multi_select and len(self.options) < ASK_OPTION_MIN_COUNT:
            raise ValueError("multi-select ask_user requires 2-5 options")
        return self


class AskUserSync(_Base):
    """Wrapper -> one client: authoritative pending-question baseline.

    This unsequenced Hello frame means that every AskUser immediately following
    it for the same ``sid`` is the complete set visible to that client. It is
    deliberately independent of the replay ring: a retained historical ask is
    not evidence that the question is still open.
    """
    type: Literal["ask_user_sync"] = "ask_user_sync"


class AskUserClosed(_Base):
    """wrapper -> client: replayable terminal boundary for an AskUser card."""
    type: Literal["ask_user_closed"] = "ask_user_closed"
    ask_id: WireId
    reason: Literal["answered", "cancelled", "timeout", "superseded"]


class AnswerQuestion(_Command):
    """client -> wrapper: the user's answer to an AskUser prompt. answer is the
    selected option's label (or free text if the agent allowed it)."""
    type: Literal["answer_question"] = "answer_question"
    ask_id: WireId
    answer: AskAnswer


class GetGoal(_Command):
    type: Literal["get_goal"] = "get_goal"


class SetGoal(_Command):
    type: Literal["set_goal"] = "set_goal"
    objective: Optional[str] = Field(default=None, max_length=16 * 1024)
    status: Optional[GoalStatus] = None
    token_budget: Optional[int] = Field(default=None, ge=1)


class ClearGoal(_Command):
    type: Literal["clear_goal"] = "clear_goal"


class DismissGoal(_Command):
    """Hide one exact Goal generation on every connected client."""

    type: Literal["dismiss_goal"] = "dismiss_goal"
    goal_id: WireId


class AcknowledgeCompletion(_Command):
    """Mark one exact main-session completion as seen across clients."""

    type: Literal["acknowledge_completion"] = "acknowledge_completion"
    completion_id: WireId


class ThreadGoal(BaseModel):
    """Display-safe goal state shared by the Codex and Claude engines.

    Keep this model explicit and extra-forbidden: Codex's app-server goal API is
    experimental, so blindly forwarding its response would make every future
    server field part of cc-remote's public wire protocol.  Claude's transcript
    bridge uses the same base fields and only the four lifecycle extensions
    below.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    threadId: WireId
    objective: str = Field(min_length=1, max_length=16 * 1024)
    status: GoalStatus
    engine: Literal["claude", "codex", "dsh"]
    tokenBudget: Optional[int] = Field(default=None, ge=1)
    tokensUsed: int = Field(ge=0)
    timeUsedSeconds: int = Field(ge=0)
    # Codex emits integer epoch seconds; Claude's transcript bridge preserves
    # sub-second timestamps. Both are safe wire numbers and intentionally
    # accepted without coercing strings/bools under strict mode.
    createdAt: Optional[Union[int, float]] = Field(default=None, ge=0)
    updatedAt: Optional[Union[int, float]] = Field(default=None, ge=0)
    # Claude Code native /goal lifecycle extensions.
    iterations: Optional[int] = Field(default=None, ge=0)
    lastReason: Optional[str] = Field(default=None, max_length=16 * 1024)
    setAt: Optional[float] = Field(default=None, ge=0)
    tokensAtStart: Optional[int] = Field(default=None, ge=0)


class GoalState(_Base):
    """Authoritative engine goal. goal=None means no active goal.

    Both engines expose the common camelCase fields used by Codex
    (threadId/objective/status/tokensUsed/timeUsedSeconds). Claude may add
    iterations, lastReason, setAt, and tokensAtStart.
    """
    type: Literal["goal_state"] = "goal_state"
    goal: Optional[ThreadGoal] = None
    # Opaque identity for the exact objective generation. DismissGoal echoes it
    # so a delayed click can never hide a replacement Goal.
    goal_id: Optional[WireId] = None
    dismissed: bool = False
    # Present only on the one-shot GetGoal response. Broadcast mutations omit
    # it, so clients can freeze request-time surface ownership without changing
    # the normal per-session Goal stream.
    request_id: Optional[WireId] = None


class CompletionState(_Base):
    """Authoritative cross-client unread state for a main-session turn."""

    type: Literal["completion_state"] = "completion_state"
    completion_id: Optional[WireId] = None
    unread: bool = False
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def unread_requires_identity(self):
        if self.unread and self.completion_id is None:
            raise ValueError("unread completion state requires completion_id")
        return self


AnyMessage = Union[
    BrowseFiles, FilesListed, SetCodexContext, CodexContext,
    GetTurnFileChanges, TurnFileChangesPage,
    Hello, Query, CancelQueuedQuery, GetQueuedQuery, QueuedQueryDetail, UpdateQueuedQuery, QueuedQueryUpdated, QueryQueueState, Steer, Interrupt, Takeover, TakeoverState, SessionControl, SetModel, SetEffort, SetAutoCompact, SetServiceTier, SetCollaborationMode, SetPerm, GetPermissionProfiles, SetPermissionProfile, SetWebSearch, Fast, CollaborationMode, OpenBtw, CloseBtw, SyncBtw, BtwOpened, BtwSync, BtwClosed, GetContext, GetStatus, ConsumeRateLimitResetCredit, GetDiff, GetFilePreview, SaveMarkdown, GetPreviewAsset, AuthorizePreview, GetHistory, GetTurnDetail, GetAgentDetail, GetHistoryImage, GetModels, GetEngineCapabilities, ManageEnginePlugin, ManageEngineSkill, ManageEngineHook, ListSessions, SwitchSession, NewSession, DeleteWorkSession, DeleteSession, RollbackSession, RollbackResult, CompactSession, StartReview, GetWorkDashboard, CreateWorkProject, DeleteWorkProject, AddWorkSource, DeleteWorkSource, CreateWorkPlugin, DeleteWorkPlugin, CreateWorkSchedule, DeleteWorkSchedule, GetWorkArtifacts, ListDir, Ping, Pong, CommandAck,
    ReplayStart, ReplayEnd, Snapshot, StateEvent, Model, Effort, AutoCompact, Perm, PermissionProfiles, PermissionProfile, WebSearch, ContextReport, StatusReport, RateLimitResetResult, Notice, RateLimitUpdate, DiffReport, FilePreview, FileSaveResult, PreviewAsset, PreviewAuthorizationRequired, PreviewAuthorizationResult, History, TurnDetail, AgentDetail, HistoryImage, HistoryInvalidated, ArtifactInvalidated, Models, EngineCapabilities, AskUser, AskUserSync, AskUserClosed, AnswerQuestion, BackgroundProcessSync,
    SessionList, SessionListInvalidated, SessionActivity, SessionFocus, SessionRekey, RenameSession, ArchiveSession, PinSession, WorkDashboard, WorkArtifacts,
    ForkSession, ForkSessionWorktree, SessionForked, MigrateSession, SessionMigrated, DirList,
    GetGoal, SetGoal, ClearGoal, DismissGoal, GoalState,
    AcknowledgeCompletion, CompletionState,
    UserMsg, TurnSteered, AssistantMsgStart, Delta, ToolUse, ToolDelta, ToolResult,
    AssistantMsgEnd, ProcessEvent, TurnPlan, TurnDiff, TurnFileChanges, TurnBinding,
    TurnEnd, Error, WrapperDisconnected, WrapperReconnected,
]

# Session-narrative events the wrapper seqs and buffers. Replay/snapshot/
# control frames (replay_start, replay_end, snapshot, ask_user_sync,
# background_process_sync,
# wrapper_disconnected, wrapper_reconnected) are synthesized per-reconnect and
# are NOT seq'd/buffered.
DOWNSTREAM_TYPES = frozenset({
    "user_msg", "turn_steered", "state", "model", "effort", "auto_compact", "perm", "dsh_state",
    "permission_profile", "web_search", "fast", "codex_context",
    "collaboration_mode", "session_control", "query_queue", "btw_opened",
    "assistant_msg_start", "delta", "tool_use", "tool_delta", "tool_result",
    "assistant_msg_end", "process", "turn_plan", "turn_diff", "turn_file_changes", "turn_binding",
    "turn_end", "completion_state",
    "error", "ask_user", "ask_user_closed", "history_invalidated", "artifact_invalidated",
})

_TYPE_MAP: dict[str, type[BaseModel]] = {
    "browse_files": BrowseFiles,
    "files_listed": FilesListed,
    "set_codex_context": SetCodexContext,
    "codex_context": CodexContext,
    "hello": Hello,
    "query": Query,
    "cancel_queued_query": CancelQueuedQuery,
    "get_queued_query": GetQueuedQuery,
    "queued_query_detail": QueuedQueryDetail,
    "update_queued_query": UpdateQueuedQuery,
    "queued_query_updated": QueuedQueryUpdated,
    "query_queue": QueryQueueState,
    "steer": Steer,
    "interrupt": Interrupt,
    "takeover": Takeover,
    "takeover_state": TakeoverState,
    "session_control": SessionControl,
    "set_model": SetModel,
    "set_effort": SetEffort,
    "set_auto_compact": SetAutoCompact,
    "set_service_tier": SetServiceTier,
    "set_collaboration_mode": SetCollaborationMode,
    "open_btw": OpenBtw,
    "close_btw": CloseBtw,
    "sync_btw": SyncBtw,
    "btw_opened": BtwOpened,
    "btw_sync": BtwSync,
    "btw_closed": BtwClosed,
    "set_perm": SetPerm,
    "get_permission_profiles": GetPermissionProfiles,
    "permission_profiles": PermissionProfiles,
    "set_permission_profile": SetPermissionProfile,
    "permission_profile": PermissionProfile,
    "set_web_search": SetWebSearch,
    "web_search": WebSearch,
    "get_context": GetContext,
    "get_status": GetStatus,
    "consume_rate_limit_reset_credit": ConsumeRateLimitResetCredit,
    "get_diff": GetDiff,
    "get_turn_file_changes": GetTurnFileChanges,
    "turn_file_changes_page": TurnFileChangesPage,
    "get_file_preview": GetFilePreview,
    "save_markdown": SaveMarkdown,
    "get_preview_asset": GetPreviewAsset,
    "authorize_preview": AuthorizePreview,
    "get_history": GetHistory,
    "get_turn_detail": GetTurnDetail,
    "get_agent_detail": GetAgentDetail,
    "get_history_image": GetHistoryImage,
    "get_models": GetModels,
    "models": Models,
    "dsh_state": DshState,
    "dsh_command_result": DshCommandResult,
    "set_dsh_control": SetDshControl,
    "get_engine_capabilities": GetEngineCapabilities,
    "engine_capabilities": EngineCapabilities,
    "manage_engine_plugin": ManageEnginePlugin,
    "manage_engine_skill": ManageEngineSkill,
    "manage_engine_hook": ManageEngineHook,
    "list_sessions": ListSessions,
    "switch_session": SwitchSession,
    "new_session": NewSession,
    "delete_work_session": DeleteWorkSession,
    "delete_session": DeleteSession,
    "rollback_session": RollbackSession,
    "rollback_result": RollbackResult,
    "compact_session": CompactSession,
    "start_review": StartReview,
    "get_work_dashboard": GetWorkDashboard,
    "create_work_project": CreateWorkProject,
    "delete_work_project": DeleteWorkProject,
    "add_work_source": AddWorkSource,
    "delete_work_source": DeleteWorkSource,
    "create_work_plugin": CreateWorkPlugin,
    "delete_work_plugin": DeleteWorkPlugin,
    "create_work_schedule": CreateWorkSchedule,
    "delete_work_schedule": DeleteWorkSchedule,
    "get_work_artifacts": GetWorkArtifacts,
    "rename_session": RenameSession,
    "archive_session": ArchiveSession,
    "pin_session": PinSession,
    "fork_session": ForkSession,
    "fork_session_worktree": ForkSessionWorktree,
    "session_forked": SessionForked,
    "migrate_session": MigrateSession,
    "session_migrated": SessionMigrated,
    "list_dir": ListDir,
    "dir_list": DirList,
    "ping": Ping,
    "pong": Pong,
    "command_ack": CommandAck,
    "replay_start": ReplayStart,
    "replay_end": ReplayEnd,
    "snapshot": Snapshot,
    "state": StateEvent,
    "model": Model,
    "effort": Effort,
    "auto_compact": AutoCompact,
    "fast": Fast,
    "collaboration_mode": CollaborationMode,
    "perm": Perm,
    "context_report": ContextReport,
    "status_report": StatusReport,
    "rate_limit_reset_result": RateLimitResetResult,
    "notice": Notice,
    "rate_limit_update": RateLimitUpdate,
    "diff_report": DiffReport,
    "file_preview": FilePreview,
    "file_save_result": FileSaveResult,
    "preview_asset": PreviewAsset,
    "preview_authorization_required": PreviewAuthorizationRequired,
    "preview_authorization_result": PreviewAuthorizationResult,
    "work_artifacts": WorkArtifacts,
    "history": History,
    "turn_detail": TurnDetail,
    "agent_detail": AgentDetail,
    "history_image": HistoryImage,
    "history_invalidated": HistoryInvalidated,
    "artifact_invalidated": ArtifactInvalidated,
    "ask_user": AskUser,
    "ask_user_sync": AskUserSync,
    "ask_user_closed": AskUserClosed,
    "answer_question": AnswerQuestion,
    "get_goal": GetGoal,
    "set_goal": SetGoal,
    "clear_goal": ClearGoal,
    "dismiss_goal": DismissGoal,
    "goal_state": GoalState,
    "acknowledge_completion": AcknowledgeCompletion,
    "completion_state": CompletionState,
    "session_list": SessionList,
    "session_list_invalidated": SessionListInvalidated,
    "session_activity": SessionActivity,
    "session_focus": SessionFocus,
    "session_rekey": SessionRekey,
    "work_dashboard": WorkDashboard,
    "user_msg": UserMsg,
    "turn_steered": TurnSteered,
    "assistant_msg_start": AssistantMsgStart,
    "delta": Delta,
    "tool_use": ToolUse,
    "tool_delta": ToolDelta,
    "tool_result": ToolResult,
    "assistant_msg_end": AssistantMsgEnd,
    "process": ProcessEvent,
    "background_process_sync": BackgroundProcessSync,
    "turn_plan": TurnPlan,
    "turn_diff": TurnDiff,
    "turn_file_changes": TurnFileChanges,
    "turn_binding": TurnBinding,
    "turn_end": TurnEnd,
    "error": Error,
    "wrapper_disconnected": WrapperDisconnected,
    "wrapper_reconnected": WrapperReconnected,
}


class ProtocolError(Exception):
    pass


def deserialize(raw: str | bytes) -> AnyMessage:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise ProtocolError("invalid JSON payload") from exc
    if not isinstance(data, dict):
        raise ProtocolError("payload must be a JSON object")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version mismatch; expected v{PROTOCOL_VERSION}")
    t = data.get("type")
    cls = _TYPE_MAP.get(t)
    if cls is None:
        raise ProtocolError("unknown message type")
    try:
        return cls.model_validate(data)  # type: ignore[return-value]
    except ValidationError as exc:
        # Pydantic's default string includes attacker-controlled input_value.
        # Keep only bounded field locations and stable error kinds so malformed
        # prompts/attachments can never be copied into relay or wrapper logs.
        summaries = []
        for error in exc.errors(
            include_url=False, include_context=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in error.get("loc", ())) or "payload"
            summaries.append(f"{location}:{error.get('type', 'invalid')}")
        detail = ", ".join(summaries) or "validation failed"
        raise ProtocolError(f"invalid {t} message ({detail})") from exc


def serialize(msg: BaseModel) -> str:
    # ``owner_id`` is a relay-only routing envelope. Avoid adding a null field
    # to every ordinary event.
    common_exclude = ({"owner_id"}
                      if getattr(msg, "owner_id", None) is None else set())
    if isinstance(msg, ContextReport):
        exclude: set[str] = set(common_exclude)
        if all(value is None for value in (
                msg.session_tokens, msg.fixed_tokens,
                msg.session_percentage)):
            # Keep the existing Code wire shape byte-for-field compatible. The
            # optional breakdown exists only on Work reports that actually have
            # a trustworthy new-session baseline.
            exclude.update({
                "session_tokens", "fixed_tokens", "session_percentage",
            })
        if msg.available is None:
            exclude.add("available")
        if msg.auto_compact_threshold_tokens is None:
            exclude.add("auto_compact_threshold_tokens")
        if msg.raw_max_tokens is None:
            exclude.add("raw_max_tokens")
        if exclude:
            return msg.model_dump_json(exclude=exclude)
    return msg.model_dump_json(exclude=common_exclude)


def is_downstream(msg: BaseModel) -> bool:
    return msg.type in DOWNSTREAM_TYPES


def is_reliable_command(msg: BaseModel) -> bool:
    """True only for client commands that participate in cmd_id ACK/retry."""
    return isinstance(msg, _Command)


def is_client_message(msg: BaseModel) -> bool:
    """Whether a browser/TUI is allowed to originate this wire message.

    Deserialization covers both directions, so the relay must still enforce the
    role boundary. This prevents an authenticated client from feeding large
    wrapper-only History/tool/result frames into the wrapper's command queue.
    """
    return (
        isinstance(msg, _Command)
        or isinstance(msg, Ping)
        or (isinstance(msg, Hello) and msg.role == "client")
    )
