# cc-remote

<p align="center"><strong>Bring Claude Code / Codex on your machine to your phone and any browser.</strong></p>
<p align="center">Self-hosted · Dual-engine · Multi-session · Live process · Responsive Web</p>
<p align="center">
  <a href="README.md">中文</a> ·
  <a href="#quick-start-local-one-machine-5-min">5-minute quick start</a> ·
  <a href="#production-deploy-public-vps-relay--wrapper-on-your-machine">Production deploy</a> ·
  <a href="#security-please-read">Security</a>
</p>

cc-remote is an open-source remote control plane. A local `wrapper` drives the
already installed and authenticated `claude` / `codex` CLI, while browsers view
and control its sessions through your self-hosted WebSocket relay. Models,
authentication, and tool execution remain under the local CLI; cc-remote does
not proxy model APIs or bake API keys into the web client.

<p align="center">
  <img src="assets/readme-claude-multisession.jpg" alt="cc-remote Claude sessions and multi-session workspace" width="960">
</p>

---

## Table of contents

- [Core capabilities](#core-capabilities)
- [Architecture](#architecture)
- [Real interface and practical features](#real-interface-and-practical-features)
- [Quick start (local, one machine, 5 min)](#quick-start-local-one-machine-5-min)
- [Production deploy (public VPS relay + wrapper on your machine)](#production-deploy-public-vps-relay--wrapper-on-your-machine)
- [Environment variables](#environment-variables)
- [Auth model](#auth-model)
- [Reliability boundary](#reliability-boundary)
- [Security (please read)](#security-please-read)
- [Model backend (optional)](#model-backend-optional)
- [Development](#development)
- [FAQ](#faq)
- [License](#license)

---

## Core capabilities

| Scenario | What you can do |
|---|---|
| **Two engines** | Use Claude Code and Codex in the same web UI. Every session keeps its own model, reasoning effort, permissions, and runtime state. |
| **Remote operation** | Watch streaming replies from a phone, tablet, or desktop browser; send attachments, queue the next message, and interrupt the current turn at any time. |
| **Complete process** | Expand the reasoning summaries, plans, command output, file diffs, MCP calls, collaboration agents, Hooks, and terminal interaction events exposed by each engine. |
| **File preview and Markdown editing** | Open local file links from a reply directly in a source viewer at the referenced line. Changed-file chips and `/preview path` also open Markdown with live rendered/source modes, bounded local images, and conflict-safe atomic saves via Ctrl/Cmd+S or the Save button. |
| **Human approval** | Return Claude `can_use_tool` decisions and Codex command, file-change, user-input, general-permission, and MCP elicitation responses. Mirror a terminal-owned session read-only or take it over explicitly. |
| **Session management** | Search, switch, rename, archive, and fork from individual messages. Codex sessions can also fork into an isolated Git worktree. |
| **Runtime controls** | Change the model, reasoning effort, service tier, permissions, and Plan mode. Use `/goal` for long-running goals and Codex `/status` for app-server status, usage, and rate limits. |
| **Continuity** | Let background sessions keep running and synchronize them across clients. Restore paged history from Claude transcripts or Codex rollouts and resume from a cursor after reconnecting. |
| **Self-hosted** | The wrapper only makes outbound connections. The VPS needs no model SDK, web auth uses an HttpOnly cookie, and CLI credentials or API keys never enter the frontend. |

> Available models, service tiers, and runtime controls depend on the local CLI and the capabilities exposed by its SDK or app-server.

## Architecture

Two **independent** links:

```
MODEL LINK (cc-remote never touches):  claude / codex ──(their local config)──▶ model service

CONTROL LINK (this repo):              browser ⇄ relay(WebSocket) ⇄ wrapper ⇄ SDK / app-server ⇄ local CLI
```

| Component | Runs where | What it does |
|---|---|---|
| **wrapper** | the machine where `claude` / `codex` runs | Holds a session pool, translates SDK/app-server events to the wire protocol, handles interrupt/drain, and reads transcript/rollout history on demand. **Outbound-only to the relay — no inbound ports needed.** |
| **relay** | public VPS (or local) | Pure WebSocket forwarder (FastAPI). The wrapper uses a Bearer token and browsers use an HttpOnly session cookie; single wrapper slot, multi-client fan-out. **Never imports `claude-agent-sdk`, never touches the model API.** |
| **web** | the browser | React client; the relay serves its static files (`web/dist`) from the same origin. |

## Real interface and practical features

The screenshots below come from a running cc-remote installation, not design mockups.

### Multi-session management: keep work running in the background

The session pool groups conversations by working directory and lets you search,
switch, rename, and archive them. You can move to another session while one is
still working in the background, then return to its complete live progress.
Claude Code and Codex share the same workspace while retaining independent
context, models, permissions, and runtime state.

<p align="center">
  <img src="assets/readme-multi-session.jpg" alt="Multi-session workspace grouped by project with search and switching" width="960">
</p>

### Claude Code: see reasoning, tool calls, and Hooks

A Claude session is more than a simplified chat showing only the final text.
cc-remote receives the reasoning, command calls, tool results, and Hook lifecycle
events exposed by the Claude Code SDK and presents them as a collapsible timeline.
The composer also shows the session's current model, reasoning effort, permission
mode, and context usage.

<p align="center">
  <img src="assets/readme-claude-session.jpg" alt="Claude Code reasoning, command calls, and Hook events" width="960">
</p>

### New sessions: choose the engine and working directory first

Create either a Claude Code or Codex session from one entry point, browse for its
working directory, and attach images or files to the first message. Once the
session exists, adjust its model, permissions, or Plan mode only when needed
instead of filling in a row of defaults up front.

<p align="center">
  <img src="assets/readme-new-session.jpg" alt="Create a new session by choosing its engine and working directory" width="960">
</p>

### Codex: preserve plans and the complete process

Codex sessions organize the reasoning summaries, plans, commands, diffs, MCP
calls, collaboration agents, and Hooks reported by app-server into a collapsible
timeline. Expand it while a turn is running to follow the details, then collapse
the completed work into a concise summary; the final response always remains
separate.

<p align="center">
  <img src="assets/readme-process-timeline.jpg" alt="Collapsible Codex plan, Hook, and tool-call timeline" width="960">
</p>

### Per-session Codex controls: model, reasoning, permissions, and status

The model, reasoning effort, service tier, and permissions belong to the current
session, so you can change the next turn without editing the machine's global
configuration. The composer also provides attachments, queue/interrupt controls,
context usage, and command entry points such as `/goal` and `/status`.

<p align="center">
  <img src="assets/readme-model-controls.jpg" alt="Codex model selection and per-session controls" width="960">
</p>

### Common operations at a glance

- **Sessions:** create, search, run in the background, rename, archive, fork, and create a Codex worktree.
- **Turns:** stream, queue, interrupt, copy, edit and resend, or fork from a specific message.
- **Tools:** inspect command output, file changes and diffs, MCP, collaboration agents, Hooks, approvals, and user-input requests.
- **Terminal coordination:** detect a native CLI owner and mirror new messages in real time while preserving the remote user's explicit takeover control.
- **Status:** inspect the model, reasoning effort, permissions, Plan mode, context, goals, usage, rate limits, and runtime warnings.
- **Devices:** use a responsive mobile UI, light or dark themes, multi-browser synchronization, and reconnect recovery.

## Quick start (local, one machine, 5 min)

First get the relay + wrapper + web running on the **machine where the agent CLI runs** to validate the whole chain. Production deploy is the next section.

### Prerequisites

- A machine with **Claude Code CLI** (`claude`, v2.1.51+) or a **Codex CLI** (`codex`) that supports `app-server`, already installed and **able to chat**. Install both to switch engines in the web UI.
- **Python 3.10+**, **Node 20.19+** (to build the web client).

### 1) Install deps + build the web client

```bash
git clone https://github.com/muggle-stack/cc-remote.git && cd cc-remote

python3 -m venv .venv && source .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements.lock

npm --prefix web ci
npm --prefix web run build          # produces web/dist/
```

### 2) Configure

```bash
install -m 600 .env.example .env
```

Edit `.env` — at minimum:

```ini
# web login password (pick a strong one)
LOGIN_PASSWORD=<a strong password>
# HMAC secret used to sign session tokens
SESSION_SECRET=<openssl rand -hex 32>
# shared token between wrapper and relay
WRAPPER_TOKEN=<openssl rand -hex 32>
# exact browser origin; plain HTTP is allowed only on loopback
PUBLIC_ORIGIN=http://127.0.0.1:8765
# let the relay serve the web client from the same origin
WEB_STATIC_DIR=web/dist
# default working directory of the agent session (the project you want it to work on)
CC_CWD=/path/to/your/project
# optional explicit Claude CLI path when systemd/PATH cannot find it
CLAUDE_BIN=/absolute/path/to/claude
```

> For a local loopback quick start, the relay and wrapper may share this `.env`;
> it is not a production secret store. A public deployment must use the
> root-only `/etc/cc-remote/wrapper.env` below so bypass-permissions model/tools
> cannot read control-plane credentials directly.

### 3) Run (two terminals)

```bash
# Terminal 1: relay (serves web + /ws + /api on http://127.0.0.1:8765)
python -m cc_remote.relay

# Terminal 2: wrapper (drives the local claude / codex CLI)
python -m cc_remote.wrapper
```

### 4) Open the web client

Browse to **http://127.0.0.1:8765** → log in with `LOGIN_PASSWORD` → send a message. You should see streaming replies, interrupt, and multi-session switching.

> To hack on the UI use dev mode: `npm --prefix web run dev` (Vite). For running/testing, the `build` + relay-served approach above is simpler (same origin).

## Production deploy (public VPS relay + wrapper on your machine)

Move the relay to the public internet; the wrapper dials it **outbound** over `wss://`, and phones hit the same domain. The model link is untouched.

```
your machine wrapper ──wss:443──▶ Caddy(VPS, auto HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── phone browser
                                                                └─ serves web/dist (same origin)
```

### Prerequisites

- **VPS**: Ubuntu 22.04+ / Debian 12+ (or another Debian-family host with Python 3.10+) with **ports 80 + 443** open (80 for Let's Encrypt, 443 for wss).
- **Domain**: an A record pointing at the VPS IP (Caddy auto-provisions + renews the TLS cert).
- **Your machine**: Linux (systemd runs the wrapper below), outbound 443 allowed.

If you do not have a domain yet, a temporary public-IPv4 + plain-HTTP/WS
escape hatch is also supported: open port 80 on the VPS and allow outbound 80
from the wrapper machine. Caddy still proxies to the loopback-only relay, so
request limits and service hardening remain in place, but there is **no
transport encryption**: the login password, cookie, wrapper token, and all
session content can be read or modified on the network path.

### 1) Generate tokens / password

```bash
openssl rand -hex 32   # WRAPPER_TOKEN (must match on relay + wrapper)
openssl rand -hex 32   # SESSION_SECRET (relay)
# also pick a LOGIN_PASSWORD (web login password)
```

### 2) Build the web client on your dev machine

```bash
npm --prefix web ci
npm --prefix web run build   # produces web/dist/
```

> The web client no longer bakes any token into the JS: login POSTs the password to the relay for a short-lived session token. So the build needs no `VITE_*` variables.

> **Upgrading to protocol v10:** the wire gate rejects mixed versions. Deploy
> `cc_remote/` and the new `web/dist/` in one maintenance window, then restart the
> relay and wrapper; do not run a rolling mixture. Existing sockets reconnect
> briefly, and a relay restart intentionally requires browsers to log in again.
> Any already-open older page also needs one **hard refresh** to load the new hashed
> assets; logging in again inside the old JavaScript bundle isn't sufficient.
> For a manual release, stop the local wrapper first, stop and update relay + web,
> then start the v10 relay and v10 wrapper so the old wrapper cannot occupy the
> relay's single wrapper slot.

### 3) Publish from staging during a maintenance stop

```bash
# dev machine: the normal account writes its own staging directory, not root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:~/cc-remote-upload/

# VPS: publish protocol-v10 Python + web/dist together while relay is stopped
ssh <vps-user>@<vps>
sudo systemctl stop cc-remote-relay 2>/dev/null || true
sudo mkdir -p /opt/cc-remote
sudo rsync -a --delete --exclude='.env' --exclude='.venv' \
  ~/cc-remote-upload/ /opt/cc-remote/
```

Make sure the VPS has `/opt/cc-remote/cc_remote/`, `web/dist/cc-remote-build.json`, `requirements.lock`, and `deploy/`. Use the same staging + stop procedure for upgrades; never rsync over the live tree.

### 4) VPS: fill `.env` + run setup

```bash
# on the VPS (/opt/cc-remote becomes root-owned after setup)
sudo test -f /opt/cc-remote/.env || sudo install -m 600 \
  /opt/cc-remote/deploy/env.relay.example /opt/cc-remote/.env
sudoedit /opt/cc-remote/.env     # set LOGIN_PASSWORD / SESSION_SECRET / WRAPPER_TOKEN

# install deps + Caddy + systemd (pass your domain)
sudo bash /opt/cc-remote/deploy/setup-vps.sh your-domain.com
```

For a public-IPv4-only deployment, use this matching configuration and target:

```ini
# /opt/cc-remote/.env
PUBLIC_ORIGIN=http://your-public-ip
ALLOW_INSECURE_HTTP=1
```

```bash
sudo bash /opt/cc-remote/deploy/setup-vps.sh your-public-ip
```

The installer selects the plain-HTTP Caddy template only when the opt-in is
enabled, the argument is a public IPv4 address, and `PUBLIC_ORIGIN` matches it
exactly. Private, loopback, reserved, and malformed addresses fail closed.

The script installs `python3-venv` + Caddy, creates a `ccremote` system user, builds a venv + `pip install`, merges both a marked cc-remote site and global HTTP timeout/header limits into the Caddyfile while preserving other global options and sites, then starts `cc-remote-relay` + `caddy`. If the new relay restart or readiness check fails, the venv, Caddyfile, and systemd unit are restored as one transaction and the previous relay's `/healthz` is verified.

Verify:

```bash
curl https://your-domain.com/healthz
# expect: {"ok":true,"wrapper_connected":false,"clients":0}
```

In insecure mode, use `curl http://your-public-ip/healthz` instead.

### 5) Your machine: root-only wrapper environment + systemd

```bash
cd /path/to/cc-remote
python3 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# Root owns the secret source; model/tools run as your ordinary account and
# cannot read this file directly.
sudo install -d -o root -g root -m 0755 /etc/cc-remote
sudo install -o root -g root -m 0600 deploy/env.wrapper.example \
  /etc/cc-remote/wrapper.env
sudoedit /etc/cc-remote/wrapper.env  # set RELAY_URL / WRAPPER_TOKEN / CC_CWD

# Edit User and repository/venv/home paths; do not point it back at a repo .env.
sudo cp deploy/cc-remote-wrapper.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cc-remote-wrapper
journalctl -u cc-remote-wrapper -f     # expect: connected to relay / wrapper running
```

In insecure mode, also set both wrapper-side values:

```ini
RELAY_URL=ws://your-public-ip/ws
ALLOW_INSECURE_HTTP=1
```

Back on the VPS, the matching mode's `/healthz` should now show `wrapper_connected:true`.

### 6) Verify from a phone

Open the matching `https://your-domain.com/` or `http://your-public-ip/` on
your phone (any network) → log in with `LOGIN_PASSWORD` → send a message. You
should get streaming replies, interrupt, and multi-device sync.

### Behind a corporate HTTP proxy?

The wrapper dials out via `websockets`, which honors `HTTPS_PROXY` / `ALL_PROXY`.
Add it to `/etc/cc-remote/wrapper.env`:

```ini
HTTPS_PROXY=http://your-proxy:port      # for SOCKS use ALL_PROXY=socks5://...
```

(If the proxy does TLS MITM, add its root CA to the system trust store.)

## Environment variables

**Relay**

| Var | Default | Notes |
|---|---|---|
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | Listen address (behind Caddy in prod — keep 127.0.0.1). |
| `LOGIN_PASSWORD` | empty | Web login password. **Required** or you can't log in. |
| `SESSION_SECRET` | empty | HMAC secret to sign session tokens. **Required** (`openssl rand -hex 32`). |
| `SESSION_TTL_SECONDS` | `604800` | Session token lifetime (default 7 days). |
| `LOGIN_BODY_MAX_BYTES` / `LOGIN_READ_TIMEOUT` / `LOGIN_INFLIGHT_CAP` | `4096` / `10` / `32` | Hard limits for login body bytes, total read seconds, and concurrent body reads. |
| `SESSION_REGISTRY_CAP` | `1024` | Hard limit for process-local revocable browser sessions. |
| `PUBLIC_ORIGIN` | empty | Exact browser origin allowed to connect, e.g. `https://remote.example.com`; **required**, and non-loopback origins must use HTTPS (unless `ALLOW_INSECURE_HTTP` is set). |
| `ALLOW_INSECURE_HTTP` | `0` | Escape hatch: set to `1` to let a non-loopback `PUBLIC_ORIGIN`/`RELAY_URL` stay plain `http://`/`ws://` instead of requiring HTTPS/WSS — e.g. reaching the relay over a bare public IP with no TLS terminator in front. Off by default; enabling it sends the login password, session cookie, and all traffic in cleartext, so anyone on the network path can read or hijack the session. Prefer TLS (Caddy/nginx, see the deploy section) whenever possible. |
| `WRAPPER_TOKEN` | placeholder | Bearer token the wrapper presents; must match on both sides. Startup rejects placeholders and short values. |
| `WEB_STATIC_DIR` | empty | Point at `web/dist` to serve the web client same-origin; empty = API/WS only. |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | Hard per-client pending-frame/byte limits; a slow client is disconnected instead of silently losing frames. |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | Hard limits for accepted clients and seconds allowed for the first Hello frame. |
| `WS_MAX_SIZE_BYTES` | `16777216` | Maximum single WebSocket frame accepted by both relay and wrapper transports. |

**Wrapper**

| Var | Default | Notes |
|---|---|---|
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | Relay WebSocket URL (`wss://domain/ws` in prod, unless `ALLOW_INSECURE_HTTP` is set). |
| `ALLOW_INSECURE_HTTP` | `0` | Same escape hatch as the relay; the wrapper reads it too so `RELAY_URL` can stay `ws://` against a non-loopback host. |
| `WRAPPER_TOKEN` | `change-me-wrapper` | Same as relay. |
| `CLAUDE_BIN` | empty | Optional absolute Claude CLI path; set it when systemd/PATH cannot find `claude`. |
| `CC_CWD` | cwd | Default working directory for new sessions. Claude `--resume` needs it to locate `~/.claude/projects/` — **it must be correct**; Codex resume first recovers the original cwd from its rollout. |
| `CC_RESUME_SESSION_ID` | empty | Resume a specific session UUID; empty starts fresh. The id is persisted to `~/.cc-remote/` after first start. |
| `MAX_CONCURRENT_SESSIONS` | `20` | Maximum resident agent subprocesses (memory varies by engine/version). Over the cap, an idle process is evicted; client history remains available. |
| `DRAIN_TIMEOUT` | `15` | Seconds to wait for the terminal ResultMessage after interrupt before forcing a reconnect (drain safety net). |
| `CODEX_TURN_IDLE_WARN_SECONDS` | `90` | Show a non-terminal waiting notice after this many seconds without a Codex app-server event; `0` disables it. It does not auto-interrupt long reasoning or tools. |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | see `.env.example` | Live-tail buffer / tool-output truncation tuning. |
| `HISTORY_SOURCE_MAX_BYTES` | `67108864` | Maximum transcript/rollout source file read; larger histories return an explicit error instead of exhausting memory. |
| `WRAPPER_INBOX_CAP` / `WRAPPER_SEND_QUEUE_CAP` | `1024` / `8192` | Hard item-count bounds for the wrapper's inbound and outbound queues. |
| `WRAPPER_INBOX_BYTES` / `WRAPPER_SEND_QUEUE_BYTES` | `33554432` / `33554432` | Hard serialized-byte bounds for the wrapper's inbound and outbound queues. |
| `TURN_READER_QUEUE_CAP` | `4` | Per-turn SDK/app-server reader queue; a full queue backpressures the model stream. |

Each message accepts at most 8 attachments, at most 6 MiB each and 8 MiB decoded in total; oversized input is rejected before a model turn starts.

## Auth model

- **Web client**: `POST /api/login` (with `LOGIN_PASSWORD`) creates a short-lived HMAC session in an **HttpOnly, SameSite=Strict** cookie. JavaScript cannot read it, no token appears in the URL, and the WebSocket must pass an exact `Origin` check.
- **Wrapper ⇄ relay**: `Authorization: Bearer <WRAPPER_TOKEN>` at the WS handshake.
- Tokens travel only in cookies/headers, never in URLs or wire-protocol message bodies; logging redacts token/password fields.

## Reliability boundary

- The Web and TUI attach a stable `cmd_id` to retryable commands and resend them after a socket reconnect or wrapper recovery. The wrapper deduplicates them and ACKs completion within the same wrapper process lifetime. Each live session also pairs its cursor with a wrapper generation so a restart cannot make an old sequence number look current.
- Unacknowledged-command queues and the general command-deduplication table are **bounded in-memory state**. A hard browser refresh, TUI exit, or wrapper crash does not promise cross-process exactly-once delivery. cc-remote is an interactive control plane, not a durable job queue; after such a failure, inspect the transcript/rollout and live session state before resending.
- Persisted Claude transcripts and Codex rollouts are the history source of truth. The live ring only provides bounded reconnect catch-up; it does not replace those files.

## Security (please read)

> **cc-remote lets a remote person run arbitrary commands on your machine. Treat it like handing someone a shell.**

- Claude sessions default to `permissionMode: bypassPermissions`. Codex defaults to approval policy `never` while inheriting the machine's Codex sandbox configuration; it can also use `on-request` / `untrusted`, with approval requests bridged to the web client. Regardless of the policy currently displayed, authenticated clients can create/switch sessions and change available controls. **Treat anyone authenticated to the relay as holding remote agent/shell authority on the wrapper machine.**
- `LOGIN_PASSWORD` / `WRAPPER_TOKEN` / `SESSION_SECRET` are the only gate: use strong random values, never commit or paste them into chats, and rotate them. A repository `.env` is for local development only; production wrappers must use the root-only `/etc/cc-remote/wrapper.env` above. The systemd template prevents the service and model descendants from reading that source file or a legacy repository `.env`; on Linux the wrapper also disables dumpability so children cannot recover the captured token through `/proc/<pid>/environ` or process memory.
- Always use TLS (`wss://`) in production (this repo uses Caddy for automatic certs). Do not expose plain `ws://` publicly. Only set `ALLOW_INSECURE_HTTP=1` when you genuinely need to run temporarily over a bare public IP with plain HTTP/WS (e.g. before a domain and TLS are set up); with it on, the login password, session cookie, and all traffic are unencrypted and can be read or hijacked by anyone on the network path — switch back to TLS as soon as you can.
- Recommended: restrict the relay by IP / only run it when needed; login is rate-limited (5/min per IP) out of the box.

## Model backend (optional)

cc-remote **does not touch the model API** — it drives already-configured local CLIs. Claude uses `~/.claude/settings.json`; Codex uses its own login and `~/.codex/config.toml`. So:

- **Official Anthropic API**: install `claude`, make sure it can chat, done.
- **Compatible endpoint (e.g. GLM / z.AI)**: set `ANTHROPIC_BASE_URL` in `settings.json` as usual (pointing at an official-compatible endpoint or your own proxy); cc-remote still only does the control link.
- **Codex**: first make sure local `codex` can chat and `codex app-server` starts. cc-remote neither reads its API key nor rewrites global authentication.

## Development

```
cc_remote/
  protocol.py      # pydantic wire protocol (client/relay/wrapper all depend on it)
  config.py        # env-driven config
  relay/           # FastAPI relay: server / auth / pairing / forward
  wrapper/         # Claude SDK + Codex app-server / pool / stream / ringbuffer / transport
web/               # React client (Vite + TS)
tests/             # zero-token unit tests + e2e scripts
deploy/            # Caddyfile / systemd / setup-vps.sh / env examples
```

```bash
python -m pip install -r requirements-dev.txt
pytest                              # unit tests (no model, zero tokens)
npm --prefix web run test:reliability # pure web reliability tests
npm --prefix web run lint           # web static checks
npm --prefix web run dev            # web dev server
npm --prefix web run build          # web production build
```

Architecture notes & contribution contracts are in [CLAUDE.md](CLAUDE.md).

## FAQ

- **Does restarting the wrapper lose history?** Persisted history does not disappear; it comes from Claude transcripts / Codex rollouts. A restart does lose unacknowledged in-memory commands and the live ring; see the reliability boundary above.
- **Does restarting the relay drop the session?** It briefly disconnects and requires login again because the process-local revocation registry resets. The conversation remains intact on the wrapper machine.
- **Do I need inbound ports?** No. The wrapper only dials out to the relay.
- **How expensive is it?** cc-remote itself has zero model cost; browsing / refreshing / viewing history spends no tokens. Actual model cost depends on the backend used by the local agent CLI.

## License

MIT — see [LICENSE](LICENSE).
