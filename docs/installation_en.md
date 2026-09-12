# Installation and upgrades

[中文](installation.md) · [README](../README_en.md) · [Deployment contract](../deploy/README.md)

Prefer a tested source snapshot for current features. Use a published artifact
when selecting that tag and after checking its features and protocol; the latest
published tag may lag the maintained source branch. This guide selects the installation path;
[deploy/README.md](../deploy/README.md) remains authoritative for staging,
activation, rollback and acceptance. Existing custom services retain their
ownership, private configuration and installation layout.

[Release packages](#release-install) · [Source deployment](#source-install)

<a id="release-install"></a>

## One-command GitHub Release install (for a selected published version)

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
`install.sh` and `SHA256SUMS` from that same release. The example uses `3.0.0`;
replace it with the published tag you selected (without the leading `v`). This
does not select an unpublished development-branch build:

```bash
export CC_REMOTE_VERSION=3.0.0
release_base="https://github.com/muggle-stack/cc-remote/releases/download/v${CC_REMOTE_VERSION}"
curl -fLO "$release_base/install.sh"
curl -fLO "$release_base/SHA256SUMS"

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

The current Wrapper installer checks that the service user has an executable
`~/.local/bin/claude`, including when another engine will be used. Install that
daily CLI before using this installer; an arbitrary `CLAUDE_BIN` override in a
source-run config does not bypass the installer check. Authenticate whichever
engine you intend to use. Then run:

```bash
./install.sh wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX \
  --name "My laptop"
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

### Download configuration

`CC_REMOTE_RELEASE_BASE_URL` can select a trusted artifact mirror or local `file://`
source containing the matching bundles and `SHA256SUMS`. `UV_DEFAULT_INDEX` and
`UV_PYTHON_INSTALL_MIRROR` configure dependency/runtime downloads. Hash validation
still applies. A failed download before installation can be retried; a lost
connection during activation requires inspecting the original transaction first.

<a id="source-install"></a>

## Production deploy (public VPS relay + wrapper on your machine)

The source-staging path below is recommended for current features and also
supports custom deployments and recovery. Move the relay to the public internet; the wrapper dials it
**outbound** over `wss://`, and phones hit the same domain. The model link is
untouched.

For AI-assisted deployment, use the repository's
[deployment skill](../.agents/skills/cc-remote-deploy/SKILL.md) and
[shared-control acceptance](../deploy/README.md#codex-code-shared-control-plane-acceptance):
each Codex Code account's CLI and Wrapper must connect to the same official
daemon. After deployment, a supported installed Codex App can be offered
[optional attachment](../deploy/README.md#optional-codex-app-attachment). The agent
asks first; declining or deferring leaves the App unchanged and does not block
cc-remote deployment. Existing explicit attachment authorization does not need
another question. Follow the separate [macOS](codex-desktop-launcher.md) or
[Linux](codex-desktop-linux.md) procedure, preserve account boundaries, and
verify CLI, Wrapper and App connections individually.

```
your machine wrapper ──wss:443──▶ Caddy(VPS, auto HTTPS) ──▶ relay(127.0.0.1:8765) ◀──wss:443── phone browser
                                                                └─ serves web/dist (same origin)
```

### Prerequisites

- **VPS**: Ubuntu 22.04+ / Debian 12+ (another supported Debian-family host; source/CI verification uses Python 3.13) with **ports 80 + 443** open (80 for Let's Encrypt, 443 for wss).
- **Domain**: an A record pointing at the VPS IP (Caddy auto-provisions + renews the TLS cert).
- **Your machine**: macOS or glibc Linux, with outbound 443 allowed. Preserve its existing service manager when upgrading.

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

### 2) Test and freeze one source snapshot

Use Node 24. Run the complete gate in [AGENTS.md](../AGENTS.md#commit-and-pr-gate),
then freeze those tested source files and the built web client together. Keep
credentials, private state, `.git`, virtual environments and dependencies outside
the snapshot. Every target must use these same bytes. The web build commands are:

These commands are already part of the full gate; do not rerun them after it passes.

```bash
npm --prefix web ci
npm --prefix web run build   # produces web/dist/
```

Validate the source/Web protocol pair with `deploy/validate_protocol_bundle.py`
as described in the deployment contract. No browser secret is needed for a build.

**Stage every target before changing live services.** The commands below describe
the Relay and Wrapper separately; do not activate Relay until every Wrapper stage
has passed validation. Protocol v66 cannot be mixed with older clients. Stop old
incompatible Wrappers, activate Relay + Web, then activate Wrappers and hard-refresh
browser tabs. Wrapper activation must snapshot Work SQLite and private profile
control state with `deploy/work_registry_snapshot.py`; this is not limited to
upgrades from a particular old protocol. Rollback restores the matching state
before starting old code. Never copy only a live SQLite database without its WAL.

### 3) Upload staging, then publish it as an atomic release

```bash
# dev machine: the normal account writes its own staging directory, not root-owned /opt
rsync -av --delete --exclude='.git' --exclude='.venv' \
  --exclude='web/node_modules' --exclude='.env' \
  /absolute/path/to/tested-snapshot/ "<vps-user>@<vps>:~/cc-remote-upload/"

# VPS: never overlay the running /opt tree with the staging upload
ssh "<vps-user>@<vps>"
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

# Activate only after ALL Wrapper stages pass; stop incompatible old Wrappers first.
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
release's `/healthz` is verified. Start the v66 wrapper after success.

Verify:

```bash
curl https://your-domain.com/healthz
# Check ok and protocol; Wrapper connectivity is checked after Wrapper activation.
```

In insecure mode, use `curl http://your-public-ip/healthz` instead.

### 5) Stage and activate Wrappers

For a new managed installation, build the Wrapper role package from the same
frozen snapshot. Use a builder and a verified `uv` executable matching the target
OS/architecture; the pinned uv/Python versions are in `deploy/uv-version.txt` and
`deploy/python-version.txt`. The [release workflow](../.github/workflows/release.yml)
shows the complete platform matrix. Example for macOS arm64 (replace all paths
and the Git SHA):

```bash
python3.13 deploy/build_release.py \
  --root /absolute/path/to/tested-snapshot \
  --output-dir /absolute/path/to/artifacts \
  --role wrapper --os darwin --arch arm64 \
  --uv-bin /absolute/path/to/verified/uv \
  --git-sha FULL_40_CHARACTER_COMMIT_SHA
```

After verifying and unpacking the matching archive on the Wrapper host, use its
installer; it owns the private configuration, service, immutable release and
rollback transaction. It requires the daily Claude CLI noted above:

```bash
bash /absolute/path/to/unpacked-wrapper/deploy/install-wrapper.sh \
  /absolute/path/to/unpacked-wrapper \
  --relay https://remote.example.com \
  --pair XXXXX-XXXXX-XXXXX-XXXXX --name "My laptop"
```

Run this macOS example as the logged-in desktop user. A direct Linux invocation
of the bundled script requires `sudo bash` and `--user youruser`, with `youruser`
replaced by the ordinary account that will run Wrapper.

An existing paired installation omits `--relay`/`--pair`/`--name`. Do not use a
first-install template to replace a custom topology. For an existing manually
managed immutable Wrapper, `deploy/prepare_wrapper_stage.py` validates the stage
and prepares its environment; it **does not activate**. Use its `--help` and the
existing installation's activation transaction. Keep the same service user and
external configuration, snapshot private state while stopped, atomically switch
`current`, validate, and retain the old release for rollback.

Linux credentials belong in root-only `/etc/cc-remote/wrapper.env` and/or
`/etc/cc-remote/device.env`, not a source `.env`; macOS uses installer-managed
private files under the desktop user. A self-hosted Wrapper must be activated
from an independent controller or one OS-owned job that survives its shutdown.
A dropped SSH/control connection is an unknown result: inspect that original
transaction before retrying.

Office conversion is optional on a Linux Wrapper:

```bash
sudo apt-get update
sudo apt-get install -y libreoffice bubblewrap
```

This is for DOCX/PPTX and other convertible Office formats. **XLSX previews need
neither package** and also work on macOS. Do not install converters on Relay.

#### Pair a Mac or Linux machine from Device Center (recommended)

Skip this section if the installer already paired and started the machine.
These commands are for a manually managed source installation; do not start a
second Wrapper for the same device.

Sign in, open the device icon in the header, and choose **Allow adding devices**.
The page creates a single-use code that expires after 10 minutes by default:

```bash
.venv/bin/python -m cc_remote.device pair https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "My laptop"
.venv/bin/python -m cc_remote.wrapper
```

Interactive pairing stores a mode-`0600` credential in
`~/.cc-remote/device.json`. For a Linux systemd service, write the credential
straight to a root-only EnvironmentFile and restart the wrapper:

```bash
sudo .venv/bin/python -m cc_remote.device pair \
  https://your-domain.com XXXXX-XXXXX-XXXXX-XXXXX \
  --name "My server" --env-file /etc/cc-remote/device.env
sudo systemctl restart cc-remote-wrapper
```

The relay stores only the credential hash. Device Center shows online/offline
state and supports switching, renaming, and per-device revocation. The legacy
manual `WRAPPER_TOKEN` / `WRAPPER_TOKENS_JSON` path remains compatible.

### 6) Verify the deployment

First complete [deployment acceptance](../deploy/README.md): protocol/build
identity, stable service PIDs/restart counts, public health, every expected
Wrapper, recent error logs, and shared Codex control for every account. Optional
App attachment is reported separately. Then verify the interaction from a phone:

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
