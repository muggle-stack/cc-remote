# deploy/

Reference files for the production deploy (public VPS relay + wrapper on your
machine). The **full step-by-step guide is in the main [README](../README.md#生产部署公网-vps-中继--你机器上的-wrapper)**
([English](../README_en.md#production-deploy-public-vps-relay--wrapper-on-your-machine)).

- `setup-vps.sh` — idempotent VPS setup for either the normal domain + TLS
  path or the explicit `ALLOW_INSECURE_HTTP=1` public-IPv4 + plain-HTTP escape
  hatch: validates required secrets, installs
  Caddy + a hardened `ccremote` systemd service, keeps the app/venv root-owned
  and read-only to that service account, updates a marked cc-remote
  site block without overwriting unrelated Caddy sites, manages bounded global
  HTTP read/write/header limits, validates the merged config, and explicitly
  restarts both services. If the new relay fails to restart or become ready,
  the venv, Caddyfile, and relay unit are restored as one transaction and the
  previous relay is health-checked.
  Run as `sudo bash deploy/setup-vps.sh your-domain.com` for TLS, or set
  `PUBLIC_ORIGIN=http://your-public-ip` and `ALLOW_INSECURE_HTTP=1` before
  running `sudo bash deploy/setup-vps.sh your-public-ip`. The insecure mode
  still keeps the relay on loopback behind Caddy, but Caddy listens on port 80
  without TLS. Run the script after following the
  main README's maintenance-window publish flow: upload to a user-owned staging
  directory, stop the relay, then `sudo rsync --delete` into the root-owned
  `/opt/cc-remote`. Keep the existing `.env` and `.venv` excluded from that
  promotion. The script builds and import-checks a new venv before switching,
  then restores the previous venv if restart/readiness fails.
- `Caddyfile` — reverse proxy + auto Let's Encrypt TLS (`wss://domain/ws` →
  `127.0.0.1:8765`) plus an early 4 KiB login-body limit. Replace
  `cc-remote.example.com` with your domain.
- `Caddyfile.insecure` — explicit plain-HTTP public-IP template selected only
  when `ALLOW_INSECURE_HTTP=1`; it omits HSTS and permits `ws://` in CSP.
- `cc-remote-relay.service` — systemd unit for the relay on the VPS.
- `cc-remote-wrapper.service` — systemd unit for the wrapper on your machine
  (edit `User` + paths first). It reads root-only
  `/etc/cc-remote/wrapper.env`, hides that file and any legacy repository
  `.env` from model descendants, and disables core dumps.
- `env.relay.example` / `env.wrapper.example` — environment templates for each
  side. Install the wrapper template as root:root mode 0600 at the path above.

Protocol v10 is a coordinated upgrade: publish the Python package and freshly
built `web/dist` together in the documented stop window, then restart relay and
wrapper. The strict protocol gate is intentional and mixed protocol versions will
not communicate. `setup-vps.sh` also rejects a missing/old web build manifest.
For a manual upgrade, stop the wrapper before stopping the relay; start the v10
relay first and the v10 wrapper last.

## Security (short version)

The relay is exposed publicly; `LOGIN_PASSWORD`, `SESSION_SECRET`, and
`WRAPPER_TOKEN` are the authentication secrets. Claude defaults to
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
