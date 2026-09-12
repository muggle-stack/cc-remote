# Configuration, accounts and data

[中文](configuration.md) · [README](../README_en.md) · [Installation](installation_en.md)

- [Native clients](#native-clients)
- [Multiple accounts](#multiple-accounts)
- [Environment variables](#environment-variables)
- [Authentication](#auth-model)
- [Reliability](#reliability-boundary)
- [Security](#security-please-read)

## Native clients

### Claude

Wrapper uses daily Claude Code, normally `~/.local/bin/claude`, with a minimum
version of `2.1.258`. Empty `CLAUDE_BIN` still selects this path; an explicit
override must be absolute. Agent SDK is pinned to `0.2.151`; its bundled CLI does
not replace your daily installation.

Native CLI, Desktop and Agent View sessions are mirrored read-only until explicit
takeover. Wrapper sends SIGTERM only to a verified same-user Claude process,
waits for it to release the session, then resumes that session. It does not kill
the terminal shell or use SIGKILL. The official `claude` command is not replaced
by an alias, shim or PATH interception.

### Codex

Code prefers the official shared app-server daemon. Check in your daily CLI environment:

```bash
codex app-server daemon --help
codex app-server proxy --help
```

These are prerequisites, not complete acceptance. Follow the
[shared-control checks](../deploy/README.md#codex-code-shared-control-plane-acceptance)
to verify that each account's CLI and Wrapper connect to the same daemon.
The single-account compatibility path may fall back to private stdio when the
daemon is unavailable; `CC_REMOTE_CODEX_DAEMON=off` forces that private path.
It does not provide shared control. Explicit multi-account configurations that
require a shared daemon refuse an unverifiable connection instead of silently
changing accounts. Codex Work always uses private processes and directories.

Desktop App attachment is a separate choice; see the [macOS](codex-desktop-launcher.md)
or [Linux](codex-desktop-linux.md) guide.

### DSH

DSH runs independently as a loopback Web service. See
[DSH setup](../integrations/dsh/README.md) for the bridge, pairing file and
`CC_REMOTE_DSH_CONNECTION_FILE`. Configure model accounts, tools, plugins and
Presets in DSH. Wrapper neither starts/upgrades DSH nor sends its local control
Cookie to Relay. Stopping Wrapper detaches subscriptions; cancelling a task is
an explicit operation.

## Multiple accounts

Claude and Codex each support up to 32 Profiles. Each registry needs exactly one
`default: true`, with unique absolute directories already authenticated/configured
through the native CLI. Single-account mode retains native session IDs and UI.
Multiple accounts share a labeled sidebar while keeping control state isolated.

### Configuration sources

| Engine | JSON file variable | Inline JSON variable | Default file read by the macOS installer |
|---|---|---|---|
| Claude | `CC_REMOTE_CLAUDE_PROFILES_FILE` | `CC_REMOTE_CLAUDE_PROFILES_JSON` | `~/.cc-remote/claude-profiles.json` |
| Codex | `CC_REMOTE_CODEX_PROFILES_FILE` | `CC_REMOTE_CODEX_PROFILES_JSON` | `~/.cc-remote/codex-profiles.json` |

Inline JSON takes precedence. No configuration, or a missing file, preserves
single-account behavior. Save the relevant example as a private JSON file,
replace its paths, and set the file variable in Linux's external Wrapper config.

Claude example:

```json
{
  "personal": {"label": "Personal", "config_dir": "/home/youruser/.claude", "default": true},
  "company": {"label": "Company", "config_dir": "/home/youruser/.claude-company"}
}
```

Codex example:

```json
{
  "personal": {"label": "Personal", "home": "/home/youruser/.codex", "default": true},
  "company": {"label": "Company", "home": "/home/youruser/.codex-company"}
}
```

Restart Wrapper through the release procedure after changes. Each Profile keeps
its native login, configuration, sessions, models and extensions. Explicit Claude
profiles load only their own user settings and clear inherited account/provider
variables; provider settings belong in that Profile, not project/local settings.
Claude still discovers project instruction files natively. Each Codex `CODEX_HOME`
uses an independent daemon; Remote and the terminal should use the same directory.

Multi-account routing uses `<profile>@<native-session-id>` internally; Copy session
ID still copies the native UUID. Code, Work and Work schedules can select an
account. Work freezes its ownership, so retries or default changes do not switch
accounts. Removing a Profile makes its existing Work fail clearly; restore the
Profile or create new work under another account.

When first enabling explicit profiles, include the currently effective native
account directory. Later Profile-id changes migrate local state by resolved
directory; never rebind an existing id to another directory. After an interrupted
migration, keep the same target configuration and restart. The affected engine
refuses access until migration completes. Native login credentials are not migrated.

### Codex account-switch hook

When using `codex-auth` to switch accounts within a native home, configure
[`scripts/codex-auth-daemon-restart`](../scripts/codex-auth-daemon-restart) as its
post-switch hook. It records a daemon generation and hands the official restart
to a separate worker so Remote can resume the task on the same thread. Queued
messages wait for that task's native terminal boundary. It stores no credentials
and does not replay the original prompt.

Every explicit Profile, including the default, should supply its stable id:

```bash
scripts/codex-auth-daemon-restart \
  --profile-id company --codex-home /home/youruser/.codex-company
```

The hook and Wrapper must share `CC_REMOTE_STATE_DIR`. Logs default to
`~/.cc-remote/codex-daemon-restart.log`. Legacy single-account hook ownership does
not change when the default is reordered; new hooks should supply the Profile id.

## Environment variables

Common settings below; [config.py](../cc_remote/config.py) and the deployment environment templates define the full contract. Real environment variables take precedence over a local development `.env`.

**Relay**

| Var | Default | Notes |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | Listen address (keep `127.0.0.1` for Caddy-only public access; use `0.0.0.0` together with `ALLOW_PRIVATE_ORIGINS=1` for simultaneous LAN/Tailscale IPv4 access, and restrict it with a firewall). |
| `LOGIN_PASSWORD` | empty | Single-user web login password. **Required** unless `LOGIN_USERS_JSON` is set. |
| `LOGIN_USERS_JSON` | empty | Optional multi-user policy: `{"alice":{"password":"…","machines":["laptop","server"]}}`; replaces `LOGIN_PASSWORD`. |
| `SESSION_SECRET` | empty | HMAC secret to sign session tokens. **Required** (`openssl rand -hex 32`). |
| `SESSION_TTL_SECONDS` | `604800` | Session token lifetime (default 7 days). |
| `LOGIN_BODY_MAX_BYTES` / `LOGIN_READ_TIMEOUT` / `LOGIN_INFLIGHT_CAP` | `4096` / `10` / `32` | Hard limits for login body bytes, total read seconds, and concurrent body reads. |
| `SESSION_REGISTRY_CAP` | `1024` | Hard limit for process-local revocable browser sessions. |
| `PUSH_VAPID_PUBLIC_KEY` / `PUSH_VAPID_PRIVATE_KEY` / `PUSH_VAPID_SUBJECT` | empty | Optional real Web Push; all three must be configured. Prefer an absolute PEM path readable by the relay user. Existing users and the default mode send only completion/failure state. Only explicit session mode adds a safely truncated name and an exact device-local route; prompts, answers, paths, and tool output are never included. |
| `PUSH_DB_PATH` | `~/.cc-remote/relay-push.sqlite3` | Durable browser subscription store, isolated by user and machine. |
| `DEVICE_DB_PATH` | `~/.cc-remote/relay-devices.sqlite3` | Durable device names, last-seen metadata, and credential hashes; never sessions or artifacts. |
| `DEVICE_PAIRING_TTL_SECONDS` | `600` | Lifetime of a single-use pairing code in seconds; allowed range 60–3600. |
| `PUBLIC_ORIGIN` | empty | Exact browser origin allowed to connect, e.g. `https://remote.example.com`; **required**, and non-loopback origins must use HTTPS unless `ALLOW_INSECURE_HTTP` is enabled. |
| `ALLOW_PRIVATE_ORIGINS` | `0` | Set to `1` to retain `PUBLIC_ORIGIN` while also accepting literal private/loopback IP origins on `RELAY_PORT`: `127/8`, `10/8`, `172.16/12`, `192.168/16`, Tailscale `100.64/10`, IPv6 loopback, and ULA. The Origin scheme/host/port must also exactly match the effective request target; hostnames, public IPs, and other ports remain rejected. Private HTTP is unencrypted and normally cannot install a PWA. |
| `ALLOW_INSECURE_HTTP` | `0` | Escape hatch for a bare public IPv4 address: allows plain `http://`/`ws://` outside loopback. Off by default; login credentials, cookies, wrapper tokens, and all session traffic are unencrypted while enabled. Prefer TLS whenever possible. |
| `WRAPPER_TOKEN` | placeholder | Wrapper Bearer token for single-machine/compatibility mode; required unless `WRAPPER_TOKENS_JSON` is set. |
| `WRAPPER_TOKENS_JSON` | empty | Optional machine-bound tokens: `{"laptop":"…","server":"…"}`; replaces the relay's wildcard `WRAPPER_TOKEN`. |
| `WEB_STATIC_DIR` | empty | Point at `web/dist` to serve the web client same-origin; empty = API/WS only. |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | Hard per-client pending-frame/byte limits; a slow client is disconnected instead of silently losing frames. |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | Hard limits for accepted clients and seconds allowed for the first Hello frame. |
| `WS_MAX_SIZE_BYTES` | `16777216` | Maximum single WebSocket frame accepted by both relay and wrapper transports. |

**Wrapper**

| Var | Default | Notes |
|---|---|---|
| `CC_REMOTE_DSH_CONNECTION_FILE` | empty | Private local DSH pairing file created by `dsh_pair`; see [DSH setup](../integrations/dsh/README.md). Empty disables DSH. |
| `CC_REMOTE_VIEWER_HOME_PREVIEW` | `1` | Discover only verified, explicitly referenced home-directory pages or owned static listeners. Set `0` to opt out; see [Viewer](remote-viewer.md). |
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | Relay WebSocket URL (`wss://domain/ws` in prod, unless `ALLOW_INSECURE_HTTP` is set). |
| `ALLOW_INSECURE_HTTP` | `0` | Same escape hatch as the relay; the wrapper reads it too so `RELAY_URL` can stay `ws://` against a non-loopback host. |
| `WRAPPER_TOKEN` | `change-me-wrapper` | Same as relay. |
| `CC_REMOTE_MACHINE_ID` | `default` | Stable route id on a multi-machine relay; must match its `WRAPPER_TOKENS_JSON` key when that policy is enabled. |
| `CC_REMOTE_DEVICE_CONFIG` | `~/.cc-remote/device.json` | Interactive pairing credential; the file must be private to the current user. Explicit `RELAY_URL` / `WRAPPER_TOKEN` / `CC_REMOTE_MACHINE_ID` values take precedence. |
| `CLAUDE_BIN` | `~/.local/bin/claude` | Daily Claude Code executable launched by the wrapper. Empty still selects this default; use another absolute path only when the CLI is installed elsewhere. |
| `CC_REMOTE_CLAUDE_PROFILES_JSON` | empty | Optional Claude multi-account registry in the form `{profile_id:{"label":"…","config_dir":"/absolute/CLAUDE_CONFIG_DIR","default":true}}`. At most 32 unique directories are allowed and exactly one entry must be the default. Code, Work, and schedules may select any entry. Empty preserves the current single-account behavior; inline JSON takes precedence over the file. |
| `CC_REMOTE_CLAUDE_PROFILES_FILE` | empty (macOS LaunchAgent: `~/.cc-remote/claude-profiles.json`) | Optional bounded regular JSON file. A missing file means single-account mode, allowing installation before configuration. |
| `CC_REMOTE_CODEX_PROXY` | empty | Optional HTTP(S)/SOCKS5 proxy injected only into Codex subprocesses launched by the wrapper. It does not change the wrapper-to-relay connection or the user's terminal `codex`. |
| `CC_REMOTE_CODEX_DAEMON` | `auto` | Code prefers Codex's official shared daemon; `off` forces private stdio app-server and loses live bidirectional coordination with native Codex CLI/App. Work is always private and ignores this setting. |
| `CC_REMOTE_CODEX_PROFILES_JSON` | empty | Optional multi-account registry in the form `{profile_id:{"label":"…","home":"/absolute/CODEX_HOME","default":true}}`. At most 32 unique homes are allowed and exactly one entry must be the default. Each entry owns an independent daemon; Code combines and labels their sessions, while new Codex Work sessions and schedules may select any entry. Empty preserves single-account compatibility. Inline JSON takes precedence over the file. |
| `CC_REMOTE_CODEX_PROFILES_FILE` | empty (macOS LaunchAgent: `~/.cc-remote/codex-profiles.json`) | Optional bounded regular JSON file. A missing file means single-account mode, allowing installation before configuration. |
| `CC_REMOTE_STATE_DIR` | `~/.cc-remote` | Local wrapper state directory. The account-switch hook and wrapper must use the same value; the daemon generation barrier stored here contains no Codex credentials. |
| `CC_CWD` | cwd | Default working directory for new sessions. Claude `--resume` needs it to locate `~/.claude/projects/` — **it must be correct**; Codex resume first recovers the original cwd from its rollout. |
| `CC_RESUME_SESSION_ID` | empty | Resume a specific session UUID; empty starts fresh. The id is persisted to `~/.cc-remote/` after first start. |
| `CLAUDE_WORK_ROOT` | `~/.claude/cc-remote/work` | Private Claude Work root for the registry, knowledge sources, sessions, and generated policy files. |
| `CODEX_WORK_ROOT` | `~/.codex/cc-remote/work` | Private Codex Work root for the registry, knowledge sources, sessions, and generated policy files. |
| `MAX_CONCURRENT_SESSIONS` | `20` | Maximum Wrapper-resident sessions (memory varies by engine/version). Over the cap, an idle process is evicted; client history remains available. |
| `DRAIN_TIMEOUT` | `15` | Seconds to wait for the terminal ResultMessage after interrupt before forcing a reconnect (drain safety net). |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | see [`.env.example`](../.env.example) | Live-tail buffer / tool-output truncation tuning. |
| `HISTORY_SOURCE_MAX_BYTES` | `67108864` | Safe source limit for one Claude transcript; larger SDK transcripts return an explicit error instead of exhausting memory. Codex rollouts are not subject to this whole-file cap. |
| `CODEX_HISTORY_WINDOW_MAX_BYTES` | `33554432` | Maximum Codex rollout source window parsed per page. Long histories stream backwards by turn; an oversized single turn keeps its recent tail plus a stable cursor for loading older history. |
| `WRAPPER_INBOX_CAP` / `WRAPPER_SEND_QUEUE_CAP` | `1024` / `8192` | Hard item-count bounds for the wrapper's inbound and outbound queues. |
| `WRAPPER_INBOX_BYTES` / `WRAPPER_SEND_QUEUE_BYTES` | `33554432` / `33554432` | Hard serialized-byte bounds for the wrapper's inbound and outbound queues. |
| `TURN_READER_QUEUE_CAP` | `4` | Per-turn engine-event consumer queue. Codex app-server stdout has a separate byte-bounded burst buffer so slow relay I/O cannot block RPCs or terminal events. |

Each message accepts at most 8 attachments, at most 6 MiB each and 8 MiB decoded in total; oversized input is rejected before a model turn starts.

## Auth model

- **Web client**: `POST /api/login` creates a short-lived HMAC session in an **HttpOnly, SameSite=Strict** cookie. JavaScript cannot read it and no token appears in the URL. With `LOGIN_USERS_JSON`, the signed session also carries its allowed machines; both discovery and WebSocket routing enforce that set. The WebSocket must also pass an exact `Origin` check.
- **Wrapper ⇄ relay**: the WS handshake carries a machine credential. Manual setups use `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON`; Device Center issues an independent, machine-bound, individually revocable credential. The relay stores only its hash, and no credential may announce another device's `machine_id`.
- Tokens travel only in cookies/headers, never in URLs or wire-protocol message bodies; logging redacts token/password fields.

## Reliability boundary

- The Web client attaches a stable `cmd_id` to retryable commands and resend them after a socket reconnect or wrapper recovery. The wrapper deduplicates them and ACKs completion within the same wrapper process lifetime. Each live session also pairs its cursor with a wrapper generation so a restart cannot make an old sequence number look current.
- Once the wrapper accepts a queued or interrupt-replacement message, its bounded in-memory queue owns that work. It starts after the active turn's real terminal boundary even if every Web/PWA client sleeps, disconnects, or hard-refreshes, and reconnecting clients recover a payload-bounded queue summary. Opening a summary privately fetches the full instruction, whose text can be atomically edited before execution without dropping attachments; full payloads never enter the replay ring. This queue is not persisted across a wrapper process crash or restart.
- Unacknowledged-command queues and the general command-deduplication table are **bounded in-memory state**. A hard browser refresh, client exit, or wrapper crash does not promise cross-process exactly-once delivery. cc-remote is an interactive control plane, not a durable job queue; after such a failure, inspect the transcript/rollout and live session state before resending.
- Persisted Claude transcripts, Codex rollouts and DSH native records are the history sources of truth. DSH history requires the authenticated bridge; a cold read must not activate an Agent. The wrapper SQLite summary index and browser IndexedDB are rebuildable projections; the live ring only provides bounded reconnect catch-up. Heavy tool/reasoning detail loads per turn instead of blocking first paint.
- Work schedules are the exception: schedules, run records, leases, heartbeats, retry counts, and next-run timestamps live in SQLite. An expired lease is recovered after a wrapper restart, but an uncertain outcome is never reported as success.

## Security (please read)

> **cc-remote lets a remote person run arbitrary commands on your machine. Treat it like handing someone a shell.**

- Code sessions remain a remote development control plane: Claude defaults to `permissionMode: bypassPermissions`; Codex defaults to approval policy `never` and may select any named permission profile app-server allows for the current cwd. Approval policy cannot widen a profile boundary, while Full Access materially expands capability. **Treat anyone who can log in and enter Code as holding remote agent/shell authority on the wrapper machine.** Work uses the fixed `cc_remote_work` profile and a separate private root without external directories, but this only narrows the default capability surface; it is not a substitute for OS-user, container, or VM isolation.
- `LOGIN_PASSWORD` / `LOGIN_USERS_JSON`, `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON`, and `SESSION_SECRET` form the authentication boundary: use strong random values, never commit or paste them into chats, and rotate them. A repository `.env` is for local development only; Linux production wrappers use root-only `/etc/cc-remote/wrapper.env`; macOS uses installer-managed private configuration outside releases. The systemd template prevents the service and model descendants from reading that source file or a legacy repository `.env`; on Linux the wrapper also disables dumpability so children cannot recover captured credentials through `/proc/<pid>/environ` or process memory.
- Always use TLS (`wss://`) in production. Only set `ALLOW_INSECURE_HTTP=1` for a temporary bare-public-IPv4 deployment; login credentials, cookies, wrapper tokens, and all session traffic are unencrypted while it is enabled, so switch back to TLS as soon as possible. `ALLOW_PRIVATE_ORIGINS=1` adds only same-port literal private-IP entry points that match the effective request target and does not relax the public-domain check. Cookie `Secure` follows the trusted request transport, not the caller-provided Origin, but login credentials, cookies, and session traffic are still plaintext when a private HTTP entry point is used.
- Recommended: restrict the relay by IP / only run it when needed; login is rate-limited (5/min per IP) out of the box.

## Model configuration

Configure and authenticate each engine natively before connecting Remote.
Single-account Claude uses its effective `CLAUDE_CONFIG_DIR` (normally `~/.claude`),
Codex its `CODEX_HOME` (normally `~/.codex`), and DSH its own configuration.
Subscription or provider credentials remain there. Profiles select the native
configuration boundary; cc-remote does not distribute model credentials or act as
a model API gateway.

For proxying the Wrapper-to-Relay connection, set `HTTPS_PROXY` / `ALL_PROXY` in
external Wrapper configuration. `CC_REMOTE_CODEX_PROXY` instead affects only
Wrapper-launched Codex children. These control different connections.
