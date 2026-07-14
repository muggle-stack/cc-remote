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
    model_validator,
)

from cc_remote.attachments import (
    MAX_ATTACHMENT_COUNT,
    MAX_FILENAME_BYTES,
    MAX_SINGLE_ATTACHMENT_BYTES,
)

PROTOCOL_VERSION = 8

State = Literal["idle", "running", "interrupting", "draining"]
Engine = Literal["claude", "codex"]
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
PermissionMode = Literal[
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
    "never", "on-request", "untrusted",
]
CollaborationModeName = Literal["default", "plan"]
ModelName = Annotated[str, StringConstraints(min_length=1, max_length=256)]
WireId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]

MAX_ENCODED_ATTACHMENT_CHARS = ((MAX_SINGLE_ATTACHMENT_BYTES + 2) // 3) * 4
ASK_QUESTION_MAX_CHARS = 16 * 1024
ASK_OPTION_LABEL_MAX_CHARS = 512
ASK_OPTION_DESCRIPTION_MAX_CHARS = 2 * 1024
ASK_ANSWER_MAX_CHARS = 4 * 1024
ASK_OPTION_MIN_COUNT = 2
ASK_OPTION_MAX_COUNT = 5


def _valid_attachment_filename(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("attachment filename must be valid UTF-8") from exc
    if len(encoded) > MAX_FILENAME_BYTES:
        raise ValueError(
            f"attachment filename exceeds {MAX_FILENAME_BYTES} UTF-8 bytes")
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
ERR_WRAPPER_OFFLINE = "wrapper_offline"
ERR_WRAPPER_ALREADY_CONNECTED = "wrapper_already_connected"
ERR_AUTH = "auth"
ERR_FORK_RECONCILING = "fork_reconciling"


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

    @model_validator(mode="after")
    def bounded_attachment_count(self):
        if _attachment_count(self.images, self.files) > MAX_ATTACHMENT_COUNT:
            raise ValueError(
                f"query attachments exceed {MAX_ATTACHMENT_COUNT} items")
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


class SetModel(_Command):
    type: Literal["set_model"] = "set_model"
    model: ModelName


class SetEffort(_Command):
    """client -> wrapper: set the session's reasoning effort (thinking strength).
    Unlike set_model, effort is a spawn-time CLI flag (--effort), so applying it
    respawns the cc subprocess with resume — done lazily at the next turn."""
    type: Literal["set_effort"] = "set_effort"
    effort: EffortLevel


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
    engine: str


class UserMsg(_Base):
    """A user's query, broadcast to all clients so every device sees the full
    conversation (prompt + response). The originating client dedups by msg_id
    (it already created the turn optimistically on send). Carries images so
    other devices / fresh replays render the attachment."""
    type: Literal["user_msg"] = "user_msg"
    msg_id: WireId
    prompt: str
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    # Metadata only: file bodies stay out of replay/cache, while names remain
    # visible across devices and after transcript history reload.
    files: Optional[list[UserFileMeta]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)


class AssistantMsgStart(_Base):
    type: Literal["assistant_msg_start"] = "assistant_msg_start"
    message_id: WireId
    channel: AssistantChannel = "unknown"


class Delta(_Base):
    type: Literal["delta"] = "delta"
    message_id: WireId
    text: str
    # Some engines only reveal whether text is commentary or final on the
    # assembled message. Repeating the channel on deltas/end lets the client
    # promote a provisional block without duplicating it.
    channel: AssistantChannel = "unknown"


class ToolUse(_Base):
    type: Literal["tool_use"] = "tool_use"
    message_id: WireId
    tool_use_id: WireId
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
    stream: Literal["progress", "output", "diff", "summary", "terminal"]
    delta: str = Field(max_length=512 * 1024)


class ToolResult(_Base):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: WireId
    content: str
    is_error: bool
    truncated: Optional[bool] = None
    status: Optional[ProcessStatus] = None
    summary: Optional[str] = Field(default=None, max_length=64 * 1024)
    diff: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)


class AssistantMsgEnd(_Base):
    type: Literal["assistant_msg_end"] = "assistant_msg_end"
    message_id: WireId
    channel: AssistantChannel = "unknown"


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


class TurnResult(BaseModel):
    """Subset of ResultMessage forwarded to clients."""
    model_config = ConfigDict(extra="allow")
    subtype: str
    duration_ms: int
    is_error: bool
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None


class TurnEnd(_Base):
    type: Literal["turn_end"] = "turn_end"
    result: TurnResult
    # Engine-specific authoritative fork point. Codex sends app-server's turn
    # id; Claude sends the final assistant transcript UUID in this user turn.
    # Synthetic/legacy boundaries without a real engine id leave it unset.
    # The legacy wire name stays stable for protocol-v5 browser compatibility.
    turn_id: Optional[WireId] = None


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
    state: Optional[State] = None  # if resident: this session's idle/running/... (sidebar status dot)
    engine: Optional[str] = None  # "claude" | "codex"; None = claude (legacy sidebar badge)
    forked_from_id: Optional[WireId] = None  # Codex thread/fork parent, when present
    codex_status: Optional[CodexThreadStatus] = None  # authoritative app-server status


class ListSessions(_Command):
    """client -> wrapper: request the session list. `engine` picks the backend's
    session store (Claude ~/.claude/projects vs Codex ~/.codex/sessions);
    optional, default claude."""
    type: Literal["list_sessions"] = "list_sessions"
    engine: Literal["claude", "codex"] = "claude"


class SessionList(_Base):
    """wrapper -> client: the sessions (downstream so a reconnect restores it)."""
    type: Literal["session_list"] = "session_list"
    engine: Literal["claude", "codex"]
    sessions: list[SessionInfo]


class SwitchSession(_Command):
    """client -> wrapper: resume a different existing session. `engine` tells the
    wrapper which backend to resume it with (codex threads resume differently)."""
    type: Literal["switch_session"] = "switch_session"
    session_id: WireId
    engine: Optional[Engine] = None


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
    model: Optional[ModelName] = None    # None -> engine default (settings.json / codex config)
    effort: Optional[EffortLevel] = None  # None -> engine default
    collaboration_mode: Optional[CollaborationModeName] = None  # Codex only; first turn included
    permission_mode: Optional[
        Literal["never", "on-request", "untrusted"]
    ] = None  # Codex only; persisted before the first turn
    service_tier: Optional[Literal["default", "fast"]] = None  # Codex only
    prompt: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    msg_id: Optional[WireId] = None
    images: Optional[list[QueryImage]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)
    files: Optional[list[QueryFile]] = Field(default=None, max_length=MAX_ATTACHMENT_COUNT)

    @model_validator(mode="after")
    def initial_query_requires_message_id(self):
        if _attachment_count(self.images, self.files) > MAX_ATTACHMENT_COUNT:
            raise ValueError(
                f"new_session attachments exceed {MAX_ATTACHMENT_COUNT} items")
        if (self.prompt is not None or self.images or self.files) and not self.msg_id:
            raise ValueError("msg_id is required when new_session carries a query")
        codex_only = {
            "collaboration_mode": self.collaboration_mode,
            "permission_mode": self.permission_mode,
            "service_tier": self.service_tier,
        }
        invalid = [name for name, value in codex_only.items()
                   if value is not None and self.engine != "codex"]
        if invalid:
            raise ValueError(
                f"{', '.join(invalid)} only supported for Codex sessions")
        return self


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


class ArchiveSession(_Command):
    """client -> wrapper: toggle the "archived" tag on a session."""
    type: Literal["archive_session"] = "archive_session"
    session_id: WireId
    archived: bool


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


# ---- model catalog (the engine is the source of truth, not the client) ----

class GetModels(_Command):
    """client -> wrapper: what models does this engine actually offer?

    `codex` answers with app-server's real catalog. Claude has no equivalent
    catalog RPC, but can resolve explicit no-override settings for a cwd;
    its model list therefore remains empty and the client keeps the static table.
    """
    type: Literal["get_models"] = "get_models"
    engine: Optional[Literal["cc", "claude", "codex"]] = None
    client_id: Optional[WireId] = None  # requester, so the wrapper routes Models back to=<client_id>
    # Claude defaults can depend on project/local settings, so resolve them in
    # the same directory the prospective new session will use.
    cwd: Optional[str] = Field(default=None, max_length=4096)


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
    engine: str
    models: list[dict[str, Any]] = []
    default_model: Optional[str] = Field(default=None, max_length=256)
    default_effort: Optional[str] = Field(default=None, max_length=64)
    # Echoes GetModels.cwd for cwd-sensitive Claude defaults so a late response
    # can never be rendered against a different directory in the new-chat form.
    cwd: Optional[str] = Field(default=None, max_length=4096)


class SetPerm(_Command):
    """client -> wrapper: switch the cc session's permission mode (runtime, no reconnect)."""
    type: Literal["set_perm"] = "set_perm"
    mode: PermissionMode


class Perm(_Base):
    """The cc session's current permission mode. Downstream so a reconnecting
    client restores the readout."""
    type: Literal["perm"] = "perm"
    mode: str


class GetContext(_Command):
    """client -> wrapper: request current context window usage."""
    type: Literal["get_context"] = "get_context"


class ContextReport(_Base):
    """wrapper -> client: context window usage (one-shot response to GetContext,
    like SessionList — not buffered)."""
    type: Literal["context_report"] = "context_report"
    total_tokens: int
    max_tokens: int
    percentage: float
    model: Optional[str] = None
    is_auto_compact_enabled: Optional[bool] = None
    categories: list[dict[str, Any]] = []


class GetStatus(_Command):
    """client -> wrapper: read the authoritative Codex app-server status.

    Unlike GetContext this is a composed, one-shot control response. It never
    enters transcript history and is safe for the reliable command layer to
    re-run when a response/ACK is lost.
    """
    type: Literal["get_status"] = "get_status"


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


class StatusUsage(_StatusPart):
    lifetime_tokens: Optional[int] = Field(default=None, ge=0)
    peak_daily_tokens: Optional[int] = Field(default=None, ge=0)
    current_streak_days: Optional[int] = Field(default=None, ge=0)
    longest_streak_days: Optional[int] = Field(default=None, ge=0)
    longest_running_turn_sec: Optional[int] = Field(default=None, ge=0)


class StatusReport(_Base):
    """wrapper -> client: sanitized Codex app-server status snapshot.

    Every nested model forbids extras. The wrapper copies only explicitly
    approved display fields; account email, credentials, config instructions,
    rollout paths/previews, credit balances and daily usage rows never cross the
    wire. ``component_errors`` makes partial RPC failure visible without hiding
    the successful sections.
    """
    type: Literal["status_report"] = "status_report"
    thread: StatusThread
    runtime: StatusRuntime
    context: StatusContext
    account: Optional[StatusAccount] = None
    rate_limits: list[StatusRateLimit] = Field(default_factory=list, max_length=16)
    usage: Optional[StatusUsage] = None
    # Entries are ``<component>: <generic reason>``. Raw RPC error text is never
    # allowed, because it may contain provider or account details.
    component_errors: list[StatusErrorText] = Field(default_factory=list, max_length=5)


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
    """Sparse-safe, sanitized rolling Codex rate-limit state.

    The app-server's credits, individual spend control and future unknown
    fields are intentionally absent.  Names differ slightly from StatusReport
    so the live event remains concise; the Web reducer projects them into the
    existing StatusRateLimit view model.
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


class DiffReport(_Base):
    """wrapper -> client: git diff text (one-shot, like ContextReport)."""
    type: Literal["diff_report"] = "diff_report"
    file: str
    diff: str


class GetHistory(_Command):
    """client -> wrapper: request a session's history, read ON-DEMAND from its
    transcript (NOT the ring buffer, NOT requiring the session to be resident).
    This replaces per-session buffer replay on hello — history is fetched once
    when a session is opened, like a web chat's GET /conversation. `before`/
    `limit` page older turns (load-more); omit both for the whole history."""
    type: Literal["get_history"] = "get_history"
    session_id: WireId
    client_id: Optional[WireId] = None  # requester, so the wrapper routes History back to=<client_id>
    cwd: Optional[str] = Field(default=None, max_length=4096)
    # Stable cursor for the oldest loaded turn: a user msg_id normally, or the
    # authoritative engine turn_id for an assistant-only automatic continuation.
    before: Optional[WireId] = None
    limit: Optional[int] = Field(default=None, ge=1, le=200)


class History(_Base):
    """wrapper -> client: a session's history as ONE bulk frame (one-shot, like
    ContextReport/SessionList — NOT seq'd/buffered). `events` are already-
    serialized narrative event dicts (same shape as the live stream) that the
    client applies in a single reducer pass into runtimes[session_id], deduped
    by msg_id/message_id against any live tail. Routed to=<client_id>, EXCEPT the
    mirror push below, which is broadcast."""
    type: Literal["history"] = "history"
    session_id: WireId
    events: list[dict[str, Any]] = []
    has_more: bool = False            # older turns exist beyond what's returned (pagination)
    oldest_id: Optional[str] = None   # first returned stable turn cursor
    newest_id: Optional[str] = None   # last returned stable turn cursor
    before: Optional[str] = None      # echoes the request's `before`: set => this is an OLDER page (client prepends)
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

    @model_validator(mode="after")
    def choices_or_text(self):
        if not self.allow_text and len(self.options) < ASK_OPTION_MIN_COUNT:
            raise ValueError("ask_user requires 2-5 options unless text input is enabled")
        return self


class AnswerQuestion(_Command):
    """client -> wrapper: the user's answer to an AskUser prompt. answer is the
    selected option's label (or free text if the agent allowed it)."""
    type: Literal["answer_question"] = "answer_question"
    ask_id: WireId
    answer: AskAnswerText


class GetGoal(_Command):
    type: Literal["get_goal"] = "get_goal"


class SetGoal(_Command):
    type: Literal["set_goal"] = "set_goal"
    objective: Optional[str] = Field(default=None, max_length=16 * 1024)
    status: Optional[GoalStatus] = None
    token_budget: Optional[int] = Field(default=None, ge=1)


class ClearGoal(_Command):
    type: Literal["clear_goal"] = "clear_goal"


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
    engine: Literal["claude", "codex"]
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


AnyMessage = Union[
    Hello, Query, Interrupt, Takeover, TakeoverState, SetModel, SetEffort, SetServiceTier, SetCollaborationMode, SetPerm, Fast, CollaborationMode, OpenBtw, CloseBtw, BtwOpened, GetContext, GetStatus, GetDiff, GetHistory, GetModels, ListSessions, SwitchSession, NewSession, ListDir, Ping, Pong, CommandAck,
    ReplayStart, ReplayEnd, Snapshot, StateEvent, Model, Effort, Perm, ContextReport, StatusReport, Notice, RateLimitUpdate, DiffReport, History, Models, AskUser, AnswerQuestion,
    SessionList, SessionFocus, SessionRekey, RenameSession, ArchiveSession,
    ForkSession, ForkSessionWorktree, SessionForked, DirList,
    GetGoal, SetGoal, ClearGoal, GoalState,
    UserMsg, AssistantMsgStart, Delta, ToolUse, ToolDelta, ToolResult,
    AssistantMsgEnd, ProcessEvent, TurnPlan, TurnDiff,
    TurnEnd, Error, WrapperDisconnected, WrapperReconnected,
]

# Session-narrative events the wrapper seqs and buffers. Replay/snapshot/
# control frames (replay_start, replay_end, snapshot, wrapper_disconnected,
# wrapper_reconnected) are synthesized per-reconnect and are NOT seq'd/buffered.
DOWNSTREAM_TYPES = frozenset({
    "user_msg", "state", "model", "effort", "perm", "fast",
    "collaboration_mode", "btw_opened",
    "assistant_msg_start", "delta", "tool_use", "tool_delta", "tool_result",
    "assistant_msg_end", "process", "turn_plan", "turn_diff", "turn_end",
    "error", "ask_user",
})

_TYPE_MAP: dict[str, type[BaseModel]] = {
    "hello": Hello,
    "query": Query,
    "interrupt": Interrupt,
    "takeover": Takeover,
    "takeover_state": TakeoverState,
    "set_model": SetModel,
    "set_effort": SetEffort,
    "set_service_tier": SetServiceTier,
    "set_collaboration_mode": SetCollaborationMode,
    "open_btw": OpenBtw,
    "close_btw": CloseBtw,
    "btw_opened": BtwOpened,
    "set_perm": SetPerm,
    "get_context": GetContext,
    "get_status": GetStatus,
    "get_diff": GetDiff,
    "get_history": GetHistory,
    "get_models": GetModels,
    "models": Models,
    "list_sessions": ListSessions,
    "switch_session": SwitchSession,
    "new_session": NewSession,
    "rename_session": RenameSession,
    "archive_session": ArchiveSession,
    "fork_session": ForkSession,
    "fork_session_worktree": ForkSessionWorktree,
    "session_forked": SessionForked,
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
    "fast": Fast,
    "collaboration_mode": CollaborationMode,
    "perm": Perm,
    "context_report": ContextReport,
    "status_report": StatusReport,
    "notice": Notice,
    "rate_limit_update": RateLimitUpdate,
    "diff_report": DiffReport,
    "history": History,
    "ask_user": AskUser,
    "answer_question": AnswerQuestion,
    "get_goal": GetGoal,
    "set_goal": SetGoal,
    "clear_goal": ClearGoal,
    "goal_state": GoalState,
    "session_list": SessionList,
    "session_focus": SessionFocus,
    "session_rekey": SessionRekey,
    "user_msg": UserMsg,
    "assistant_msg_start": AssistantMsgStart,
    "delta": Delta,
    "tool_use": ToolUse,
    "tool_delta": ToolDelta,
    "tool_result": ToolResult,
    "assistant_msg_end": AssistantMsgEnd,
    "process": ProcessEvent,
    "turn_plan": TurnPlan,
    "turn_diff": TurnDiff,
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
    return msg.model_dump_json()


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
