"""Codex app-server lifecycle: connect / query / interrupt / receive / disconnect.

The Codex analog of SdkHandle (sdk.py). Drives one persistent `codex app-server`
subprocess over newline-delimited JSON-RPC 2.0 (stdio) and presents the SAME
async surface the machine's per-turn consumer expects:

  connect(resume_id, cwd) -> initialize/initialized handshake + thread/start|resume
  query(prompt)           -> turn/start (opens a fresh per-turn queue)
  receive_response()      -> async-gen of raw notification dicts until turn/completed
  interrupt()             -> turn/interrupt {threadId, turnId}
  disconnect()            -> terminate the subprocess

Model-agnostic: whatever backend Codex is pointed at (user's cc-switch) is Codex's
concern. We never set a model/provider here.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from typing import Any, Awaitable, Callable, Optional

from cc_remote.log import logger
from cc_remote.protocol import Notice, RateLimitUpdate, ThreadGoal
from cc_remote.wrapper.codex_sessions import (
    codex_approval,
    codex_context_window,
    codex_effort,
    codex_fast_enabled,
    codex_model,
)
from cc_remote.wrapper.child_env import sanitized_child_env

log = logger("cc_remote.wrapper.codex_handle")

_REQ_TIMEOUT = 60.0
_APPROVAL_TIMEOUT = 5 * 60.0
_MAX_SERVER_REQUEST_TASKS = 32
_BIN_CACHE: Optional[str] = None
_MAX_CODEX_CANDIDATES = 16
_MAX_STANDALONE_CANDIDATES = 6
_MAX_NVM_CANDIDATES = 3
_CODEX_VERSION_TIMEOUT = 5
_THREAD_SETTINGS_NOTIFY_TIMEOUT = 1.0
_OWNED_TURN_IDS_MAX = 512
_STATUS_RATE_LIMIT_MAX = 16
_RUNTIME_EVENT_PENDING_MAX = 32
_RUNTIME_EVENT_SEEN_MAX = 128
_NOTICE_MESSAGE_MAX = 2 * 1024
_NOTICE_DETAIL_MAX = 4 * 1024
_NOTICE_PATH_MAX = 1024
_NOTICE_PATH_SAMPLE_MAX = 3
_SPONTANEOUS_QUEUE_MIN_ITEMS = 64
_SPONTANEOUS_QUEUE_MAX_ITEMS = 256
_SPONTANEOUS_QUEUE_MIN_BYTES = 4 * 1024 * 1024

ApprovalCallback = Callable[[str, dict], Awaitable[str]]
InteractionCallback = Callable[[str, dict], Awaitable[dict[str, Any]]]
GoalCallback = Callable[[Optional[dict[str, Any]]], Awaitable[None]]
TurnLifecycleCallback = Callable[[str, str], Awaitable[None]]
RuntimeEvent = Notice | RateLimitUpdate
RuntimeEventCallback = Callable[[RuntimeEvent], Awaitable[None]]


class CodexSpontaneousOverflow:
    """Internal bridge signal: live detail was shed to protect stdout reading."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: str):
        self.turn_id = turn_id


class CodexSpontaneousClosed:
    """Internal bridge signal: app-server ended before a terminal notification."""

    __slots__ = ("turn_id",)

    def __init__(self, turn_id: str):
        self.turn_id = turn_id


class _SpontaneousNotificationQueue:
    """Single-loop FIFO bounded by both parsed frames and original wire bytes.

    The app-server stdout reader must keep draining even if the relay is slow.  A
    regular ``asyncio.Queue.put`` would transfer relay backpressure all the way to
    stdout and can deadlock JSON-RPC responses/approvals.  This queue therefore has
    a synchronous, fail-fast producer and one asynchronous consumer.
    """

    def __init__(self, max_items: int, max_bytes: int):
        self.max_items = max(2, max_items)
        self.max_bytes = max(1024, max_bytes)
        self._items: deque[tuple[object, int]] = deque()
        self._bytes = 0
        self._ready = asyncio.Event()

    @property
    def byte_size(self) -> int:
        return self._bytes

    def qsize(self) -> int:
        return len(self._items)

    def put_nowait(self, item: object, size: int = 0) -> bool:
        size = max(0, size)
        if (size > self.max_bytes or len(self._items) >= self.max_items
                or self._bytes + size > self.max_bytes):
            return False
        self._items.append((item, size))
        self._bytes += size
        self._ready.set()
        return True

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0
        self._ready.clear()

    async def get(self) -> object:
        while not self._items:
            self._ready.clear()
            if not self._items:
                await self._ready.wait()
        item, size = self._items.popleft()
        self._bytes = max(0, self._bytes - size)
        if not self._items:
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


def _codex_candidates() -> list[str]:
    """Every codex install we can find, in tie-break order (earlier wins ties).
    Managed standalone releases first: `codex upgrade` writes there, so it's the
    one the user actually updates. An npm-global under nvm is often stale but
    shadows everything else on PATH."""
    home = os.path.expanduser("~")
    out = list(islice(glob.iglob(
        os.path.join(home, ".codex/packages/standalone/releases/*/bin/codex")),
        _MAX_STANDALONE_CANDIDATES))
    out.append(os.path.join(home, ".local/bin/codex"))
    which = shutil.which("codex")
    if which:
        out.append(which)
    out += list(islice(glob.iglob(
        os.path.join(home, ".nvm/versions/node/*/bin/codex")),
        _MAX_NVM_CANDIDATES))
    out += ["/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex"]
    seen, uniq = set(), []
    for c in out:
        if not os.path.exists(c):
            continue
        real = os.path.realpath(c)
        if real in seen:
            continue
        seen.add(real)
        uniq.append(c)
        if len(uniq) >= _MAX_CODEX_CANDIDATES:
            break
    return uniq


def _codex_version(path: str) -> tuple[int, ...]:
    """`codex --version` -> (0, 144, 1). (-1,) when it can't be run/parsed, so a
    broken install always loses to a working one."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           timeout=_CODEX_VERSION_TIMEOUT, env=_codex_env(path))
    except Exception:
        return (-1,)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
    return tuple(int(g) for g in m.groups()) if m else (-1,)


def _resolve_codex_bin() -> str:
    """Locate the codex CLI, preferring the NEWEST install.

    $CODEX_BIN short-circuits. Otherwise we version-probe every candidate and take
    the highest — plain PATH order is wrong: a stale npm-global (nvm bin sits first
    on the wrapper's PATH) shadowed a newer standalone release, so the wrapper kept
    spawning an old app-server whose `model/list` predated the current model family.
    The app-server IS our model catalog, so serving a stale one silently corrupts
    every model/effort decision downstream. Blocking (subprocess); cached for the
    process — call via asyncio.to_thread from async code."""
    global _BIN_CACHE
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    if _BIN_CACHE:
        return _BIN_CACHE
    cands = _codex_candidates()
    if not cands:
        return "codex"  # last resort — errors clearly if truly absent
    # A broken candidate must not stall startup serially.  Keep both the list and
    # the worker pool small; each individual probe also has a hard timeout.
    with ThreadPoolExecutor(max_workers=min(4, len(cands))) as pool:
        probed = list(pool.map(_codex_version, cands))
    versions = list(zip(probed, cands))
    best_v, best = max(versions, key=lambda p: p[0])
    if best_v == (-1,):
        best = cands[0]
    _BIN_CACHE = best
    log.info("codex bin resolved", path=best, version=".".join(map(str, best_v)),
             considered=[{"path": c, "version": ".".join(map(str, v))} for v, c in versions])
    return best


def _codex_env(bin_path: str) -> dict:
    """Child env for the codex subprocess. codex.js runs via `#!/usr/bin/env
    node`, so the child needs `node` on PATH. When codex was resolved from a
    dir that also ships node (nvm / npm-global bin), prepend that dir so the
    shebang resolves even if the wrapper's own PATH lacks it."""
    env = sanitized_child_env()
    bindir = os.path.dirname(os.path.abspath(bin_path)) if os.sep in bin_path else ""
    if bindir and os.path.exists(os.path.join(bindir, "node")):
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    return env


def _initialize_params() -> dict[str, Any]:
    """Declare the capability required by collaborationMode/list and turn/start."""
    return {
        "clientInfo": {"name": "cc-remote", "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    }


class CodexHandle:
    def __init__(self, cfg, cwd: Optional[str] = None,
                 approval_callback: Optional[ApprovalCallback] = None,
                 interaction_callback: Optional[InteractionCallback] = None,
                 goal_callback: Optional[GoalCallback] = None,
                 turn_lifecycle_callback: Optional[TurnLifecycleCallback] = None,
                 runtime_event_callback: Optional[RuntimeEventCallback] = None):
        self.cfg = cfg
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.thread_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.turn_start_pending = False
        self.turn_active = False
        # turn/start ids produced by this wrapper. Codex can flush a rollout for
        # tens of seconds after turn/completed; retaining ids lets the transcript
        # watcher attribute those late records to us instead of to a terminal.
        self._owned_turn_ids: OrderedDict[str, None] = OrderedDict()
        self._cwd = cwd
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._turn_q: Optional[asyncio.Queue] = None
        self._reader: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._thread_settings_updated = asyncio.Event()
        # Human approval can take minutes.  It must not block the sole stdout
        # reader, which still has to consume turn/interrupt and other RPC replies.
        # Keep detached request handlers generation-owned and cancel them on
        # disconnect so an old approval cannot reply to a new app-server process.
        self._server_request_tasks: set[asyncio.Task] = set()
        # POSIX app-server processes get their own group so disconnecting a
        # session also terminates any shell/tool descendants it spawned.
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
        self.last_token_usage: Optional[dict] = None
        self.context_window: Optional[int] = None
        self.app_server_version: Optional[str] = None
        self.last_thread_status: Optional[dict] = None
        self.last_rate_limits: Optional[dict] = None
        self.last_rate_limits_by_id: dict[str, dict] = {}
        self.last_goal: Optional[dict[str, Any]] = None
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
        self.approval: str = codex_approval()                # UI/callback projection
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

    async def connect(self, resume_id: Optional[str] = None, cwd: Optional[str] = None,
                      fork: bool = False) -> None:
        if self.proc is not None:
            await self.disconnect()
        self._cwd = cwd or self._cwd or getattr(self.cfg, "cc_cwd", None) or os.getcwd()
        # version-probes subprocesses on first call; keep it off the event loop.
        codex_bin = await asyncio.to_thread(_resolve_codex_bin)
        proc = await asyncio.create_subprocess_exec(
            codex_bin, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=_codex_env(codex_bin),
            # a single JSON-RPC line can exceed asyncio's default 64KB StreamReader
            # cap (e.g. an image echo or a big tool output) and crash readline —
            # raise it so the reader never dies mid-turn.
            limit=16 * 1024 * 1024,
            start_new_session=(os.name == "posix"),
        )
        self.proc = proc
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
        self._spontaneous_turn_id = None
        self.last_token_usage = None
        self.context_window = None
        self._reader = asyncio.create_task(self._read_loop(proc, generation))
        self._stderr_task = asyncio.create_task(self._drain_stderr(proc, generation))

        try:
            initialized = await self._request(
                "initialize", _initialize_params())
            self.app_server_version = _app_server_version(initialized)
            await self._notify("initialized")

            if fork and resume_id:
                # ephemeral /btw fork: inherits resume_id's context into a throwaway
                # thread; the parent thread is never touched (verified: fork answers
                # from parent context, parent stays coherent).
                res = await self._request("thread/fork", {
                    "threadId": resume_id, "ephemeral": True,
                    "cwd": self._cwd,
                    "approvalPolicy": self.approval_policy})
                self.thread_id = _thread_id_of(res)
            elif resume_id:
                # Do not send local/config defaults here: omitted fields tell
                # app-server to resume the thread's persisted settings. The
                # authoritative response is adopted below.
                res = await self._request("thread/resume", {
                    "threadId": resume_id, "cwd": self._cwd})
                self.thread_id = _thread_id_of(res) or resume_id
            else:
                params: dict[str, Any] = {
                    "cwd": self._cwd,
                    "approvalPolicy": self.approval_policy,
                    "serviceTier": self.service_tier,
                }
                if self.model:
                    params["model"] = self.model
                res = await self._request("thread/start", params)
                self.thread_id = _thread_id_of(res)
            if not self.thread_id:
                raise RuntimeError("codex app-server did not return a thread id")
            if isinstance(res, dict):
                authoritative = res
                if not resume_id:
                    # thread/start has no effort/collaboration params in 0.144.1.
                    # Preserve an explicit new-session first-turn selection instead
                    # of replacing it with the response's config-derived default.
                    authoritative = dict(res)
                    authoritative.pop("reasoningEffort", None)
                self._apply_thread_settings(authoritative)
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
                await self._update_thread_settings(
                    wait_for_notification=True, **sticky)
        except BaseException:
            await self.disconnect()
            raise
        log.info("codex connected", thread_id=self.thread_id, cwd=self._cwd,
                 resume=bool(resume_id), fork=fork)

    async def query(self, prompt, images=None) -> None:
        if self.thread_id and (
            self.proc is None or self._dead or self.proc.returncode is not None
        ):
            await self.force_reconnect(self.thread_id, self._cwd, reason="app-server unavailable")
        assert self.proc is not None and self.thread_id, "connect() first"
        self._turn_q = asyncio.Queue(
            maxsize=max(1, getattr(self.cfg, "turn_reader_queue_cap", 4)))
        params = {
            "threadId": self.thread_id,
            "input": _to_input(prompt, images),
            "approvalPolicy": self.approval_policy,
        }
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        # Codex Plan mode is a collaboration-mode override, not an approval
        # policy.  The app-server schema requires settings.model; null developer
        # instructions selects Codex's built-in instructions for the chosen mode.
        collaboration_model = self.model or codex_model()
        if collaboration_model:
            settings: dict[str, Any] = {
                "model": collaboration_model,
                "developer_instructions": None,
            }
            if self.effort:
                settings["reasoning_effort"] = self.effort
            params["collaborationMode"] = {
                "mode": self.collaboration_mode,
                "settings": settings,
            }
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

    def remember_owned_turn_id(self, turn_id: str) -> None:
        self._owned_turn_ids[turn_id] = None
        self._owned_turn_ids.move_to_end(turn_id)
        while len(self._owned_turn_ids) > _OWNED_TURN_IDS_MAX:
            self._owned_turn_ids.popitem(last=False)

    @property
    def owned_turn_ids(self) -> frozenset[str]:
        return frozenset(self._owned_turn_ids)

    async def receive_response(self):
        """Async-gen of this turn's raw notification dicts, ending at turn/completed."""
        q = self._turn_q
        if q is None:
            return
        try:
            while True:
                msg = await q.get()
                if msg is None:      # sentinel pushed by the reader on turn/completed
                    break
                yield msg
        finally:
            if self._turn_q is q:
                self._turn_q = None
            # An automatic continuation may have started after this managed
            # queue received its terminal sentinel but before its consumer
            # unwound. Do not let the old generator clear the new turn's active
            # ownership (thread-scoped hooks depend on this flag).
            if self._spontaneous_turn_id is None:
                self.turn_active = False

    def _open_spontaneous_stream(self, turn_id: str) -> None:
        """Create the bounded raw-notification bridge before announcing a turn."""
        if self._spontaneous_q is not None:
            self._close_spontaneous_stream(self._spontaneous_queue_turn_id)
        reader_cap = max(1, int(getattr(self.cfg, "turn_reader_queue_cap", 4)))
        item_cap = min(
            _SPONTANEOUS_QUEUE_MAX_ITEMS,
            max(_SPONTANEOUS_QUEUE_MIN_ITEMS, reader_cap * 16),
        )
        ws_cap = max(1024, int(getattr(
            self.cfg, "ws_max_size_bytes", 16 * 1024 * 1024)))
        tool_cap = max(1024, int(getattr(self.cfg, "tool_result_max", 65536)))
        byte_cap = min(
            ws_cap,
            max(_SPONTANEOUS_QUEUE_MIN_BYTES, tool_cap * 16),
        )
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
        if isinstance(turn_id, str) and turn_id:
            turn["id"] = turn_id
        status = raw_turn.get("status")
        if status in {"completed", "interrupted", "failed"}:
            turn["status"] = status
        duration = raw_turn.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            turn["durationMs"] = max(0, int(duration))
        out_params: dict[str, Any] = {"turn": turn}
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            out_params["threadId"] = thread_id
        if isinstance(turn_id, str) and turn_id:
            out_params["turnId"] = turn_id
        return {"method": "turn/completed", "params": out_params}

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
        if self._spontaneous_overflow and not terminal:
            return True
        size = (
            raw_size if isinstance(raw_size, int) and raw_size >= 0
            else self._notification_wire_size(message)
        )
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
        q.clear()
        q.put_nowait(CodexSpontaneousOverflow(turn_id))
        if terminal:
            terminal_message = message
            terminal_size = size
            if terminal_size > q.max_bytes:
                terminal_message = self._minimal_turn_completed(message)
                terminal_size = self._notification_wire_size(terminal_message)
            # The queue always reserves at least two item slots. Byte overflow is
            # impossible for the bounded minimal fallback above.
            q.put_nowait(terminal_message, terminal_size)
        return True

    def _close_spontaneous_stream(self, turn_id: Optional[str]) -> None:
        """Wake the bridge consumer after disconnect/EOF, without blocking stdout."""
        q = self._spontaneous_q
        current = self._spontaneous_queue_turn_id
        if q is None or current is None or (turn_id and turn_id != current):
            return
        closed = CodexSpontaneousClosed(current)
        if q.put_nowait(closed):
            return
        self._spontaneous_overflow = True
        q.clear()
        q.put_nowait(CodexSpontaneousOverflow(current))
        q.put_nowait(closed)

    async def receive_spontaneous_response(self, turn_id: str):
        """Yield exactly one spontaneous turn's raw frames and internal signals."""
        q = self._spontaneous_q
        if q is None or self._spontaneous_queue_turn_id != turn_id:
            return
        try:
            while True:
                item = await q.get()
                yield item
                if isinstance(item, CodexSpontaneousClosed):
                    break
                if isinstance(item, dict) and item.get("method") == "turn/completed":
                    break
        finally:
            if self._spontaneous_q is q:
                self._spontaneous_q = None
                self._spontaneous_queue_turn_id = None
                self._spontaneous_overflow = False

    async def interrupt(self) -> None:
        if self.proc and self.thread_id and self.turn_id:
            try:
                await self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
            except Exception as e:
                log.warning("codex interrupt failed", error=str(e))

    async def disconnect(self) -> None:
        proc = self.proc
        process_group = self._process_group
        spontaneous_turn_id = self._spontaneous_turn_id
        self._close_spontaneous_stream(spontaneous_turn_id)
        self._spontaneous_turn_id = None
        tasks = [t for t in (self._reader, self._stderr_task)
                 if t is not None and t is not asyncio.current_task()]
        server_tasks = [
            task for task in self._server_request_tasks
            if task is not asyncio.current_task()
        ]
        self._server_request_tasks.clear()
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
            # The app-server parent can exit before a tool subprocess that ignored
            # SIGTERM. A final group kill guarantees no descendant survives session
            # eviction, /btw close, or reconnect.
            if process_group is not None:
                stop(signal.SIGKILL, force=True)
        if self._turn_q is not None:
            self._force_turn_sentinel(self._turn_q)
            self._turn_q = None
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

    async def force_reconnect(self, resume_id: Optional[str], cwd: Optional[str] = None,
                              reason: str = "reconnect") -> None:
        log.warning("codex force-reconnect", reason=reason)
        target = resume_id or self.thread_id
        await self.disconnect()
        await self.connect(resume_id=target, cwd=cwd or self._cwd)

    # --- live controls (persisted for this thread by app-server 0.144.1) ---
    @property
    def approval(self) -> str:
        return self._approval

    @approval.setter
    def approval(self, value: str) -> None:
        # Existing machine/tests assign this projection directly. Keep the raw
        # policy in lockstep for named policies; granular snapshots set _approval
        # directly so turn/start can preserve their full official object.
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
            "developer_instructions": None,
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
        if isinstance(approval, str) and approval in {
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

    async def set_model(self, model: str) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("Codex model must be non-empty")
        authoritative = await self._update_thread_settings(
            model=model, wait_for_notification=True)
        if not authoritative:
            self.model = model
        log.info("codex thread model set", requested=model, applied=self.model)

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
        authoritative = await self._update_thread_settings(
            approvalPolicy=mode, wait_for_notification=True)
        if not authoritative:
            self.approval = mode
        log.info("codex thread approval set", requested=mode,
                 applied=self.approval)

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
            "sandbox_mode": _bounded_string(raw_config.get("sandbox_mode"), 64),
            "web_search": _bounded_string(raw_config.get("web_search"), 64),
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
    async def _request(self, method: str, params: Optional[dict] = None):
        self._id += 1
        rid = self._id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        obj = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            obj["params"] = params
        await self._send(obj)
        try:
            return await asyncio.wait_for(fut, timeout=_REQ_TIMEOUT)
        finally:
            self._pending.pop(rid, None)

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

    async def _approval_decision(self, method: str, params: dict) -> str:
        """Return one current-schema approval decision, failing closed."""
        if self.approval == "never":
            return "decline"
        if self.approval not in {"on-request", "untrusted"}:
            log.warning("invalid codex approval policy; denying", approval=self.approval)
            return "decline"
        callback = self.approval_callback
        if callback is None:
            log.warning("codex approval requested without a client callback", method=method)
            return "decline"
        try:
            decision = await asyncio.wait_for(
                callback(method, params), timeout=_APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("codex approval timed out; denying", method=method)
            return "decline"
        except Exception as exc:
            log.warning("codex approval callback failed; denying",
                        method=method, error=str(exc))
            return "decline"
        if decision not in _APPROVAL_DECISIONS:
            log.warning("invalid codex approval decision; denying",
                        method=method, decision=decision)
            return "decline"
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
                await self._respond_error(rid, -32000, "remote interaction callback unavailable")
                return
            try:
                result = await asyncio.wait_for(
                    callback(method, params), timeout=_APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                await self._respond_error(rid, -32001, "remote user input timed out")
                return
            except Exception as exc:
                log.warning("codex interaction callback failed", method=method, error=str(exc))
                await self._respond_error(rid, -32000, "remote user input failed")
                return
            await self._respond(rid, result)
            return

        decision = await self._approval_decision(method, params)
        if method in _LEGACY_APPROVAL_METHODS:
            decision = _LEGACY_DECISIONS[decision]
        await self._respond(rid, {"decision": decision})

    def _server_request_done(self, task: asyncio.Task) -> None:
        """Observe a detached server-request task and keep the set bounded."""
        self._server_request_tasks.discard(task)
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

    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_loop(self, proc: asyncio.subprocess.Process,
                         generation: int) -> None:
        assert proc.stdout
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if generation != self._generation:
                    return
                await self._dispatch(m, raw_size=len(line))
                if self._spontaneous_q is not None:
                    # StreamReader can satisfy many buffered readline() calls
                    # without yielding. Give the independent bridge consumer one
                    # scheduling opportunity per frame; never wait for its relay
                    # I/O or for queue capacity.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("codex read loop ended", error=str(e))
        finally:
            # A stale reader belongs to an app-server generation already replaced
            # by disconnect/reconnect.  Leave the new generation alone, but do not
            # `return` from finally: that would suppress cancellation or an active
            # exception from this reader task.
            if generation == self._generation:
                self._dead = True
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
        if (method in _MODEL_TURN_METHODS
                and (target_thread_id is None or self.thread_id is None)):
            # These 0.144.1 notifications require both threadId and turnId.
            # Unlike legacy error/hook frames, there is no valid thread-scoped
            # form, so a partial payload must never be guessed into this session.
            log.warning("unattributed codex model notification dropped",
                        method=method)
            return False
        if (target_thread_id is not None and self.thread_id is not None
                and target_thread_id != self.thread_id):
            log.warning("foreign codex thread notification dropped", method=method)
            return False
        target_turn_id = _notification_turn_id(message)
        if (not _is_turn_notification(method)
                and method not in {"error", "thread/compacted"}):
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
            return self.turn_active
        if isinstance(method, str) and method.startswith("hook/"):
            return self.turn_active
        log.warning("unattributed codex turn notification dropped", method=method)
        return False

    async def _dispatch(self, m: dict, raw_size: Optional[int] = None) -> None:
        has_id = "id" in m
        has_method = "method" in m
        if has_id and not has_method:                       # response to our request
            fut = self._pending.get(m["id"])
            if fut and not fut.done():
                if "error" in m:
                    fut.set_exception(RuntimeError(str(m["error"])))
                else:
                    fut.set_result(m.get("result"))
            return
        if has_id and has_method:                            # server -> client request
            if len(self._server_request_tasks) >= _MAX_SERVER_REQUEST_TASKS:
                method = m.get("method")
                rid = m.get("id")
                log.warning("codex server request cap reached; rejecting",
                            method=method)
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
            task.add_done_callback(self._server_request_done)
            return
        # notification
        method = m.get("method")
        if not self._notification_is_current(m):
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
            if isinstance(turn_id, str) and turn_id:
                self.turn_id = turn_id
                self.remember_owned_turn_id(turn_id)
            self.turn_active = True
            if not was_active and isinstance(turn_id, str) and turn_id:
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
                    await self._publish_goal(goal)
        elif method == "thread/goal/cleared":
            params = m.get("params") or {}
            if params.get("threadId") == self.thread_id:
                self.last_goal = None
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
        if self._turn_q is not None and _is_turn_queue_notification(method):
            queue = self._turn_q
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

    @staticmethod
    def _force_turn_sentinel(queue: asyncio.Queue) -> None:
        """Wake a consumer during disconnect even when the bounded queue is full."""
        try:
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
