"""Claude Agent SDK transport with a child-only account environment boundary."""
from __future__ import annotations

from dataclasses import replace
import asyncio
import json
from collections.abc import Awaitable, Callable

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport import subprocess_cli as _sdk_cli

from cc_remote.wrapper.child_env import claude_sdk_process_env


UserDialogCallback = Callable[[str, dict], Awaitable[dict]]
FALLBACK_DIALOG_KIND = "refusal_fallback_prompt"


class UserDialogCLITransport(_sdk_cli.SubprocessCLITransport):
    """Bridge the native dialog control channel missing from pinned SDK 0.2.174.

    Dialogs use the existing stdout reader and serialized stdin writer. Their
    waiters must run separately so a pending human choice never blocks native
    cancellation, ordinary SDK controls, or another session's stream.
    """

    def __init__(self, *args, on_user_dialog: UserDialogCallback | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_user_dialog = on_user_dialog
        self.dialog_tasks: dict[str, asyncio.Task] = {}
        self.dialog_responses: dict[str, dict | None] = {}

    def _remember_dialog_response(self, request_id: str, response: dict | None) -> None:
        self.dialog_responses[request_id] = response
        if len(self.dialog_responses) > 64:
            self.dialog_responses.pop(next(iter(self.dialog_responses)))

    async def write(self, data: str) -> None:
        if self.on_user_dialog is not None and '"initialize"' in data:
            message = json.loads(data)
            request = message.get("request", {})
            if message.get("type") == "control_request" and request.get("subtype") == "initialize":
                kinds = request.setdefault("supportedDialogKinds", [])
                if FALLBACK_DIALOG_KIND not in kinds:
                    kinds.append(FALLBACK_DIALOG_KIND)
                data = json.dumps(message) + "\n"
        await super().write(data)

    async def _answer_dialog(self, request_id: str, payload: dict) -> None:
        try:
            result = await self.on_user_dialog(request_id, payload)
            if (not isinstance(result, dict) or result.get("behavior") != "completed"
                    or result.get("result") not in {"retry_fallback", "edit_prompt", "cancelled"}):
                result = {"behavior": "cancelled"}
        except asyncio.CancelledError:
            raise
        except Exception:
            result = {"behavior": "cancelled"}
        try:
            response = {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request_id,
                             "response": result},
            }
            await self.write(json.dumps(response) + "\n")
            self._remember_dialog_response(request_id, response)
        except Exception:
            # A closing native child owns the terminal outcome. Never retry a
            # dialog response into a replacement child or replay a model query.
            pass
        finally:
            self.dialog_tasks.pop(request_id, None)

    async def _read_messages_impl(self):
        try:
            async for message in super()._read_messages_impl():
                request_id = message.get("request_id")
                request = message.get("request", {})
                if (self.on_user_dialog is not None and message.get("type") == "control_request"
                        and request.get("subtype") == "request_user_dialog"
                        and request.get("dialog_kind") == FALLBACK_DIALOG_KIND):
                    if request_id in self.dialog_responses:
                        response = self.dialog_responses[request_id]
                        if response is not None:
                            await self.write(json.dumps(response) + "\n")
                        continue
                    if request_id not in self.dialog_tasks:
                        self.dialog_tasks[request_id] = asyncio.create_task(
                            self._answer_dialog(request_id, request.get("payload", {})))
                    continue
                if message.get("type") == "control_cancel_request":
                    task = self.dialog_tasks.pop(request_id, None)
                    if task is not None or request_id in self.dialog_responses:
                        self._remember_dialog_response(request_id, None)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        continue
                yield message
        finally:
            await self._cancel_dialogs()

    async def _cancel_dialogs(self) -> None:
        tasks = list(self.dialog_tasks.values())
        self.dialog_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        await self._cancel_dialogs()
        await super().close()


class AccountIsolatedSubprocessCLITransport(
    UserDialogCLITransport,
):
    """Spawn one Claude child without inherited account selectors.

    Agent SDK 0.2.174 treats ``ClaudeAgentOptions.env`` as an overlay on the
    Wrapper process environment. Empty strings are not equivalent to unsetting
    modern Claude authentication selectors, while mutating ``os.environ`` would
    race unrelated Codex and tool spawns. This pinned adapter mirrors the SDK's
    subprocess setup and changes only how that one child's environment starts.
    """

    async def connect(self) -> None:
        """Start the pinned SDK subprocess with an exact isolated environment."""
        if self._process:
            return

        if self._cli_path is None:
            self._cli_path = await _sdk_cli.anyio.to_thread.run_sync(
                self._find_cli,
            )

        self._reject_windows_batch_cli(self._cli_path)

        if not _sdk_cli.os.environ.get("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"):
            await self._check_claude_version()

        cmd = self._build_command()
        try:
            # Keep the SDK's precedence while starting from an account-clean
            # copy. Explicit request-local values (notably CLAUDE_CONFIG_DIR)
            # may intentionally repopulate a selector for this profile.
            process_env = claude_sdk_process_env({
                "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                **self._options.env,
                "CLAUDE_AGENT_SDK_VERSION": _sdk_cli.__version__,
            })

            # Preserve the pinned SDK's best-effort OpenTelemetry propagation.
            try:
                from opentelemetry import propagate

                carrier: dict[str, str] = {}
                propagate.inject(carrier)
                if "traceparent" in carrier:
                    for key in ("TRACEPARENT", "TRACESTATE"):
                        if key not in self._options.env:
                            process_env.pop(key, None)
                    for key, value in carrier.items():
                        env_key = key.upper()
                        if env_key not in self._options.env:
                            process_env[env_key] = value
            except Exception:  # pragma: no cover - optional telemetry package
                _sdk_cli.logger.debug(
                    "OTEL trace context injection failed",
                    exc_info=True,
                )

            # Mirror the SDK's run-boundary handshake so a completed child
            # task cannot close stdin before its parent continuation arrives.
            state_key = _sdk_cli._SDK_READS_SESSION_STATE_ENV
            if not any(key.upper() == state_key for key in process_env):
                process_env[state_key] = "1"

            if self._options.enable_file_checkpointing:
                process_env[
                    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"
                ] = "true"

            if self._cwd:
                process_env["PWD"] = self._cwd

            stderr_dest = (
                _sdk_cli.PIPE if self._options.stderr is not None else None
            )
            self._process = await _sdk_cli.anyio.open_process(
                cmd,
                stdin=_sdk_cli.PIPE,
                stdout=_sdk_cli.PIPE,
                stderr=stderr_dest,
                cwd=self._cwd,
                env=process_env,
                user=self._options.user,
            )
            _sdk_cli._ACTIVE_CHILDREN.add(self._process)

            if self._process.stdout:
                self._stdout_stream = _sdk_cli.TextReceiveStream(
                    self._process.stdout,
                )
            if self._process.stdin:
                self._stdin_stream = _sdk_cli.TextSendStream(
                    self._process.stdin,
                )
            if stderr_dest is not None and self._process.stderr:
                self._stderr_stream = _sdk_cli.TextReceiveStream(
                    self._process.stderr,
                )
                self._stderr_task = _sdk_cli.spawn_detached(
                    self._handle_stderr(),
                )

            self._ready = True
        except FileNotFoundError as exc:
            if self._cwd and not _sdk_cli.Path(self._cwd).exists():
                error = _sdk_cli.CLIConnectionError(
                    f"Working directory does not exist: {self._cwd}",
                )
                self._exit_error = error
                raise error from exc
            error = _sdk_cli.CLINotFoundError(
                f"Claude Code not found at: {self._cli_path}",
            )
            self._exit_error = error
            raise error from exc
        except Exception as exc:
            error = _sdk_cli.CLIConnectionError(
                f"Failed to start Claude Code: {exc}",
            )
            self._exit_error = error
            raise error from exc


def account_isolated_transport(
    options: ClaudeAgentOptions,
    *, on_user_dialog: UserDialogCallback | None = None,
) -> AccountIsolatedSubprocessCLITransport:
    """Construct the pinned custom transport with SDK permission wiring."""
    if options.session_store is not None:
        raise ValueError(
            "account-isolated Claude profiles do not support session_store",
        )
    # ClaudeSDKClient validates the combination and configures its Query from
    # the original options. The preconstructed transport needs the same stdio
    # flag on its command, but must not invoke the SDK warning path a second time.
    configured = (
        replace(options, permission_prompt_tool_name="stdio")
        if options.can_use_tool else options
    )
    return AccountIsolatedSubprocessCLITransport(
        prompt="",
        options=configured,
        on_user_dialog=on_user_dialog,
    )


def user_dialog_transport(
    options: ClaudeAgentOptions, on_user_dialog: UserDialogCallback, *, isolated: bool = False,
) -> UserDialogCLITransport:
    if isolated:
        return account_isolated_transport(options, on_user_dialog=on_user_dialog)
    configured = (replace(options, permission_prompt_tool_name="stdio")
                  if options.can_use_tool else options)
    return UserDialogCLITransport(prompt="", options=configured, on_user_dialog=on_user_dialog)
