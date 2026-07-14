"""ClaudeSDKClient lifecycle: connect / query / interrupt / receive / resume.

Isolates the one version-sensitive call site (`include_partial_messages` on
ClaudeAgentOptions) so an SDK upgrade touches only this file. The wrapper does
NOT set ANTHROPIC_BASE_URL or setting_sources — it wants ~/.claude/settings.json
loaded so cc inherits the user's model link and model id. Permission mode is
explicit live session state and survives reconnects.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions, PermissionResultDeny,
    __version__ as SDK_VERSION,
)
from claude_agent_sdk._internal.message_parser import parse_message as _parse_sdk_message
from claude_agent_sdk.types import ResultMessage, SystemMessage
from mcp.server import Server

from cc_remote.config import WrapperConfig
from cc_remote.log import logger
from cc_remote.wrapper.child_env import child_env_tombstones
from cc_remote.wrapper.claude_goal import (
    NO_GOAL_EVENT,
    active_goal_from_message,
    current_goal,
    goal_message_update,
    make_claude_goal,
    read_claude_goal,
)

log = logger("cc_remote.wrapper.sdk")

REQUIRED_SDK = (0, 2)  # 0.2.x; the interrupt/drain contract is version-sensitive
CLAUDE_DEFAULT_EFFORT = "max"


class _MessagePumpFailure:
    def __init__(self, error: BaseException):
        self.error = error


_MESSAGE_PUMP_END = object()


def _explicit_cli_path(value: str) -> str | None:
    """Normalize an opt-in CLI path without changing blank/PATH behavior."""
    value = value.strip()
    if not value:
        return None
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise RuntimeError("CLAUDE_BIN must be an absolute path")
    return path


class SdkHandle:
    def __init__(self, cfg: WrapperConfig, ask_server: Server | None = None):
        self.cfg = cfg
        self.ask_server = ask_server  # in-process MCP server exposing ask_user
        self.client: ClaudeSDKClient | None = None
        # reasoning effort is a spawn-time flag (--effort), not a runtime setter.
        # `effort` is the desired level; `applied_effort` is what the live client
        # was spawned with — they differ after set_effort until the next reconnect.
        # Default to "max" so new sessions get the strongest reasoning out of the
        # box (matches the client's default chip); the user can lower it per session.
        self.effort: str | None = CLAUDE_DEFAULT_EFFORT
        self.applied_effort: str | None = None
        # Authoritative selected Claude alias for this session.  The transcript
        # may expose a proxy's upstream model (for example glm-5.2), so recover
        # this from the SDK control plane and preserve it across reconnects.
        self.model: str | None = None
        # Desired and live Claude permission mode. Runtime changes update this
        # only after the CLI accepts them; every later reconnect passes the same
        # value back through ClaudeAgentOptions instead of silently reverting.
        self.permission_mode = "bypassPermissions"
        # A SetPerm can arrive while a turn task is respawning Claude for effort
        # or stale history. Serialize those two control paths so a successful
        # runtime change cannot land on the old child after the new options were
        # already captured.
        self._permission_reconnect_lock = asyncio.Lock()
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
        # Machine sets this immediately before query(). It is copied onto every
        # post-Result background envelope so even a parentless Stop hook or a
        # newly-announced task remains attached to the turn that spawned it.
        self.next_turn_id: str | None = None
        self._turn_origin_id: str | None = None
        self._message_pump_task: asyncio.Task | None = None
        self._background_task: asyncio.Task | None = None
        self._turn_messages: asyncio.Queue | None = None
        self._background_messages: asyncio.Queue | None = None
        self._turn_active = False
        self._turn_consumer_active = False
        self._turn_background_release: asyncio.Event | None = None
        self._message_pump_error: BaseException | None = None

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        callback = self.permission_callback
        if callback is None:
            log.warning("tool permission requested without client callback", tool=tool_name)
            return PermissionResultDeny(message="remote permission callback unavailable")
        return await callback(tool_name, tool_input, context)

    @staticmethod
    def preflight(cli_path: str = "") -> None:
        explicit = _explicit_cli_path(cli_path)
        if explicit:
            if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
                raise RuntimeError(
                    f"CLAUDE_BIN is not an executable file: {explicit}"
                )
        elif not shutil.which("claude"):
            raise RuntimeError("'claude' CLI not found on PATH; install Claude Code v2.1.51+")
        try:
            parts = SDK_VERSION.split(".")
            major, minor = int(parts[0]), int(parts[1])
        except Exception:
            raise RuntimeError(f"unparsable claude-agent-sdk version: {SDK_VERSION!r}")
        if (major, minor) != REQUIRED_SDK:
            raise RuntimeError(
                f"claude-agent-sdk {SDK_VERSION} != expected {REQUIRED_SDK[0]}.{REQUIRED_SDK[1]}.x; "
                f"the interrupt/drain contract may have changed — pin 0.2.110 or re-verify."
            )

    def _options(self, resume_id: str | None, cwd: str | None = None,
                 fork: bool = False,
                 model_override: str | None = None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            include_partial_messages=True,        # StreamEvent with content_block_delta
            # Emit hook lifecycle metadata into the SDK stream. StreamTranslator
            # forwards only the hook name/status/exit/duration; raw callback data,
            # output, commands, and environment values never cross the wire.
            include_hook_events=True,
            permission_mode=self.permission_mode,
            can_use_tool=self._can_use_tool,
            cwd=cwd or self.cfg.cc_cwd,           # dynamic: must match the resumed session's cwd
            cli_path=_explicit_cli_path(self.cfg.claude_bin),
            resume=resume_id or None,
            # fork_session=True resumes `resume_id`'s context but writes new turns to
            # a FRESH session id, leaving the original transcript untouched — used for
            # ephemeral /btw side-forks.
            fork_session=fork,
            model=model_override,
            effort=self.effort,                   # reasoning strength; None -> CLI default (high)
            # The SDK otherwise copies the wrapper's complete environment into
            # Claude/tool subprocesses. Never expose relay login/bearer secrets.
            env=child_env_tombstones(),
            stderr=self._on_stderr,               # surface cc subprocess errors
            # setting_sources left None -> load ~/.claude/settings.json (model link, model id)
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": (
                    "You have two MCP tools on the cc-remote-ask server:\n"
                    "- `ask_user(question, options)`: ask the user a multiple-choice clarifying "
                    "question (instead of plain text). Blocks until they answer.\n"
                    "- `set_mode(mode)`: switch cc's permission mode yourself, in the middle of a "
                    "turn. When the user expresses intent — 'plan first' / 'let's plan this' -> "
                    "set_mode('plan'); 'just do it' / 'go ahead' -> set_mode('bypassPermissions') "
                    "or 'acceptEdits'. The user has no Shift+Tab here, so calling this is how you "
                    "enter plan mode for them.\n"
                    "Modes: default, acceptEdits, plan, auto, bypassPermissions."
                ),
            },
            mcp_servers=(
                {"cc-remote-ask": {"type": "sdk", "name": "cc-remote-ask", "instance": self.ask_server}}
                if self.ask_server is not None else {}
            ),
        )

    @staticmethod
    def _on_stderr(line: str) -> None:
        # Child stderr may contain credentials or a pathological single line.
        # Preserve only its size; the raw text is never useful enough to justify
        # copying it into the control-plane logger.
        log.warning("cc stderr: ***", chars=len(line))

    async def connect(self, resume_id: str | None = None, cwd: str | None = None,
                      fork: bool = False,
                      model_override: str | None = None) -> None:
        opts = self._options(
            resume_id, cwd, fork=fork, model_override=model_override)
        self.client = ClaudeSDKClient(options=opts)
        await self.client.connect()
        try:
            # claude-agent-sdk 0.2.110's public helper hardcodes a 60s timeout.
            # This project pins and preflights that SDK, so use the same control
            # request with a bounded timeout; its implementation also cleans both
            # pending maps on timeout instead of leaking a cancelled request.
            query = getattr(self.client, "_query", None)
            send_control = getattr(query, "_send_control_request", None)
            if not callable(send_control):
                raise RuntimeError("bounded model control request unavailable")
            usage = await send_control(
                {"subtype": "get_context_usage"}, timeout=5.0)
            model = usage.get("model") if isinstance(usage, dict) else None
            if isinstance(model, str) and 0 < len(model.strip()) <= 256:
                self.model = model.strip()
        except Exception as exc:
            # Model readout is useful control state, but failure to obtain it
            # must not make an otherwise healthy Claude session unusable.
            log.warning("Claude model state unavailable",
                        error=type(exc).__name__)
        self.goal_session_id = None if fork else resume_id
        self.goal = None
        self._goal_message_tokens.clear()
        if resume_id and not fork:
            await self.refresh_goal(resume_id)
        self.applied_effort = self.effort  # the live subprocess now reflects this effort
        self._start_message_pump()
        log.info("sdk connected", resume=bool(resume_id), fork=fork, cwd=opts.cwd,
                 effort=self.effort, permission_mode=self.permission_mode,
                 sdk_version=SDK_VERSION)

    async def query(self, prompt) -> None:
        """Send a request. `prompt` is a string, or an async iterable of user-
        message dicts (used for multimodal input — text + image blocks)."""
        assert self.client is not None
        if self._message_pump_task is not None:
            if self._message_pump_task.done():
                raise RuntimeError("Claude SDK message pump is not running") from self._message_pump_error
            if self._turn_active or self._turn_consumer_active:
                raise RuntimeError("Claude SDK already has an active response")
            # Each turn gets its own barrier. Background messages retain the
            # barrier belonging to the Result they followed, so a later query
            # cannot re-block old queued notifications and deadlock the reader.
            self._turn_background_release = asyncio.Event()
            self._turn_origin_id = self.next_turn_id
            self.next_turn_id = None
            self._turn_active = True
            try:
                await self.client.query(prompt)
            except BaseException:
                self._turn_active = False
                self._turn_background_release.set()
                raise
            return
        # Compatibility for tests/custom clients that install a client without
        # going through connect(). Real SDK connections always use the sole pump.
        await self.client.query(prompt)

    async def interrupt(self) -> None:
        assert self.client is not None
        await self.client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch the model for the live cc subprocess (takes effect next query,
        no reconnect)."""
        assert self.client is not None
        await self.client.set_model(model)
        self.model = model
        log.info("model set", model=model)

    async def set_permission_mode(self, mode: str) -> None:
        """Switch the permission mode for the live cc subprocess (runtime, no reconnect)."""
        async with self._permission_reconnect_lock:
            assert self.client is not None
            await self.client.set_permission_mode(mode)
            self.permission_mode = mode
        log.info("permission mode set", mode=mode)

    async def get_context_usage(self) -> dict:
        """Return the cc session's context window usage (matches CLI /context)."""
        assert self.client is not None
        return await self.client.get_context_usage()

    async def prepare_goal(self, thread_id: str, objective: str) -> dict[str, Any]:
        """Install the immediate state for a soon-to-be-submitted native /goal.

        The machine submits the actual ``/goal <condition>`` through the normal
        turn path immediately after this call.  Capturing context usage first
        preserves the same baseline that Claude's in-memory activeGoal tracks.
        """
        tokens_at_start = None
        try:
            usage = await self.get_context_usage()
            value = usage.get("totalTokens") if isinstance(usage, dict) else None
            if isinstance(value, int) and not isinstance(value, bool):
                tokens_at_start = max(0, value)
        except Exception as exc:
            # Goal execution must not fail just because the optional baseline
            # control request is unavailable on an older CLI.
            log.warning("goal context baseline unavailable", error=str(exc))
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
            exists, goal = await asyncio.to_thread(read_claude_goal, target)
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
                yield message
            return
        async for data in query.receive_messages():
            message = self._parse_compat_message(data)
            if message is None:
                continue
            yield message
            if isinstance(message, ResultMessage):
                return

    def _start_message_pump(self) -> None:
        """Start the sole consumer of the SDK Query message stream."""
        if self._message_pump_task is not None:
            raise RuntimeError("Claude SDK message pump already started")
        cap = max(1, int(getattr(self.cfg, "turn_reader_queue_cap", 4)))
        self._turn_messages = asyncio.Queue(maxsize=cap)
        self._background_messages = asyncio.Queue(maxsize=cap)
        self._turn_active = False
        self._turn_consumer_active = False
        self._message_pump_error = None
        initial_release = asyncio.Event()
        initial_release.set()
        self._turn_background_release = initial_release
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
                if self._turn_active:
                    await self._turn_messages.put(message)
                    if isinstance(message, ResultMessage):
                        self._turn_active = False
                    continue
                release = self._turn_background_release
                if release is None:
                    release = asyncio.Event()
                    release.set()
                await self._background_messages.put(
                    (message, release, self._turn_origin_id))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._message_pump_error = exc
            if self._turn_active:
                self._turn_active = False
                await self._turn_messages.put(_MessagePumpFailure(exc))
            else:
                log.warning(
                    "Claude SDK message pump stopped",
                    error_type=type(exc).__name__)
        else:
            if self._turn_active:
                self._turn_active = False
                await self._turn_messages.put(_MESSAGE_PUMP_END)

    async def _background_message_worker(self) -> None:
        """Deliver idle notifications in order with bounded backpressure."""
        assert self._background_messages is not None
        while True:
            message, release, turn_id = await self._background_messages.get()
            await release.wait()
            callback = self.background_message_callback
            if callback is None:
                continue
            try:
                await callback(message, turn_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One malformed/background notification must not kill the sole
                # SDK reader and make the next user query hang forever.
                log.warning(
                    "Claude background message callback failed",
                    error_type=type(exc).__name__)

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
        self._turn_background_release = None
        self._turn_origin_id = None

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self._stop_message_pump()
                await self.client.disconnect()
            finally:
                self.client = None

    async def force_reconnect(self, resume_id: str | None, cwd: str | None = None,
                              reason: str = "drain timeout",
                              preserve_model: bool = True) -> None:
        """Tear down and reconnect with resume. Used after a drain timeout, and to
        apply a spawn-time option change (e.g. effort) to a live session."""
        async with self._permission_reconnect_lock:
            log.warning("force-reconnecting SDK client", reason=reason)
            try:
                await self.disconnect()
            except Exception as e:
                log.warning("disconnect during force-reconnect failed", error=str(e))
            model_override = self.model if preserve_model else None
            if not preserve_model:
                # An external terminal may have changed this session's model.
                # Let resume recover it instead of forcing our stale cache back.
                self.model = None
            await self.connect(
                resume_id=resume_id, cwd=cwd,
                model_override=model_override)
