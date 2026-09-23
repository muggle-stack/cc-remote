"""Claude Agent SDK transport with a child-only account environment boundary."""
from __future__ import annotations

from dataclasses import replace

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport import subprocess_cli as _sdk_cli

from cc_remote.wrapper.child_env import claude_sdk_process_env


class AccountIsolatedSubprocessCLITransport(
    _sdk_cli.SubprocessCLITransport,
):
    """Spawn one Claude child without inherited account selectors.

    Agent SDK 0.2.157 treats ``ClaudeAgentOptions.env`` as an overlay on the
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
    )
