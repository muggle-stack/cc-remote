# Desktop App tools on a shared Codex host (experimental, macOS)

This optional adapter connects the **installed official** `codex_app` MCP to an
existing shared app-server. It forwards the official tool catalog, arguments,
native thread/turn metadata, results and cancellations. It does not implement
each App tool, patch the App, or replace its native peer/signature checks.

It is independent of the Wrapper/relay/Web protocol and is off unless explicitly
configured for a Codex account. It does not start or restart an App or daemon.

## Prerequisite: the App already uses this shared host

The Desktop App must have been launched using the shared-daemon connection entry,
with its process-local `CODEX_HOME` and loopback `CODEX_APP_SERVER_WS_URL` pointing
at that host. Ordinary App launches that start a private backend are deliberately
not eligible. Installing this MCP does **not** turn an ordinary App launch into
a shared-daemon launch. Keep the existing shared-launch mechanism when reopening
the App; do not use global environment overrides or stop an active CLI.
The optional [Codex Shared launcher](codex-desktop-launcher.md) provides a
separate Finder/Dock entry for this daily quit/reopen workflow.

Discovery requires exactly one same-user official App process with the selected
profile and an active loopback connection. It reads the App's own open startup
log and cross-checks the reported tools pipe against that process's open sockets.
Sockets must be private and owned by the current user. Other profiles, ambiguous
processes, missing native tools, and failed signature checks stay unavailable.
The App's tools pipe is an internal, version-dependent interface, not a promised
public transport API. Windows and Linux are not implemented by this adapter.

## Generate the account-scoped entry

From this checkout with its Python environment available, substitute the actual
account directory and official App bundle path (no account credentials are needed):

```sh
.venv/bin/python -m cc_remote.codex_app_tools discover \
  --profile /absolute/codex-home --app /absolute/Desktop.app
.venv/bin/python -m cc_remote.codex_app_tools config --format toml \
  --profile /absolute/codex-home --app /absolute/Desktop.app
```

Inspect the generated `[mcp_servers.codex_app]` entry before adding it to only
that account's `config.toml`. Preserve any existing `codex_app` configuration;
never blindly replace it. The entry uses absolute paths to this checkout and
its Python executable: keep both in place, or regenerate the entry after moving
them. Never write an operator's actual paths or account configuration to Git.

The entry retains the installed official `desktop-mcp.json` tool approval policy
and makes the MCP **optional**. The test-only read allowlist is not installed.
Tools absent behind App feature flags are not fabricated. The official MCP
manifest fingerprint is pinned in the generated entry. If an App update changes
that manifest, the adapter exposes no App tools until the new policy is reviewed
and the entry regenerated. It does not apply old approval rules to a changed
official definition.

Use the official `config/mcpServer/reload` API on **that profile's** daemon to queue
MCP refreshes for loaded threads, or let a new thread read the new configuration.
Do not resume, interrupt, or change the model/permissions of existing threads just
to install tools. A running turn may not use its refreshed catalog until the next
turn boundary. `mcpServerStatus/list` and a read-only `mcpServer/tool/call` can
verify the result without generating a model turn.

## Lifecycle and safety

- App running: discover and expose its current official catalog as one MCP.
- App absent: initialize normally, expose an empty catalog, and return an
  actionable error for stale calls. Other MCPs/CLI/cc-remote remain independent.
- App reopened through the shared entry: detect its new pipe, recreate only the
  adapter's own connection worker and MCP child, and publish
  `notifications/tools/list_changed`.
- App updated in place: runtime identity also participates in the connection
  generation. Both the official MCP process and its direct parent are launched
  afresh using the verified installed official Node. The long-lived stdio
  adapter can survive a bundle replacement without leaving an old mapped Node
  image in the native peer's signature-checked parent position. No App, daemon,
  Wrapper or other MCP is restarted, and no signature requirement is relaxed.
- Connection readiness includes a read-only native catalog handshake, not just
  local MCP initialization. Failed native authorization stays unavailable.
- App connection lost during a call: fail that call and **never automatically
  replay it**, even if a replacement App is immediately available. A write may
  already have executed; inspect the actual result before explicitly retrying.
- Cancellation: forward the MCP cancellation to the official implementation.
- New unsupported MCP client requests: reject rather than auto-approve.
- No authenticated network listener, credential store, transcript polling or
  model request is added. Logs/diagnostics do not contain tool result bodies.

Catalog refresh timing also depends on the daemon/client's native tool refresh
behavior. Mac App UI tools still operate the Mac App: this does not reproduce
its sidebars or dialogs inside the cc-remote Web UI. Tool execution can wait on
the App renderer; discovery success is not a guarantee that every UI action is
fast or available in every App state.

## Verification and rollback

```sh
.venv/bin/python -m pytest tests/test_codex_app_tools.py
node --test tests/codex_app_tools.test.mjs
```

The regression tests include absent/reopened App generations, exact profile
selection, ambiguous identity/PID reuse, private socket checks, metadata
preservation, cancellations, lost writes, runtime replacement/signature races,
worker cleanup, and the exit-during-startup race.
Real read-only tests use an ephemeral thread and never start a model turn.
Write/UI actions require explicit test targets; do not test them on real user
conversations or projects merely to increase a test count.

To disable, set `enabled = false` in this account's `mcp_servers.codex_app`
entry and request the native MCP reload. Remove only the entry added for this
adapter when uninstalling; do not restore an entire old config over newer edits.
No App/daemon/Wrapper restart is needed to remove this MCP connection.

References: [official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
and [official app-server protocol](https://learn.chatgpt.com/docs/app-server).
