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
from typing import Optional

from cc_remote.protocol import State
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.sdk import SdkHandle
from cc_remote.wrapper.stream import StreamTranslator


@dataclass
class SessionContext:
    # None until the first ResultMessage/init SystemMessage captures the real id
    # (a brand-new session). A resumed session knows its id at spawn time.
    session_id: Optional[str]
    sdk: SdkHandle                     # one ClaudeSDKClient → one `claude` subprocess
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
    turn_task: Optional[asyncio.Task] = None
    # Correlates asynchronous turn crashes/drain failures with the optimistic
    # client turn. Control-command errors must never terminate an unrelated turn.
    active_msg_id: Optional[str] = None
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
    # /btw ephemeral fork: a throwaway side-session forked from `parent_sid` that
    # inherits its context. Never persisted, excluded from the session list, and
    # discarded on close. Its turns reuse the normal _run_turn path.
    btw: bool = False
    parent_sid: Optional[str] = None
    owner_client_id: Optional[str] = None
    # cc fork_session persists a transcript under a new id (unlike codex's
    # ephemeral fork); capture it here so close_btw can hard-delete it.
    btw_real_id: Optional[str] = None
    announced_model: Optional[str] = None
    announced_effort: Optional[str] = None
    announced_perm: Optional[str] = None
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
    pending_asks: dict = field(default_factory=dict)
    emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serialize the tiny "final preflight check -> query accepted by engine"
    # window against interrupt().  Reconnects happen before this lock; once held,
    # interrupt either marks the event before query (so the turn aborts) or waits
    # until query() has returned and can interrupt the newly-created live turn.
    launch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq
