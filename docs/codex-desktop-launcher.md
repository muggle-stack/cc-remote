# Shared Desktop launcher (experimental, macOS)

For Linux, use [App, CLI and Wrapper sharing on Linux](codex-desktop-linux.md).
The Python helper and Finder/Dock instructions on this page are macOS-specific.

This is a separate, clickable **Codex Shared** application for an already-running
official shared app-server. It leaves the signed official Desktop App, the
Wrapper, the CLI, the daemon and their authentication untouched. It is optional
and independent of the relay/Web wire protocol.

The launcher sets `CODEX_HOME`, `CODEX_APP_SERVER_WS_URL` and
`CODEX_APP_SERVER_FORCE_CLI=0` only for the App it launches. A same-user,
App-process-checked loopback WebSocket bridge forwards bytes unchanged to that
profile's private Unix control socket. There is no global environment override,
model API proxy, App patch, automatic takeover or private stdio fallback.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
documents WebSockets over the Unix control socket. WebSocket transport is
experimental. The Desktop `CODEX_APP_SERVER_WS_URL` launch override is an
internal, version-dependent App behavior, **not a documented compatibility
guarantee**; it has been locally checked with Desktop 26.901.41600 and Codex
0.153.4, and rechecked after a Desktop update to 26.903.61454. Revalidate it after
updating the official App; these observations do not guarantee other builds.

## Post-deploy choice and preflight

Follow the [deployment opt-in checkpoint](../deploy/README.md#optional-codex-app-attachment)
before changing anything. CLI/Wrapper sharing is a core Codex Code acceptance
check; this Desktop attachment is optional. A decline, unanswered offer or
unsupported App leaves the desktop unchanged and does not block core deployment.

After the user chooses attachment:

1. Confirm this is an in-scope macOS Wrapper desktop and operate as its logged-in
   user, not root. Complete the selected account's
   [CLI/Wrapper shared-daemon checks](../deploy/README.md#codex-code-shared-control-plane-acceptance)
   first. An App launcher cannot repair a Wrapper's private stdio fallback.
2. Resolve the installed official App using bundle metadata
   (`CFBundleIdentifier=com.openai.codex`), not its filename. Resolve the user's
   chosen account directory explicitly; the helper's `--profile` means that
   `CODEX_HOME`, not the official CLI's named `--profile` config option. Do not
   select another account merely because its daemon is available.
3. Check the requirements below and use helpers from a trusted, tested checkout
   that will remain available. Record App/CLI versions, daemon identity, Wrapper
   identity and any existing shared launcher before installation. Inspect only
   the necessary account/path fields, not credentials or full environments.
4. Preserve a working launcher for the same account. If its runtime moved or
   the user selects a different account, agree on a separate target or a
   recoverable replacement; installation intentionally refuses to overwrite.
5. A running private/wrong-account App must be fully quit by the user when they
   are ready. Never kill it, a CLI or the daemon. Installation may finish while
   reopening is pending; report that state instead of claiming App sharing.

## Install a daily entry

Requirements: macOS, the installed signed official App, this checkout's Python
environment, Xcode Command Line Tools (`swiftc`), and an already-running official
daemon for the selected account. Account-specific paths belong in local installed
configuration, never in this repository.

```sh
.venv/bin/python -m cc_remote.codex_desktop install \
  --profile /absolute/codex-home --app /absolute/Desktop.app \
  --target "$HOME/Applications/Codex Shared.app"
```

Installation refuses to overwrite an existing application. It builds a small
locally ad-hoc-signed Finder/Dock launcher with a distinct bundle identifier;
the official App signature is neither changed nor bypassed. The generated
`Contents/Resources/launcher.json` selects the account and the runtime. Keep
the checkout and Python environment in place; do not point them at a temporary
staging directory or a release about to be pruned. Rebuild the entry if they move.
Installation neither opens the official App nor changes the default account.

If the user also wants a Dock shortcut:

```sh
.venv/bin/python -m cc_remote.codex_desktop pin-dock \
  --target "$HOME/Applications/Codex Shared.app"
```

Dock pinning appends one entry, is idempotent, and does not remove the original
App or change other Dock preferences. It records the previous pinned-app list
in the launcher's private state directory. macOS may need a Dock refresh before
the new icon appears; this does not require restarting Codex or its daemon.

## Daily behavior

- **App fully quit:** open **Codex Shared** to start it on the shared daemon.
  The App can use the same sessions as the daily CLI and cc-remote.
- **Shared App already open:** clicking again focuses that existing App. It does
  not launch a second backend or replace a working bridge, including one started
  with an earlier trial launcher.
- **Only the window closed:** the App process and shared connection stay alive;
  the entry reopens/focuses that App.
- **App fully quits:** this launcher's bridge closes and releases its launch lock.
  Reopening through this entry creates a fresh connection to the same daemon.
- **App open privately, on another account, or not connected:** show an
  explanatory error. The user must fully quit that App first; the launcher never
  kills it or changes its existing session ownership.
- **Daemon missing or unreadable:** stop with instructions to start the account's
  Wrapper. No independent daemon is silently started.
- **Rapid duplicate clicks:** one bundle-scoped lock serializes launches even
  across account-specific entries. The lock is kernel-owned and requires no
  stale-PID killing or lock-file deletion.

**The original App icon remains an ordinary launch.** After quitting, use the new
shared entry, not the original icon. The running official App may have its own
Dock tile beside the fixed launcher; that is not a second backend.

The optional [Desktop tools adapter](codex-app-tools.md) reconnects to the new
App tools pipe independently. This launcher does not change tool permissions or
the adapter's pinned official MCP manifest policy. Installing that adapter needs
a separate user choice: it lets prompts from CLI/cc-remote operate the desktop
App, not the Web sidebar. Sharing alone does not install or authorize those tools.

## Verification and limitations

The regression checks use no model calls and do not replace live acceptance:

```sh
.venv/bin/python -m pytest tests/test_codex_desktop.py tests/test_codex_app_tools.py
```

Once the user is ready to open the shared App, use the installed entry or:

```sh
.venv/bin/python -m cc_remote.codex_desktop launch \
  --profile /absolute/codex-home --app /absolute/Desktop.app
```

The launch command returns `reused` for an existing shared App and `connected`
after a new App's connection is admitted. Verify the live App/profile/bridge
connection reaches the **same daemon endpoint and identity** already checked for
CLI and Wrapper. Matching `CODEX_HOME`, a visible icon or an existing socket file
alone is insufficient. Confirm the CLI/Wrapper remain connected and were not
restarted by the optional step.

Use an operator-approved idle session or existing read-only connection evidence
to check that all three clients refer to the same native thread. Do not resume a
busy production session, force takeover or start a model turn for this check.
A live three-direction message test needs separate authorization because it
spends model tokens and modifies the conversation. State whether only transport
or an actual message round trip was verified.

If App tools were separately enabled, follow their guide's native catalog/reload
checks. An in-progress turn may keep its previous catalog until the next turn
boundary; do not restart the daemon to force refresh. A UI tool returning
`queued` is not proof that its panel has opened.

Tests cover real socket byte forwarding,
browser/foreign-process rejection, exact profile/PID identity, repeat clicks,
simulated quit/reopen, lock release, non-overwriting installation and idempotent
Dock changes. They make no model calls. Never quit a real active App merely to
test this launcher; perform full desktop quit/reopen acceptance when the user is
ready. An App auto-update that relaunches itself without preserving the launch
environment may require quitting and reopening through **Codex Shared**.
If a check fails, leave the original App and existing CLI/Wrapper service intact,
report the exact failed or pending check, and do not fall back to a private
App backend while reporting successful sharing.

The listener binds only `127.0.0.1` on an ephemeral port, rejects any browser
Origin, validates Host/path/upgrade framing, and checks the peer's OS process
identity, owner, exact executable and account-specific launch environment.
Only its own bounded startup preflight can connect without being the App.
Connections are bounded and streaming/backpressured; RPC bodies, headers,
environment contents and user messages are not logged.

## Remove the entry

Remove only **Codex Shared** from the Dock and move its separate application to
Trash. Do not delete the official App or restore a whole old Dock snapshot over
new user changes. If currently running through this launcher, let the App quit
normally before removing its runtime. The official daemon, Wrapper, CLI and MCP
configuration are not removed by uninstalling this entry.
