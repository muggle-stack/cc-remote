# Claude sessions across Wrapper deployments

The optional Claude session service owns the pinned Agent SDK and the user's
daily Claude Code CLI processes. The Wrapper remains the controller. Restarting
the Wrapper detaches the controller; it does not interrupt the native SDK or
resubmit an accepted prompt.

The service continues reading native output while the Wrapper is offline. Its
private SQLite journal retains each unacknowledged turn and its real terminal
result. On return, the Wrapper reconstructs the translator and restores pending
tool approvals and MCP questions. Replayed text replaces the existing message
prefix. The original browser message ID remains the turn's owner. Human and
autonomous results are separate; a background result cannot acknowledge a human
turn. Answers are saved in the service before the browser receives acceptance,
including answers to earlier pages of a multi-question tool call.

This is a cc-remote SDK service, not Claude Code's terminal background mode or
the experimental PTY broker. The daily native Claude TUI keeps its existing
external-ownership rules; this service does not give it shared input ownership.

## Install before enabling

Use the tested immutable Wrapper source and venv selected by the normal
[deployment procedure](../deploy/README.md). Run as the actual Wrapper user:

```bash
cd "<immutable-wrapper-release>"
.venv/bin/python deploy/install_claude_service.py \
  --state-dir "<private-wrapper-state>/claude-service"
```

On macOS this creates a separate LaunchAgent. On Linux it creates the separate
systemd user unit `cc-remote-claude-service.service`. That user's systemd manager
must be available and configured to survive logout for unattended operation.
This works alongside either a system or user Wrapper unit. Do not launch the
SDK service as an ordinary child of the Wrapper: `setsid` does not protect
descendants from systemd's control-group shutdown.

The installer does not replace or restart an existing service. An unreadable
existing endpoint requires investigation, not a second daemon or an in-process
fallback. Inspect `service.json`, service-manager state and the socket before
deciding how to recover it.

After service readiness is verified, add this to the Wrapper's existing external
configuration, preserving all other values:

```dotenv
CC_REMOTE_CLAUDE_SERVICE_SOCKET=<private-wrapper-state>/claude-service/service.sock
```

An empty setting retains the previous in-process SDK behavior. The service
can also be registered without editing a root-owned environment file: add
`--register-wrapper "<private-wrapper-state>"` to the installer command. This
writes a private `claude-service.json` inside the Wrapper's `CC_REMOTE_STATE_DIR`
(normally `~/.cc-remote`). The socket environment variable takes precedence;
an explicitly empty value disables the registration. Merely starting the service
without either configuration does not enable it for the Wrapper. Registration
is read at Wrapper startup, so the first-migration drain still applies.

The service
directory must belong to the Wrapper user and be mode 0700; its socket and
journals are private. Both ends check the local protocol and exact SDK version.
Profile config roots are part of session identity, so equal native UUIDs in
different accounts cannot attach to the same worker. Native environment and
SDK options travel through the local socket only, not through relay state,
release artifacts, service descriptors or unit files.

## First migration and subsequent deployments

An existing in-process SDK child cannot be adopted in flight. Before first
activation, wait for those Claude turns to finish. Starting the independent
service early is safe; restarting the old Wrapper early is not.

For ordinary deployments after migration, keep the SDK service running and
restart only the Wrapper through its immutable activation transaction. Restore
service-owned sessions before creating a bootstrap session. Do not classify
their separate SDK process tree as an unrelated terminal owner.

The Wrapper requests readable thinking summaries at SDK child launch with
`--thinking-display summarized`. Claude Code's `showThinkingSummaries` setting
applies to interactive terminals and does not enable this in SDK mode. When
reattaching to an existing service-owned child, the Wrapper also tries a bounded
native display control; it does not restart the child or replay the prompt to
apply this preference. Native thinking mode, token budget and effort remain in
effect. Claude Code 2.1.269 acknowledges a display update during a running turn,
but the active agent loop keeps its original configuration, including across
tool continuations and steering. The updated display applies to the next
top-level query after that turn ends naturally. A successful control response
does not prove that the current turn will return summaries; never interrupt it
to force this preference to apply.

An unsupported or timed-out display update does not fail attachment;
the launch option applies on the next ordinary child start. Only subsequently
returned summaries can be displayed, and provider support still determines
whether the CLI receives readable thinking content.

The current integration covers regular Claude Code and Work sessions. Private
`/btw` forks retain their existing lifetime. They, any other in-process Claude
instance, and Wrapper-owned deferred queries must drain before restarting the
Wrapper. Deployment automation must check these separately: a healthy service
socket alone does not prove that every active operation has migrated.

The SDK service stays on its original immutable source and venv while its
sessions are alive. Keep that release; `service.json` records the source and
process identity. If an upgrade changes the pinned SDK or local service
protocol, stage it first and wait for native turns, pending callbacks and
autonomous work to finish before replacing the service itself. Do not bypass a
version mismatch or restart a busy service to satisfy an acceptance check.

An OS reboot, explicit interrupt, service crash or explicit service stop is
outside the Wrapper-only deployment guarantee. The journal is disk-backed;
acknowledged ranges are reclaimed. A long unacknowledged turn needs storage
proportional to its native transcript. Stopping the sole reader at a byte cap
would prevent the terminal result that releases that turn from arriving.
Journals contain private task data and belong with private runtime state.

## Acceptance

- Keep the same SDK-service and native CLI PIDs across a Wrapper restart.
- Restore the original session and message ID without another query.
- Verify output, offline completion, permission/MCP-question recovery and
  interrupted-turn drain using zero-model tests.
- Confirm replay replaces existing text and settles the actual native result.
- Complete normal account, build, protocol, health and stability checks. This
  does not replace Codex sharing acceptance or authorize a live model prompt.

`tests/test_claude_service.py` includes an actual controller-process termination
test with a stub model and exercises the pinned SDK's real MCP bridge.
