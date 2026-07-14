# cc-remote

Drive **Claude Code or Codex** running on your machine from a phone or any browser — self-hosted, open source.

A `claude` or `codex` session on one machine, remote-controlled in real time from a phone or browser through a WebSocket relay on a VPS: **live streaming, interrupt anytime, multi-device sync, multi-session switching, instant on-demand history**.

> Inspired by Claude Code's official Remote Control, but fully self-hosted. The **local CLI chooses the model backend**: Claude can use Anthropic or a compatible endpoint, while Codex keeps the machine's Codex configuration. cc-remote **never touches the model API**; it only builds the *control* link.

**中文:** [README.md](README.md)

<p align="center">
  <img src="assets/01-cc-remote-UI.png" alt="cc-remote browser UI" width="600">
  &nbsp;
  <img src="assets/02-cc-remote-iphone.png" alt="cc-remote on a phone browser" width="175">
</p>

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
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

## What it does

- 📱 **Real-time remote control from a phone/browser** — drive Claude Code or Codex on your home/office machine from anywhere; watch it stream tokens and run tools.
- 🧭 **Complete process timeline** — each turn can reveal reasoning summaries, commentary, plans, command output, file diffs, MCP calls, collaboration, and Hook lifecycles while keeping the final answer separate.
- ⏹️ **Interrupt anytime** — cancel the current turn (handles SDK/app-server termination semantics correctly, no cross-talk).
- 🔀 **Multi-session** — a resident session pool with a sidebar; background sessions keep running with live status dots.
- 🕘 **Instant history** — history is paged on demand from the transcript/rollout (like web chats); refresh is fast, no replay flood.
- 🔗 **Multi-device sync** — several devices on the same relay see the same conversation.
- 🔒 **Self-hosted** — the relay is a pure WebSocket forwarder that never touches the model; your code and keys stay on your machine.

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

> **Upgrading to protocol v8:** the wire gate rejects mixed versions. Deploy
> `cc_remote/` and the new `web/dist/` in one maintenance window, then restart the
> relay and wrapper; do not run a rolling mixture. Existing sockets reconnect
> briefly, and a relay restart intentionally requires browsers to log in again.
> Any already-open older page also needs one **hard refresh** to load the new hashed
> assets; logging in again inside the old JavaScript bundle isn't sufficient.
> For a manual release, stop the local wrapper first, stop and update relay + web,
> then start the v8 relay and v8 wrapper so the old wrapper cannot occupy the
> relay's single wrapper slot.

### 3) Publish from staging during a maintenance stop

```bash
# dev machine: the normal account writes its own staging directory, not root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:~/cc-remote-upload/

# VPS: publish protocol-v8 Python + web/dist together while relay is stopped
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

The script installs `python3-venv` + Caddy, creates a `ccremote` system user, builds a venv + `pip install`, merges both a marked cc-remote site and global HTTP timeout/header limits into the Caddyfile while preserving other global options and sites, then starts `cc-remote-relay` + `caddy`. If the new relay restart or readiness check fails, the venv, Caddyfile, and systemd unit are restored as one transaction and the previous relay's `/healthz` is verified.

Verify:

```bash
curl https://your-domain.com/healthz
# expect: {"ok":true,"wrapper_connected":false,"clients":0}
```

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

Back on the VPS, `curl https://your-domain.com/healthz` should now show `wrapper_connected:true`.

### 6) Verify from a phone

Open `https://your-domain.com/` on your phone (any network) → log in with `LOGIN_PASSWORD` → send a message. You should get streaming replies, interrupt, and multi-device sync.

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
| `PUBLIC_ORIGIN` | empty | Exact browser origin allowed to connect, e.g. `https://remote.example.com`; **required**, and non-loopback origins must use HTTPS. |
| `WRAPPER_TOKEN` | placeholder | Bearer token the wrapper presents; must match on both sides. Startup rejects placeholders and short values. |
| `WEB_STATIC_DIR` | empty | Point at `web/dist` to serve the web client same-origin; empty = API/WS only. |
| `CLIENT_QUEUE_CAP` / `CLIENT_QUEUE_BYTES` | `4096` / `16777216` | Hard per-client pending-frame/byte limits; a slow client is disconnected instead of silently losing frames. |
| `MAX_CLIENTS` / `CLIENT_HELLO_TIMEOUT` | `8` / `10` | Hard limits for accepted clients and seconds allowed for the first Hello frame. |
| `WS_MAX_SIZE_BYTES` | `16777216` | Maximum single WebSocket frame accepted by both relay and wrapper transports. |

**Wrapper**

| Var | Default | Notes |
|---|---|---|
| `RELAY_URL` | `ws://127.0.0.1:8765/ws` | Relay WebSocket URL (`wss://domain/ws` in prod). |
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
- Always use TLS (`wss://`) in production (this repo uses Caddy for automatic certs). Do not expose plain `ws://` publicly.
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
