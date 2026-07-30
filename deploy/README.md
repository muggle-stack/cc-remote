# deploy/

Reference files for the production deploy (public VPS relay + wrapper on your
machine). The **full step-by-step guide is in the main [README](../README.md#生产部署公网-vps-中继--你机器上的-wrapper)**
([English](../README_en.md#production-deploy-public-vps-relay--wrapper-on-your-machine)).

- `install.sh` — versioned GitHub Release bootstrap. It requires an explicit
  `relay` or `wrapper` role, detects OS/CPU, downloads that one role archive,
  verifies its `SHA256SUMS` entry before extraction, rejects unsafe archive
  paths, and then invokes the in-bundle installer. It never pipes a network
  response into a shell.
- `build_release.py` / `release_manifest.py` — reproducible role-bundle builder
  and fail-closed manifest validator. Relay artifacts contain `web/dist` and
  `requirements-relay.lock`; Wrapper artifacts contain no Web tree and use
  `requirements-wrapper.lock`. Each artifact carries the product version,
  protocol, full Git SHA, OS, architecture, and Python runtime contract.
- `install-relay.sh` — first-install/upgrade entry for a published Relay
  bundle. It creates secrets only when `/opt/cc-remote/.env` does not exist,
  then delegates to the existing transactional VPS installer. The explicit
  `--allow-private-origins` first-install option binds IPv4 `0.0.0.0:8765`
  for simultaneous LAN/Tailscale access and requires firewall restriction;
  the default remains loopback-only behind Caddy.
- `install-wrapper.sh` — first-install/upgrade entry for published macOS and
  Linux Wrapper bundles. It builds the immutable release before pairing and
  activation, stores device authority outside the release, atomically switches
  `current`, installs a per-user LaunchAgent or root-managed systemd unit, and
  restores the previous release/service definition on failure. The installer
  requires and explicitly selects the service user's daily
  `~/.local/bin/claude`; it never silently falls back to the SDK-bundled CLI.
- `setup-vps.sh` — atomic VPS release installer. It validates a user-owned
  upload, copies it to a new root-owned
  `/opt/cc-remote/releases/release-*` directory, builds that release's own
  venv, validates the Python/web protocol pair, then switches the
  `/opt/cc-remote/current` symlink in one rename. The running tree is never
  overlaid with `rsync --delete`. If relay restart/readiness fails, `current`,
  Caddyfile, and the relay unit roll back together and the previous release is
  health-checked. The previous full code + web + venv directory is retained.
  Run `sudo bash ~/cc-remote-upload/deploy/setup-vps.sh your-domain.com \
  ~/cc-remote-upload`; the optional second argument defaults to the repository
  containing the invoked script. Shared secrets stay only in
  `/opt/cc-remote/.env`, whose `WEB_STATIC_DIR` must point to
  `/opt/cc-remote/current/web/dist`.
- `Caddyfile` — reverse proxy + auto Let's Encrypt TLS (`wss://domain/ws` →
  `127.0.0.1:8765`) plus an early 4 KiB login-body limit. Replace
  `cc-remote.example.com` with your domain.
- `Caddyfile.insecure` — explicit plain-HTTP public-IP template selected only
  when `ALLOW_INSECURE_HTTP=1`, the setup target is a public IPv4 address, and
  `PUBLIC_ORIGIN` exactly matches `http://that-address`. It omits HSTS and
  permits `ws://` in CSP; login credentials, cookies, wrapper tokens, and all
  session traffic are unencrypted in this mode. Pass the IP and source
  directory to the same immutable `setup-vps.sh` flow used for TLS.
- `cc-remote-relay.service` — systemd unit for the relay on the VPS.
- `cc-remote-wrapper.service` — systemd unit for the wrapper on your machine
  (edit `User` + paths first). It reads root-only
  `/etc/cc-remote/wrapper.env`, hides that file and any legacy repository
  `.env` from model descendants, and disables core dumps.
- `env.relay.example` / `env.wrapper.example` — environment templates for each
  side. Install the wrapper template as root:root mode 0600 at the path above.
- `com.muggle.cc-remote.wrapper.plist.in` — secret-free macOS LaunchAgent
  template. The runtime reads the current user's mode-0600 device JSON instead
  of embedding control credentials in the plist.

Protocol v27 is a coordinated upgrade: publish freshly built Relay/Web and
Wrapper artifacts from the same tagged commit. The strict protocol gate is
intentional and mixed protocol versions will not communicate. `setup-vps.sh`
rejects a missing or mismatched web build manifest. Stop the wrapper first;
activate the v27 relay/web release; then start the v27 wrapper.

## Native terminal coordination

- **Claude Code:** run `claude` directly for the untouched official process;
  Remote treats direct CLI, Desktop, and Agent View ownership as read-only.
  Explicit takeover may gracefully terminate the exact same-user Claude process
  with SIGTERM and then resume through the SDK, but it never kills the terminal
  shell, escalates to SIGKILL, or silently adopts a process.
- **Codex Code:** `CC_REMOTE_CODEX_DAEMON=auto` prefers Codex's official shared
  app-server daemon. Set it to `off` only to force the legacy private stdio path.
- **Work:** both engines stay on private per-process control planes regardless
  of the Code settings.

## Security (short version)

The relay is exposed publicly; `LOGIN_PASSWORD` or `LOGIN_USERS_JSON`,
`SESSION_SECRET`, and `WRAPPER_TOKEN` or `WRAPPER_TOKENS_JSON` are the
authentication secrets. Claude defaults to
`bypassPermissions`; Codex inherits its local sandbox and defaults to approval
policy `never`. Treat every logged-in client as holding remote agent/shell
authority on the wrapper machine. Use strong secrets, keep relay `.env` out of
git, never store the production wrapper token in a model-readable repository
file. Always prefer TLS at Caddy; the public-IP escape hatch sends the login
password, browser cookie, wrapper token, and session traffic unencrypted.
See the [security section](../README.md#安全须知务必读) of the main README.

The relay itself limits unfinished login bodies to 32 concurrent reads and 10
seconds each. The managed Caddy global block additionally sets 10-second header,
15-second body, 30-second write, 2-minute idle, and 64 KiB header limits before
requests reach the relay. Other global options and sites are preserved. If a
shared Caddyfile already contains an unmanaged `servers` block, setup fails
closed and asks the administrator to reconcile it instead of silently creating
ambiguous global behavior.
