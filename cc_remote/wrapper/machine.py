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
import json
import os
import re
import signal
import shutil
import stat
import tempfile
import time
from collections import OrderedDict
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

from cc_remote.attachments import decode_attachment, validate_attachments
from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    ASK_OPTION_MAX_COUNT, FILE_PREVIEW_MAX_BYTES, PREVIEW_ASSET_MAX_BYTES,
    Error, Hello, Query, Interrupt, CommandAck, Model, Models, Effort, Fast,
    CollaborationMode, Perm, BtwOpened, ContextReport, StatusReport, Notice,
    RateLimitUpdate, DiffReport, FilePreview, PreviewAsset, History, AskUser,
    GoalState, Pong, Snapshot, StateEvent, State, TakeoverState, UserMsg,
    TurnEnd, TurnResult, is_downstream, is_reliable_command,
    SessionInfo, SessionList, SessionFocus, SessionRekey, SessionForked, DirList,
    ERR_BUSY, ERR_NOT_RUNNING, ERR_BAD_PROMPT, ERR_DRAIN_TIMEOUT,
    ERR_CC_CRASH, ERR_INTERNAL, ERR_AUTH, ERR_PROTOCOL,
    ERR_FORK_RECONCILING,
)
from cc_remote.wrapper.ringbuffer import RingBuffer
from cc_remote.wrapper.ask import make_ask_server
from cc_remote.wrapper.sdk import CLAUDE_DEFAULT_EFFORT, SdkHandle
from cc_remote.wrapper.session import load_session_id, save_session_id
from cc_remote.wrapper.session_ctx import SessionContext
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_session_id, extract_model,
    translate_history, last_assistant_model, transcript_timestamps, transcript_path,
    translate_subagent_history, merge_subagent_history,
)
from cc_remote.wrapper.codex_handle import (
    CodexHandle, CodexSpontaneousClosed, CodexSpontaneousOverflow,
)
from cc_remote.wrapper.codex_stream import (
    CodexStreamTranslator, codex_session_id, is_turn_terminal, codex_translate_history,
)
from cc_remote.wrapper.codex_sessions import (
    list_codex_sessions, codex_session_cwd, codex_rollout_path, codex_model,
    codex_session_settings,
)
from cc_remote.wrapper.codex_models import codex_catalog, clamp_effort
from cc_remote.wrapper.codex_rpc import (
    CodexRpcOutcomeUnknown, CodexRpcRejected, codex_rpc,
)
from cc_remote.wrapper.codex_worktrees import (
    WorktreeError, prepare_worktree, rollback_worktree,
)
from cc_remote.wrapper.codex_forks import (
    CodexForkJournal, ForkJournalError, find_rollout_fork,
    fork_thread_source,
)
from cc_remote.wrapper.claude_forks import (
    ClaudeForkJournal, ClaudeForkJournalError, claude_fork_marker,
    find_claude_fork,
)
from cc_remote.wrapper.claude_external import claude_session_holders
from cc_remote.wrapper.codex_external import (
    ProcessIdentity, parse_turn_markers, process_identity,
    writable_rollout_holders,
)
from cc_remote.wrapper.transport import WrapperTransport

log = logger("cc_remote.wrapper.machine")

CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})
CODEX_PERMISSION_MODES = frozenset({"never", "on-request", "untrusted"})
CODEX_COLLABORATION_MODES = frozenset({"default", "plan"})
CODEX_FAST_SERVICE_TIERS = frozenset({"fast", "priority"})


def _codex_fast_on(value: Optional[str]) -> bool:
    """0.144.1 accepts ``fast`` but reports the persisted tier as ``priority``."""
    return value in CODEX_FAST_SERVICE_TIERS


def _session_permission_mode(ctx: SessionContext) -> str:
    """Return the permission mode actually configured on this live engine."""
    if ctx.engine == "codex":
        return getattr(ctx.sdk, "approval", "never")
    return getattr(ctx.sdk, "permission_mode", "bypassPermissions")


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


class _ForkOutcomeUncertain(RuntimeError):
    """A persistent fork may have committed and must not be ACKed/replayed."""


class WrapperMachine:
    # Browser/TUI outboxes hold at most 256 commands. Keep a 2x retry window per
    # client and at most the relay's configured maximum of 64 client identities;
    # cached one-shot responses must not become an unbounded second history store.
    COMMAND_IDS_PER_CLIENT = 512
    COMMAND_CLIENTS = 64
    SESSION_ALIAS_CAP = 256
    SESSION_ALIAS_TTL = 7 * 24 * 3600
    SESSION_ALIAS_FILE_MAX_BYTES = 2 * 1024 * 1024
    # A worst-case 4096-byte cwd can expand ~6x under JSON escaping. 64 entries
    # therefore still fit the 2 MiB state-file cap while matching the maximum
    # number of resident sessions allowed by config validation.
    PRIVATE_BTW_CAP = 64
    PRIVATE_BTW_FILE_MAX_BYTES = 2 * 1024 * 1024
    CLAUDE_SETTINGS_MAX_BYTES = 1024 * 1024
    BG_JOB_SCAN_MAX = 1_000
    BG_JOB_STATE_MAX_BYTES = 64 * 1024
    FORK_RECONCILE_ATTEMPTS = 4
    FORK_RECONCILE_DELAY = 0.1
    FORK_BACKGROUND_ATTEMPTS = 100
    UNCERTAIN_FORK_CAP = 4096
    MARKDOWN_PREVIEW_SUFFIXES = frozenset({".md", ".markdown"})
    PREVIEW_ASSET_MEDIA_TYPES = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".avif": "image/avif",
    }
    SAFE_RETRY_COMMANDS = frozenset({
        "list_sessions", "get_history", "get_models",
        "get_context", "get_status", "get_diff", "get_file_preview",
        "get_preview_asset", "get_goal", "list_dir",
    })
    # Commands whose target is a runtime ``sid``.  A /btw runtime is private to
    # the client that created it, so every operation against that sid must pass
    # the owner check before its handler is allowed to read or mutate state.
    BTW_SID_COMMANDS = frozenset({
        "query", "interrupt", "takeover", "set_model", "set_effort",
        "set_service_tier", "set_collaboration_mode", "open_btw", "close_btw", "set_perm",
        "get_context", "get_status", "get_diff", "get_file_preview",
        "get_preview_asset", "answer_question", "get_goal", "set_goal", "clear_goal",
    })
    # These commands address a session through ``session_id`` instead.
    BTW_SESSION_COMMANDS = frozenset({
        "get_history", "switch_session", "rename_session", "archive_session",
        "fork_session", "fork_session_worktree",
    })

    def __init__(self, cfg: WrapperConfig, transport: WrapperTransport):
        self.cfg = cfg
        self.transport = transport
        self.instance_id = uuid4().hex
        # Pool of resident sessions, keyed by real session_id (or a `tmp-<uuid>`
        # temp key for a brand-new session until its id is captured).
        self.sessions: dict[str, SessionContext] = {}
        self.focused_sid: Optional[str] = None  # pool key of the viewed session
        self.transport.on_connected = self._on_transport_connected
        # Transcript mirror: sessions a client has opened (registered on GetHistory),
        # sid -> {"path", "size", "engine"}. The watcher polls each file's SIZE and,
        # when it grows without us having written it, mirrors the append to clients.
        self._watch: dict[str, dict] = {}
        self._watch_task: Optional[asyncio.Task] = None
        self._codex_watch_lock = asyncio.Lock()
        self._codex_probe_warned = False
        self._claude_probe_warned = False
        # In-memory at-most-once window for client retries. The outer and inner
        # OrderedDicts are both bounded; wrapper process restart intentionally
        # resets this window (documented residual risk, not durable exactly-once).
        self._processed_commands: OrderedDict[
            str, OrderedDict[str, tuple[object, ...]]
        ] = OrderedDict()
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

    # ---- pool helpers ----

    def _focused_ctx(self) -> Optional[SessionContext]:
        return self.sessions.get(self.focused_sid) if self.focused_sid else None

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
            ctx = await self._spawn(
                resume_id=bootstrap_sid, cwd=self.cfg.cc_cwd, bootstrap=True)
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

            self._watch_task = asyncio.create_task(self._watch_loop())
            async for cmd in self.transport.incoming():
                if cmd.type == "get_models":
                    self._start_models_command(cmd)
                    continue
                await self._process_command_safely(cmd)
        finally:
            models_tasks = list(self._models_command_tasks.values())
            for task in models_tasks:
                task.cancel()
            if models_tasks:
                await asyncio.gather(*models_tasks, return_exceptions=True)
            self._models_command_tasks.clear()
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
            await self.transport.stop()
            for c in list(self.sessions.values()):
                disconnected = False
                try:
                    await c.sdk.disconnect()
                    disconnected = True
                except Exception:
                    pass
                if c.btw and c.engine != "codex" and c.btw_real_id:
                    await self._delete_private_btw(
                        c.btw_real_id, c.cwd, forget=disconnected)

    async def _on_transport_connected(self) -> None:
        ctx = self._focused_ctx()
        await self.transport.send(Hello(
            role="wrapper",
            wrapper_generation=self.instance_id,
            cc_session_id=ctx.session_id if ctx else None,
            state=(ctx.state if ctx else "idle"),
            buffer_head_seq=(ctx.buffer.head_seq if ctx else 0),
            buffer_tail_seq=(ctx.buffer.tail_seq if ctx else 0),
        ))

    # ---- emit (per-ctx seq + buffer + best-effort send), serialized per ctx ----

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
        if is_downstream(msg):
            msg.seq = ctx.next_seq()
            ctx.buffer.append(msg)
        await self.transport.send(msg)

    async def _emit(self, ctx: SessionContext, msg) -> None:
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

    async def _set_state(self, ctx: SessionContext, state: State) -> None:
        ctx.state = state
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
        bucket = self._processed_commands.get(client_id)
        if bucket is None:
            bucket = OrderedDict()
            self._processed_commands[client_id] = bucket
        bucket[cmd_id] = responses
        bucket.move_to_end(cmd_id)
        self._processed_commands.move_to_end(client_id)
        while len(bucket) > self.COMMAND_IDS_PER_CLIENT:
            bucket.popitem(last=False)
        while len(self._processed_commands) > self.COMMAND_CLIENTS:
            self._processed_commands.popitem(last=False)

    def _rekey_cached_create_responses(self, old_key: str, session_id: str,
                                       cwd: str) -> None:
        """Keep a cached create response usable if id capture happened while its
        original Focus/ACK were lost. Replay re-key first, then focus the real id."""
        for bucket in self._processed_commands.values():
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
        t = cmd.type
        if t == "hello" and cmd.role == "client":
            await self._handle_client_hello(cmd)
        elif t == "query":
            return await self._handle_query(cmd)
        elif t == "interrupt":
            await self._handle_interrupt(cmd)
        elif t == "takeover":
            return await self._handle_takeover(cmd)
        elif t == "set_model":
            await self._handle_set_model(cmd)
        elif t == "set_effort":
            await self._handle_set_effort(cmd)
        elif t == "set_service_tier":
            await self._handle_set_service_tier(cmd)
        elif t == "set_collaboration_mode":
            await self._handle_set_collaboration_mode(cmd)
        elif t == "open_btw":
            return await self._handle_open_btw(cmd)
        elif t == "close_btw":
            await self._handle_close_btw(cmd)
        elif t == "set_perm":
            await self._handle_set_perm(cmd)
        elif t == "get_context":
            await self._handle_get_context(cmd)
        elif t == "get_status":
            return await self._handle_get_status(cmd)
        elif t == "get_diff":
            await self._handle_get_diff(cmd)
        elif t == "get_file_preview":
            return await self._handle_get_file_preview(cmd)
        elif t == "get_preview_asset":
            return await self._handle_get_preview_asset(cmd)
        elif t == "get_history":
            await self._handle_get_history(cmd)
        elif t == "get_models":
            await self._handle_get_models(cmd)
        elif t == "answer_question":
            await self._handle_answer_question(cmd)
        elif t == "get_goal":
            return await self._handle_get_goal(cmd)
        elif t == "set_goal":
            return await self._handle_set_goal(cmd)
        elif t == "clear_goal":
            return await self._handle_clear_goal(cmd)
        elif t == "list_sessions":
            await self._handle_list_sessions(cmd)
        elif t == "switch_session":
            return await self._handle_switch_session(cmd)
        elif t == "new_session":
            return await self._handle_new_session(cmd)
        elif t == "list_dir":
            await self._handle_list_dir(cmd)
        elif t == "rename_session":
            await self._handle_rename_session(cmd)
        elif t == "archive_session":
            await self._handle_archive_session(cmd)
        elif t == "fork_session":
            return await self._handle_fork_session(cmd)
        elif t == "fork_session_worktree":
            return await self._handle_fork_session_worktree(cmd)
        elif t == "ping":
            await self._emit_focused(Pong(n=cmd.n))
        else:
            log.warning("unexpected command", type=t, role=getattr(cmd, "role", None))

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
    WATCH_READ_MAX = 4 * 1024 * 1024
    CODEX_TAIL_READ_MAX = 16 * 1024 * 1024
    MIRROR_LIMIT = 60       # turns per mirrored push (matches the client's initial page)
    WATCH_MAX = 32          # cap on simultaneously watched transcripts

    def _ctx_by_sid(self, sid: str) -> Optional[SessionContext]:
        sid = self._resolve_session_alias(sid) or sid
        return self.sessions.get(sid) or next(
            (c for c in self.sessions.values() if c.session_id == sid), None)

    def _is_external(self, sid: str) -> bool:
        """Whether a stable external owner (or incomplete scan) blocks Remote."""
        w = self._watch.get(sid)
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

    def _watch_session(self, sid: str) -> None:
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
            if ctx is None or existing.get("engine") == ctx.engine:
                if (ctx is not None and ctx.engine == "claude"
                        and not existing.get("cwd")):
                    existing["cwd"] = ctx.cwd
                return
            # A cold history request may have registered before the resident
            # engine was known. Correct the watcher once the real context exists.
            self._watch.pop(sid, None)
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
                (watched_sid for watched_sid in self._watch
                 if not self._is_external(watched_sid)), None)
            if victim is None:
                log.warning("transcript watch cap reached; all watches are external",
                            session_id=sid)
                return
            self._watch.pop(victim, None)
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
                "pending_wrapper_turns": {},
                "takeover_holders": set(),
                "takeover_interactive_holders": set(),
                "takeover_pending": None,
                "partial": tail_partial,
            })
            if active_turns:
                watch["external_ts"] = time.time()
        else:
            watch.update({
                "cwd": ctx.cwd if ctx is not None else None,
                "external": False,
                "holders": set(),
                "takeover_pending": False,
                "file_available": True,
                # Fail closed until the first real process scan completes.
                "scan_complete": False,
            })
        self._watch[sid] = watch
        log.info("watching transcript", session_id=sid, engine=engine, size=st.st_size)

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

    async def _prime_claude_ownership(self, sid: str) -> bool:
        """Synchronously close the watcher interval before History/Query."""
        watch = self._watch.get(sid)
        if not watch or watch.get("engine") != "claude":
            return False
        async with self._codex_watch_lock:
            paths, cwds = self._claude_watch_inputs(sid)
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
    ) -> tuple[set[ProcessIdentity], set[ProcessIdentity]]:
        """Return (interactive owners, all non-ignored external writers)."""
        raw = set(scan.holders.get(sid, ()))
        seeded: set[str] = w.setdefault("seeded_external_turns", set())
        if seeded and scan.complete:
            # A tail marker is only a cold-start candidate. Without any live
            # foreign writer, an unmatched historical task_started is a crashed
            # orphan and must not manufacture a fresh 60-second read-only lock.
            if not raw:
                active = w.setdefault("active_external_turns", {})
                for turn_id in seeded:
                    active.pop(turn_id, None)
            seeded.clear()
        ignored: set[ProcessIdentity] = w.setdefault("takeover_holders", set())
        ignored_interactive: set[ProcessIdentity] = w.setdefault(
            "takeover_interactive_holders", set())
        if scan.complete:
            # Exact start ticks make this safe against PID reuse. Once a captured
            # process exits, a later process with the same PID is a new owner.
            ignored.intersection_update(raw)
            ignored_interactive.intersection_update(raw)
        passive = set(scan.passive_holders.get(sid, ()))
        writers = raw.difference(ignored)
        return writers.difference(passive), writers

    async def _prime_codex_ownership(self, sid: str) -> bool:
        """Atomically consume growth and refresh one owner before History/Query."""
        w = self._watch.get(sid)
        if not w or w.get("engine") != "codex":
            return False
        async with self._codex_watch_lock:
            scan = await self._probe_codex_holders({sid: w["path"]})
            holders, writers = self._codex_holder_sets(w, scan, sid)
            if not scan.complete:
                holders.update(w.get("holders", ()))
                writers.update(w.get("writers", ()))
            await self._poll_codex_watch(
                sid, w, holders, time.time(), writers=writers,
                ownership_scan_complete=scan.complete)
            return bool(w.get("external"))

    @classmethod
    def _codex_tail_state(cls, path: str, size: int) -> tuple[set[str], bytes]:
        """Best-effort seed when a watch begins during an external Codex turn."""
        try:
            start = max(0, size - cls.CODEX_TAIL_READ_MAX)
            with open(path, "rb") as stream:
                stream.seek(start)
                data = stream.read(cls.CODEX_TAIL_READ_MAX)
        except OSError:
            return set(), b""
        if start:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return set(), b""
        markers = parse_turn_markers(data)
        # A Codex thread has one current turn. Historical crash/orphan starts can
        # lack a matching terminal record, so set subtraction would resurrect an
        # ancient turn forever. The last ordered lifecycle marker is authoritative.
        active: set[str] = set()
        for kind, turn_id in markers.ordered:
            active = {turn_id} if kind == "task_started" else set()
        return active, markers.partial

    @classmethod
    def _read_watch_growth(cls, path: str, offset: int, available: int) -> bytes:
        with open(path, "rb") as stream:
            stream.seek(offset)
            return stream.read(min(max(0, available), cls.WATCH_READ_MAX))

    async def _push_mirrored_history(self, sid: str) -> History:
        hist = await self._build_history(sid, limit=self.MIRROR_LIMIT)
        hist.sid = sid
        await self.transport.send(hist)
        return hist

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
        ownership_scan_complete: bool = True,
    ) -> None:
        if writers is None:
            writers = set(holders)
        was_external = bool(w.get("external"))
        w["holders"] = holders
        w["writers"] = writers
        external_growth = False
        takeover_cleared = False
        takeover_clear_message: Optional[str] = None
        data = b""
        try:
            st = await asyncio.to_thread(os.stat, w["path"])
        except OSError:
            return

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

        ctx = self._ctx_by_sid(sid)
        sdk = ctx.sdk if ctx is not None else None
        own_turn_ids = set(getattr(sdk, "owned_turn_ids", ())) if sdk else set()
        active: dict[str, float] = w["active_external_turns"]
        pending: dict[str, dict[str, object]] = w["pending_wrapper_turns"]
        wrapper_may_own_turn = bool(
            sdk is not None and (
                getattr(sdk, "turn_start_pending", False)
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
                awaiting_rpc and not getattr(sdk, "turn_start_pending", False))
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
                        "awaiting_rpc": bool(
                            getattr(sdk, "turn_start_pending", False)),
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

        # An interactive TUI owns the session even while idle. Headless app-server
        # daemons are filtered from holders and lock only while an external turn's
        # task_started marker remains unmatched.
        is_external = bool(holders or active or w.get("takeover_pending"))
        w["external"] = is_external
        if (external_growth or (is_external and not was_external)) and ctx is not None:
            ctx.needs_reload = True
            # A native Codex turn may itself switch `/plan`. Mirror that control
            # state with the transcript so Remote does not keep a stale override.
            await self._refresh_codex_collaboration_mode(ctx)

        if external_growth or is_external != was_external or takeover_cleared:
            log.info(
                "codex external ownership changed" if is_external != was_external
                else "codex external rollout append -> mirroring",
                session_id=sid,
                external=is_external,
                holders=len(holders),
                active_turns=len(active),
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
                grew = size - w["size"]
                w["size"] = size
                # A stable foreign process always wins. With no foreign process,
                # only a query that this wrapper has actually launched may claim
                # growth as its own; generic running state and post-turn TTLs are
                # deliberately insufficient.
                external_growth = bool(holders) or not self._own_write(sid)
                log.info(
                    "Claude transcript append observed",
                    session_id=sid,
                    bytes=grew,
                    external=external_growth,
                    holders=len(holders),
                    resident=ctx is not None,
                )

        if external_growth and ctx is not None:
            w["external_ts"] = now
            ctx.external_ts = now
            ctx.needs_reload = True

        is_external = self._is_external(sid)
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
                    holders, writers = self._codex_holder_sets(w, scan, sid)
                    if not scan.complete:
                        holders.update(w.get("holders", ()))
                        writers.update(w.get("writers", ()))
                    await self._poll_codex_watch(
                        sid, w, holders, now, writers=writers,
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
                await self._poll_watches_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("transcript watch loop error")

    async def _build_history(self, sid: str, before=None, limit=None, cwd_hint=None) -> History:
        """Read a session's transcript and assemble ONE History frame. Shared by
        GetHistory (routed to the requester) and the watcher (broadcast on external
        append). No spawn, no ring buffer; the parse runs in a thread."""
        # Reading requires the session's own cwd (transcript lives under it).
        # Prefer a resident ctx's cwd, else the client-provided cwd, else default.
        ctx = self._ctx_by_sid(sid)
        in_progress = bool(ctx is not None and ctx.state != "idle")
        events: list = []
        mdl = None
        watched_engine = (self._watch.get(sid) or {}).get("engine")
        is_codex_hist = bool(
            (ctx is not None and ctx.engine == "codex") or watched_engine == "codex")
        source_path = None
        try:
            source_path = await asyncio.to_thread(
                codex_rollout_path if is_codex_hist else transcript_path, sid)
            if (source_path
                    and await asyncio.to_thread(os.path.getsize, source_path)
                    > self.cfg.history_source_max_bytes):
                notice = Error(
                    code=ERR_INTERNAL,
                    message=("历史文件超过安全读取上限；请调大 "
                             "HISTORY_SOURCE_MAX_BYTES 或在终端中查看"),
                    sid=sid,
                )
                return History(
                    session_id=sid,
                    events=[notice.model_dump(mode="json")],
                    has_more=False,
                    before=before,
                    external=self._is_external(sid),
                    takeover_pending=bool(
                        (self._watch.get(sid) or {}).get("takeover_pending")),
                    in_progress=in_progress,
                )
        except OSError:
            source_path = None
        if is_codex_hist:
            # Codex history lives in ~/.codex/sessions rollout files, not the
            # Claude transcript store.
            try:
                path = source_path or await asyncio.to_thread(codex_rollout_path, sid)
                if path:
                    events, mdl = await asyncio.to_thread(
                        codex_translate_history, path, self.cfg.tool_result_max)
            except Exception as e:
                log.warning("codex get_history failed", session_id=sid, error=str(e))
        else:
            directory = (ctx.cwd if ctx else None) or cwd_hint or self.cfg.cc_cwd
            try:
                def _read():
                    return (get_session_messages(sid, directory=directory),
                            transcript_timestamps(sid))
                msgs, tss = await asyncio.to_thread(_read)
                events = translate_history(msgs, self.cfg.tool_result_max, timestamps=tss)
                subagent_events = await asyncio.to_thread(
                    translate_subagent_history, sid, self.cfg.tool_result_max)
                events = merge_subagent_history(events, subagent_events)
                mdl = last_assistant_model(msgs)
            except Exception as e:
                log.warning("get_history failed", session_id=sid, error=str(e))
        for ev in events:
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
            return History(
                session_id=sid,
                events=payload,
                has_more=effective_start > 0,
                before=before,
                oldest_id=(_tid(selected[0]) if selected else None),
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
        return history

    async def _handle_get_history(self, cmd) -> None:
        """Client opened a session: return its history as ONE bulk frame, routed to
        the requester — like a web chat's GET /conversation. Opening it also starts
        MIRRORING its transcript, so appends made by a native Claude/Codex process
        stream through to every client (read-only)."""
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        self._watch_session(sid)   # snapshot size before we read, so no append is missed
        watched_engine = (self._watch.get(sid) or {}).get("engine")
        # Do not leave a short ownership window while waiting for the 1.5s poll.
        if watched_engine == "codex":
            await self._prime_codex_ownership(sid)
        elif watched_engine == "claude":
            await self._prime_claude_ownership(sid)
        hist = await self._build_history(
            sid, before=getattr(cmd, "before", None), limit=getattr(cmd, "limit", None),
            cwd_hint=getattr(cmd, "cwd", None))
        client_id = getattr(cmd, "client_id", None)
        if client_id:
            hist.to = client_id            # relay routes it to just this client
        hist.sid = sid                     # tag with THIS session (never the focused one)
        await self.transport.send(hist)
        log.info("history sent", session_id=sid, events=len(hist.events),
                 has_more=hist.has_more, before=bool(hist.before),
                 external=hist.external, client_id=client_id)

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

        resolved_sid = ctx.session_id or sid
        if not resolved_sid:
            error = Error(code=ERR_NOT_RUNNING, message="该会话尚无可接管的会话 ID")
            await self._emit(ctx, error)
            return error
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
                holders, writers = self._codex_holder_sets(
                    w, scan, resolved_sid)
                await self._poll_codex_watch(
                    resolved_sid, w, holders, time.time(), writers=writers,
                    ownership_scan_complete=scan.complete)
                if w["active_external_turns"]:
                    w["takeover_pending"] = {
                        "writers": set(writers),
                        "interactive": set(holders),
                        "turn_ids": set(w["active_external_turns"]),
                    }
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
                paths, cwds = self._claude_watch_inputs(resolved_sid)
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
                    await self._push_mirrored_history(resolved_sid)
                    await self._emit(ctx, TakeoverState(
                        pending=True,
                        message=("已登记接管；退出当前终端 Claude 后会自动交给 "
                                 "Remote，期间不会允许双端同时发送"),
                    ))
                    log.info(
                        "Claude takeover queued until terminal exit",
                        session_id=resolved_sid,
                        holders=len(holders),
                    )
                    return None
                w["takeover_pending"] = False
                w["external"] = False
                w["scan_complete"] = True
                ctx.external_ts = 0.0
                ctx.needs_reload = True

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
        if ctx.state != "idle":
            error = Error(
                code=ERR_BUSY, message="该会话正忙,先 interrupt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        if ctx.session_id:
            self._watch_session(ctx.session_id)
            external = (
                await self._prime_codex_ownership(ctx.session_id)
                if ctx.engine == "codex"
                else await self._prime_claude_ownership(ctx.session_id)
            )
            if external:
                engine_name = "Codex" if ctx.engine == "codex" else "Claude"
                error = Error(
                    code=ERR_BUSY,
                    message=(f"该 {engine_name} 会话正在被本机终端使用，或终端状态"
                             "暂不可确认；请退出终端或点击『接管』"),
                    msg_id=getattr(cmd, "msg_id", None),
                )
                await self._emit(ctx, error)
                return error
        if not cmd.prompt and not cmd.images and not cmd.files:
            error = Error(
                code=ERR_BAD_PROMPT, message="empty prompt",
                msg_id=getattr(cmd, "msg_id", None))
            await self._emit(ctx, error)
            return error
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message=f"invalid attachments: {attachment_error}",
                msg_id=getattr(cmd, "msg_id", None),
            )
            await self._emit(ctx, error)
            return error
        # claim synchronously so a concurrent query on THIS ctx can't race in
        ctx.interrupt_event.clear()
        ctx.interrupt_deadline = None
        ctx.active_msg_id = cmd.msg_id
        ctx.state = "running"
        async with ctx.emit_lock:
            await self._emit_locked(ctx, StateEvent(state="running"))
        ctx.turn_task = asyncio.create_task(
            self._run_turn(ctx, cmd.prompt, getattr(cmd, "images", None), getattr(cmd, "files", None)))

    async def _handle_interrupt(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            await self._emit_focused(Error(code=ERR_NOT_RUNNING, message="no active session"))
            return
        if ctx.state != "running":
            await self._emit(ctx, Error(code=ERR_NOT_RUNNING, message="该会话没有正在运行的回合"))
            return
        # Set the deadline and wake the turn consumer before entering any await:
        # it may already be blocked in the queue.get() that began while running.
        ctx.interrupt_deadline = (
            asyncio.get_running_loop().time() + self.cfg.drain_timeout
        )
        ctx.state = "interrupting"
        ctx.interrupt_event.set()
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
            except Exception as e:
                log.exception("interrupt call failed", error=str(e))
                await self._emit(ctx, Error(
                    code=ERR_INTERNAL, message=f"interrupt failed: {e}"))
                # leave interrupting; the drain timeout will recover

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
        live CLI, so an absent explicit value deliberately returns None. The UI
        then keeps the honest generic "local default" label.
        """
        root = cls._claude_project_root(cwd)
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            user_settings = os.path.join(
                os.path.realpath(os.path.expanduser(config_dir)),
                "settings.json")
        else:
            user_settings = os.path.expanduser("~/.claude/settings.json")
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

        # Managed policy cannot be overridden. Otherwise the launch environment
        # wins over settings, and settings-provided ANTHROPIC_MODEL wins over the
        # scalar model field just as it does in Claude Code.
        external_model_set, external_model = explicit_model(
            os.environ.get("ANTHROPIC_MODEL"))
        if managed_env_model_set:
            return managed_env_model
        if managed_model_set:
            return managed_model
        if external_model_set:
            return external_model
        if settings_env_model_set:
            return settings_env_model
        return model if model_set else None

    async def _claude_new_session_defaults(
        self, cwd: Optional[str],
    ) -> tuple[Optional[str], str]:
        raw_cwd = cwd or self.cfg.cc_cwd
        target_cwd = os.path.realpath(os.path.expanduser(raw_cwd))
        if not os.path.isdir(target_cwd):
            return None, CLAUDE_DEFAULT_EFFORT
        model = await asyncio.to_thread(
            self._claude_configured_model, target_cwd)
        return model, CLAUDE_DEFAULT_EFFORT

    async def _handle_get_models(self, cmd) -> None:
        """Answer with the engine's catalog and effective new-session defaults.

        Codex exposes its catalog through app-server. Claude has no side-effect-
        free catalog/default RPC, so its list stays empty while bounded settings
        reads resolve an explicit cwd-aware model; the client keeps its static
        presentation table.
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

    async def _handle_set_model(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            await ctx.sdk.set_model(cmd.model)
            applied_model = getattr(ctx.sdk, "model", None) or cmd.model
            ctx.announced_model = applied_model
            await self._emit(ctx, Model(model=applied_model))
            if ctx.engine == "codex":
                # thread/settings/updated is authoritative. app-server may adjust
                # effort when the selected model cannot use the old level; never
                # overwrite that decision with a Web-side guess or stale chip.
                applied = getattr(ctx.sdk, "effort", None)
                if applied and applied != ctx.announced_effort:
                    ctx.announced_effort = applied
                    await self._emit(ctx, Effort(effort=applied))
        except Exception as e:
            log.exception("set_model failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_model failed: {e}"))

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

    async def _handle_set_effort(self, cmd) -> None:
        # cc: effort is a spawn-time flag (--effort), so we can't flip it on the live
        # subprocess — record it and let _run_turn respawn-with-resume lazily at the
        # next turn (an idle tweak then costs no tokens).
        # codex: effort is a per-turn turn/start param, so the live session honors it
        # immediately — no reconnect needed.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        if ctx.engine == "codex":
            applied = await self._apply_codex_effort(ctx, cmd.effort) or cmd.effort
            ctx.announced_effort = applied
            await self._emit(ctx, Effort(effort=applied))
            log.info("effort set", sid=ctx.session_id, effort=applied,
                     requested=cmd.effort, engine=ctx.engine)
            return
        ctx.sdk.effort = cmd.effort
        ctx.announced_effort = cmd.effort
        await self._emit(ctx, Effort(effort=cmd.effort))
        log.info("effort set", sid=ctx.session_id, effort=cmd.effort, engine=ctx.engine)

    async def _handle_set_service_tier(self, cmd) -> None:
        # Codex Fast is a thread setting in app-server 0.144.1. Never mutate the
        # user's global config.toml and never leak one session's choice to another.
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None or ctx.engine != "codex":
            return
        if cmd.service_tier == "toggle":
            on = not _codex_fast_on(
                getattr(ctx.sdk, "service_tier", None))
        else:
            on = (cmd.service_tier == "fast")
        try:
            await ctx.sdk.set_service_tier("fast" if on else None)
            applied_on = _codex_fast_on(
                getattr(ctx.sdk, "service_tier", None))
            await self._emit(ctx, Fast(on=applied_on))
            log.info("codex thread service tier set", sid=ctx.session_id,
                     requested=on, applied=applied_on)
        except Exception as e:
            log.exception("set_service_tier failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_service_tier failed: {e}"))

    async def _handle_set_collaboration_mode(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        if ctx.engine != "codex" or cmd.mode not in CODEX_COLLABORATION_MODES:
            await self._emit(ctx, Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持 Codex 协作模式 {cmd.mode!r}; "
                         "可选: default, plan"),
            ))
            return
        try:
            await ctx.sdk.set_collaboration_mode(cmd.mode)
            applied = getattr(ctx.sdk, "collaboration_mode", cmd.mode)
            ctx.announced_collaboration_mode = applied
            await self._emit(ctx, CollaborationMode(mode=applied))
        except Exception as e:
            log.exception("set_collaboration_mode failed", error=str(e))
            await self._emit(ctx, Error(
                code=ERR_INTERNAL,
                message=f"set_collaboration_mode failed: {e}",
            ))

    async def _refresh_codex_collaboration_mode(
            self, ctx: SessionContext) -> None:
        """Adopt settings persisted by a native Codex turn's rollout tail."""
        if ctx.engine != "codex" or not ctx.session_id:
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
        if (approval in CODEX_PERMISSION_MODES
                and ctx.sdk.approval != approval):
            ctx.sdk.approval = approval
            ctx.announced_perm = approval
            await self._emit(ctx, Perm(mode=approval))
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
            generation=self.instance_id)
        snap.sid = btw.key
        if cid:
            snap.to = cid
        await self.transport.send(snap)
        permission_mode = _session_permission_mode(btw)
        btw.announced_perm = permission_mode
        permission = Perm(mode=permission_mode, sid=btw.key, to=cid)
        await self.transport.send(permission)
        if btw.engine == "codex":
            # BtwOpened + Snapshot create the owner-only browser runtime before
            # pending app-server notices are released into that route.
            await btw.sdk.activate_runtime_events()
        log.info("btw opened", btw_sid=btw.key, parent=parent.session_id, client_id=cid)
        # All three one-shot frames are required to reconstruct the fork after a
        # lost response. Reliable-command retries replay them without re-forking.
        return ev, snap, permission

    async def _handle_close_btw(self, cmd) -> None:
        sid = getattr(cmd, "sid", None)
        ctx = self.sessions.get(sid) if sid else None
        if ctx is None or not ctx.btw:
            return
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
        # Codex forks are ephemeral (no rollout). Claude fork_session persists a
        # transcript under btw_real_id; keep its tombstone on deletion failure so
        # it stays hidden and cannot be cold-resumed.
        if ctx.engine != "codex" and ctx.btw_real_id:
            await self._delete_private_btw(
                ctx.btw_real_id, ctx.cwd, forget=disconnected)
        log.info("btw closed", btw_sid=sid)

    async def _handle_set_perm(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        allowed = (CODEX_PERMISSION_MODES if ctx.engine == "codex"
                   else CLAUDE_PERMISSION_MODES)
        if cmd.mode not in allowed:
            log.warning("invalid permission mode", sid=ctx.session_id,
                        engine=ctx.engine, mode=cmd.mode)
            await self._emit(ctx, Error(
                code=ERR_INTERNAL,
                message=(f"{ctx.engine} 不支持权限模式 {cmd.mode!r}; "
                         f"可选: {', '.join(sorted(allowed))}"),
            ))
            return
        try:
            await ctx.sdk.set_permission_mode(cmd.mode)
            applied = getattr(ctx.sdk, "approval", cmd.mode)
            ctx.announced_perm = applied
            await self._emit(ctx, Perm(mode=applied))
        except Exception as e:
            log.exception("set_permission_mode failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"set_perm failed: {e}"))

    async def _on_set_mode(self, ctx: SessionContext, mode: str) -> None:
        """Agent-facing set_mode MCP tool (called within a turn). Same effect as
        SetPerm: sdk.set_permission_mode + Perm broadcast on this ctx."""
        if ctx.engine != "claude" or mode not in CLAUDE_PERMISSION_MODES:
            raise ValueError(f"unsupported {ctx.engine} permission mode: {mode}")
        try:
            await ctx.sdk.set_permission_mode(mode)
            ctx.announced_perm = mode
            await self._emit(ctx, Perm(mode=mode))
            log.info("agent set permission mode", sid=ctx.session_id, mode=mode)
        except Exception as e:
            log.exception("agent set_mode failed", error=str(e))
            raise

    async def _handle_get_context(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        try:
            usage = await ctx.sdk.get_context_usage()
            if ctx.engine == "codex":
                used = usage.get("used_tokens") or 0
                win = usage.get("context_window") or 0
                await self._emit(ctx, ContextReport(
                    total_tokens=used, max_tokens=win,
                    percentage=(used / win * 100.0) if win else 0.0,
                    model=ctx.sdk.model, is_auto_compact_enabled=None, categories=[]))
                return
            await self._emit(ctx, ContextReport(
                total_tokens=usage.get("totalTokens", 0),
                max_tokens=usage.get("maxTokens", 0),
                percentage=usage.get("percentage", 0.0),
                model=usage.get("model"),
                is_auto_compact_enabled=usage.get("isAutoCompactEnabled"),
                categories=usage.get("categories", []) or [],
            ))
        except Exception as e:
            log.exception("get_context_usage failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"get_context failed: {e}"))

    async def _handle_get_status(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        if ctx.engine != "codex":
            await self._emit(ctx, Error(
                code=ERR_INTERNAL,
                message="/status 需要 Codex app-server 会话",
            ))
            return
        try:
            event = StatusReport(
                **await ctx.sdk.get_status(),
                to=getattr(cmd, "client_id", None),
            )
            await self._emit(ctx, event)
            return event
        except Exception:
            # get_status already degrades individual RPC failures. Reaching this
            # path means the composed report itself could not be produced; do not
            # copy a raw provider/app-server exception onto the wire.
            log.exception("get_status failed")
            error = Error(
                code=ERR_INTERNAL,
                message="Codex status unavailable",
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
            ctx.codex_spontaneous_turn_id = turn_id
            if ctx.turn_task is not None:
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
                ctx, turn_id, announce_running=announce_running))
            ctx.codex_spontaneous_task = task
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
        ctx.codex_spontaneous_turn_id = None
        current = asyncio.current_task()
        if (ctx.codex_spontaneous_task is current
                or (ctx.codex_spontaneous_task is not None
                    and ctx.codex_spontaneous_task.done())):
            ctx.codex_spontaneous_task = None
        ctx.active_msg_id = None
        ctx.interrupt_deadline = None
        ctx.interrupt_event.clear()
        # If this continuation began while the previous managed consumer was
        # still unwinding, that task performs the final unlock after it releases
        # translator/queue ownership. Unlocking here would admit a second
        # _run_turn against the same SessionContext.
        if ctx.turn_task is None and ctx.state != "idle":
            await self._set_state(ctx, "idle")

    async def _run_codex_spontaneous_turn(
        self, ctx: SessionContext, turn_id: str, *, announce_running: bool,
    ) -> None:
        """Translate one goal/automatic turn from the handle's bounded bridge."""
        translator = CodexStreamTranslator(self.cfg.tool_result_max)
        overflowed = False
        terminal_seen = False
        stream_closed = False
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

            # Automatic continuations have no user prompt. A real empty anchor
            # gives their assistant/process events a stable turn owner without
            # rendering a fabricated user bubble.
            await self._emit(ctx, UserMsg(msg_id=turn_id, prompt=""))

            async for raw in ctx.sdk.receive_spontaneous_response(turn_id):
                if isinstance(raw, CodexSpontaneousOverflow):
                    overflowed = True
                    if ctx.state == "running":
                        await self._emit(ctx, StateEvent(
                            state="running",
                            phase="waiting",
                            detail=("部分实时过程因本机积压未展示；回合结束后可刷新历史。"),
                            msg_id=turn_id,
                        ))
                    continue
                if isinstance(raw, CodexSpontaneousClosed):
                    stream_closed = True
                    break
                if not isinstance(raw, dict):
                    continue

                events = translator.feed(raw)
                terminal = is_turn_terminal(raw)
                if overflowed and terminal:
                    # Feed the terminal frame so open assistant blocks close, but
                    # replace its misleading "empty upstream" failure with the
                    # authoritative local-overflow error below.
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
                    terminal_seen = True
                    break

            if overflowed and terminal_seen:
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message=("Codex 实时事件积压，部分过程未能同步；"
                             "请刷新会话以从历史恢复。"),
                    msg_id=turn_id,
                ))
                await self._emit(ctx, TurnEnd(
                    result=TurnResult(
                        subtype="error",
                        duration_ms=0,
                        is_error=True,
                    ),
                    turn_id=turn_id,
                ))
            elif stream_closed and not terminal_seen:
                synthetic = {
                    "method": "turn/completed",
                    "params": {"turn": {
                        "id": turn_id,
                        "status": "failed",
                        "error": {"message": "Codex app-server connection closed"},
                    }},
                }
                for event in translator.feed(synthetic):
                    if isinstance(event, Error) and event.msg_id is None:
                        event.msg_id = turn_id
                    await self._emit(ctx, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "codex spontaneous turn consumer failed",
                turn_id=turn_id,
                error_type=type(exc).__name__,
            )
            try:
                await self._emit(ctx, Error(
                    code=ERR_CC_CRASH,
                    message="Codex 自动回合实时同步失败；请刷新会话。",
                    msg_id=turn_id,
                ))
                await self._emit(ctx, TurnEnd(
                    result=TurnResult(
                        subtype="error", duration_ms=0, is_error=True),
                    turn_id=turn_id,
                ))
            except Exception:
                log.warning(
                    "codex spontaneous failure event could not be emitted",
                    turn_id=turn_id,
                )
        finally:
            try:
                await self._finish_codex_spontaneous_turn(ctx, turn_id)
            except Exception as exc:
                log.warning(
                    "codex spontaneous turn cleanup failed",
                    turn_id=turn_id,
                    error_type=type(exc).__name__,
                )

    async def _set_idle_after_managed_turn(self, ctx: SessionContext) -> None:
        """Do not unlock a thread already claimed by an automatic continuation."""
        if ctx.engine == "codex" and ctx.codex_spontaneous_turn_id is not None:
            return
        await self._set_state(ctx, "idle")

    async def _emit_goal_error(self, ctx: SessionContext, cmd,
                               message: str) -> Error:
        error = Error(
            code=ERR_INTERNAL,
            message=message,
            to=getattr(cmd, "client_id", None),
        )
        await self._emit(ctx, error)
        return error

    async def _handle_get_goal(self, cmd) -> None:
        ctx = await self._goal_ctx(cmd)
        if ctx is None:
            return
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
            return
        if ctx.engine != "codex":
            if getattr(cmd, "token_budget", None) is not None:
                error = Error(
                    code=ERR_PROTOCOL,
                    message="Claude /goal 不支持 token budget",
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            if getattr(cmd, "status", None) not in (None, "active"):
                error = Error(
                    code=ERR_PROTOCOL,
                    message="Claude /goal 只支持设置目标或 clear，不支持暂停/状态切换",
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            objective = (getattr(cmd, "objective", None) or "").strip()
            if not objective:
                error = Error(
                    code=ERR_BAD_PROMPT,
                    message="Claude /goal 需要非空完成条件",
                    to=getattr(cmd, "client_id", None),
                )
                await self._emit(ctx, error)
                return error
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY, message="该会话正忙,先 interrupt",
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
                    error = Error(
                        code=ERR_BUSY, message="该会话正忙,先 interrupt",
                        to=getattr(cmd, "client_id", None))
                    await self._emit(ctx, error)
                    return error
                ctx.interrupt_event.clear()
                ctx.interrupt_deadline = None
                ctx.state = "running"
                await self._emit(ctx, StateEvent(state="running"))
                goal = await ctx.sdk.set_goal(
                    objective=cmd.objective, status=cmd.status,
                    token_budget=cmd.token_budget)
                automatic_turn_live = bool(
                    ctx.codex_spontaneous_turn_id
                    or getattr(ctx.sdk, "turn_active", False))
                if not automatic_turn_live and ctx.state != "idle":
                    await self._set_idle_after_managed_turn(ctx)
            event = GoalState(goal=goal)
            await self._emit(ctx, event)
            return event
        except Exception as exc:
            automatic_turn_live = bool(
                ctx.codex_spontaneous_turn_id
                or getattr(ctx.sdk, "turn_active", False))
            if not automatic_turn_live and ctx.state != "idle":
                await self._set_state(ctx, "idle")
            log.warning("set_goal failed", error_type=type(exc).__name__)
            return await self._emit_goal_error(ctx, cmd, "设置 Goal 失败")

    async def _handle_clear_goal(self, cmd) -> None:
        ctx = await self._goal_ctx(cmd)
        if ctx is None:
            return
        if ctx.engine != "codex":
            if ctx.state != "idle":
                error = Error(
                    code=ERR_BUSY, message="该会话正忙,先 interrupt",
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
                to=getattr(cmd, "client_id", None))
            await self._emit(ctx, error)
            return error
        try:
            async with ctx.launch_lock:
                await ctx.sdk.clear_goal()
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
                to=getattr(cmd, "client_id", None),
            )
            if sid:
                await self._emit_to_sid(sid, error)
            else:
                await self._emit_focused(error)
            return
        try:
            diff = await self._git_diff(ctx.cwd, cmd.file)
            await self._emit(ctx, DiffReport(file=cmd.file, diff=diff))
        except Exception as e:
            log.exception("get_diff failed", error=str(e))
            await self._emit(ctx, Error(code=ERR_INTERNAL, message=f"get_diff failed: {e}"))

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
            path, content, size, truncated, mtime_ns, file_format = (
                await asyncio.to_thread(
                    self._read_text_preview, ctx.cwd, cmd.path))
            response = FilePreview(
                path=path,
                request_id=cmd.request_id,
                format=file_format,
                content=content,
                size=size,
                truncated=truncated,
                mtime_ns=mtime_ns,
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
                self._read_preview_asset, ctx.cwd, cmd.path)
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

    async def _on_ask(self, ctx: SessionContext, question: str,
                      options: list[dict[str, str]], *, header: str | None = None,
                      allow_text: bool = False, secret: bool = False) -> str:
        """Called by THIS ctx's in-process MCP server when the agent invokes
        `ask_user`. Emits AskUser on the ctx and blocks until AnswerQuestion.
        Runs in the ctx's reader task while its turn loop is blocked on
        receive_response(); other ctxs' turns are unaffected."""
        # ask_id is an identity, not a downstream sequence.  Consuming next_seq
        # here would leave an invisible hole before _emit assigns AskUser.seq;
        # reconnect replay would then appear to have lost a frame.
        ask_id = f"ask-{uuid4().hex}"
        # Validate model-originated text before registering a pending Future.
        # Otherwise a malformed/oversized AskUser raises during emit and leaves
        # an unreachable entry in pending_asks for the life of the session.
        event = AskUser(ask_id=ask_id, question=question, options=options,
                        header=header, allow_text=allow_text, secret=secret)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        ctx.pending_asks[ask_id] = fut
        await self._emit(ctx, event)
        log.info("ask_user emitted", sid=ctx.session_id, ask_id=ask_id, options=len(options))
        try:
            return await asyncio.wait_for(fut, timeout=30 * 60)
        except asyncio.TimeoutError:
            log.warning("ask_user timed out", ask_id=ask_id)
            return "(用户未回答，已超时)"
        finally:
            ctx.pending_asks.pop(ask_id, None)

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
        answer = await self._on_ask(ctx, "\n".join(lines), options)
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
            answer = await self._on_ask(ctx, prompt, [
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
            answer = await self._on_ask(
                ctx, prompt, options, header=str(question.get("header") or "")[:512] or None,
                allow_text=allow_text, secret=bool(question.get("isSecret")))
            answers[question_id] = {"answers": [answer]}
        return {"answers": answers}

    async def _on_codex_mcp_elicitation(self, ctx: SessionContext,
                                        params: dict) -> dict:
        mode = params.get("mode")
        message = str(params.get("message") or "MCP 服务请求输入")[:16000]
        server = str(params.get("serverName") or "MCP")[:512]
        if mode == "url":
            url = str(params.get("url") or "")[:4096]
            answer = await self._on_ask(ctx, f"{message}\n\n{url}", [
                {"label": "已完成并继续", "ds": "我已在链接页面完成操作"},
                {"label": "拒绝", "ds": "不继续这次 MCP 请求"},
                {"label": "取消", "ds": "取消当前操作"},
            ], header=f"{server} 请求网页操作")
            return {"action": {"已完成并继续": "accept", "取消": "cancel"}.get(answer, "decline")}

        schema = params.get("requestedSchema")
        if not isinstance(schema, dict):
            # openai/form schemas may be intentionally opaque. Preserve a usable
            # accept/decline path instead of rejecting the server request.
            answer = await self._on_ask(ctx, message, [
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
            answer = await self._on_ask(
                ctx, question, options, header=f"{server} · {title}",
                allow_text=len(options) < 2, secret=bool(spec.get("format") == "password"))
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

    async def _on_claude_tool_permission(self, ctx: SessionContext,
                                         tool_name: str, tool_input: dict,
                                         permission_context):
        """Bridge Claude Agent SDK can_use_tool to the remote client."""
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
        answer = await self._on_ask(ctx, "\n".join(lines), [
            {"label": "允许一次", "ds": "仅批准这一次工具调用"},
            {"label": "拒绝", "ds": "拒绝这次工具调用"},
        ])
        if answer == "允许一次":
            return PermissionResultAllow()
        return PermissionResultDeny(message="用户拒绝了远程工具授权")

    async def _handle_answer_question(self, cmd) -> None:
        ctx = self._ctx_for(getattr(cmd, "sid", None))
        if ctx is None:
            return
        fut = ctx.pending_asks.get(cmd.ask_id)
        if fut is None:
            log.warning("answer for unknown ask_id", ask_id=cmd.ask_id)
            return
        if not fut.done():
            fut.set_result(cmd.answer)
            log.info("ask_user answered", ask_id=cmd.ask_id)
        else:
            log.warning("answer for already-done ask_id", ask_id=cmd.ask_id)

    @staticmethod
    def _read_session_file(
        cwd: str,
        path: str,
        *,
        allowed_suffixes: Optional[frozenset[str]],
        max_bytes: int,
        allow_truncate: bool,
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
        try:
            if os.path.commonpath((root, candidate)) != root:
                raise ValueError("预览路径超出当前工作目录")
        except ValueError as exc:
            raise ValueError("预览路径超出当前工作目录") from exc

        suffix = os.path.splitext(candidate)[1].lower()
        if allowed_suffixes is not None and suffix not in allowed_suffixes:
            raise ValueError("不支持预览该文件类型")

        relative = os.path.relpath(candidate, root)
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
            dir_fd = os.open(root, dir_flags)
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
                raise ValueError("预览图片超过 4 MiB 限制")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(max_bytes + 1)
        finally:
            os.close(fd)

        truncated = len(data) > max_bytes or file_stat.st_size > max_bytes
        if truncated and not allow_truncate:
            raise ValueError("预览图片超过 4 MiB 限制")
        return relative.replace(os.sep, "/"), data[:max_bytes], file_stat, truncated

    @classmethod
    def _read_text_preview(
        cls, cwd: str, path: str,
    ) -> tuple[str, str, int, bool, int, str]:
        relative, data, file_stat, truncated = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=None,
            max_bytes=FILE_PREVIEW_MAX_BYTES,
            allow_truncate=True,
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
        )

    @classmethod
    def _read_markdown_preview(
        cls, cwd: str, path: str,
    ) -> tuple[str, str, int, bool, int]:
        relative, data, file_stat, truncated = cls._read_session_file(
            cwd,
            path,
            allowed_suffixes=cls.MARKDOWN_PREVIEW_SUFFIXES,
            max_bytes=FILE_PREVIEW_MAX_BYTES,
            allow_truncate=True,
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
        return (
            "markdown"
            if os.path.splitext(path)[1].lower() in cls.MARKDOWN_PREVIEW_SUFFIXES
            else "text"
        )

    @classmethod
    def _read_preview_asset(
        cls, cwd: str, path: str,
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
        )
        return relative, media_type, data

    async def _git_diff(self, cwd: str, file: str) -> str:
        """Raw `git diff` (vs HEAD) text for a cwd. Empty file => all files; a
        single untracked file falls back to --no-index (full-add diff)."""
        max_bytes = max(64 * 1024, min(4 * 1024 * 1024,
                                       self.cfg.ws_max_size_bytes // 2))
        if not file:
            return await self._bounded_process_output(
                ("git", "-C", cwd, "diff", "--no-ext-diff", "--no-textconv",
                 "HEAD"), max_bytes)

        root_text = await self._bounded_process_output(
            ("git", "-C", cwd, "rev-parse", "--show-toplevel"), 4096)
        root = os.path.realpath(root_text.strip().splitlines()[0] if root_text.strip() else cwd)
        candidate = os.path.abspath(
            os.path.expanduser(file) if os.path.isabs(os.path.expanduser(file))
            else os.path.join(cwd, os.path.expanduser(file)))
        # Resolve parent symlinks, but not the final component: a tracked symlink
        # can be diffed safely by git, while a path through a symlinked directory
        # must not escape the repository.
        parent = os.path.realpath(os.path.dirname(candidate))
        contained = os.path.join(parent, os.path.basename(candidate))
        try:
            if os.path.commonpath((root, contained)) != root:
                raise ValueError("diff path is outside the session repository")
        except ValueError as exc:
            raise ValueError("diff path is outside the session repository") from exc
        rel_file = os.path.relpath(contained, root)

        diff = await self._bounded_process_output(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "HEAD", "--", rel_file), max_bytes)
        if diff.strip():
            return diff

        # An empty tracked-file diff means "unchanged", not "show its complete
        # contents". Reject untracked special files even when `git ls-files
        # --others` omits them; an explicit FIFO/device must never be opened by a
        # later no-index diff.
        try:
            file_stat = os.lstat(contained)
        except OSError:
            return ""
        if not stat.S_ISREG(file_stat.st_mode):
            tracked = await self._bounded_process_output(
                ("git", "-C", root, "ls-files", "--stage", "--", rel_file),
                64 * 1024,
            )
            if tracked:
                return ""  # unchanged tracked symlink
            raise ValueError("untracked diff target must be a regular file")
        # Only an actual, non-ignored untracked regular file gets no-index.
        untracked = await self._bounded_process_output(
            ("git", "-C", root, "ls-files", "--others", "--exclude-standard",
             "--", rel_file), 64 * 1024)
        if not untracked:
            return ""
        source_cap = getattr(self.cfg, "history_source_max_bytes", 64 * 1024 * 1024)
        if file_stat.st_size > source_cap:
            raise ValueError("untracked diff target exceeds the source size limit")
        return await self._bounded_process_output(
            ("git", "-C", root, "diff", "--no-ext-diff", "--no-textconv",
             "--no-index", "--", "/dev/null", rel_file),
            max_bytes,
        )

    @staticmethod
    async def _bounded_process_output(
        argv: tuple[str, ...], max_bytes: int, timeout: float = 10.0,
    ) -> str:
        """Capture stdout up to a hard byte limit without buffering stderr."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert proc.stdout is not None
        chunks: list[bytes] = []
        total = 0
        truncated = False

        async def read_stdout() -> None:
            nonlocal total, truncated
            while True:
                chunk = await proc.stdout.read(min(64 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                remaining = max_bytes - total
                if len(chunk) > remaining:
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)

        async def discard_stdout_and_wait() -> None:
            """Reap without asking communicate() to accumulate residual stdout."""
            async def discard_stdout() -> None:
                while await proc.stdout.read(64 * 1024):
                    pass

            await asyncio.gather(proc.wait(), discard_stdout())

        timed_out = False

        def stop_group(sig: signal.Signals) -> None:
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                pass

        reaped = False
        try:
            try:
                await asyncio.wait_for(read_stdout(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
            if timed_out:
                stop_group(signal.SIGKILL)
            elif truncated:
                stop_group(signal.SIGTERM)
            try:
                # Discard residual bytes incrementally. communicate() would collect
                # them all and defeat the hard output cap while SIGTERM is draining.
                await asyncio.wait_for(discard_stdout_and_wait(), timeout=2.0)
            except asyncio.TimeoutError:
                stop_group(signal.SIGKILL)
                await discard_stdout_and_wait()
            reaped = True
        finally:
            if not reaped:
                stop_group(signal.SIGKILL)
                await discard_stdout_and_wait()
        if timed_out:
            raise asyncio.TimeoutError("diff command exceeded its time limit")
        text = b"".join(chunks).decode(errors="replace")
        if truncated:
            text += "\n\n[diff truncated at transport safety limit]\n"
        return text

    # ---- sessions (list / switch / new) ----

    async def _handle_list_sessions(self, cmd) -> None:
        engine = getattr(cmd, "engine", "claude")
        if engine == "codex":
            await self._list_codex_sessions(cmd)
            return
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
                await self.transport.send(Error(
                    code=ERR_BUSY,
                    message="临时 btw 会话正在初始化，请稍后刷新会话列表",
                    to=client_id,
                ))
            log.warning("Claude session list withheld during btw id capture",
                        client_id=client_id)
            return
        try:
            infos = await asyncio.to_thread(list_sessions, limit=200)
            blocked = await asyncio.to_thread(self._bg_blocked_session_ids)
            private_btw_ids = set(self._private_btw_sessions)
            resident_ids = {c.session_id for c in self.sessions.values() if c.session_id}
            resident_state = {c.session_id: c.state for c in self.sessions.values() if c.session_id}
            sessions = [
                SessionInfo(
                    session_id=i.session_id,
                    summary=(i.summary or (i.custom_title if hasattr(i, "custom_title") else None) or "")[:500] or None,
                    last_modified=str(i.last_modified) if i.last_modified else None,
                    first_prompt=(i.first_prompt or "")[:2000] or None,
                    git_branch=(i.git_branch or "")[:500] or None,
                    cwd=(i.cwd or "")[:4096] or None,
                    tag=(i.tag or "")[:128] or None,
                    state=resident_state.get(i.session_id),
                )
                for i in infos
                if i.session_id not in blocked
                and i.session_id not in private_btw_ids
            ]
            await self.transport.send(SessionList(
                engine="claude",
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            ))
            log.info("listed sessions", count=len(sessions), resident=len(resident_ids),
                     client_id=getattr(cmd, "client_id", None))
        except Exception as e:
            log.exception("list_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_sessions failed: {e}"))

    async def _list_codex_sessions(self, cmd) -> None:
        """Sidebar list from the app-server's authoritative thread state DB."""
        try:
            # Load the bounded app-server maximum for each archive state. Search
            # is client-side, so stopping at the old 60-row page made older
            # threads impossible to rename, restore, or fork.
            raw = await list_codex_sessions(200)
            resident_state = {c.session_id: c.state for c in self.sessions.values()
                              if c.session_id and c.engine == "codex"}
            sessions = [
                SessionInfo(
                    session_id=r["session_id"],
                    summary=r.get("summary"),
                    first_prompt=r.get("first_prompt"),
                    cwd=r.get("cwd"),
                    last_modified=r.get("last_modified"),
                    git_branch=r.get("git_branch"),
                    tag=r.get("tag"),
                    engine="codex",
                    state=(resident_state.get(r["session_id"])
                           or _codex_list_state(r.get("status"))),
                    forked_from_id=r.get("forked_from_id"),
                    codex_status=r.get("status"),
                )
                for r in raw
            ]
            await self.transport.send(SessionList(
                engine="codex",
                sessions=sessions,
                to=getattr(cmd, "client_id", None),
            ))
            log.info("listed codex sessions", count=len(sessions),
                     client_id=getattr(cmd, "client_id", None))
        except Exception as e:
            log.exception("list_codex_sessions failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_codex_sessions failed: {e}"))

    async def _handle_switch_session(self, cmd) -> None:
        # Focus change — NO disconnect. If the session is already resident, just
        # focus it (its turn keeps running in the background). If not resident,
        # spawn (resume) it. The previously-focused session is NOT interrupted.
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        ctx = self.sessions.get(sid)
        if ctx is None:
            ctx = next((c for c in self.sessions.values() if c.session_id == sid), None)
        newly_spawned = ctx is None
        if ctx is None:
            ctx = await self._spawn(resume_id=sid, engine=getattr(cmd, "engine", None) or "claude")
            if ctx is None:
                # surface it on the session the user switched INTO (not the stale
                # focused one), so a spawn failure never looks like silent "no
                # response". Common cause: bad codex config / backend.
                error = Error(code=ERR_CC_CRASH,
                    message="会话启动失败:可能是 codex 配置/后端问题(见服务端日志)")
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
                            generation=self.instance_id)
            await self._emit(ctx, snap)
        focus = SessionFocus(
            session_id=ctx.session_id or self.focused_sid or sid, cwd=ctx.cwd)
        await self._emit(ctx, focus)
        cached_responses = [snap, focus] if snap is not None else [focus]
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
        log.info("captured cc session id", sid=sid, focus_followed=(self.focused_sid == sid))

    async def _handle_new_session(self, cmd) -> None:
        attachment_error = validate_attachments(
            getattr(cmd, "images", None), getattr(cmd, "files", None))
        if attachment_error:
            error = Error(
                code=ERR_BAD_PROMPT,
                message=f"invalid attachments: {attachment_error}",
                request_id=getattr(cmd, "request_id", None),
                to=getattr(cmd, "client_id", None),
            )
            await self.transport.send(error)
            return error
        ctx = await self._spawn(resume_id=None, cwd=getattr(cmd, "cwd", None),
                                engine=getattr(cmd, "engine", "claude"),
                                model=getattr(cmd, "model", None),
                                effort=getattr(cmd, "effort", None),
                                collaboration_mode=getattr(
                                    cmd, "collaboration_mode", None),
                                permission_mode=getattr(
                                    cmd, "permission_mode", None),
                                service_tier=getattr(
                                    cmd, "service_tier", None))
        if ctx is None:
            error = Error(
                code=ERR_CC_CRASH,
                message="新会话启动失败，请检查工作目录和引擎配置",
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
        return tuple(cached_responses)

    async def _handle_rename_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if await self._is_codex_session(sid):
            try:
                await codex_rpc("thread/name/set", {
                    "threadId": sid, "name": cmd.title,
                })
                log.info("codex session renamed", session_id=sid,
                         title_length=len(cmd.title))
                await self._list_codex_sessions(cmd)
            except Exception as e:
                log.exception("codex rename_session failed", error=str(e))
                await self._emit_to_sid(sid, Error(
                    code=ERR_INTERNAL, message=f"rename failed: {e}"))
                await self._list_codex_sessions(cmd)
            return
        try:
            await asyncio.to_thread(rename_session, sid, cmd.title)
            # our own append -> re-baseline, else the watcher calls it an external write
            self._resync_watch(sid)
            log.info("session renamed", session_id=sid,
                     title_length=len(cmd.title))
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("rename_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"rename failed: {e}"))

    async def _handle_archive_session(self, cmd) -> None:
        sid = self._resolve_session_alias(cmd.session_id) or cmd.session_id
        if await self._is_codex_session(sid):
            method = "thread/archive" if cmd.archived else "thread/unarchive"
            try:
                await codex_rpc(method, {"threadId": sid})
                log.info("codex session archive toggled", session_id=sid,
                         archived=cmd.archived)
                await self._list_codex_sessions(cmd)
            except Exception as e:
                log.exception("codex archive_session failed", error=str(e))
                await self._emit_to_sid(sid, Error(
                    code=ERR_INTERNAL, message=f"archive failed: {e}"))
                # The UI waits for this authoritative result instead of moving
                # the card optimistically. It also reconciles a timeout where the
                # app-server committed the mutation but its response was lost.
                await self._list_codex_sessions(cmd)
            return
        try:
            tag = "archived" if cmd.archived else None
            await asyncio.to_thread(tag_session, sid, tag)
            # our own append -> re-baseline (see _resync_watch)
            self._resync_watch(sid)
            log.info("session archive toggled", session_id=sid, archived=cmd.archived)
            await self._handle_list_sessions(cmd)
        except Exception as e:
            log.exception("archive_session failed", error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"archive failed: {e}"))

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
        if await self._is_codex_session(sid):
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
            try:
                await asyncio.to_thread(
                    self._claude_forks.reject, cmd.request_id,
                    f"Claude 会话派生失败: {exc}")
            except ClaudeForkJournalError as journal_exc:
                log.warning("Claude fork rejection journal failed",
                            error=str(journal_exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"Claude 会话派生失败: {exc}")
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
                    f"Codex 会话派生失败: {exc}")
            except ForkJournalError as journal_exc:
                log.warning("codex fork rejection journal failed",
                            error=str(journal_exc))
            return await self._send_session_fork_error(
                cmd, ERR_INTERNAL, f"Codex 会话派生失败: {exc}")
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
            return await self._send_worktree_fork_error(
                cmd, ERR_INTERNAL, f"创建 Git 工作树失败: {exc}")

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
                        else f"Codex 会话派生失败: {exc}"
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

    # ---- directory picker (arbitrary-cwd session creation) ----

    async def _handle_list_dir(self, cmd) -> None:
        try:
            path, parent, dirs = await asyncio.to_thread(self._scan_dir, cmd.path)
            await self._emit_focused(DirList(path=path, parent=parent, dirs=dirs))
        except Exception as e:
            log.exception("list_dir failed", path=getattr(cmd, "path", None), error=str(e))
            await self._emit_focused(Error(code=ERR_INTERNAL, message=f"list_dir failed: {e}"))

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
        jobs = os.path.expanduser("~/.claude/jobs")
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
                     service_tier: Optional[str] = None) -> Optional[SessionContext]:
        """Create a SessionContext, connect its SDK subprocess, load history.
        Returns the ctx (added to the pool under its real or temp key) or None
        on failure (an Error has been emitted). `bootstrap` exempts the cap and
        retries resume→fresh on connect failure. `model`/`effort` (new_session
        only) pre-select those at spawn: effort BEFORE connect so the first turn
        runs at that strength with no respawn; cc model via a live set_model
        after connect; codex model as a per-turn field. Omitted => engine
        defaults (unchanged behavior)."""
        if resume_id and resume_id in self._private_btw_sessions:
            log.warning("refusing cold resume of private btw transcript",
                        session_id=resume_id)
            await self._emit_to_sid(resume_id, Error(
                code=ERR_AUTH,
                message="临时 btw 会话不可恢复",
            ))
            return None
        # Claude's CLI/version preflight belongs to Claude spawn, not wrapper
        # process startup.  Keep it before cap eviction: an unavailable optional
        # engine must not evict a healthy resident Codex session and then fail.
        if engine != "codex":
            try:
                SdkHandle.preflight(self.cfg.claude_bin)
            except Exception as exc:
                log.warning("Claude preflight failed; engine unavailable",
                            error=str(exc))
                await self._emit_to_sid(resume_id, Error(
                    code=ERR_CC_CRASH,
                    message=f"Claude 引擎不可用: {exc}",
                ))
                return None

        # Concurrency cap (bootstrap always allowed). When full, evict an idle,
        # non-focused session (tear down its subprocess; the client keeps its
        # runtime and re-spawns on re-focus). Only reject if ALL are running —
        # so merely browsing between sessions never wedges you.
        if not bootstrap and len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle" and not c.btw), None)
            if victim is None:
                await self._emit_focused(Error(
                    code=ERR_BUSY,
                    message="所有会话都在运行,先中断一个再切换"))
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
            # so resolve cwd from the rollout meta, not get_session_info.
            cwd_hint = await asyncio.to_thread(codex_session_cwd, resume_id)
            target_cwd = cwd_hint or self.cfg.cc_cwd
            if not os.path.isdir(target_cwd):
                target_cwd = self.cfg.cc_cwd
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
                    await self._emit_focused(Error(code=ERR_INTERNAL, message=f"session not found: {resume_id}"))
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
                await self._emit_focused(Error(code=ERR_INTERNAL, message=f"目录不存在: {cwd}"))
                return None
        else:
            target_cwd = self.cfg.cc_cwd

        sdk = CodexHandle(self.cfg, cwd=target_cwd) if engine == "codex" else SdkHandle(self.cfg)
        # Pre-select effort at spawn (before connect): cc reads it via _options at
        # connect so --effort is baked into the first turn (no respawn); codex uses
        # it as a per-turn param. codex model is also a per-turn field, so set it
        # here; cc's model needs a live set_model AFTER connect (below). Set
        # applied_effort too so _run_turn's "effort != applied" reconnect check
        # sees the first turn as already-applied (cc's connect re-syncs it anyway;
        # this is what keeps codex from a spurious first-turn reconnect).
        if engine == "codex":
            if collaboration_mode in CODEX_COLLABORATION_MODES:
                sdk.collaboration_mode = collaboration_mode
            if permission_mode in CODEX_PERMISSION_MODES:
                sdk.approval = permission_mode
            if service_tier in {"default", "fast"}:
                sdk.service_tier = (
                    "fast" if service_tier == "fast" else None)
            # Seed from the session's own bounded rollout tail, never config.toml.
            # CodexHandle.connect then adopts thread/resume's authoritative fields;
            # the rollout remains required for collaboration mode, which 0.144.1's
            # resume response does not expose.
            if resume_id:
                prev = await asyncio.to_thread(
                    codex_session_settings, resume_id,
                    self.cfg.history_source_max_bytes)
                model = model or prev.get("model")
                effort = effort or prev.get("effort")
                approval = prev.get("approval_policy")
                if (permission_mode is None
                        and approval in CODEX_PERMISSION_MODES):
                    sdk.approval = approval
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
        ctx = SessionContext(
            session_id=resume_id,
            sdk=sdk,
            buffer=RingBuffer(self.cfg.ring_max_events, self.cfg.ring_max_bytes),
            cwd=target_cwd,
            engine=engine,
        )
        # Per-ctx MCP ask server is Claude-only (the cc-remote-ask tools). Codex
        # handles approvals through its own app-server protocol, so skip it.
        if engine != "codex":
            ctx.sdk.ask_server = make_ask_server(
                lambda q, o: self._on_ask(ctx, q, o),
                lambda m: self._on_set_mode(ctx, m),
            )
            ctx.sdk.permission_callback = (
                lambda tool, tool_input, permission_context:
                self._on_claude_tool_permission(
                    ctx, tool, tool_input, permission_context))
            ctx.sdk.background_message_callback = (
                lambda message, turn_id: self._on_claude_background_message(
                    ctx, message, turn_id))
        else:
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
            await ctx.sdk.connect(resume_id=resume_id, cwd=target_cwd)
        except Exception as e:
            if bootstrap and resume_id:
                log.warning("resume failed, starting a fresh session", error=str(e))
                ctx.session_id = None
                try:
                    await ctx.sdk.connect(resume_id=None, cwd=target_cwd)
                except Exception as e2:
                    log.exception("fresh connect also failed", error=str(e2))
                    await self._emit_focused(Error(code=ERR_CC_CRASH, message=f"connect failed: {e2}"))
                    return None
            else:
                log.exception("connect failed", error=str(e))
                await self._emit_focused(Error(code=ERR_CC_CRASH, message=f"connect failed: {e}"))
                return None

        # cc model is a runtime switch on the live subprocess (set_model), so apply
        # a pre-selected model now that we're connected. codex was set pre-connect.
        if model and engine != "codex":
            try:
                await ctx.sdk.set_model(model)
            except Exception as e:
                log.warning("spawn set_model failed", model=model, error=str(e))
        # Record the pre-selected values so _run_turn doesn't redundantly re-announce
        # them (the client already reflects its own pick optimistically).
        if model:
            ctx.announced_model = model
        if effort:
            ctx.announced_effort = effort
        # Pool key: real sid if known, else a temp id until captured in _run_turn.
        key = resume_id or f"tmp-{uuid4().hex}"
        self.sessions[key] = ctx
        ctx.key = key
        if resume_id:
            self._watch_session(resume_id)
            if engine == "codex":
                await self._prime_codex_ownership(resume_id)
            else:
                await self._prime_claude_ownership(resume_id)
        if resume_id and engine != "codex":
            save_session_id(self.cfg.state_dir, target_cwd, resume_id)
        await self._load_history(ctx, resume_id)
        if bootstrap:
            ctx.announced_perm = _session_permission_mode(ctx)
            await self._emit(ctx, Perm(mode=ctx.announced_perm))
            if engine == "codex":
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
                ERR_BUSY, "btw 隐私清理队列已满，请先检查 wrapper 日志")
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
                    ERR_CC_CRASH, f"Claude 引擎不可用: {exc}") from exc
        # btw counts toward the cap; evict an idle, non-focused, non-btw victim.
        if len(self.sessions) >= self.cfg.max_concurrent_sessions:
            victim = next((k for k, c in self.sessions.items()
                           if k != self.focused_sid and c.state == "idle" and not c.btw), None)
            if victim is None:
                raise _BtwSpawnFailure(ERR_BUSY, "会话已满,先关闭一个再开 btw")
            vc = self.sessions.pop(victim)
            try:
                await vc.sdk.disconnect()
            except Exception:
                pass
            log.info("evicted idle session for btw", key=victim)
        sdk = CodexHandle(self.cfg, cwd=parent.cwd) if engine == "codex" else SdkHandle(self.cfg)
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
            ctx.sdk.ask_server = make_ask_server(
                lambda q, o: self._on_ask(ctx, q, o),
                lambda m: self._on_set_mode(ctx, m),
            )
            ctx.sdk.permission_callback = (
                lambda tool, tool_input, permission_context:
                self._on_claude_tool_permission(
                    ctx, tool, tool_input, permission_context))
            ctx.sdk.background_message_callback = (
                lambda message, turn_id: self._on_claude_background_message(
                    ctx, message, turn_id))
        else:
            ctx.sdk.approval = parent.sdk.approval
            ctx.sdk.approval_policy = parent.sdk.approval_policy
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
            raise _BtwSpawnFailure(ERR_CC_CRASH, f"btw fork 失败: {e}") from e
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
            r"/\S*cc-remote-turn-[A-Za-z0-9_-]+/\d{2}-([^\s]+)")
        marker = "\n\n[用户附件,请用工具读取以下文件]:\n"
        if marker in prompt:
            original, block = prompt.rsplit(marker, 1)
            names = [match.group(1) for match in path_re.finditer(block)]
            if names:
                return original, [{"filename": name} for name in names]

        matches = list(re.finditer(
            r"@(?P<path>/\S*cc-remote-turn-[A-Za-z0-9_-]+/\d{2}-(?P<name>[^\s]+))",
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

    async def _run_turn(self, ctx: SessionContext, prompt: str,
                        images: Optional[list] = None, files: Optional[list] = None) -> None:
        is_codex = ctx.engine == "codex"
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
        notice_active = False
        claude_turn_completed = False

        async def reader() -> None:
            cancelled = False
            try:
                async for msg in ctx.sdk.receive_response():
                    await queue.put(msg)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except BaseException as e:
                reader_exc.append(e)
            finally:
                if not cancelled:
                    await queue.put(None)

        async def next_turn_message():
            nonlocal notice_active
            """Wait for one raw engine event, warning on a silent Codex turn.

            This is deliberately not a hard timeout: ultra reasoning and long
            tools can be valid. A real interrupt still uses the existing bounded
            drain path, and any raw app-server event rearms the warning timer on
            the next loop iteration.
            """
            warn = (self.cfg.codex_turn_idle_warn_seconds if is_codex else 0)
            if warn <= 0 or ctx.state == "interrupting":
                return await self._next_from_queue(ctx, queue)
            wait_task = asyncio.create_task(self._next_from_queue(ctx, queue))
            try:
                done, _ = await asyncio.wait((wait_task,), timeout=warn)
                if wait_task in done:
                    # Preserve a real drain-timeout exception from
                    # _next_from_queue.
                    return wait_task.result()
                if ctx.state == "running":
                    await self._emit(ctx, StateEvent(
                        state="running",
                        phase="waiting",
                        detail=(f"Codex 已 {warn:g} 秒没有收到新进展，仍在等待；"
                                "可点击停止。"),
                        msg_id=ctx.active_msg_id,
                    ))
                    notice_active = True
                # An interrupt racing the warning now enters the normal absolute
                # DRAIN_TIMEOUT path. Keep awaiting the SAME task: it may have
                # consumed a boundary event just as asyncio.wait timed out.
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
            if ctx.needs_reload and ctx.session_id:
                log.info("reloading session after external transcript change",
                         sid=ctx.session_id)
                if is_codex:
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
                            await self._emit(ctx, Error(
                                code=ERR_BUSY,
                                message=("该 Claude 会话在重载期间又被本机终端更新，"
                                         "本次发送已取消；请退出终端后重试"),
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
                    if ctx.session_id:
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
                    await ctx.sdk.query(prompt, images=img_paths)
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
            reader_task = asyncio.create_task(reader())
            while True:
                msg = await next_turn_message()
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
                    sid = codex_session_id(msg)
                    if sid and not ctx.session_id:
                        await self._capture_session_id(ctx, sid)
                    for ev in ctx.translator.feed(msg):
                        await emit_codex_event(ev)
                    if is_turn_terminal(msg):
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

            await self._set_idle_after_managed_turn(ctx)
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
                    message="agent reconnect failed; see wrapper logs",
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
                message="agent turn failed; see wrapper logs",
                msg_id=ctx.active_msg_id))
            await self._set_idle_after_managed_turn(ctx)
        finally:
            if not is_codex:
                release_background = getattr(
                    ctx.sdk, "release_background_messages", None)
                if release_background is not None:
                    release_background()
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
            if temp_dir is not None:
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
