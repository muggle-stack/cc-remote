# Share one Codex daemon on Linux

Use this after the user chooses to attach their Linux desktop App. The topology
for one account is **daily CLI → official app-server ← cc-remote Wrapper**, with
the **desktop App → that same app-server**. Each account retains its own
`CODEX_HOME` and daemon. This applies to Codex **Code**; cc-remote Work keeps its
private control plane.

The [official Linux App](https://learn.chatgpt.com/docs/linux/linux-app) may be
named **ChatGPT**; its mode menu includes **Codex**. The procedure below was
verified with Linux x86_64 App **26.908.40834** and an existing official daemon
**0.154.0**, including login reuse, old conversation rendering, plain CLI resume,
and live Unix socket connections. No model messages are needed for these checks.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
supports WebSockets over its Unix control socket. The App's
`CODEX_APP_SERVER_WS_URL` override is internal and version-dependent. Recheck
the installed App after an update; this is a verified integration recipe, not
an official guarantee for every Linux App/CLI version. The macOS
`cc_remote.codex_desktop` helper does not implement this Linux path.

## Choose the account and verify CLI/Wrapper first

1. Operate as the logged-in desktop user. Resolve the existing graphical session
   and the official App's executable from its package or `.desktop` entry;
   do not guess a display number or run the GUI as root. An SSH connection
   alone does not establish that a desktop session is available. Install the
   official App only when installation is also requested, following the
   current official distribution/architecture instructions. A user-local
   extracted package is not a system package install and does not gain package
   manager auto-updates.
2. Resolve the chosen account's real `CODEX_HOME`, its daily CLI/alias, and the
   Wrapper's account registry entry and `CODEX_BIN`. The CLI's `--profile` is
   a named configuration profile, not selection of an account home. A default
   `codex` command may use a different account from an existing account-specific
   launcher; preserve that distinction and the user's aliases. Never read or
   copy `auth.json` to make the App appear signed in.
3. Follow the complete [CLI/Wrapper acceptance](../deploy/README.md#codex-code-shared-control-plane-acceptance).
   Keep `CC_REMOTE_CODEX_DAEMON=auto`; match the chosen registry entry's `home`
   to the daily CLI home. Wrapper startup prepares that account's official
   daemon. Do not start a second backend or add another daemon service for App.
   Compare these read-only probes with the actual resolved executable paths:

   ```sh
   CODEX_HOME="/absolute/account-home" "/absolute/daily-codex" app-server daemon version
   CODEX_HOME="/absolute/account-home" "/absolute/wrapper-codex" app-server daemon version
   ```

   Require `status=running`, a compatible server version and the same
   `socketPath` and server process identity. Check the current Wrapper startup's
   `Codex profile shared daemon ready` record for this profile with
   `remote_control=true`; a private stdio fallback is not shared readiness.
4. Check ordinary account-specific `codex resume <idle-session-id>` without
   `--remote`, using an idle test conversation or existing CLI connection.
   Do not submit a prompt. Explicit `--remote unix://` is useful for diagnosis,
   but success with it alone does not prove ordinary resume auto-discovers the
   daemon. Check the live route before altering any daily launcher.

## Create a separate App launcher

Use a distinct entry for the selected account; keep the original App entry.
Record the App version/path, daemon PID/start identity, Wrapper PID and existing
launcher before changing anything. Preserve an already working shared entry.
An App currently open privately or on another account must be quit normally
when the user is ready. Never kill it, the CLI or the daemon to run a test.

The verified Linux build accepts this **direct local** connection:

```text
ws+unix://localhost/absolute/account-home/app-server-control/app-server-control.sock:/rpc
```

The `localhost` hostname matters in this build: an empty-host URI such as
`ws+unix:///…` was incorrectly routed through an App SOCKS proxy and failed
before initialization. The actual connection still uses the filesystem Unix
socket, not TCP. Do not expose the daemon on the LAN or change system proxy
settings to work around this local transport issue.

Save the following template to a new private executable, for example
`~/.local/bin/codex-app-shared-account`. Replace the three paths with the selected
account, the installed official launch executable, and a persistent private
state directory for this entry. Do not overwrite an existing launcher. The
official package can provide a `codex-launcher` alongside `ChatGPT`; resolve
its actual installation path rather than assuming `/usr/lib`.

```sh
#!/bin/sh
set -eu
umask 077
ccr_account='/absolute/account-home'
ccr_app='/absolute/official-app/codex-launcher'
ccr_state='/absolute/private-state/codex-app-shared-account'
ccr_socket="$ccr_account/app-server-control/app-server-control.sock"

if [ ! -S "$ccr_socket" ] || [ ! -x "$ccr_app" ]; then
    printf '%s\n' 'Shared daemon or official App unavailable; check the selected account.' >&2
    exit 1
fi
if [ "$(stat -Lc '%u' "$ccr_socket")" != "$(id -u)" ] ||
   [ "$(stat -Lc '%a' "$ccr_socket")" != 600 ]; then
    printf '%s\n' 'Expected a private daemon socket owned by this desktop user.' >&2
    exit 1
fi

export CODEX_HOME="$ccr_account"
export CODEX_APP_SERVER_FORCE_CLI=0
export CODEX_APP_SERVER_WS_URL="ws+unix://localhost$ccr_socket:/rpc"
export XDG_CONFIG_HOME="$ccr_state/config"
export XDG_CACHE_HOME="$ccr_state/cache"
export XDG_DATA_HOME="$ccr_state/data"
mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$ccr_state/user-data"
exec "$ccr_app" --user-data-dir="$ccr_state/user-data" "$@" >>"$ccr_state/app.log" 2>&1
```

Validate with `sh -n <launcher-path>` and make only that new file executable
(`chmod 700 <launcher-path>`). Keep `HOME` and the existing desktop session
environment intact. Launch variables belong to this child process; do not
export them globally in shell startup files or systemd user environments.
Keep the account's established authentication method; do not inject credentials
from another profile or import another App's account as an attachment step.

For a desktop-menu entry, create a new file under
`~/.local/share/applications/`, replacing both paths and the account label:

```ini
[Desktop Entry]
Version=1.0
Type=Application
Name=Codex Shared · Account
Exec=/absolute/path/to/codex-app-shared-account
Icon=/absolute/official-app/resources/icon-chatgpt.png
Terminal=false
Categories=Development;
```

Use desktop-entry quoting for paths with spaces; `.desktop` fields do not expand
`~` or `$HOME`. Run `desktop-file-validate <entry-path>` if available. This entry
does not replace the original one or change the default CLI account. Always
reopen through the shared entry; an ordinary App launch may select its private
backend instead. Do not put temporary debugging flags in the daily launcher.

## Verify the actual three-client connection

Open the shared entry in the desktop session, select **Codex** in the App mode
menu, and open an existing idle conversation. The App should reuse the chosen
account's login. If it asks to log in, stop and check account selection and the
transport instead of copying credentials or silently selecting another account.
App first-start setup may update native preferences and reconcile its bundled
plugins; do not claim the profile config is byte-for-byte unchanged or restore
a whole old config over those changes.

Check each client separately:

| Client | Required evidence |
|---|---|
| CLI | The ordinary resume process is connected to the selected daemon's Unix endpoint; opening old rollout text alone is insufficient. |
| Wrapper | The selected profile is ready in the current startup, and a resident Code session uses the official `app-server proxy --sock …` connection to that daemon. An idle Wrapper may have no resident proxy; report readiness separately from an observed session connection. |
| App | Startup initialization succeeds, reports the expected server version, and the actual App process has a live Unix socket connection to that same daemon. A login screen or matching home path is insufficient. |

On Linux, match client and server endpoint inode pairs from `ss -xnpH`, together
with PID/start time and executable identity. Filter locally to the selected
processes/socket before returning output; a desktop can have a very large
socket table. On a restricted host use available service-owned structured
connection logs. Do not bypass `/proc` restrictions to read Wrapper credentials
or dump full process environments. A socket pathname without a live endpoint
pair is not evidence of a connection.

Inspect only the bounded account/path/version/status fields needed for the
check. Native `account/read` with `refreshToken=false` and `thread/list` can
confirm login and thread identity without reading auth files or creating a model
turn. A read-only WebSocket probe over the Unix control socket should disable
WebSocket compression (`compression=None`) for the verified daemon build.

Confirm Wrapper and daemon PID/start identities remain unchanged and healthy.
Exit only the temporary idle CLI you opened, using its normal quit action.
Leave pre-existing App/CLI sessions intact. Report which checks passed and
whether new model messages were tested; a three-direction live prompt test
requires authorization and is not needed for a transport check.

Cloud project/feature requests use a separate network path. Report their errors
separately from successful local daemon initialization and history rendering;
one does not establish the other. If the App ignores the override, creates a
private server, or loses its connection, report attachment as unverified. Do
not silently fall back to a private App backend while declaring sharing ready.

## App tools, upgrades and removal

App attachment and optional App-control tools are separate choices. Inspect the
installed App's native `codex-app-tools` plugin/catalog before adding a custom
MCP entry. The current [custom adapter](codex-app-tools.md) is macOS-only; do not
run its macOS discovery/signature code on Linux or replace native approvals.
Discovery of a catalog does not prove every UI tool or Computer Use is available.

After an App upgrade, resolve any changed launch path, recheck the transport and
normally reopen through the shared entry when the user is ready. A user-local
package extraction needs its own deliberate update; attaching it does not add
a package manager repository. Do not restart a healthy daemon merely to align
an App's bundled CLI version; check the existing server's compatibility first.

To remove this integration, quit this App normally and remove only its separate
launcher and desktop entry. Keep the official App, account, daemon, Wrapper and
CLI. Preserve its private App state unless the user also requests its removal.
