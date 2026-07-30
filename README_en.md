# cc-remote

**Bring Claude Code / Codex on your machine to your phone and any browser.**

Self-hosted · Dual-engine · Multi-session · Live process · Responsive Web

**Current release: v3.0.0** · Wire protocol v27

[中文](README.md) ·
[5-minute quick start](#quick-start-local-one-machine-5-min) ·
[Production deploy](#production-deploy-public-vps-relay--wrapper-on-your-machine) ·
[Security](#security-please-read) ·
[Changelog](CHANGELOG.md)

cc-remote is an open-source remote control plane. A local `wrapper` drives the
already installed and authenticated `claude` / `codex` CLI, while browsers view
and control its sessions through your self-hosted WebSocket relay. Models,
authentication, and tool execution remain under the local CLI; cc-remote does
not proxy model APIs or bake API keys into the web client.

v3.0.0 is not a visual rebrand. It adds isolated Code / Work spaces on top of
the existing two-engine, multi-session remote control plane, while redesigning
history projection, native-client coordination, multi-device routing, and
release boundaries. The work targets real failures seen with very long
sessions, stale App/CLI state, mobile history jumps, and cross-machine leakage.

![cc-remote Claude sessions and multi-session workspace](assets/readme-claude-multisession.jpg)

---

## Table of contents

- [What changed in v3](#what-changed-in-v3)
- [Core capabilities](#core-capabilities)
- [Architecture](#architecture)
- [Real interface and practical features](#real-interface-and-practical-features)
- [Quick start (local, one machine, 5 min)](#quick-start-local-one-machine-5-min)
- [One-command GitHub Release install (recommended for production)](#one-command-github-release-install-recommended-for-production)
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

## What changed in v3

v3 advances cc-remote from “control a CLI in a browser” into a local-first,
recoverable control plane that can safely connect multiple machines. Compared
with the previous public release, the major changes are:

| Area | v3.0.0 |
|---|---|
| **Code / Work spaces** | Add an independent Cowork surface beside repository-oriented Code sessions. Claude and Codex each receive private projects, file/link/note knowledge sources, reusable templates, schedules, and artifacts. Work and Code isolate directories, sessions, base prompts, and permission boundaries. |
| **History startup and very long sessions** | The browser first paints its last validated IndexedDB projection. A source-fingerprinted SQLite index on the wrapper serves recent turn summaries first, while tool output, reasoning, process logs, and oversized text load per turn. Short sessions no longer wait for a full source scan; long sessions page backward without losing the reader's viewport. |
| **Large Codex rollouts** | Codex history is read backward by turn while preserving app-server-native resume and compaction state; cc-remote never re-uploads the entire rollout to the model. A tightly guarded official HTTP compatibility path is used only for a specific oversized Codex Desktop + OpenAI resume case. |
| **Native App / CLI coordination** | Claude CLI/Desktop/Agent View and Codex shared daemon/App/CLI retain engine-specific ownership models. v3 reconciles running, read-only, interrupt, steer, compact, turn binding, and terminal state so sibling sessions do not lock each other, old turns do not move to the tail, and interrupted work does not leave ghost activity. |
| **Multi-device isolation** | Device Center adds single-use pairing, independently revocable machine credentials, and presence. The relay routes only an account's allowed `machine_id` values. Device, Code / Work, engine, connection generation, and session ownership are isolated so delayed frames cannot mutate the active view. |
| **Mobile and artifact UX** | Loading older history preserves the scroll anchor. Images load on demand and support a lightbox, tap-to-close, and pinch zoom. Markdown, source, HTML, PDF, and Office previews remain within the local security boundary. PWA icons, narrow-screen sheets, error presentation, and process timelines are also aligned. |
| **Rollback-safe releases** | The product version is v3.0.0 and the wire protocol is v27. Builds and deployments validate both values. The VPS uses immutable releases, release-local virtual environments, an atomic `current` switch, and rollback instead of overwriting a live directory. |

> **The trust boundary has not changed:** model accounts, API keys, session
> sources, and tool execution stay on the wrapper machine. The VPS relay stores
> no conversations or artifacts. Browsing history reads only local transcripts,
> rollouts, and rebuildable projections; it never resumes an engine or creates a
> model turn.

See [CHANGELOG.md](CHANGELOG.md) for the complete release notes and upgrade
requirements.

## Core capabilities

| Scenario | What you can do |
|---|---|
| **Two engines** | Use Claude Code and Codex in the same web UI. Every session keeps its own model, reasoning effort, permissions, and runtime state. |
| **Code / Work spaces** | Code remains repository-oriented. Work is an independent Cowork surface for documents, spreadsheets, presentations, research, and temporary collaboration, with a separate session list. |
| **Work projects and knowledge** | Keep provider-scoped projects, file/link/note sources, and reusable work templates. Starting a Work session materializes the selected context into its private directory. |
| **Work schedules and isolation** | Run one-shot, daily, or weekly tasks with persisted run records, leases, retries, and overlap prevention. Each work item can access only its private directory; add required material explicitly through attachments or the project knowledge collection. |
| **Remote operation** | Watch streaming replies and send attachments from a phone, tablet, or desktop browser. While Codex is busy, new input defaults to native steering of the active task, with queue still available; Claude retains interrupt-and-send. Stop remains a separate action. |
| **Complete process** | Expand the reasoning summaries, plans, command output, file diffs, MCP calls, collaboration agents, Hooks, and terminal interaction events exposed by each engine. |
| **Artifacts and file preview** | Work automatically lists files produced by the current task. Source opens at referenced lines, Markdown is previewable and conflict-safe to edit, HTML renders in an isolated iframe, images/PDF open directly, and DOCX/XLSX/PPTX are previewed after a temporary sandboxed conversion on the wrapper host. |
| **Human approval** | Return Claude `can_use_tool` decisions and Codex command, file-change, user-input, general-permission, and MCP elicitation responses. Mirror a terminal-owned session read-only or take it over explicitly. |
| **Session management** | Search, switch, rename, archive, delete, and fork from individual messages. Codex supports explicit compact, native Review, isolated Git worktree forks, and moving an idle conversation to another working directory. |
| **Runtime controls** | Change the model, reasoning effort, service tier, permissions, and Plan mode. Codex Code uses one compact `/permissions` sheet for approval policy, official execution-environment profiles, and Cached/Live Web Search. Use `/goal` for long-running goals and `/status` for read-only app-server status, usage, and rate limits. |
| **Real extension catalog** | Open `/extensions`, `/skills`, `/plugins`, `/apps`, `/mcp`, or `/hooks` against the current engine. Code can manage Skills, plugins, and Claude Hooks where the engine allows it; Codex Hooks remain read-only because the official API has no write path. Work presents every extension category read-only to preserve its private environment. |
| **Continuity** | Let background sessions keep running and synchronize them across clients. Paint the browser projection first, validate paged materialized summaries from Claude transcripts or Codex rollouts, and resume only the live tail after reconnecting. |
| **Multi-machine and PWA** | Connect multiple named wrappers to one relay and optionally restrict accounts to selected machines. Install the web client as a PWA; notifications default to a generic privacy mode, with an explicit opt-in for a safely truncated session name and exact navigation. |
| **Self-hosted** | The wrapper only makes outbound connections. Sessions, Work data, and preview conversion stay on that machine; the replaceable VPS remains a stateless relay. Web auth uses an HttpOnly cookie, and CLI credentials or API keys never enter the frontend. |

> Available models, service tiers, and runtime controls depend on the local CLI and the capabilities exposed by its SDK or app-server.

## Architecture

Two **independent** links:

```
MODEL LINK (cc-remote never touches):  claude / codex ──(their local config)──▶ model service

CONTROL LINK (this repo):              browser ⇄ relay(WebSocket) ⇄ wrapper ⇄ SDK / app-server ⇄ local CLI
```

| Component | Runs where | What it does |
|---|---|---|
| **wrapper** | the machine where `claude` / `codex` runs | Holds a session pool, translates SDK/app-server events to the wire protocol, handles interrupt/drain, reads transcript/rollout history on demand, and temporarily converts Office previews locally. **Outbound-only to the relay — no inbound ports needed.** |
| **relay** | public VPS (or local) | Pure WebSocket forwarder (FastAPI). It keeps one wrapper slot per `machine_id`; browsers use an HttpOnly session cookie and receive events only from their selected machine. **Does not persist sessions or artifacts, never imports `claude-agent-sdk`, and never touches the model API.** |
| **web** | the browser | React client; the relay serves its static files (`web/dist`) from the same origin. |

### Code and Work

The **Code / Work** switch at the top of the sidebar reuses the same Claude and
Codex engines while isolating storage, session lists, and permissions:

- **Code** preserves the existing repository-oriented behavior for development,
  debugging, and deployment.
- **Work** targets documents, spreadsheets, presentations, research, knowledge
  collections, and temporary chats. Claude stores it under
  `~/.claude/cc-remote/work` and Codex under `~/.codex/cc-remote/work` by default.
  Every work item has its own `workspace/` and uploads. Artifacts are ordinary
  files produced inside that workspace. Deletion is limited to a directory that
  the registry proves belongs to Work. Work replaces both CLIs' coding-oriented
  base prompts: casual chat does not inspect files or mention code projects, while
  explicit software requests can still use the same engines and tools.
- Work does not expose the home directory or arbitrary external directories.
  Add existing material explicitly through attachments or the project knowledge
  collection so a conversation cannot discover unrelated projects or history.
- A Work template is an instruction/workflow template written into the
  project's `WORK.md`; it does not execute unreviewed third-party code. Scheduled
  tasks are persisted and claimed by the wrapper under the same Work isolation.

### How native terminals and Remote cooperate

Code sessions follow each CLI's real control plane without replacing official
commands:

- **Claude:** `claude` always remains the official command and official TUI;
  cc-remote installs no alias, shim, or PATH interception. Sessions opened directly
  by `claude`, Claude Desktop, or Agent View are read-only mirrors in Remote by
  default, which prevents two independent input owners. To write from Remote, the
  user explicitly chooses takeover; cc-remote sends SIGTERM only to the exact
  same-user Claude process identity, waits for release, and then resumes the same
  session through the SDK. It never kills the terminal shell, escalates to SIGKILL,
  or takes over a process silently.
- **Codex Code:** prefers Codex's official shared app-server daemon, so native
  Codex clients and Remote share the thread and control state. If the installed
  version cannot provide it, cc-remote explicitly falls back to a private
  app-server. Set `CC_REMOTE_CODEX_DAEMON=off` for troubleshooting.
- **Switching Codex accounts:** configure
  `scripts/codex-auth-daemon-restart` as the `codex-auth` post-switch hook.
  It publishes a local generation barrier around the official daemon restart.
  Remote immediately interrupts its in-flight turn on the old daemon, keeps the
  session running, resumes the same thread on the replacement, and continues
  the same task before any queued browser message can drain. Goals use Codex's
  native automatic continuation; ordinary conversations use contextual-only
  continuation input that is hidden from user history and rollback user-turn
  boundaries. The official graceful restart may still wait for other native
  clients, so the hook hands it to a detached worker and returns immediately.
  Worker output is written to
  `~/.cc-remote/codex-daemon-restart.log`. It never replays a prompt or
  reads/stores Codex credentials.
- **Work:** Claude and Codex Work keep private processes and directories and do
  not join the Code control plane, preventing work material from leaking into code
  sessions.

### Where artifact preview runs

- HTML is sanitized with DOMPurify in the browser and rendered in a scriptless,
  network-blocked sandbox iframe.
- PNG/JPEG/GIF/WebP/AVIF and PDF are path-, type-, and size-checked by the wrapper,
  then returned only to the requesting browser through the authenticated WebSocket.
- DOC/DOCX/ODT/RTF, XLS/XLSX/ODS, and PPT/PPTX/ODP are converted to PDF by
  LibreOffice on the **wrapper host**. On Linux, bubblewrap removes network and
  user-directory access and mounts only that request's temporary directory. The
  directory is deleted immediately after conversion.
- The VPS relay forwards bounded preview frames and stores neither originals nor
  converted files. Replacing the VPS requires no session migration. Moving to a
  new wrapper device means migrating the local transcripts/rollouts, Work roots,
  and cc-remote state.

## Real interface and practical features

The screenshots below come from a running cc-remote installation, not design mockups.

### Multi-session management: keep work running in the background

The session pool groups conversations by working directory and lets you search,
switch, rename, and archive them. You can move to another session while one is
still working in the background, then return to its complete live progress.
Claude Code and Codex share the same workspace while retaining independent
context, models, permissions, and runtime state.

![Multi-session workspace grouped by project with search and switching](assets/readme-multi-session.jpg)

### Claude Code: see reasoning, tool calls, and Hooks

A Claude session is more than a simplified chat showing only the final text.
cc-remote receives the reasoning, command calls, tool results, and Hook lifecycle
events exposed by the Claude Code SDK and presents them as a collapsible timeline.
The composer also shows the session's current model, reasoning effort, permission
mode, and context usage.

![Claude Code reasoning, command calls, and Hook events](assets/readme-claude-session.jpg)

### New sessions: choose the engine and working directory first

Create either a Claude Code or Codex session from one entry point, browse for its
working directory, and attach images or files to the first message. Once the
session exists, adjust its model, permissions, or Plan mode only when needed
instead of filling in a row of defaults up front.

![Create a new session by choosing its engine and working directory](assets/readme-new-session.jpg)

### Codex: preserve plans and the complete process

Codex sessions organize the reasoning summaries, plans, commands, diffs, MCP
calls, collaboration agents, and Hooks reported by app-server into a collapsible
timeline. Expand it while a turn is running to follow the details, then collapse
the completed work into a concise summary; the final response always remains
separate.

![Collapsible Codex plan, Hook, and tool-call timeline](assets/readme-process-timeline.jpg)

### Per-session Codex controls: model, reasoning, permissions, search, and status

The model, reasoning effort, service tier, and permissions belong to the current
session, so you can change the next turn without editing the machine's global
configuration. In Codex, approval policy and execution environment are separate:
`never` / `on-request` / `untrusted` decide when Codex asks, while Read Only,
Workspace, Full Access, or a custom named profile defines filesystem and network
boundaries. `/permissions` keeps those controls and Cached/Live Web Search in one
sheet so the mobile composer stays compact. While Codex is running, Enter steers
the active task by default; queue remains selectable, and an empty composer
exposes Stop as a separate action. The composer also provides attachments,
context usage, and command entry points such as `/goal` and `/status`.

![Codex model selection and per-session controls](assets/readme-model-controls.jpg)

### Common operations at a glance

- **Sessions:** create, search, run in the background, rename, archive, delete, fork, compact Codex context, run native Review, and create an isolated Codex worktree. The three-dot menu on every unarchived Codex Code session can also continue an idle conversation in another directory without changing its thread ID.
- **Turns:** stream, steer Codex natively, queue, stop/interrupt, copy, edit and resend, or fork from a specific message.
- **Tools:** inspect command output, file changes and diffs, MCP, collaboration agents, Hooks, approvals, and user-input requests.
- **Terminal coordination:** Codex Code shares the official daemon with native
  bidirectional control. Native Claude CLI, Desktop, and Agent View sessions are
  mirrored read-only until the user explicitly takes over from Remote.
- **Status:** inspect the model, reasoning effort, permissions, Plan mode, context, goals, usage, rate limits, and runtime warnings.
- **Extensions:** inspect the live Skills, Plugins, Apps, MCP, and Hooks inventory.
  Code can create/remove local Skills, manage Claude Hooks, and install/uninstall
  plugins through native managers where supported. Codex Hooks and every Work
  extension category remain read-only.
- **Devices:** use a responsive mobile UI, light or dark themes, multi-browser/multi-machine synchronization, PWA installation, generic or session-aware completion alerts, and reconnect recovery. After login, notification, theme, and logout actions share the Header three-dot menu.

## Quick start (local, one machine, 5 min)

First get the relay + wrapper + web running on the **machine where the agent CLI runs** to validate the whole chain. Production deploy is the next section.

### Prerequisites

- A machine signed in to **Claude Code** or to a **Codex CLI** that supports `app-server`, with the CLI itself already **able to chat**. The Claude wrapper explicitly launches the daily `~/.local/bin/claude` rather than the SDK-bundled copy; each new Codex app-server reselects the newest usable local install. Make both available to switch engines in the web UI.
- **Python 3.10+**, **Node 20.19+** (to build the web client).
- Optional: Office artifact preview requires **LibreOffice + bubblewrap** on the
  Linux wrapper host (for example, `sudo apt install libreoffice bubblewrap`).
  The VPS does not need either package.

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
# share the same Claude Code install used by the normal terminal
CLAUDE_BIN=~/.local/bin/claude
```

> For a local loopback quick start, the relay and wrapper may share this `.env`;
> it is not a production secret store. A public deployment must use the
> root-only `/etc/cc-remote/wrapper.env` below so bypass-permissions model/tools
> cannot read control-plane credentials directly.
>
> The Python SDK remains pinned to the repository-tested version and owns the
> message protocol plus interrupt/drain behavior. `CLAUDE_BIN` is the Claude
> Code executable that actually runs each session, so Remote and the terminal
> share CLI updates, Keychain/login state, and `~/.claude/settings.json`.
> An empty `CLAUDE_BIN` still resolves to `~/.local/bin/claude`; it does not
> silently switch back to the SDK bundle.

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

## One-command GitHub Release install (recommended for production)

Published releases split Relay and Wrapper into system/architecture-specific
artifacts. Relay contains only the backend and prebuilt Web client; Wrapper
contains only the local control plane. Both bundle `uv` and create a managed
Python 3.13 environment during installation. Users do not need to clone the
repository, install Node, or paste tokens into service definitions.

| Role | System | Architectures | Service |
|---|---|---|---|
| Relay | Ubuntu 22.04+ / Debian 12+ | x86_64, arm64 | systemd + Caddy |
| Wrapper | macOS | Intel, Apple Silicon | per-user LaunchAgent |
| Wrapper | glibc Linux with systemd (Ubuntu 22.04+ / Debian 12+ recommended) | x86_64, arm64 | systemd under a chosen ordinary user |

### 1) Download and verify the bootstrap

Confirm the version and release attestation on GitHub, then download
`install.sh` and `SHA256SUMS` from that same release:

```bash
release=https://github.com/muggle-stack/cc-remote/releases/download/v3.0.0
curl -fLO "$release/install.sh"
curl -fLO "$release/SHA256SUMS"

# Linux
grep ' install.sh$' SHA256SUMS | sha256sum -c -
# On macOS use:
# grep ' install.sh$' SHA256SUMS | shasum -a 256 -c -
chmod +x install.sh
```

The bootstrap detects OS/CPU, downloads only the selected role artifact, and
checks its SHA-256 before extraction or execution.

### 2) Install Relay on the VPS

Point the domain's A/AAAA record at the VPS, open ports 80/443, then run:

```bash
./install.sh relay --domain remote.example.com
```

On Linux the script requests `sudo` itself. A first install asks interactively
for a web password of at least 16 characters, generates Relay secrets, installs
Caddy/systemd, and performs immutable staging, atomic `current` activation, and
rollback under `/opt/cc-remote/releases/`. An existing
`/opt/cc-remote/.env` is preserved.

To add direct LAN/Tailscale IPv4 access to the same Relay, opt in on the first
install:

```bash
./install.sh relay --domain remote.example.com --allow-private-origins
```

This binds the Relay to `0.0.0.0:8765`; Caddy still serves the public domain
over HTTPS. Port 8765 is then present on every IPv4 interface, so use the host
firewall to admit only trusted LAN/Tailscale peers. Existing installs preserve
`.env`; enable this mode by setting both `RELAY_HOST=0.0.0.0` and
`ALLOW_PRIVATE_ORIGINS=1` there before upgrading with the same option.

Open `https://remote.example.com/`, sign in, choose **Allow adding devices** in
Device Center, and copy the one-time pair code.

### 3) Install Wrapper where Claude / Codex runs

First ensure the native `claude` or `codex` CLI is signed in and works on that
machine, then run:

```bash
./install.sh wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX \
  --name "MacBook Pro"
```

Run the macOS installer as the logged-in desktop user; it creates a per-user
LaunchAgent. Linux requests `sudo`, while Wrapper and all model/tool descendants
still run as the ordinary user who started installation. The long-lived device
credential is stored only in a mode-`0600` private config:
`~/.cc-remote/device.json` on macOS or `/etc/cc-remote/device.env` on Linux. It
is never embedded in a plist, systemd unit, or release directory.

For an upgrade, download the new version's `install.sh` and rerun it. Relay
still needs `--domain`; a previously paired Wrapper needs only:

```bash
./install.sh wrapper
```

Complete protocol upgrades for Relay, Web, and every Wrapper in one maintenance
window, then hard-refresh open browser tabs. The installers retain the previous
release and restore both `current` and the service definition if activation
does not become healthy.

## Production deploy (public VPS relay + wrapper on your machine)

The source-staging/manual path below remains available for development, custom
deployments, and recovery. Normal production installs should prefer the GitHub
Release path above. Move the relay to the public internet; the wrapper dials it
**outbound** over `wss://`, and phones hit the same domain. The model link is
untouched.

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

> **Upgrading to protocol v27:** the wire gate rejects mixed versions. Deploy
> `cc_remote/` and the new `web/dist/` in one maintenance window, then restart the
> relay and wrapper; do not run a rolling mixture. Existing sockets reconnect
> briefly, and a relay restart intentionally requires browsers to log in again.
> Any already-open older page also needs one **hard refresh** to load the new hashed
> assets; logging in again inside the old JavaScript bundle isn't sufficient.
> For a manual release, stop the local wrapper first, stop and update relay + web,
> then start the v27 relay and v27 wrapper so the old wrapper cannot occupy the
> slot for the same `machine_id`.

### 3) Upload staging, then publish it as an atomic release

```bash
# dev machine: the normal account writes its own staging directory, not root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  ./ <vps-user>@<vps>:~/cc-remote-upload/

# VPS: never overlay the running /opt tree with the staging upload
ssh <vps-user>@<vps>
sudo mkdir -p /opt/cc-remote
```

The installer copies staging into a new
`/opt/cc-remote/releases/release-*`, builds a release-local venv, and switches
`/opt/cc-remote/current` atomically only after every check passes. The previous
full code, `web/dist`, and venv remain available for rollback; the dirty live
tree is never updated with `rsync --delete`.

### 4) VPS: fill `.env` + run setup

```bash
# on the VPS: .env is the only runtime config shared by releases
sudo test -f /opt/cc-remote/.env || sudo install -m 600 \
  ~/cc-remote-upload/deploy/env.relay.example /opt/cc-remote/.env
sudoedit /opt/cc-remote/.env
# set LOGIN_PASSWORD / SESSION_SECRET / WRAPPER_TOKEN and keep:
# WEB_STATIC_DIR=/opt/cc-remote/current/web/dist

# for upgrades, stop the local wrapper first; then switch relay + web together
sudo bash ~/cc-remote-upload/deploy/setup-vps.sh \
  your-domain.com ~/cc-remote-upload
```

For a public-IPv4-only deployment, use this matching configuration and target:

```ini
# /opt/cc-remote/.env
PUBLIC_ORIGIN=http://your-public-ip
ALLOW_INSECURE_HTTP=1
```

```bash
sudo bash ~/cc-remote-upload/deploy/setup-vps.sh \
  your-public-ip ~/cc-remote-upload
```

The installer selects the plain-HTTP Caddy template only when the opt-in is
enabled, the argument is a public IPv4 address, and `PUBLIC_ORIGIN` matches it
exactly. Private, loopback, reserved, and malformed addresses fail closed.

The script installs `python3-venv` + Caddy, creates the `ccremote` service user,
builds an immutable release and its venv, merges Caddy configuration, atomically
switches `current`, and restarts the relay. If restart/readiness fails, `current`,
the Caddyfile, and the systemd unit roll back as one transaction and the previous
release's `/healthz` is verified. Start the v27 wrapper after success.

Verify:

```bash
curl https://your-domain.com/healthz
# expect: {"ok":true,"wrapper_connected":false,"clients":0}
```

In insecure mode, use `curl http://your-public-ip/healthz` instead.

### 5) Your machine: root-only wrapper environment + systemd

For Office artifact preview, install the converter sandbox on this wrapper host,
not on the VPS:

```bash
sudo apt-get update && sudo apt-get install -y libreoffice bubblewrap
```

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

#### Pair a Mac or Linux machine from Device Center (recommended)

Sign in, open the device icon in the header, and choose **Allow adding devices**.
The page creates a single-use code that expires after 10 minutes by default:

```bash
python -m cc_remote.device pair https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "MacBook Pro"
python -m cc_remote.wrapper
```

Interactive pairing stores a mode-`0600` credential in
`~/.cc-remote/device.json`. For a Linux systemd service, write the credential
straight to a root-only EnvironmentFile and restart the wrapper:

```bash
sudo .venv/bin/python -m cc_remote.device pair \
  https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name nono --env-file /etc/cc-remote/device.env
sudo systemctl restart cc-remote-wrapper
```

The relay stores only the credential hash. Device Center shows online/offline
state and supports switching, renaming, and per-device revocation. The legacy
manual `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON` path remains compatible.

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
| `RELAY_HOST` / `RELAY_PORT` | `127.0.0.1` / `8765` | Listen address (keep `127.0.0.1` for Caddy-only public access; use `0.0.0.0` together with `ALLOW_PRIVATE_ORIGINS=1` for simultaneous LAN/Tailscale IPv4 access, and restrict it with a firewall). |
| `LOGIN_PASSWORD` | empty | Single-user web login password. **Required** unless `LOGIN_USERS_JSON` is set. |
| `LOGIN_USERS_JSON` | empty | Optional multi-user policy: `{"alice":{"password":"…","machines":["mac","nono"]}}`; replaces `LOGIN_PASSWORD`. |
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
| `WRAPPER_TOKENS_JSON` | empty | Optional machine-bound tokens: `{"mac":"…","nono":"…"}`; replaces the relay's wildcard `WRAPPER_TOKEN`. |
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
| `CC_REMOTE_MACHINE_ID` | `default` | Stable route id on a multi-machine relay; must match its `WRAPPER_TOKENS_JSON` key when that policy is enabled. |
| `CC_REMOTE_DEVICE_CONFIG` | `~/.cc-remote/device.json` | Interactive pairing credential; the file must be private to the current user. Explicit `RELAY_URL` / `WRAPPER_TOKEN` / `CC_REMOTE_MACHINE_ID` values take precedence. |
| `CLAUDE_BIN` | `~/.local/bin/claude` | Daily Claude Code executable launched by the wrapper. Empty still selects this default; use another absolute path only when the CLI is installed elsewhere. |
| `CC_REMOTE_CODEX_PROXY` | empty | Optional HTTP(S)/SOCKS5 proxy injected only into Codex subprocesses launched by the wrapper. It does not change the wrapper-to-relay connection or the user's terminal `codex`. |
| `CC_REMOTE_CODEX_DAEMON` | `auto` | Code prefers Codex's official shared daemon; `off` forces private stdio app-server. Work is always private and ignores this setting. |
| `CC_REMOTE_STATE_DIR` | `~/.cc-remote` | Local wrapper state directory. The account-switch hook and wrapper must use the same value; the daemon generation barrier stored here contains no Codex credentials. |
| `CC_CWD` | cwd | Default working directory for new sessions. Claude `--resume` needs it to locate `~/.claude/projects/` — **it must be correct**; Codex resume first recovers the original cwd from its rollout. |
| `CC_RESUME_SESSION_ID` | empty | Resume a specific session UUID; empty starts fresh. The id is persisted to `~/.cc-remote/` after first start. |
| `CLAUDE_WORK_ROOT` | `~/.claude/cc-remote/work` | Private Claude Work root for the registry, knowledge sources, sessions, and generated policy files. |
| `CODEX_WORK_ROOT` | `~/.codex/cc-remote/work` | Private Codex Work root for the registry, knowledge sources, sessions, and generated policy files. |
| `MAX_CONCURRENT_SESSIONS` | `20` | Maximum resident agent subprocesses (memory varies by engine/version). Over the cap, an idle process is evicted; client history remains available. |
| `DRAIN_TIMEOUT` | `15` | Seconds to wait for the terminal ResultMessage after interrupt before forcing a reconnect (drain safety net). |
| `CODEX_TURN_IDLE_WARN_SECONDS` | `90` | Show a non-terminal waiting notice after this many seconds without a Codex app-server event; `0` disables it. It does not auto-interrupt long reasoning or tools. |
| `RING_MAX_EVENTS` / `RING_MAX_BYTES` / `TOOL_RESULT_MAX` | see `.env.example` | Live-tail buffer / tool-output truncation tuning. |
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

- The Web and TUI attach a stable `cmd_id` to retryable commands and resend them after a socket reconnect or wrapper recovery. The wrapper deduplicates them and ACKs completion within the same wrapper process lifetime. Each live session also pairs its cursor with a wrapper generation so a restart cannot make an old sequence number look current.
- Once the wrapper accepts a queued or interrupt-replacement message, its bounded in-memory queue owns that work. It starts after the active turn's real terminal boundary even if every Web/PWA client sleeps, disconnects, or hard-refreshes, and reconnecting clients recover a payload-bounded queue summary. Opening a summary privately fetches the full instruction, whose text can be atomically edited before execution without dropping attachments; full payloads never enter the replay ring. This queue is not persisted across a wrapper process crash or restart.
- Unacknowledged-command queues and the general command-deduplication table are **bounded in-memory state**. A hard browser refresh, TUI exit, or wrapper crash does not promise cross-process exactly-once delivery. cc-remote is an interactive control plane, not a durable job queue; after such a failure, inspect the transcript/rollout and live session state before resending.
- Persisted Claude transcripts and Codex rollouts are the history source of truth. The wrapper SQLite summary index and browser IndexedDB are rebuildable projections; the live ring only provides bounded reconnect catch-up. Heavy tool/reasoning detail loads per turn instead of blocking first paint.
- Work schedules are the exception: schedules, run records, leases, heartbeats, retry counts, and next-run timestamps live in SQLite. An expired lease is recovered after a wrapper restart, but an uncertain outcome is never reported as success.

## Security (please read)

> **cc-remote lets a remote person run arbitrary commands on your machine. Treat it like handing someone a shell.**

- Code sessions remain a remote development control plane: Claude defaults to `permissionMode: bypassPermissions`; Codex defaults to approval policy `never` and may select any named permission profile app-server allows for the current cwd. Approval policy cannot widen a profile boundary, while Full Access materially expands capability. **Treat anyone who can log in and enter Code as holding remote agent/shell authority on the wrapper machine.** Work uses the fixed `cc_remote_work` profile and a separate private root without external directories, but this only narrows the default capability surface; it is not a substitute for OS-user, container, or VM isolation.
- `LOGIN_PASSWORD` / `LOGIN_USERS_JSON`, `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON`, and `SESSION_SECRET` form the authentication boundary: use strong random values, never commit or paste them into chats, and rotate them. A repository `.env` is for local development only; production wrappers must use the root-only `/etc/cc-remote/wrapper.env` above. The systemd template prevents the service and model descendants from reading that source file or a legacy repository `.env`; on Linux the wrapper also disables dumpability so children cannot recover captured credentials through `/proc/<pid>/environ` or process memory.
- Always use TLS (`wss://`) in production. Only set `ALLOW_INSECURE_HTTP=1` for a temporary bare-public-IPv4 deployment; login credentials, cookies, wrapper tokens, and all session traffic are unencrypted while it is enabled, so switch back to TLS as soon as possible. `ALLOW_PRIVATE_ORIGINS=1` adds only same-port literal private-IP entry points that match the effective request target and does not relax the public-domain check. Cookie `Secure` follows the trusted request transport, not the caller-provided Origin, but login credentials, cookies, and session traffic are still plaintext when a private HTTP entry point is used.
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

# Explicit live path (requires a running relay + wrapper and calls the model)
CC_REMOTE_RUN_E2E=1 CC_REMOTE_E2E_SCENARIO=smoke \
  RELAY_URL=wss://remote.example/ws LOGIN_PASSWORD='...' \
  pytest -q tests/test_e2e_entry.py
npm --prefix web run lint           # web static checks
npm --prefix web run dev            # web dev server
npm --prefix web run build          # web production build
```

Architecture notes & contribution contracts are in [CLAUDE.md](CLAUDE.md).

## FAQ

- **Does restarting the wrapper lose history?** Persisted history does not disappear; it comes from Claude transcripts / Codex rollouts. A restart does lose unacknowledged in-memory commands and the live ring; see the reliability boundary above.
- **Does restarting the relay drop the session?** It briefly disconnects and requires login again because the process-local revocation registry resets. The conversation remains intact on the wrapper machine.
- **Can I replace the VPS or move to a new device?** Yes. The VPS only serves the relay and static web bundle; it is not the session authority. Deploy the same version on the new VPS and point the wrapper at its new `RELAY_URL`. To move the wrapper, copy the Claude transcripts, Codex rollouts, `CLAUDE_WORK_ROOT` / `CODEX_WORK_ROOT`, and `~/.cc-remote`, re-authenticate each CLI on the new machine, then start the wrapper.
- **Do I need inbound ports?** No. The wrapper only dials out to the relay.
- **How expensive is it?** cc-remote itself has zero model cost; browsing / refreshing / viewing history spends no tokens. Actual model cost depends on the backend used by the local agent CLI.

## License

MIT — see [LICENSE](LICENSE).
