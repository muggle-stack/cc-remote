"""ClaudeSDKClient lifecycle: connect / query / interrupt / receive / resume.

Isolates the one version-sensitive call site (`include_partial_messages` on
ClaudeAgentOptions) so an SDK upgrade touches only this file. Code sessions use
Claude's ordinary setting sources. An explicit multi-account profile keeps the
real HOME, selects its native storage boundary with ``CLAUDE_CONFIG_DIR``, and
loads only that profile's user settings. Project/local settings cannot silently
replace the selected provider, and cc-remote never opens or promotes the
profile's credential-bearing file. Work sessions use one wrapper-owned settings
file containing only provider connectivity and the fail-closed sandbox; they
must never inherit user/project memory, hooks or skills. Permission mode is
explicit live session state and survives reconnects.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, PermissionResultDeny,
    __version__ as SDK_VERSION,
)
from claude_agent_sdk._internal.message_parser import parse_message as _parse_sdk_message
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from mcp.server import Server

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.protocol import MAX_SAFE_WIRE_INTEGER
from cc_remote.wrapper.child_env import claude_profile_child_env
from cc_remote.wrapper.claude_transport import account_isolated_transport
from cc_remote.wrapper.claude_rewind import (
    ClaudeConversationRewindCapability,
    ClaudeConversationRewindResult,
    ClaudeRewindError,
    classify_control_failure,
    is_unsupported_control_error,
    parse_conversation_rewind_response,
    response_proves_conversation_rewind,
    validate_rewind_target,
)
from cc_remote.wrapper.claude_runtime import inspect_claude_runtime
from cc_remote.wrapper.claude_model_fallback import model_fallback_event
from cc_remote.wrapper.claude_controls import (
    CLAUDE_DEFAULT_AUTO_COMPACT_MODE,
    claude_auto_compact_cli_value,
    valid_claude_auto_compact,
    valid_claude_model,
)
from cc_remote.wrapper.work_prompt import WORK_SYSTEM_PROMPT
from cc_remote.wrapper.work_context import claude_recent_context_usage
from cc_remote.wrapper import claude_catalog
from cc_remote.wrapper.claude_goal import (
    NO_GOAL_EVENT,
    active_goal_from_message,
    current_goal,
    goal_message_update,
    make_claude_goal,
    read_claude_goal,
)

log = logger("cc_remote.wrapper.sdk")

CLAUDE_DEFAULT_MODEL = "claude-opus-5[1m]"
CLAUDE_DEFAULT_EFFORT = "max"
CLAUDE_MAX_BUFFER_SIZE = 16 * 1024 * 1024
_CLAUDE_1M_MODEL_PINS = {
    "opus": CLAUDE_DEFAULT_MODEL,
    "opus[1m]": CLAUDE_DEFAULT_MODEL,
    "claude-opus-5": CLAUDE_DEFAULT_MODEL,
    "claude-opus-5[1m]": CLAUDE_DEFAULT_MODEL,
    "claude-fable-5-1": "claude-fable-5-1[1m]",
    "claude-fable-5-1[1m]": "claude-fable-5-1[1m]",
    "claude-mythos-5-1": "claude-mythos-5-1[1m]",
    "claude-mythos-5-1[1m]": "claude-mythos-5-1[1m]",
}
_CONVERSATION_REWIND_PROBE_UUID = "00000000-0000-0000-0000-000000000000"
_CONTEXT_CONTROL_TIMEOUT = 15.0
_CONTEXT_STARTUP_TIMEOUT = 5.0
_CURRENT_LAUNCH_VALUE = object()


def normalize_claude_model_selection(model: str | None) -> str | None:
    """Keep curated long-context aliases pinned across every child generation."""
    if model is None:
        return None
    normalized = model.strip()
    return _CLAUDE_1M_MODEL_PINS.get(normalized.lower(), normalized)

# Work keeps the file primitives needed for documents and other deliverables,
# plus first-party web research.  Deliberately omit Agent/Task, Skill,
# NotebookEdit and the coding-only planning tools: the private Work workspace
# can still generate DOCX/XLSX/PPTX/PDF through Bash without loading their
# schemas or any subagent definitions into every conversation.
CLAUDE_WORK_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "WebSearch",
    "WebFetch",
]


class _MessagePumpFailure:
    def __init__(self, error: BaseException):
        self.error = error


_MESSAGE_PUMP_END = object()


class ClaudeAutonomousFollowupPending(RuntimeError):
    """A background Claude continuation owns the next response boundary."""


def _message_origin_kind(message: Any) -> str | None:
    """Return the SDK's authoritative turn provenance when one is present."""
    origin = getattr(message, "origin", None)
    if not isinstance(origin, dict):
        return None
    kind = origin.get("kind")
    return kind if isinstance(kind, str) and kind else None


def _explicit_cli_path(value: str) -> str | None:
    """Normalize an opt-in CLI path without changing blank/PATH behavior."""
    value = value.strip()
    if not value:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise RuntimeError("CLAUDE_BIN must be an absolute path")
    return path


def _claude_profile_settings_path(
    config_dir: str | None,
) -> str | None:
    """Return the selected native settings path without reading its contents."""
    if config_dir is None:
        return None
    path = os.path.join(config_dir, "settings.json")
    return path if os.path.isfile(path) else None


def _is_control_request_timeout(
    error: BaseException,
    *,
    subtype: str,
) -> bool:
    """Recognize the pinned SDK's untyped control-timeout wrapper.

    The SDK currently raises a plain ``Exception`` and preserves the timeout
    only in its message. Walk the local exception chain as well so a narrow
    adapter can add context without hiding the poisoned-control signal.
    """
    marker = f"control request timeout: {subtype}".casefold()
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if marker in str(current).casefold():
            return True
        current = current.__cause__ or current.__context__
    return False


class SdkHandle:
    def __init__(
        self,
        cfg: WrapperConfig,
        ask_server: Server | None = None,
        *,
        claude_config_dir: str | None = None,
        isolate_account_env: bool = False,
    ):
        self.cfg = cfg
        self.ask_server = ask_server  # in-process MCP server exposing ask_user
        self.claude_config_dir = claude_config_dir
        self.isolate_account_env = isolate_account_env
        self.client: ClaudeSDKClient | None = None
        # reasoning effort is a spawn-time flag (--effort), not a runtime setter.
        # `effort` is the desired level; `applied_effort` is what the live client
        # was spawned with — they differ after set_effort until the next reconnect.
        # Default to "max" so new sessions get the strongest reasoning out of the
        # box (matches the client's default chip); the user can lower it per session.
        self.effort: str | None = CLAUDE_DEFAULT_EFFORT
        self.applied_effort: str | None = None
        # Automatic compaction is also a spawn-time CLI option. Keep desired and
        # applied values separate so a busy turn can drain to ResultMessage before
        # the wrapper reconnects this exact session with the new threshold.
        self.auto_compact_mode = CLAUDE_DEFAULT_AUTO_COMPACT_MODE
        self.auto_compact_threshold_tokens: int | None = None
        self.applied_auto_compact_mode: str | None = None
        self.applied_auto_compact_threshold_tokens: int | None = None
        self.effective_auto_compact_threshold_tokens: int | None = None
        self.raw_context_max_tokens: int | None = None
        # Authoritative selected Claude alias for this session.  The transcript
        # may expose a proxy's upstream model (for example glm-5.2), so recover
        # this from the SDK control plane and preserve it across reconnects.
        self.model: str | None = None
        # Desired and live Claude permission mode. Runtime changes update this
        # only after the CLI accepts them; every later reconnect passes the same
        # value back through ClaudeAgentOptions instead of silently reverting.
        self.permission_mode = "bypassPermissions"
        self.work_mode = False
        self.work_settings_path: str | None = None
        # Captured once, before the first Work turn.  The UI can subtract this
        # fixed engine/tool overhead without pretending those real tokens do not
        # exist.  Code sessions intentionally leave it unset.
        self.work_context_baseline_tokens: int | None = None
        # A SetPerm can arrive while a turn task is respawning Claude for effort
        # or stale history. Serialize those two control paths so a successful
        # runtime change cannot land on the old child after the new options were
        # already captured.
        self._permission_reconnect_lock = asyncio.Lock()
        # Context inspection and query acceptance both write the SDK's one
        # stdin control stream. Keep their acceptance windows ordered even
        # though the SDK can track multiple request ids: Claude Code may process
        # a slow context rebuild synchronously and otherwise strand a prompt
        # behind it.
        self._control_request_lock = asyncio.Lock()
        # A timed-out control request is not cancelled inside Claude Code by the
        # SDK. The current child is therefore unsafe to reuse even if its
        # process and stdout reader still look alive.
        self.control_plane_failed = False
        # Compatibility/public state for adapters which expose whether context
        # inspection is temporarily unavailable. A timed-out generation is now
        # replaced instead of quarantining its successor until another model
        # turn: the replacement skips only its eager startup probe and is safe
        # for an explicit user read immediately.
        self.context_probe_suppressed = False
        self._last_context_usage: dict[str, Any] | None = None
        # Unlike the rich control response, the latest main-chain assistant
        # usage is session/transcript state and remains valid across a child
        # reconnect.  It keeps the UI useful when the optional control request
        # is quarantined or unavailable.
        self._last_recent_context_usage: dict[str, Any] | None = None
        self._conversation_rewind_probe_lock = asyncio.Lock()
        self._conversation_rewind_capability: (
            ClaudeConversationRewindCapability | None
        ) = None
        self.permission_callback: Callable[[str, dict[str, Any], Any], Awaitable[Any]] | None = None
        # Claude has no goal RPC.  This cache is reconstructed from native
        # goal_status transcript attachments on resume and updated by the live
        # /goal turn.  It is never persisted separately by cc-remote.
        self.goal: dict[str, Any] | None = None
        self.goal_session_id: str | None = None
        self._goal_message_tokens: dict[str, int] = {}
        # Claude's Query owns one anyio MemoryObjectReceiveStream.  Keep exactly
        # one session-long consumer of that stream, then route messages into a
        # bounded active-turn queue or a bounded idle/background queue.  This is
        # what lets task/hook notifications emitted after ResultMessage reach the
        # UI immediately without racing the next receive_response() consumer.
        self.background_message_callback: Callable[
            [Any, str | None], Awaitable[None]] | None = None
        # Query launch and background delivery share one ordered boundary.  The
        # machine guard is deliberately synchronous: it runs after every
        # already-routed background callback completed, while the pump cannot
        # classify another message, and immediately before the user prompt is
        # written to Claude Code.
        self.query_launch_guard: Callable[[], None] | None = None
        self.message_pump_failure_callback: Callable[
            [BaseException], Awaitable[None]] | None = None
        # Machine owns the connection-local task ledger because lifecycle
        # messages must be observed in delivery order. Disconnect nevertheless
        # invalidates that whole generation, so give it one synchronous reset
        # hook rather than leaving stale task ids on the resident context.
        self.lifecycle_reset_callback: Callable[[], None] | None = None
        # Machine sets this immediately before query(). It is copied onto every
        # post-Result background envelope so even a parentless Stop hook or a
        # newly-announced task remains attached to the turn that spawned it.
        self.next_turn_id: str | None = None
        self._turn_origin_id: str | None = None
        self._message_pump_task: asyncio.Task | None = None
        self._background_task: asyncio.Task | None = None
        self._turn_messages: asyncio.Queue | None = None
        self._background_messages: asyncio.Queue | None = None
        # ``_turn_active`` means a wrapper-submitted prompt is still waiting for
        # its own ResultMessage. Claude may inject complete autonomous turns on
        # the same stream before that result; ``_message_route_owner`` follows
        # their UserMessage/ResultMessage ``origin`` instead of assigning every
        # frame to whichever browser query happened to write most recently.
        self._turn_active = False
        self._turn_consumer_active = False
        self._message_route_owner: str | None = None
        self._turn_background_release: asyncio.Event | None = None
        self._pending_turn_background_release: asyncio.Event | None = None
        self._pending_turn_origin_id: str | None = None
        self._message_pump_error: BaseException | None = None
        self._message_route_lock = asyncio.Lock()
        self._background_callbacks_pending = 0
        self._background_callbacks_drained = asyncio.Event()
        self._background_callbacks_drained.set()

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        callback = self.permission_callback
        if callback is None:
            log.warning("tool permission requested without client callback", tool=tool_name)
            return PermissionResultDeny(message="remote permission callback unavailable")
        return await callback(tool_name, tool_input, context)

    @staticmethod
    def preflight(cli_path: str = "") -> None:
        # WrapperConfig supplies the user's daily ~/.local/bin/claude by
        # default. Inspect that exact runtime (or an explicit override), and
        # reject any unverified SDK patch release exactly.
        runtime = inspect_claude_runtime(cli_path)
        log.info(
            "Claude runtime verified",
            sdk_version=runtime.sdk_version,
            cli_version=runtime.cli_version,
            cli_source=runtime.cli_source,
            cli_path=runtime.cli_path,
        )

    def _options(
        self,
        resume_id: str | None,
        cwd: str | None = None,
        fork: bool = False,
        model_override: str | None = None,
        *,
        auto_compact_override: tuple[str, int | None] | None = None,
        effort_override: object = _CURRENT_LAUNCH_VALUE,
    ) -> ClaudeAgentOptions:
        if not self.work_mode:
            model_override = normalize_claude_model_selection(model_override)
        code_prompt_append = (
            "You have two MCP tools on the cc-remote-ask server:\n"
            "- `ask_user(question, options)`: ask the user a clarifying question with "
            "suggested choices and a custom-text fallback (instead of plain text). "
            "Blocks until they answer.\n"
            "- `set_mode(mode)`: switch cc's permission mode yourself, in the middle of a "
            "turn. When the user expresses intent — 'plan first' / 'let's plan this' -> "
            "set_mode('plan'); 'just do it' / 'go ahead' -> set_mode('bypassPermissions') "
            "or 'acceptEdits'. The user has no Shift+Tab here, so calling this is how you "
            "enter plan mode for them.\n"
            "Modes: default, acceptEdits, plan, auto, bypassPermissions."
        )
        extra_args = {"replay-user-messages": None}
        auto_compact_mode, auto_compact_threshold = (
            auto_compact_override
            if auto_compact_override is not None
            else (
                self.auto_compact_mode,
                self.auto_compact_threshold_tokens,
            )
        )
        auto_compact = claude_auto_compact_cli_value(
            auto_compact_mode,
            auto_compact_threshold,
        )
        launch_effort = (
            self.effort
            if effort_override is _CURRENT_LAUNCH_VALUE
            else effort_override
        )
        if auto_compact is not None:
            # SDK 0.2.151 has no typed option yet, but intentionally forwards
            # bounded extra_args to the pinned Claude Code runtime.
            extra_args["autocompact"] = auto_compact
        if self.work_mode:
            # Safe mode suppresses user-installed agents/plugins/MCP and other
            # customizations while retaining ordinary OAuth/provider auth.  Do
            # not use --bare here: it intentionally disables OAuth/keychain
            # auth, which would break subscription-backed Work sessions.
            extra_args["safe-mode"] = None
        elif self.permission_mode != "bypassPermissions":
            # A Code session restored while currently in a safer mode must still
            # retain Claude's native ability to cycle back to bypass later.
            extra_args["allow-dangerously-skip-permissions"] = None
        if (
            not self.work_mode
            and self.isolate_account_env
            and self.claude_config_dir is None
        ):
            raise ValueError(
                "isolated Claude profile requires a config directory")
        session_cwd = cwd or self.cfg.cc_cwd
        code_profile_settings = (
            None
            if self.isolate_account_env
            else _claude_profile_settings_path(self.claude_config_dir)
        )
        code_setting_sources = (
            ["user"]
            if self.isolate_account_env and not self.work_mode
            else None
        )
        code_child_env = claude_profile_child_env(
            self.claude_config_dir,
            isolate_account_env=self.isolate_account_env,
        )
        return ClaudeAgentOptions(
            tools=list(CLAUDE_WORK_TOOLS) if self.work_mode else None,
            include_partial_messages=True,        # StreamEvent with content_block_delta
            # Claude's Read result can carry image bytes as base64 in one NDJSON
            # record. The SDK defaults to 1 MiB, below cc-remote's bounded
            # attachment/image envelope, so an otherwise valid tool result can
            # kill the sole stdout reader while the Claude child keeps running.
            # This is an on-demand line limit, not a per-session preallocation.
            max_buffer_size=CLAUDE_MAX_BUFFER_SIZE,
            # Code exposes Claude-owned subagent output as a read-only process
            # projection. Work deliberately has no Agent tool or agent surface.
            forward_subagent_text=not self.work_mode,
            # Claude's public rewind_files() needs both checkpoint creation and
            # replayed UserMessage UUIDs.  The latter are also the stable UI
            # anchors used to choose a code-only rewind point.
            enable_file_checkpointing=True,
            extra_args=extra_args,
            # Emit hook lifecycle metadata into the SDK stream. StreamTranslator
            # forwards only the hook name/status/exit/duration; raw callback data,
            # output, commands, and environment values never cross the wire.
            include_hook_events=True,
            permission_mode=self.permission_mode,
            can_use_tool=self._can_use_tool,
            cwd=session_cwd,                      # dynamic: must match the resumed session's cwd
            cli_path=_explicit_cli_path(self.cfg.claude_bin),
            resume=resume_id or None,
            # fork_session=True resumes `resume_id`'s context but writes new turns to
            # a FRESH session id, leaving the original transcript untouched — used for
            # ephemeral /btw side-forks.
            fork_session=fork,
            model=model_override,
            # ``connect`` passes an immutable launch snapshot here.  The desired
            # value may change while the child is starting, but that later choice
            # must not be misreported as already applied to this generation.
            effort=launch_effort,                 # None -> CLI default (high)
            # The SDK otherwise copies the wrapper's complete environment into
            # Claude/tool subprocesses. Never expose relay login/bearer secrets.
            env=code_child_env,
            stderr=self._on_stderr,               # surface cc subprocess errors
            # Explicit Code profiles load only the selected native user source.
            # Project/local settings may contain provider env or helpers and
            # therefore cannot participate in an account-isolated process. Do
            # not promote the complete user file through --settings: that would
            # invert Claude's precedence for model, permissions and sandbox.
            # Work instead uses only a wrapper-owned policy that the writable
            # workspace cannot edit. [] plus safe mode is Work's complete
            # customization boundary.
            settings=(
                self.work_settings_path
                if self.work_mode else code_profile_settings
            ),
            setting_sources=(
                [] if self.work_mode else code_setting_sources
            ),
            skills=[] if self.work_mode else None,
            # The wrapper-owned Work settings file already contains the complete
            # fail-closed sandbox including its filesystem allowlist. SDK 0.2.151
            # replaces (rather than deep-merges) that object when `sandbox=` is
            # also supplied, silently dropping filesystem policy and inlining
            # provider credentials in argv. Pass only the policy path instead.
            sandbox=None,
            # Work deliberately replaces Claude Code's coding-focused preset.
            # Code retains the official preset plus cc-remote's control tools.
            system_prompt=(
                WORK_SYSTEM_PROMPT if self.work_mode else {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": code_prompt_append,
                }
            ),
            # Work asks ordinary clarifying questions in chat; it does not need
            # Code's ask/set-mode MCP schemas. Strict mode also prevents MCP
            # configured outside this explicit SDK invocation from leaking in.
            mcp_servers=(
                {} if self.work_mode else
                ({"cc-remote-ask": {"type": "sdk", "name": "cc-remote-ask", "instance": self.ask_server}}
                 if self.ask_server is not None else {})
            ),
            strict_mcp_config=self.work_mode,
            # Empty custom agents plus safe mode keeps installed/global agent
            # definitions out; the explicit tool allowlist also omits Agent.
            agents={} if self.work_mode else None,
        )

    @staticmethod
    def _on_stderr(line: str) -> None:
        # Child stderr may contain credentials or a pathological single line.
        # Preserve only its size; the raw text is never useful enough to justify
        # copying it into the control-plane logger.
        log.warning("cc stderr: ***", chars=len(line))

    async def connect(
        self,
        resume_id: str | None = None,
        cwd: str | None = None,
        fork: bool = False,
        model_override: str | None = None,
        _suppress_context_probe: bool = False,
        *,
        _launch_auto_compact: tuple[str, int | None] | None = None,
        _launch_effort: object = _CURRENT_LAUNCH_VALUE,
    ) -> None:
        # Capture every spawn-time option before the first await. A settings
        # command can run while disconnect/connect is in flight; that command is
        # the desired state for a later generation, not authority to rewrite the
        # exact argv of the child currently being launched.
        launch_auto_compact = valid_claude_auto_compact(*(
            _launch_auto_compact
            if _launch_auto_compact is not None
            else (
                self.auto_compact_mode,
                self.auto_compact_threshold_tokens,
            )
        ))
        launch_effort = (
            self.effort
            if _launch_effort is _CURRENT_LAUNCH_VALUE
            else _launch_effort
        )
        opts = self._options(
            resume_id,
            cwd,
            fork=fork,
            model_override=model_override,
            auto_compact_override=launch_auto_compact,
            effort_override=launch_effort,
        )
        if self.isolate_account_env:
            self.client = ClaudeSDKClient(
                options=opts,
                transport=account_isolated_transport(opts),
            )
        else:
            self.client = ClaudeSDKClient(options=opts)
        self._conversation_rewind_capability = None
        await self.client.connect()
        if fork:
            # A fork creates a new native conversation even though this handle
            # may be reused while a private BTW id is still being captured.
            self.invalidate_context_usage_cache()
        # The rich reading belongs to one concrete Claude child generation: a
        # reconnect can change effective autocompact capacity or model state.
        # The recent assistant total is conversation state and survives an
        # ordinary same-session reconnect; explicit transcript mutations and
        # fresh forks invalidate it through invalidate_context_usage_cache().
        self._last_context_usage = None
        self.effective_auto_compact_threshold_tokens = None
        self.raw_context_max_tokens = None
        self.control_plane_failed = False
        self.context_probe_suppressed = False
        # Resuming an old/large transcript can spend longer rebuilding context
        # than the bounded control timeout. Never make that optional metadata
        # read part of ordinary resume/reconnect readiness. A genuinely fresh
        # non-forked session has no historical rebuild and still benefits from
        # learning its provider-selected model and (for Work) startup baseline.
        eager_context_probe = bool(
            not _suppress_context_probe and resume_id is None and not fork
        )
        if eager_context_probe:
            try:
                usage = await self._read_context_usage_control(
                    timeout=_CONTEXT_STARTUP_TIMEOUT)
            except Exception as exc:
                if _is_control_request_timeout(
                    exc, subtype="get_context_usage"
                ):
                    # The SDK forgets its waiter on timeout but sends no cancel
                    # frame to Claude Code. Destroy this generation immediately;
                    # otherwise its still-blocked control loop also swallows the
                    # first query and interrupt. The replacement skips only this
                    # eager read and is immediately safe for normal traffic.
                    self.control_plane_failed = True
                    log.warning(
                        "Claude startup context probe timed out; replacing child"
                    )
                    try:
                        await self.disconnect()
                    except Exception as disconnect_exc:
                        log.warning(
                            "disconnect after Claude context timeout failed",
                            error_type=type(disconnect_exc).__name__,
                        )
                    await self.connect(
                        resume_id=resume_id,
                        cwd=cwd,
                        fork=fork,
                        model_override=model_override,
                        _suppress_context_probe=True,
                        _launch_auto_compact=launch_auto_compact,
                        _launch_effort=launch_effort,
                    )
                    return
                # Model readout is useful control state, but a semantic or
                # capability failure does not poison an otherwise live child.
                log.warning(
                    "Claude model state unavailable",
                    error=type(exc).__name__,
                )
            else:
                self._record_context_usage(
                    usage,
                    update_model=True,
                    capture_work_baseline=not resume_id,
                )

        self.goal_session_id = None if fork else resume_id
        self.goal = None
        self._goal_message_tokens.clear()
        if resume_id and not fork:
            await self.refresh_goal(resume_id)
        self.applied_effort = launch_effort
        self.applied_auto_compact_mode = launch_auto_compact[0]
        self.applied_auto_compact_threshold_tokens = (
            launch_auto_compact[1])
        self._start_message_pump()
        log.info("sdk connected", resume=bool(resume_id), fork=fork, cwd=opts.cwd,
                 effort=launch_effort, permission_mode=self.permission_mode,
                 auto_compact=launch_auto_compact[0],
                 auto_compact_threshold=launch_auto_compact[1],
                 context_probe_suppressed=self.context_probe_suppressed,
                 sdk_version=SDK_VERSION)

    async def _read_context_usage_control(
        self, *, timeout: float = _CONTEXT_CONTROL_TIMEOUT,
    ) -> dict:
        """Issue one bounded read on the pinned SDK control protocol."""
        async with self._control_request_lock:
            if self.control_plane_failed:
                raise RuntimeError("Claude SDK control plane is unhealthy")
            client = self.client
            if client is None:
                raise RuntimeError("Claude SDK is not connected")
            # The public helper hardcodes a 60-second timeout. A timeout cannot
            # be cancelled in the child, so bound it tightly and poison this
            # exact generation rather than letting later controls pile up.
            query = getattr(client, "_query", None)
            send_control = getattr(query, "_send_control_request", None)
            if not callable(send_control):
                raise RuntimeError("bounded model control request unavailable")
            try:
                usage = await send_control(
                    {"subtype": "get_context_usage"},
                    timeout=timeout,
                )
            except Exception as exc:
                if _is_control_request_timeout(
                    exc, subtype="get_context_usage"
                ):
                    self.control_plane_failed = True
                    self._conversation_rewind_capability = None
                raise
            if not isinstance(usage, dict):
                return {}
            return usage

    @asynccontextmanager
    async def context_recovery_barrier(self):
        """Freeze old-generation background routing during timeout recovery.

        A ``get_context_usage`` timeout can race an autonomous task notification
        on the same Claude stream.  The machine must not inspect an idle-looking
        ``SessionContext`` and then disconnect a generation whose background
        callback has already been queued but has not yet updated that context.

        Holding the route lock prevents the old message pump from enqueueing a
        new callback between the machine's final lifecycle check and
        ``force_reconnect()``.  Do not wait for callbacks here: a Result callback
        may legitimately be waiting for the machine's query lock.  Instead yield
        whether every already-routed callback has completed; a false value tells
        the caller to leave the poisoned generation in place until its real
        lifecycle boundary settles.
        """
        route_lock = self._message_route_lock
        acquired = False
        try:
            # Never wait behind the pump while the machine owns query_lock: a
            # background Result callback may be waiting for that same lock. A
            # short failed acquisition is itself proof that this generation is
            # not quiescent enough to reconnect from the metadata path.
            await asyncio.wait_for(
                route_lock.acquire(), timeout=0.05)
            acquired = True
        except asyncio.TimeoutError:
            yield False
            return
        try:
            yield self._background_callbacks_pending == 0
        finally:
            if acquired:
                route_lock.release()

    def _record_context_usage(
        self,
        usage: dict,
        *,
        update_model: bool = False,
        capture_work_baseline: bool = False,
    ) -> None:
        """Cache one successful reading and update generation metadata."""
        self._last_context_usage = dict(usage)
        total_tokens = usage.get("totalTokens")
        if (isinstance(total_tokens, int)
                and not isinstance(total_tokens, bool)
                and 0 <= total_tokens <= MAX_SAFE_WIRE_INTEGER):
            # Only a semantically usable exact response supersedes the latest
            # live/transcript fallback. A malformed successful control envelope
            # must not erase the last truthful reading.
            self._last_recent_context_usage = None
        if update_model:
            model = usage.get("model") if isinstance(usage, dict) else None
            selected_model = valid_claude_model(model)
            if selected_model is not None:
                # Claude's context response commonly reports the base alias even
                # when this generation was explicitly selected with ``[1m]``.
                # Preserve that proven selection across reconnects, but do not
                # promote a merely observed native/Work base model: Code owns
                # explicit model selection while Work keeps Claude's policy.
                current_model = valid_claude_model(self.model)
                if (
                    current_model is not None
                    and current_model.lower().endswith("[1m]")
                    and normalize_claude_model_selection(current_model)
                        == normalize_claude_model_selection(selected_model)
                ):
                    self.model = normalize_claude_model_selection(current_model)
                else:
                    self.model = selected_model
        auto_threshold = usage.get("autoCompactThreshold")
        self.effective_auto_compact_threshold_tokens = (
            auto_threshold
            if isinstance(auto_threshold, int)
            and not isinstance(auto_threshold, bool)
            and auto_threshold >= 0 else None
        )
        raw_max = usage.get("rawMaxTokens")
        self.raw_context_max_tokens = (
            raw_max
            if isinstance(raw_max, int)
            and not isinstance(raw_max, bool)
            and raw_max >= 0 else None
        )
        if (capture_work_baseline and self.work_mode
                and self.work_context_baseline_tokens is None):
            total_tokens = usage.get("totalTokens")
            if (isinstance(total_tokens, int)
                    and not isinstance(total_tokens, bool)
                    and total_tokens >= 0):
                self.work_context_baseline_tokens = total_tokens

    def cached_context_usage(self) -> dict | None:
        """Return the last successful reading without touching the child."""
        if self._last_context_usage is None:
            return None
        return dict(self._last_context_usage)

    def invalidate_context_usage_cache(self) -> None:
        """Discard readings after the native transcript changes identity/depth.

        Ordinary same-session child reconnects deliberately keep the latest
        assistant total. External appends, rewinds and a fresh fork do not: in
        those cases the machine calls this at the mutation boundary so the next
        cache-only read must recover from the current transcript.
        """
        self._last_context_usage = None
        self._last_recent_context_usage = None
        self.effective_auto_compact_threshold_tokens = None
        self.raw_context_max_tokens = None

    def _observe_recent_context_usage(self, message: Any) -> None:
        if (not isinstance(message, AssistantMessage)
                or message.parent_tool_use_id is not None):
            return
        recovered = claude_recent_context_usage(message.usage)
        if recovered is not None:
            self._last_recent_context_usage = recovered

    def _observe_context_boundary(self, message: Any) -> None:
        """Invalidate cached usage at every real native compact boundary."""
        if (
            isinstance(message, SystemMessage)
            and message.subtype == "compact_boundary"
        ):
            self.invalidate_context_usage_cache()

    def remember_recent_context_usage(self, usage: dict[str, Any]) -> None:
        """Seed a source-validated transcript fallback after cold resume."""
        total = usage.get("totalTokens") if isinstance(usage, dict) else None
        if (not isinstance(total, int) or isinstance(total, bool)
                or total <= 0 or total > MAX_SAFE_WIRE_INTEGER):
            return
        self._last_recent_context_usage = dict(usage)

    def cached_recent_context_usage(self) -> dict | None:
        if self._last_recent_context_usage is None:
            return None
        return dict(self._last_recent_context_usage)

    def _activate_pending_turn_route(self) -> None:
        """Bind post-Result background frames to the submitted browser turn."""
        release = self._pending_turn_background_release
        if release is None:
            return
        self._turn_background_release = release
        self._turn_origin_id = self._pending_turn_origin_id
        self._pending_turn_background_release = None
        self._pending_turn_origin_id = None

    def _discard_pending_turn_route(self) -> None:
        release = self._pending_turn_background_release
        if release is not None:
            release.set()
        self._pending_turn_background_release = None
        self._pending_turn_origin_id = None

    async def query(self, prompt) -> None:
        """Send a request. `prompt` is a string, or an async iterable of user-
        message dicts (used for multimodal input — text + image blocks)."""
        async with self._control_request_lock:
            if self.control_plane_failed:
                raise RuntimeError("Claude SDK control plane is unhealthy")
            client = self.client
            if client is None:
                raise RuntimeError("Claude SDK is not connected")
            if self._message_pump_task is not None:
                if self._message_pump_task.done():
                    raise RuntimeError("Claude SDK message pump is not running") from self._message_pump_error
                if self._turn_active or self._turn_consumer_active:
                    raise RuntimeError("Claude SDK already has an active response")
                # Do not let a Query overtake a task notification that the sole
                # reader already routed but whose machine callback has not yet
                # established the autonomous-follow-up latch.  Holding the route
                # lock through client.query() also prevents a same-tick message
                # from being classified against a half-started turn.
                async with self._message_route_lock:
                    if (
                        self._message_pump_error is not None
                        or self._message_pump_task.done()
                    ):
                        raise RuntimeError(
                            "Claude SDK message pump is not running"
                        ) from self._message_pump_error
                    await self._background_callbacks_drained.wait()
                    if (
                        self._message_pump_error is not None
                        or self._message_pump_task.done()
                    ):
                        raise RuntimeError(
                            "Claude SDK message pump is not running"
                        ) from self._message_pump_error
                    guard = self.query_launch_guard
                    try:
                        if guard is not None:
                            guard()
                    except BaseException:
                        # This id belongs only to the rejected browser prompt.
                        # Keeping it would mis-attach later autonomous frames.
                        self.next_turn_id = None
                        raise
                    # Do not replace the current background barrier yet. An
                    # autonomous turn can already be present in the SDK's
                    # internal receive queue even though our sole pump has not
                    # been scheduled. Its origin-marked User/Result frames must
                    # keep the preceding turn's released barrier. The new one is
                    # activated only when the pump sees this submitted turn.
                    self._pending_turn_background_release = asyncio.Event()
                    self._pending_turn_origin_id = self.next_turn_id
                    self.next_turn_id = None
                    self._turn_active = True
                    try:
                        await client.query(prompt)
                    except BaseException:
                        self._turn_active = False
                        self._discard_pending_turn_route()
                        raise
                return
            # Compatibility for tests/custom clients that install a client without
            # going through connect(). Real SDK connections always use the sole pump.
            await client.query(prompt)

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch the model for the live cc subprocess (takes effect next query,
        no reconnect)."""
        model = normalize_claude_model_selection(model)
        if model is None:
            raise ValueError("Claude model is required")
        async with self._control_request_lock:
            assert self.client is not None
            previous_model = self.model
            await self.client.set_model(model)
            if previous_model != model:
                # Context tokenization, capacity and category composition are
                # model-specific. Serialize this boundary with an in-flight
                # get_context_usage request, then discard every reading from
                # the previous model only after Claude confirms the switch.
                self.invalidate_context_usage_cache()
            self.model = model
        log.info("model set", model=model)

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the permission mode for the live cc subprocess (runtime, no reconnect)."""
        async with self._permission_reconnect_lock:
            assert self.client is not None
            await self.client.set_permission_mode(mode)
            self.permission_mode = mode
        log.info("permission mode set", mode=mode)

    def set_auto_compact(
        self, mode: str, threshold_tokens: int | None = None,
    ) -> None:
        """Record a validated desired threshold; reconnect is machine-owned."""
        checked_mode, checked_threshold = valid_claude_auto_compact(
            mode, threshold_tokens)
        if checked_mode != mode or checked_threshold != threshold_tokens:
            raise ValueError("invalid Claude autocompact configuration")
        self.auto_compact_mode = checked_mode
        self.auto_compact_threshold_tokens = checked_threshold

    async def get_context_usage(self) -> dict:
        """Return the cc session's context window usage (matches CLI /context)."""
        usage = await self._read_context_usage_control()
        self._record_context_usage(usage, update_model=True)
        return usage

    async def rewind_files(self, user_message_id: str) -> None:
        """Restore SDK-checkpointed files to a UserMessage UUID."""
        target = validate_rewind_target(
            user_message_id, operation="files")
        client = self.client
        if client is None:
            raise ClaudeRewindError("not_connected", operation="files")
        rewind = getattr(client, "rewind_files", None)
        if not callable(rewind):
            raise ClaudeRewindError(
                "capability_unavailable", operation="files")
        try:
            await rewind(target)
        except ClaudeRewindError:
            raise
        except Exception as exc:
            log.warning(
                "Claude file rewind failed", error_type=type(exc).__name__)
            classified = classify_control_failure(exc, operation="files")
            if classified.code in {"timeout", "capability_unavailable"}:
                raise classified from None
            raise ClaudeRewindError(
                "file_rewind_failed", operation="files") from None

    def _conversation_rewind_sender(self):
        client = self.client
        if client is None:
            return None
        query = getattr(client, "_query", None)
        sender = getattr(query, "_send_control_request", None)
        return sender if callable(sender) else None

    async def conversation_rewind_capability(
        self,
        *,
        refresh: bool = False,
    ) -> ClaudeConversationRewindCapability:
        """Probe the private subtype with an impossible UUID and cache support.

        There is no public SDK/server-info feature bit for conversation rewind.
        A semantic rejection (for example ``target not found``) proves that the
        CLI understands the subtype without changing conversation state.
        """
        if self.client is None:
            raise ClaudeRewindError(
                "not_connected", operation="conversation")
        if not refresh and self._conversation_rewind_capability is not None:
            return self._conversation_rewind_capability
        async with self._conversation_rewind_probe_lock:
            if not refresh and self._conversation_rewind_capability is not None:
                return self._conversation_rewind_capability
            sender = self._conversation_rewind_sender()
            if sender is None:
                capability = ClaudeConversationRewindCapability(
                    supported=False,
                    reason="sdk_control_unavailable",
                )
                self._conversation_rewind_capability = capability
                return capability
            try:
                response = await sender(
                    {
                        "subtype": "rewind_conversation",
                        "target_message_uuid": _CONVERSATION_REWIND_PROBE_UUID,
                        "interrupt_if_running": False,
                    },
                    timeout=5.0,
                )
            except Exception as exc:
                if is_unsupported_control_error(exc):
                    capability = ClaudeConversationRewindCapability(
                        supported=False,
                        reason="unsupported_control_subtype",
                    )
                    self._conversation_rewind_capability = capability
                    return capability
                # Busy/state errors are semantic proof that the handler exists.
                classified = classify_control_failure(exc)
                if classified.code in {
                    "commands_queued",
                    "turn_running",
                    "target_not_found",
                    "stale_target",
                    "no_preceding_assistant",
                    "state_changed",
                }:
                    capability = ClaudeConversationRewindCapability(
                        supported=True)
                    self._conversation_rewind_capability = capability
                    return capability
                raise ClaudeRewindError(
                    "capability_probe_failed",
                    operation="conversation",
                    retryable=True,
                ) from None
            if not response_proves_conversation_rewind(response):
                raise ClaudeRewindError(
                    "capability_probe_failed",
                    operation="conversation",
                    retryable=True,
                )
            capability = ClaudeConversationRewindCapability(supported=True)
            self._conversation_rewind_capability = capability
            return capability

    async def supports_rewind_conversation(self) -> bool:
        return (await self.conversation_rewind_capability()).supported

    async def rewind_conversation(
        self,
        target_message_uuid: str,
        *,
        interrupt_if_running: bool = False,
    ) -> ClaudeConversationRewindResult:
        """Apply Claude Code's guarded native conversation-only rewind."""
        target = validate_rewind_target(
            target_message_uuid, operation="conversation")
        capability = await self.conversation_rewind_capability()
        if not capability.supported:
            raise ClaudeRewindError(
                "capability_unavailable", operation="conversation")
        sender = self._conversation_rewind_sender()
        if sender is None:
            # The client may have disconnected after the cached probe.
            self._conversation_rewind_capability = None
            raise ClaudeRewindError(
                "not_connected", operation="conversation")
        try:
            response = await sender(
                {
                    "subtype": "rewind_conversation",
                    "target_message_uuid": target,
                    "interrupt_if_running": bool(interrupt_if_running),
                },
                timeout=30.0,
            )
        except Exception as exc:
            if is_unsupported_control_error(exc):
                self._conversation_rewind_capability = (
                    ClaudeConversationRewindCapability(
                        supported=False,
                        reason="unsupported_control_subtype",
                    )
                )
            raise classify_control_failure(exc) from None
        return parse_conversation_rewind_response(
            response, requested_target=target)

    async def prepare_conversation_rewind(
        self, *, resume_id: str, cwd: str | None,
    ) -> None:
        """Reload native state and prove rewind support before any file mutation.

        A long-lived SDK child may retain queued private-control state after
        terminal/broker ownership changes. Reconnect first so a combined
        restore never rewinds files using a runtime already unable to service
        the conversation half.
        """
        await self.force_reconnect(
            resume_id=resume_id,
            cwd=cwd,
            reason="prepare conversation rewind",
            preserve_model=True,
        )
        capability = await self.conversation_rewind_capability(refresh=True)
        if not capability.supported:
            raise ClaudeRewindError(
                "capability_unavailable", operation="conversation")

    async def prepare_goal(self, thread_id: str, objective: str) -> dict[str, Any]:
        """Install the immediate state for a soon-to-be-submitted native /goal.

        The machine submits the actual ``/goal <condition>`` through the normal
        turn path immediately after this call.  Capturing context usage first
        preserves the same baseline that Claude's in-memory activeGoal tracks.
        """
        tokens_at_start = None
        # Goal creation is not a user request to inspect /context. Reuse the
        # newest local reading so this optional accounting baseline can never
        # put a slow resumed conversation's control generation at risk.
        usage = self.cached_context_usage()
        if usage is None:
            usage = self.cached_recent_context_usage()
        value = usage.get("totalTokens") if isinstance(usage, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            tokens_at_start = max(0, value)
        self.goal_session_id = thread_id
        self.goal = make_claude_goal(
            thread_id, objective, tokens_at_start=tokens_at_start)
        self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def clear_goal_state(self) -> None:
        self.goal = None
        self._goal_message_tokens.clear()

    def restore_goal_state(self, goal: dict[str, Any] | None) -> None:
        self.goal = dict(goal) if goal is not None else None
        self.goal_session_id = (
            self.goal.get("threadId") if self.goal is not None else self.goal_session_id)
        self._goal_message_tokens.clear()

    def rekey_goal(self, session_id: str) -> None:
        self.goal_session_id = session_id
        if self.goal is not None:
            self.goal["threadId"] = session_id

    async def refresh_goal(self, session_id: str | None = None):
        """Refresh cached state from Claude's native transcript if it exists."""
        target = session_id or self.goal_session_id
        if target:
            if self.claude_config_dir is None:
                exists, goal = await asyncio.to_thread(
                    read_claude_goal, target)
            else:
                source = await asyncio.to_thread(
                    claude_catalog.find_session_file,
                    self.claude_config_dir,
                    target,
                )
                if source is None:
                    exists, goal = False, None
                else:
                    exists, goal = await asyncio.to_thread(
                        read_claude_goal, target, path=str(source))
            if exists:
                self.goal = goal
                self.goal_session_id = target
                self._goal_message_tokens.clear()
        return current_goal(self.goal)

    def observe_goal_message(self, msg: Any, thread_id: str):
        """Apply a native active_goal event or live Stop-hook feedback."""
        update = active_goal_from_message(msg, thread_id)
        if update is not NO_GOAL_EVENT:
            self.goal = update
            self.goal_session_id = thread_id
            self._goal_message_tokens.clear()
            return True, current_goal(self.goal)
        changed = goal_message_update(
            msg, self.goal, self._goal_message_tokens)
        return changed, current_goal(self.goal)

    @staticmethod
    def _parse_compat_message(data: Any):
        if isinstance(data, dict) and data.get("type") == "active_goal":
            return SystemMessage(subtype="active_goal", data=data)
        return _parse_sdk_message(data)

    async def _receive_response_compat(self):
        """Preserve raw active_goal frames dropped by SDK <= 0.2.116.

        The SDK's public ``receive_response`` parses messages only after reading
        its private Query queue.  Its forward-compatible parser intentionally
        returns None for unknown top-level types, including ``active_goal``.
        Reading the same queue here and wrapping only that one known schema as a
        SystemMessage is the smallest compatibility layer; all other messages
        still use the SDK parser verbatim.
        """
        assert self.client is not None
        query = getattr(self.client, "_query", None)
        if query is None or not hasattr(query, "receive_messages"):
            async for message in self.client.receive_response():
                self._observe_model_fallback(message)
                yield message
            return
        async for data in query.receive_messages():
            message = self._parse_compat_message(data)
            if message is None:
                continue
            self._observe_recent_context_usage(message)
            self._observe_context_boundary(message)
            self._observe_model_fallback(message)
            if (isinstance(message, ResultMessage)
                    and not bool(getattr(message, "is_error", False))):
                # A complete successful turn proves that a no-probe replacement
                # can service normal conversation traffic. Context inspection
                # may be attempted again once the machine returns to idle.
                self.context_probe_suppressed = False
            yield message
            if isinstance(message, ResultMessage):
                return

    def _observe_model_fallback(self, message) -> None:
        if isinstance(message, SystemMessage):
            fallback = model_fallback_event(message.data)
            if (fallback is not None and fallback.input
                    and fallback.input.get("scope") == "session"):
                self.model = fallback.input["fallback_model"]
                self.invalidate_context_usage_cache()

    def _start_message_pump(self) -> None:
        """Start the sole consumer of the SDK Query message stream."""
        if self._message_pump_task is not None:
            raise RuntimeError("Claude SDK message pump already started")
        cap = max(1, int(getattr(self.cfg, "turn_reader_queue_cap", 4)))
        self._turn_messages = asyncio.Queue(maxsize=cap)
        self._background_messages = asyncio.Queue(maxsize=cap)
        self._turn_active = False
        self._turn_consumer_active = False
        self._message_route_owner = None
        self._message_pump_error = None
        self._message_route_lock = asyncio.Lock()
        self._background_callbacks_pending = 0
        self._background_callbacks_drained = asyncio.Event()
        self._background_callbacks_drained.set()
        initial_release = asyncio.Event()
        initial_release.set()
        self._turn_background_release = initial_release
        self._pending_turn_background_release = None
        self._pending_turn_origin_id = None
        client = self.client
        assert client is not None
        self._message_pump_task = asyncio.create_task(
            self._message_pump(client))
        self._background_task = asyncio.create_task(
            self._background_message_worker())

    async def _message_pump(self, client: ClaudeSDKClient) -> None:
        """Read the private SDK queue once for the complete client lifetime."""
        assert self._turn_messages is not None
        assert self._background_messages is not None
        try:
            query = getattr(client, "_query", None)
            if query is not None and hasattr(query, "receive_messages"):
                source = query.receive_messages()
                parse_raw = True
            else:
                source = client.receive_messages()
                parse_raw = False
            async for data in source:
                message = self._parse_compat_message(data) if parse_raw else data
                if message is None:
                    continue
                self._observe_recent_context_usage(message)
                self._observe_context_boundary(message)
                self._observe_model_fallback(message)
                if (isinstance(message, ResultMessage)
                        and not bool(getattr(message, "is_error", False))):
                    self.context_probe_suppressed = False
                async with self._message_route_lock:
                    origin_kind = _message_origin_kind(message)
                    top_level_user = bool(
                        isinstance(message, UserMessage)
                        and not message.parent_tool_use_id
                    )
                    if top_level_user:
                        if origin_kind is not None and origin_kind != "human":
                            owner = "background"
                        elif self._turn_active:
                            owner = "managed"
                            self._activate_pending_turn_route()
                        else:
                            owner = "background"
                        self._message_route_owner = owner
                    elif isinstance(message, ResultMessage):
                        # Result.origin is the authoritative boundary in the
                        # pinned SDK. A non-human result closes only the injected
                        # turn; the browser query remains pending for its later
                        # human/legacy-unattributed result on the same stream.
                        if origin_kind is not None and origin_kind != "human":
                            owner = "background"
                        elif self._turn_active:
                            owner = "managed"
                            self._activate_pending_turn_route()
                        else:
                            owner = self._message_route_owner or "background"
                    else:
                        owner = self._message_route_owner
                        if owner is None:
                            # While a newly submitted prompt is still waiting
                            # for its replayed human User/Result boundary, any
                            # already-buffered unattributed frame belongs to the
                            # preceding autonomous/background stream. This is
                            # the exact race where a task notification was in
                            # Claude Code's queue before client.query() wrote the
                            # browser prompt. The human boundary below remains
                            # the only authority that activates the new route.
                            owner = (
                                "background"
                                if self._pending_turn_background_release
                                is not None
                                else (
                                    "managed"
                                    if self._turn_active
                                    else "background"
                                )
                            )

                    if owner == "managed":
                        await self._turn_messages.put(message)
                        if isinstance(message, ResultMessage):
                            self._turn_active = False
                            self._message_route_owner = None
                        continue
                    release = self._turn_background_release
                    if release is None:
                        release = asyncio.Event()
                        release.set()
                    self._background_callbacks_pending += 1
                    self._background_callbacks_drained.clear()
                    try:
                        await self._background_messages.put(
                            (message, release, self._turn_origin_id))
                    except BaseException:
                        self._background_callback_completed()
                        raise
                    if isinstance(message, ResultMessage):
                        self._message_route_owner = None
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._message_pump_error = exc
            if self._turn_active:
                self._turn_active = False
                self._discard_pending_turn_route()
                await self._turn_messages.put(_MessagePumpFailure(exc))
            else:
                log.warning(
                    "Claude SDK message pump stopped",
                    error_type=type(exc).__name__)
            await self._notify_message_pump_failure(exc)
        else:
            error = RuntimeError(
                "Claude SDK stream ended without a ResultMessage")
            self._message_pump_error = error
            if self._turn_active:
                self._turn_active = False
                self._discard_pending_turn_route()
                await self._turn_messages.put(_MESSAGE_PUMP_END)
            await self._notify_message_pump_failure(error)

    async def _notify_message_pump_failure(
        self, error: BaseException,
    ) -> None:
        callback = self.message_pump_failure_callback
        if callback is None:
            return
        try:
            await callback(error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Failure recovery is advisory to the session state machine.  It
            # must never replace the pump's authoritative failure or strand
            # disconnect() while reporting a secondary UI problem.
            log.warning(
                "Claude message pump failure callback failed",
                error_type=type(exc).__name__,
            )

    def _background_callback_completed(self) -> None:
        if self._background_callbacks_pending > 0:
            self._background_callbacks_pending -= 1
        if self._background_callbacks_pending == 0:
            self._background_callbacks_drained.set()

    async def _background_message_worker(self) -> None:
        """Deliver idle notifications in order with bounded backpressure."""
        assert self._background_messages is not None
        while True:
            message, release, turn_id = await self._background_messages.get()
            try:
                await release.wait()
                callback = self.background_message_callback
                if (
                    callback is not None
                    and self._message_pump_error is None
                ):
                    await callback(message, turn_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One malformed/background notification must not kill the sole
                # SDK reader and make the next user query hang forever.
                log.warning(
                    "Claude background message callback failed",
                    error_type=type(exc).__name__)
            finally:
                self._background_callback_completed()

    async def _receive_response_pumped(self):
        queue = self._turn_messages
        if queue is None:
            raise RuntimeError("Claude SDK message pump is unavailable")
        if self._turn_consumer_active:
            raise RuntimeError("Claude SDK response already has a consumer")
        self._turn_consumer_active = True
        try:
            while True:
                message = await queue.get()
                if message is _MESSAGE_PUMP_END:
                    raise RuntimeError(
                        "Claude SDK stream ended without a ResultMessage")
                if isinstance(message, _MessagePumpFailure):
                    raise message.error
                yield message
                if isinstance(message, ResultMessage):
                    return
        finally:
            self._turn_consumer_active = False

    @property
    def message_pump_failed(self) -> bool:
        """Whether this client must be reconnected before another query."""
        task = self._message_pump_task
        return (
            self._message_pump_error is not None
            or (task is not None and task.done())
        )

    def release_background_messages(self) -> None:
        """Release notifications ordered after the processed ResultMessage."""
        if self._turn_background_release is not None:
            self._turn_background_release.set()

    def receive_response(self):
        assert self.client is not None
        if self._message_pump_task is not None:
            return self._receive_response_pumped()
        return self._receive_response_compat()

    async def _stop_message_pump(self) -> None:
        release = self._turn_background_release
        if release is not None:
            release.set()
        pending_release = self._pending_turn_background_release
        if pending_release is not None:
            pending_release.set()
        tasks = [task for task in (
            self._message_pump_task, self._background_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._message_pump_task = None
        self._background_task = None
        self._turn_messages = None
        self._background_messages = None
        self._turn_active = False
        self._turn_consumer_active = False
        self._message_route_owner = None
        self._turn_background_release = None
        self._pending_turn_background_release = None
        self._pending_turn_origin_id = None
        self._turn_origin_id = None
        self._background_callbacks_pending = 0
        self._background_callbacks_drained.set()

    async def disconnect(self) -> None:
        try:
            if self.client is not None:
                await self._stop_message_pump()
                await self.client.disconnect()
        finally:
            self.client = None
            self._conversation_rewind_capability = None
            callback = self.lifecycle_reset_callback
            if callback is not None:
                try:
                    callback()
                except Exception as exc:
                    # A lifecycle reset must never mask the real disconnect
                    # result or prevent a force_reconnect recovery.
                    log.warning(
                        "Claude lifecycle reset callback failed",
                        error_type=type(exc).__name__,
                    )

    async def force_reconnect(
        self,
        resume_id: str | None,
        cwd: str | None = None,
        reason: str = "drain timeout",
        preserve_model: bool = True,
        fork: bool = False,
        apply_pending_auto_compact: bool = False,
        launch_auto_compact: tuple[str, int | None] | None = None,
    ) -> None:
        """Tear down and reconnect with resume. Used after a drain timeout, and to
        apply a spawn-time option change (e.g. effort) to a live session."""
        async with self._permission_reconnect_lock:
            log.warning("force-reconnecting SDK client", reason=reason)
            launch_effort = self.effort
            desired_auto_compact = (
                self.auto_compact_mode,
                self.auto_compact_threshold_tokens,
            )
            if launch_auto_compact is not None:
                exact_auto_compact = valid_claude_auto_compact(
                    *launch_auto_compact)
            elif (
                not apply_pending_auto_compact
                and self.applied_auto_compact_mode
                    in {"inherit", "auto", "custom"}
            ):
                exact_auto_compact = valid_claude_auto_compact(
                    self.applied_auto_compact_mode,
                    self.applied_auto_compact_threshold_tokens,
                )
            else:
                exact_auto_compact = valid_claude_auto_compact(
                    *desired_auto_compact)
            model_override = self.model if preserve_model else None
            try:
                await self.disconnect()
            except Exception as e:
                log.warning("disconnect during force-reconnect failed", error=str(e))
            if not preserve_model:
                # An external terminal may have changed this session's model.
                # Let resume recover it instead of forcing our stale cache back.
                self.model = None
            await self.connect(
                resume_id=resume_id, cwd=cwd,
                model_override=model_override, fork=fork,
                # A reconnect is never a fresh-session metadata boundary. It
                # must become query-ready without synchronously rebuilding
                # /context, including after a poisoned control generation.
                _suppress_context_probe=True,
                _launch_auto_compact=exact_auto_compact,
                _launch_effort=launch_effort,
            )
