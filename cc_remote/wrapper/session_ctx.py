"""Per-session state for the multi-session wrapper pool.

A SessionContext owns exactly one cc subprocess (via SdkHandle) plus its
conversation state: ring buffer, seq counter, state machine, turn task,
translator, pending ask_user futures, and an emit lock. The wrapper machine
holds a pool of these keyed by session id; switching the viewed session is a
focus change (no disconnect), so background turns keep streaming.

The drain contract (one async-for per turn, running to the terminal
ResultMessage before accepting another query) holds naturally per ctx: each
turn task is spawned on its own ctx with its own SDK subprocess.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from cc_remote.protocol import AskUser, BackgroundProcessItem, State
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator
from cc_remote.wrapper.turn_changes import TurnChangeTracker


@dataclass
class CodexGoalMutation:
    """Identity and proof boundary for one in-flight Goal mutation."""

    command_id: Optional[str]
    client_id: Optional[str]
    objective: Optional[str]
    status: Optional[str]
    token_budget: Optional[int]
    goal_revision_before: int
    turn_id: Optional[str] = None
    applied: bool = False


@dataclass
class ClaudeClientAliasProbe:
    """Frozen transcript append boundary for one browser-originated turn."""

    msg_id: str
    generation: int
    session_id: Optional[str]
    path: Optional[str]
    file_id: Optional[tuple[int, int]]
    offset: int
    allow_new_file: bool
    partial: bytes = b""


@dataclass(frozen=True)
class ActiveTurnBinding:
    """Exact logical/native owner of the currently live turn segment.

    Browser runtimes intentionally do not persist reducer ownership metadata.
    A reconnect cursor may therefore sit after the original TurnBinding even
    though the rebuilt page still needs that identity before replaying tail
    frames.  Keep the authoritative wire identity and its original ordering
    boundary beside the resident session; never infer it from output text or a
    generic process turn id.
    """

    msg_id: str
    turn_id: str
    seq: int
    generation: str


@dataclass(frozen=True)
class PendingAskResolution:
    """One terminal outcome for an interactive question.

    The Future always resolves normally with this value.  Keeping cancellation,
    timeout, supersede, and answer on one result channel avoids un-retrieved
    Future exceptions when the surrounding engine task is cancelled at the
    same time as the question closes.
    """

    reason: str
    answer: str | list[str] | None = None


@dataclass
class PendingAskState:
    """Authoritative, reconnectable state for one still-open question.

    ``event`` is an unsequenced template: it never enters the replay ring and
    is copied independently for the original live emit and every eligible
    client Hello.  ``deadline`` is the original monotonic deadline and is never
    recomputed by reconnects.
    """

    event: AskUser
    future: asyncio.Future[PendingAskResolution]
    labels: frozenset[str]
    allow_text: bool
    multi_select: bool
    created_at: float
    deadline: float
    target: Optional[str] = None


@dataclass
class SessionContext:
    # None until the first ResultMessage/init SystemMessage captures the real id
    # (a brand-new session). A resumed session knows its id at spawn time.
    session_id: Optional[str]
    sdk: SdkHandle                     # engine control adapter (SDK/app-server/broker)
    buffer: RingBuffer                 # per-session ring (own seq namespace)
    cwd: str                           # resume requires cwd to match the jsonl path
    # Pool key = the client-facing routing identity: the real sid once known,
    # else a temp `tmp-<uuid>` for a brand-new session. Kept in sync with the
    # machine's `sessions` dict key so every emit can stamp `sid` WITHOUT an
    # O(n) reverse lookup — and so a pre-capture new session's live frames route
    # deterministically (never leak into whatever is currently focused).
    key: Optional[str] = None
    seq: int = 0                       # per-session monotonic counter
    state: State = "idle"
    engine: str = "claude"             # "claude" (SdkHandle) | "codex" (CodexHandle)
    # Claude's complete local account boundary. ``session_id`` remains the
    # native UUID while ``key`` is namespaced when multiple profiles exist.
    claude_profile_id: Optional[str] = None
    claude_model_fallback_notices: set[str] = field(default_factory=set)
    turn_change_tracker: TurnChangeTracker | None = None
    # Codex's complete local account boundary. ``session_id`` remains the
    # native app-server UUID while ``key`` is the browser-facing routing id
    # (namespaced for every profile when multiple profiles are configured).
    # Claude uses the parallel field above.
    codex_profile_id: Optional[str] = None
    # Product-space identity. Work sessions are native engine sessions whose
    # cwd and metadata are owned by cc-remote's private Work registry.
    space: str = "code"
    work_id: Optional[str] = None
    # Work's new-session startup zero point. It is persisted by WorkRegistry and
    # subtracted only for the Work UI's growth gauge; the raw engine total stays
    # authoritative for capacity and compaction.
    work_context_baseline_tokens: Optional[int] = None
    # Only a brand-new Work record may establish a baseline. Migrated/resumed
    # rows with no baseline must keep showing the authoritative raw total rather
    # than silently reclassifying their existing conversation as engine cost.
    work_context_baseline_pending: bool = False
    # Only an explicitly native Claude transcript mutation may replace the
    # current Remote model/effort on reload. Origin-less metadata still makes
    # the in-memory conversation stale, but must not roll back a newer Remote
    # control selection to an older completed transcript row.
    claude_native_controls_dirty: bool = False
    turn_task: Optional[asyncio.Task] = None
    # Browser queue mode transfers complete Query commands here immediately.
    # The wrapper-owned drain keeps running while browsers are disconnected or
    # suspended and starts entries only after the current native turn settles.
    queued_queries: list[Any] = field(default_factory=list)
    queued_query_bytes: int = 0
    queued_query_errors: dict[str, str] = field(default_factory=dict)
    # A launch preflight may await daemon/ownership checks.  The item remains
    # wrapper-owned and visible until that preflight accepts it, while this
    # marker prevents concurrent cancel/edit/replace from mutating it.
    queued_query_starting_msg_id: Optional[str] = None
    queued_query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queued_query_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    queued_query_drain_task: Optional[asyncio.Task] = None
    # Correlates asynchronous turn crashes/drain failures with the optimistic
    # client turn. Control-command errors must never terminate an unrelated turn.
    active_msg_id: Optional[str] = None
    # Latest exact TurnBinding/TurnSteered owner in this wrapper generation.
    # It is client-reseeded on hello when the reconnect cursor has already
    # consumed the original binding frame. It never survives a terminal/idle
    # boundary and is not a substitute for the engine's native lifecycle.
    active_turn_binding: Optional[ActiveTurnBinding] = None
    # First visible Codex process work is persisted once per exact logical /
    # native owner. This in-memory marker only suppresses repeated sidecar reads
    # for commentary deltas; it never participates in running/idle ownership.
    codex_process_clock_binding: Optional[tuple[str, str]] = None
    # Interrupt must wake a consumer that is already blocked in queue.get().  The
    # absolute monotonic deadline prevents each subsequent queue item from
    # restarting the drain timeout.
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_deadline: Optional[float] = None
    translator: Optional[StreamTranslator] = None
    # Claude's SDK response queue outlives one ResultMessage. Background task
    # updates can therefore be consumed by the next turn's translator; preserve
    # their original turn/title by stable item id across translator instances.
    claude_item_turns: dict[str, str] = field(default_factory=dict)
    claude_item_titles: dict[str, str] = field(default_factory=dict)
    claude_item_meta: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    # Read-only projection of Claude-owned Agent runs. Kept resident across SDK
    # reconnects; Work and broker-owned TUI sessions leave it unset.
    claude_agents: Any = None
    # Connection-local Claude task lifecycle, independent from the optional
    # Agent detail projection above. Work deliberately has no Agent registry but
    # still permits Bash(run_in_background=true), so spawn-time controls must
    # wait for these tasks and their autonomous post-result follow-up to drain.
    claude_active_tasks: set[str] = field(default_factory=set)
    claude_task_tracking_overflow: bool = False
    # Public, bounded activity projection used for reconnect/Hello replacement.
    # Membership follows Claude's native background task level; ProcessEvent
    # edges enrich these snapshots without becoming reconnect authority.
    claude_background_processes: dict[str, BackgroundProcessItem] = field(
        default_factory=dict)
    # Sanitized Bash command metadata survives ResultMessage translator swaps
    # so a later task_started edge can still expose the script in the dock.
    claude_item_commands: dict[str, str] = field(default_factory=dict)
    # Every terminal background notification can enqueue its own autonomous
    # Claude turn.  Keep an insertion-ordered ledger instead of a boolean so an
    # earlier Result cannot release a later notification which was already
    # delivered. Values are ``notified`` until the injected top-level user
    # boundary is observed, then ``active`` until that turn's Result.
    claude_background_followups: dict[str, str] = field(default_factory=dict)
    claude_background_followup_nonce: int = 0
    # A corrupt or adversarial stream must not grow the exact-origin ledger
    # without bound. Overflow fails closed until one controlled reconnect resets
    # the complete connection-local lifecycle.
    claude_background_followup_overflow: bool = False
    claude_followup_recovery_task: Optional[asyncio.Task] = None
    # One injected Claude turn can contain streamed text and several assembled
    # tool blocks after its parent Result. Preserve translator state until that
    # injected turn's own Result so live rendering matches history translation.
    claude_background_translator: Optional[StreamTranslator] = None
    # An autonomous post-Result continuation has no managed turn consumer.
    # Stop therefore owns a bounded watchdog which reconnects the child if its
    # terminal Result never reaches the background pump.
    claude_autonomous_interrupt_task: Optional[asyncio.Task] = None
    claude_autonomous_interrupt_wakeup: asyncio.Event = field(
        default_factory=asyncio.Event)
    # /btw ephemeral fork: a throwaway side-session forked from `parent_sid` that
    # inherits its context. Never persisted, excluded from the session list, and
    # discarded on close. Its turns reuse the normal _run_turn path.
    btw: bool = False
    parent_sid: Optional[str] = None
    # Relay-authenticated account identity for a private side chat. The legacy
    # attribute name is retained for state/test compatibility; this is not a
    # page-lifetime WebSocket client id.
    owner_client_id: Optional[str] = None
    # Stable wall-clock ordering for the owner-scoped side-chat catalog. This
    # is process-local just like the ephemeral native fork itself.
    btw_created_at: float = 0.0
    # The native fork is inserted before its one-shot open response is sent.
    # Hello must not catalog/replay it until that response has established the
    # browser runtime, otherwise a reconnect can receive Snapshot first.
    btw_announced: bool = False
    # A proven-lost native ephemeral fork remains resident for read-only replay
    # until its owner explicitly closes it. Never reuse its routing identity.
    btw_destroyed: bool = False
    # cc fork_session persists a transcript under a new id (unlike codex's
    # ephemeral fork); capture it here so close_btw can hard-delete it.
    btw_real_id: Optional[str] = None
    announced_model: Optional[str] = None
    announced_effort: Optional[str] = None
    # Claude autocompact is a spawn-time session option.  Keep the last public
    # projection and any recoverable reconnect error on the resident context so
    # hello/focus refreshes do not make a still-pending choice look applied.
    announced_auto_compact: Optional[tuple[object, ...]] = None
    auto_compact_error: Optional[str] = None
    auto_compact_phase: str = "stable"
    auto_compact_compaction_done: bool = False
    auto_compact_apply_task: Optional[asyncio.Task] = None
    auto_compact_apply_started_revision: Optional[int] = None
    # Monotonic proof that this resident context completed a real native
    # compact transaction. A manual command snapshots it before waiting for the
    # query lock so it cannot repeat maintenance that won the same race.
    claude_compaction_revision: int = 0
    # Model/effort/permission mutations and the spawn-time autocompact control
    # use separate command paths.  Persist one coherent SDK snapshot per
    # resident Claude session so an older async write cannot land after a newer
    # control and silently undo it in the private store/broker preferences.
    claude_control_persist_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock)
    # Model/cwd/process changes and thread/settings notifications can arrive
    # while config/read or model/list is resolving a nullable Codex effort.
    # Serialize those presentation-only probes per resident session; the
    # resolver still revalidates authoritative state after every await.
    codex_effort_resolve_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock)
    announced_perm: Optional[str] = None
    announced_permission_profile: Optional[str] = None
    announced_web_search: Optional[str] = None
    announced_collaboration_mode: Optional[str] = None
    # Goal state is restored silently.  The remote UI only reveals it after the
    # user explicitly invokes /goal (get/set); this avoids a permanent empty
    # panel above the composer merely because a Claude/Codex session was opened.
    goal_visible: bool = False
    # app-server goal loops/automatic continuations can start without the
    # machine calling query(). Their lifecycle is delivered separately from the
    # managed turn stream so the session remains single-writer and interruptible.
    codex_spontaneous_turn_id: Optional[str] = None
    codex_spontaneous_task: Optional[asyncio.Task] = None
    # Do not invent an empty native-turn anchor before the app-server reveals
    # whether a spontaneous turn came from a real CLI user item. The official
    # user item id is also History's durable identity; Goal/automatic turns keep
    # the native turn id only when no such user boundary exists.
    codex_spontaneous_anchor_id: Optional[str] = None
    # A matching cached Goal is not proof that the current command applied it.
    # Keep the exact command scope and the automatic turn claimed while its RPC
    # was in flight; retries are idempotent only while that turn remains live.
    codex_goal_mutation: Optional[CodexGoalMutation] = None
    # ``turn/steer`` can accept localImage paths and consume them after the RPC
    # response. Keep Code's private attachment directories alive until the
    # enclosing native turn reaches its authoritative terminal boundary.
    codex_steer_attachment_dirs: list[str] = field(default_factory=list)
    # A timed-out turn/steer may still have been accepted by app-server. Keep
    # exactly one bounded user boundary until an authoritative userMessage item
    # with the same clientId confirms it, or the enclosing turn terminates.
    # ``Any`` avoids coupling this shared context module to a v21 wire class.
    codex_uncertain_steer: Any = None
    # Successful Remote steers are published before app-server releases their
    # official userMessage items. Preserve that client id until the live consumer
    # can reconcile the official item without rendering a second steer boundary.
    # The machine keeps this insertion-ordered mapping bounded and clears it at
    # the enclosing native terminal.
    codex_published_steers: dict[str, str] = field(default_factory=dict)
    # The rollout path may lag the official live item. Retain only its bounded
    # identity proof until it can be attached to that exact rollout inode.
    codex_pending_steer_user_identities: dict[str, Any] = field(
        default_factory=dict)
    # A shared-daemon turn started by Remote is leased on disk so a replacement
    # wrapper can safely reattach without classifying it as Codex App ownership.
    codex_owned_turn_id: Optional[str] = None
    # Last logical message id successfully written into that durable lease.
    # This deliberately differs from active_msg_id after a transient write
    # failure so the next authoritative boundary can retry the same CAS base.
    codex_owned_msg_id: Optional[str] = None
    # Immutable browser owner of the first visible segment in the leased native
    # turn. Later steers move ``codex_owned_msg_id`` but must not erase the
    # identity required to rebuild completed history after a wrapper restart.
    codex_owned_initial_msg_id: Optional[str] = None
    codex_recovered_turn_id: Optional[str] = None
    codex_recovered_msg_id: Optional[str] = None
    codex_recovered_automatic: Optional[bool] = None
    # Generation from cc_remote.codex_daemon_restart that this resident shared
    # proxy joined. An intentional account-switch restart changes the generation;
    # an active managed turn is handed to the replacement without unlocking the
    # session, while an idle thread reconnects before its next model input.
    codex_daemon_epoch: Optional[str] = None
    # True only while an accepted managed turn is crossing an intentional daemon
    # restart. A resumed active goal may start a spontaneous native turn during
    # force_reconnect; that turn is the continuation, not a competing writer.
    codex_account_handoff: bool = False
    # Remote-owned Git checkpoint journal for Codex Code turns. It is created
    # lazily only in Git workspaces; Work and Claude use their own restore paths.
    codex_checkpoint: Any = None
    codex_checkpoint_turn_id: Optional[str] = None
    # ``turn/start`` acceptance is separate from the pre-turn filesystem
    # capture. A failed RPC must abort the active snapshot without consuming a
    # native-turn slot, while an accepted turn whose capture failed needs an
    # unavailable tombstone to keep count-based rollback aligned.
    codex_checkpoint_ready: bool = False
    codex_checkpoint_accepted: bool = False
    codex_checkpoint_unavailable_reason: Optional[str] = None
    # A shared ``turn/start`` response can be lost during an official daemon
    # replacement. The original runner remains the sole write owner while it
    # reconciles exact native identity; a reconnect-time turn/started callback
    # must not mistake that recovery for a competing automatic turn.
    codex_turn_start_reconciling: bool = False
    codex_deferred_turn_start_id: Optional[str] = None
    # Every native lifecycle callback advances this fence while reconciliation
    # owns the session. The recovery loop snapshots it after an ordered status
    # probe and refuses to unlock if a turn started or completed during any
    # subsequent await.
    codex_turn_start_reconcile_revision: int = 0
    # ---- external-write mirroring (a native `claude`/`codex` in the user's
    # terminal owns this session and is appending to its transcript) ----
    # epoch of the last append this wrapper did NOT make. Recent => the session is
    # externally owned: clients show it read-only and get mirrored History frames.
    external_ts: float = 0.0
    # an external append happened, so our resumed subprocess now holds a STALE
    # context. Reload (force_reconnect with resume) before running another turn,
    # else we'd continue from a conversation that has since moved on.
    needs_reload: bool = False
    # True only while this wrapper's Claude SDK child is expected to append to
    # the transcript. ``state=running`` starts earlier, during reconnect and
    # final ownership checks, so it is not sufficient write attribution.
    claude_write_active: bool = False
    # Claude may legitimately remain silent while reasoning, running a tool, or
    # waiting on its provider. Keep a turn-local activity clock so the wrapper
    # can explain that silence without treating it as a timeout or breaking the
    # mandatory ResultMessage drain. AskUser callbacks run beside the response
    # iterator, so an Event wakes that iterator's wait when a question opens or
    # closes and restarts the neutral notice clock from the right boundary.
    claude_last_activity_at: float = 0.0
    claude_activity_event: asyncio.Event = field(default_factory=asyncio.Event)
    claude_progress_notice_active: bool = False
    # Claude assigns transcript UUIDs inside its child. A turn-local append
    # boundary learns the first real sdk-py user row without waiting for the
    # later --replay-user-messages echo; replay remains the exact fallback.
    # Keep a bounded same-process copy while the durable, source-bound store is
    # unavailable or a new session has not captured its real id/path yet.
    claude_client_message_ids: dict[str, str] = field(default_factory=dict)
    claude_client_alias_bound_msg_id: Optional[str] = None
    claude_client_alias_generation: int = 0
    claude_client_alias_probe: Optional[ClaudeClientAliasProbe] = None
    # Authoritative v15 ownership/control projection.  These fields belong to
    # the resident session rather than to any browser so reconnects and history
    # refreshes cannot resurrect a stale read-only banner.  `control_revision`
    # advances whenever one of the public values changes.
    control_mode: str = "remote"
    write_state: str = "writable"
    terminal_attached: bool = False
    control_reason: Optional[str] = None
    control_can_takeover: bool = False
    control_revision: int = 0
    # Machine-local sid whose monotonic control epoch has been bound to this
    # resident context.  A newly-created context starts unbound so reopening an
    # evicted sid advances beyond the browser's last same-generation revision.
    control_revision_key: Optional[str] = None
    # A Claude session launched through the explicit local `claude-remote`
    # broker keeps the official TUI as its sole process owner.  The wrapper
    # stores only the broker generation needed to reject a stale PID/socket
    # record; it never owns or kills that process on an ordinary disconnect.
    claude_broker_generation: Optional[str] = None
    # Questions are durable for the lifetime of this resident context rather
    # than one-shot ring events. Registration and every terminal transition are
    # serialized by ``emit_lock`` so Hello cannot resurrect a closed question.
    pending_asks: dict[str, PendingAskState] = field(default_factory=dict)
    # The browser presents one question card per session. Serialize whole
    # batches so concurrent tools/subagents cannot overwrite that card.
    ask_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # A Remote model chip can trigger Claude TUI's cached-history confirmation.
    # Track its question separately so a newer model choice can supersede the
    # old one without leaving an unreachable Future behind.
    pending_model_ask_id: Optional[str] = None
    # A file outside ``cwd`` is never previewable merely because the browser
    # knows its path.  The only exception is an exact path that this session's
    # built-in file-mutation tool has completed successfully. Keep the pending
    # tool/paths binding separate so a failed or declined tool call grants no
    # read capability.  Both maps are bounded by WrapperMachine.
    preview_write_candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # A successful built-in image read outside cwd grants only the immutable
    # bytes observed at that exact tool boundary.  The machine owns the bounded
    # byte cache; this opaque token prevents a later SessionContext (or a re-key)
    # from inheriting another context's snapshots.
    preview_snapshot_token: str = field(default_factory=lambda: uuid4().hex)
    preview_image_candidates: dict[str, str] = field(default_factory=dict)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Status reads run outside the serial command lane so a daemon restart
    # barrier cannot delay Query/Interrupt. They can therefore reach the same
    # generation handoff as a user command; serialize only that reconnect
    # boundary, not the potentially long wait for the hook to become ready.
    codex_daemon_generation_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock)
    # Multiple browsers (and focus/idle refreshes from one browser) may ask for
    # status around the same turn boundary. Preserve their arrival order so an
    # old-generation read cannot finish after and overwrite the new account.
    codex_status_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serialize the tiny "final preflight check -> query accepted by engine"
    # window against interrupt().  Reconnects happen before this lock; once held,
    # interrupt either marks the event before query (so the turn aborts) or waits
    # until query() has returned and can interrupt the newly-created live turn.
    launch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Deferred queries launch from a per-session worker rather than the serial
    # browser command lane. Serialize their full idle-check/preflight/claim
    # boundary against a newly-arriving immediate query.
    query_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes multiple native turn/steer requests without blocking explicit
    # interrupt, which deliberately uses the separate launch_lock.
    steer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Native notifications can be read immediately after app-server resolves a
    # turn/steer RPC, before the handler coroutine gets CPU time to publish the
    # matching user boundary. Hold those notifications until TurnSteered (or a
    # rejection) establishes the correct visible segment.
    codex_steer_gate: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.codex_steer_gate.set()

    @property
    def claude_background_followup_pending(self) -> bool:
        """Compatibility view for callers which only need the aggregate latch."""
        return bool(
            self.claude_background_followups
            or self.claude_background_followup_overflow
        )

    @claude_background_followup_pending.setter
    def claude_background_followup_pending(self, pending: bool) -> None:
        # Narrow tests and embedded adapters historically seeded this latch
        # directly. Preserve that API without letting production lifecycle code
        # collapse the real per-origin ledger back into one boolean.
        if pending:
            if not self.claude_background_followups:
                self.claude_background_followups["legacy:manual"] = "active"
        else:
            self.claude_background_followups.clear()
            self.claude_background_followup_overflow = False

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
