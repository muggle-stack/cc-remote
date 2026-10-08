# deploy/

Reference files for production deployment (public VPS relay + wrapper on your
machine). Step-by-step installation paths are in
[安装与升级](../docs/installation.md) / [Installation and upgrades](../docs/installation_en.md).
Product features and the engine comparison live in the [main README](../README.md).

## Deployment contract for automation

The portable agent entrypoint is
[`.agents/skills/cc-remote-deploy/SKILL.md`](../.agents/skills/cc-remote-deploy/SKILL.md).
Both `AGENTS.md` and `CLAUDE.md` link it for clients without automatic skill
discovery. This document remains the deployment source of truth.

This directory is the deployment source of truth for humans and automation.
Machine inventory is deliberately external: host aliases, usernames, domains,
addresses, home directories, and credentials belong to the operator's
environment, not this repository. Replace documented placeholders only with
values the operator supplied or that were read from the existing installation;
never guess them.

Before changing a live service:

1. Inspect the source worktree, target installation, current release, service
   manager, and health. Preserve unrelated changes; do not normalize a dirty
   worktree or silently replace a custom installation layout.
   Apply [backup retention](#deployment-backup-retention) before creating another
   deployment backup or staging copy.
2. Select the matching supported path. Prefer a tested source snapshot for
   current features using the [source deployment guide](../docs/installation_en.md#source-install).
   Use `install.sh` when the operator selects a published release that includes
   the requested features; a latest tag can lag the maintained source branch.
   An existing nonstandard installation must retain
   its established service ownership and configuration boundaries rather than being overwritten
   with a first-install template.
3. Run the complete gate in `AGENTS.md`, build `web/dist`, and validate the
   Python/Web protocol pair with `validate_protocol_bundle.py`.
4. Freeze those tested bytes once. Every Relay, Web client, and Wrapper in the
   maintenance window must come from that same snapshot or coordinated artifact
   set. Do not rebuild independently on different hosts.
5. Stage and validate every target before activation. Keep `.env`, device
   authority, profile configuration, private databases, and other runtime state
   outside immutable release trees. Never upload secrets as part of a source
   snapshot.

For Claude, follow [session-service installation and acceptance](../docs/claude-session-service.md)
before planning a non-interrupting Wrapper upgrade. Keep an existing SDK service
outside the Wrapper activation transaction. First migration, remaining
in-process turns (including private `/btw` forks), and deferred queries must
drain first; daemon readiness alone does not prove an old child was adopted.
If the local service protocol or pinned SDK changes, stage first and defer the
service's own restart until native work and pending callbacks have finished.

Activate a coordinated protocol change in the order documented by the current
protocol note below: stop incompatible old Wrappers, activate Relay + Web as one
transaction, then activate/start every Wrapper and hard-refresh clients. Use the
repository installers' immutable `releases/` plus atomic `current` switch; never
overlay the live tree with `rsync --delete`. If Wrapper state requires a schema
snapshot, create it while the Wrapper is stopped and keep it with the previous
release.

A command that loses SSH, terminal, or cc-remote connectivity has an **unknown
result**, not a failed result. Inspect the exact service/job, `current` target,
logs, health endpoint, PID, and restart count before retrying. Never start a
second installer merely because the first caller stopped receiving output.
A Wrapper must not be its own only deployment controller: activate it from an
independent terminal/SSH connection or from exactly one OS-owned one-shot job
that can finish after the old Wrapper exits.

Success requires all of the following: the expected immutable releases are
active, Python and served Web build metadata report the same protocol/product,
services have stable PIDs without restart loops, the public health endpoint is
healthy, expected Wrappers reconnect, and recent logs contain no new fatal
errors. These checks must pass **after the final cleanup**, not just before it.
Installations using Codex Code must also verify the
[shared CLI control plane](#codex-code-shared-control-plane-acceptance);
an online Wrapper alone does not prove bidirectional CLI access.
After these checks, offer the [optional Codex App attachment](#optional-codex-app-attachment)
on eligible desktops. Its consent/availability is reported separately and never
turns a healthy core deployment into a failure.
On failure, use the installer-owned rollback or the retained previous
release and matching state snapshot. Never prune the active transaction's
rollback set during activation or recovery; remove older unreferenced generations
beforehand and finalize retention after coordinated acceptance as specified below.

### Deployment backup retention

Agent-led deployments retain **the active installation, one complete previous
rollback generation, and every still-referenced runtime dependency** on each
in-scope host. Runtime dependency protection always overrides the generation
count; an older release is not disposable merely because two newer ones exist.
A generation includes the matching release code/runtime, configuration copies
and private state snapshot needed to restore it; these are one recovery set,
not separate allowances for multiple historical copies. An intact immutable
release can serve as the code backup without another archive of the same tree.

1. Before creating the next backup or staging copy, identify the active release,
   the newest complete known-good rollback set, and any unresolved deployment
   transaction. Remove only confirmed older, superseded deployment backups and
   unused duplicate uploads/archives using `deploy/cleanup.py`. Determine
   generations from release and transaction records, not filename age alone.
   Preview an explicit private inventory before applying it; do not generate a
   separate retention script or bypass a deferred result with `rm`/`rmtree`.
2. Create and validate the new pre-upgrade snapshot using the normal transaction
   procedure. Keep the existing valid rollback set until coordinated acceptance
   succeeds. Temporary coexistence during this transaction must not become
   permanent retention; never delete the sole usable backup to make room for
   an unverified replacement.
3. After all protocol tiers pass acceptance and the transaction is committed,
   retain only the version just superseded and its matching recovery files.
   Remove the older rollback generation and completed, unneeded staging/upload
   copies. Quarantine candidates, repeat acceptance while their bytes remain
   recoverable, then delete and verify again. On failure or unknown outcome,
   preserve the exact transaction's recovery set and settle it before further
   cleanup or deployment attempts. Deployment is complete only after this final
   acceptance; earlier health checks are provisional.

Do not delete native transcripts, credentials, current private state, project
files or unrelated user backups under this policy. A release still referenced
by a running service (including the independent Claude service), a process cwd,
an executable, an open/mapped file, a shared venv, an active job or the retained
rollback set is a live dependency. Checking `ps ... args` alone misses processes
whose argv is independent of their cwd. Include dormant service definitions and
configuration-dependent runtime paths explicitly as protected dependencies too.
Incomplete process visibility or uncertain provenance defers cleanup. Do not
stop a daemon or active work just to meet the retention count.

Codex lifecycle commands use the service user's stable home directory, never a
Wrapper release, as their working directory. Existing daemons are not restarted
to apply this: retain any old release they still use until their normal lifecycle
has moved them away. This startup rule and the cleanup checks protect different
boundaries; neither replaces the other.

Report retained rollback paths, deleted generations, removed allocated bytes
and any deferred paths. Allocated bytes are not a measurement of free-space gain
(for example, shared filesystem blocks may remain in use). Moving old copies
into another backup directory or Trash
does not reclaim disk space or satisfy this policy.

Current-source **managed Release installers** apply this policy automatically
after activation and registration. `release_retention.py` records the exact
artifact identities in `.release-generations.json`, captures the previous service
and external configuration in `rollback-config/`, and binds the matching private
state snapshot in `rollback-data/`. It builds a bounded inventory and delegates
all deletion to `cleanup.py`, inheriting the installation lock. Fresh acceptance
checks public Relay/Web identity, stable service identity, Wrapper connectivity,
snapshot integrity and live Codex configuration as the service user. Process,
symlink and dormant service/configuration dependencies override retention limits.
Linux discovery reads the installed systemd system/global/user load paths and
the actual `UnitPath` of running managers, including runtime and generated units.
Missing tools, unreachable managers or unreadable directories defer cleanup;
there is no fallback to a partial hard-coded directory list.
Environment-file discovery handles spaced assignments and continued lines,
preserves literal spaces in filenames, and retains dependencies even across
overrides/resets. Unresolved specifiers, wildcards or ambiguous quoting/escapes
defer cleanup instead of treating an optional file as absent.
Literal paths in service definitions, launchd plists and external environment
files are resolved component by component, including multi-hop aliases and
intermediate links owned by an old release. Those releases retain their runtime
dependency closure. Broken/cyclic links, unreadable paths, bare executable names
or ambiguous path expressions defer cleanup; discovery never executes a service
or recursively crawls arbitrary external directories.

Failed/incomplete acceptance or insufficient process visibility retains files and
warns without rolling back an already committed installation. A same-version
`cc-remote update` retries deferred retention with fresh checks, without restarting
services or sending model messages; `--check` is read-only. An interrupted
installation's prepared record requires explicit inspection, not automatic retry.
Unknown backups/uploads and external legacy roots remain outside automatic
deletion and still require the reviewed agent inventory below. Configuration
backup `files.json` records each original destination, absence, owner and mode;
keep it with that generation for administrator-controlled recovery.

**Published v4.0.9 does not include automatic retention or Linux legacy migration.**
These take effect with an installer bundle containing the new code, including
upgrades initiated by older management CLIs. Source/manual deployments continue
to use reviewed cleanup inventories and their existing activation transactions.

#### Repository cleanup command

`cleanup.py` ships in both role bundles. It acquires the same `.update.lock` as
the installers, verifies the bound `current` link and completed transaction
records, and examines process cwd/executable/open-file references with `lsof`
plus argv references with `ps`. It also traces transitive symlink dependencies of
retained trees and live candidates, including shared venvs. Broken links,
unreadable paths or an exceeded scan bound stop cleanup. `lsof` is required;
missing tools, warnings and incomplete scans stop the
operation. User-owned private installations inspect that user's processes;
shared/system installations must run as root to cover every service user.

Create a mode-0600 JSON inventory **outside the source repository**, from the
actual installation and its transaction records. All paths must be absolute,
canonical, and owned/maintained within the operator's deployment scope. The tool
does not discover unknown external transactions, infer generation age, or select
files for you. List all relevant transaction records; if any outcome is unresolved,
settle it first and retain its recovery set. Never list credentials, native
sessions, live private state, project files or unrelated backups as candidates.

Inventory schema (replace the illustrative paths):

```json
{
  "schema": 1,
  "installation_root": "/opt/cc-remote",
  "current_release": "/opt/cc-remote/releases/release-new",
  "rollback_paths": [
    "/opt/cc-remote/releases/release-previous",
    "/opt/cc-remote/rollback-data/before-new"
  ],
  "protected_paths": [],
  "cleanup_roots": ["/opt/cc-remote/releases"],
  "candidates": ["/opt/cc-remote/releases/release-old"],
  "transactions": [
    {"path": "/opt/cc-remote/transactions/activation.json", "field": "phase", "equals": "committed"}
  ],
  "checks": [
    {"name": "release and public health", "argv": ["/absolute/path/to/read-only-health-check"], "cwd": "/"}
  ]
}
```

`rollback_paths` describes one complete recovery generation, including its
configuration/state snapshots; it must contain exactly one previous release.
Use `protected_paths` for additional service/runtime dependencies. Candidates
must be direct children of explicitly named, dedicated `cleanup_roots`; nested
candidates, symlink boundaries and mounts are rejected. Transaction expectations
accept only completed states (`committed`, `complete`, `deployed_verified`,
`ready`), and the records must remain unchanged throughout cleanup. Unknown
layouts remain retained until their provenance is established.

`checks` are operator-reviewed **read-only** argv arrays (no shell interpolation),
run before retirement, after quarantine, and after removal. They must cover the
actual role's release identity, stable services and health. For a Codex Wrapper,
also include a fresh configuration check, executed **as the Wrapper service user**:

```bash
<wrapper-python> deploy/check_codex_readiness.py \
  --home <service-home> --release <active-release> --after <activation-epoch> \
  --live-config --wait 0
# Add the installation's existing --plist or --env-file selector when needed.
```

On a root-managed Linux cleanup, use `runuser`/`sudo -u` in that check's argv to
select the real service user. `--live-config` sends only initialize and
`config/read` to existing account sockets: it neither starts a daemon nor
creates/resumes a thread or sends a model message, and never prints configuration
contents. It catches a daemon that accepts connections but can no longer load
configuration. Verify an existing idle session's Wrapper status route separately
as part of final acceptance; do not send a model turn without authorization.

```bash
<python> deploy/cleanup.py /private/path/cleanup-inventory.json
<python> deploy/cleanup.py /private/path/cleanup-inventory.json --apply
```

Preview does not run acceptance commands or remove artifacts. Apply rescans live
references immediately before each rename and permanent deletion. Candidates are
temporarily renamed beside their original path; a failed quarantine check restores
the intact directory. A new live reference defers deletion and restores that path.
Checks are observations, not a lock on arbitrary external programs: operators must
not launch jobs against retired paths during cleanup.

The private `<installation-root>/.cleanup-transaction.json` records intent before
each mutation. Exit 0 means success (or preview), 2 means applied cleanup completed
with retained/deferred artifacts, and 1 means failure. A lost connection, partial
deletion, or failed final check requires inspection of this exact journal before
retrying. Do not remove the journal or rerun the command to hide an unknown result.
Quarantine is temporary transaction state, not another retained backup or Trash.

### Deployment entrypoints

- `install.sh` — versioned GitHub Release bootstrap. It requires an explicit
  `relay` or `wrapper` role, detects OS/CPU, downloads that one role archive,
  verifies its `SHA256SUMS` entry before extraction, rejects unsafe archive
  paths, and then invokes the in-bundle installer. It never pipes a network
  response into a shell.
- `cc-remote update` — local management command registered by the role installers
  (`scripts/cc-remote`, `cc_remote/update.py`, `install_cli.py`). It discovers only
  registered installs carrying non-secret `installation.json` metadata, downloads
  and validates one stable role bundle, then calls its existing role installer.
  `--check` performs no activation; `--version` selects an exact published version.
  Both roles on one host require `--role`. Protocol changes require a coordinated
  maintenance window and `--allow-protocol-change`. SDK/service-contract changes
  defer to the Claude service migration procedure. The installer inherits the
  update lock so a disconnected caller cannot accidentally start a second update.
  The command must run outside the managed Wrapper/Relay process tree. Device
  updates first verify the paired Relay; an already-current compatible Relay is
  skipped. Otherwise `--relay-ssh` selects an existing administrator's SSH target
  (saved in private `update.json`). After verifying its managed domain, an OS-owned
  systemd job invokes the remote role installer. The private upstream transaction
  records that exact job before launch; an unknown outcome is inspected on retry,
  never blindly resubmitted. Public readiness precedes device activation. Pairing
  credentials do not authorize this operation. Other devices update individually.
  The new Wrapper role installer repeats upstream verification before stopping
  the local service, covering the first upgrade launched by v4.0.1's older
  updater. `CC_REMOTE_RELAY_SSH` supplies an existing SSH admin target to that
  older caller; an interactive terminal can request it when missing. First
  pairing is separate from this existing-installation upgrade path.
  Explicit Mac registration can bind an existing immutable root and LaunchAgent;
  future installs preserve that layout and service identity. The command does not
  restart the independent Claude service, automatically adopt source/Docker
  layouts, or automate downgrade rollback. Recorded managed generations use the
  retention procedure above.
  Direct role installers use the same per-installation `.update.lock` before
  reading rollback state or changing services. They validate the inherited file
  descriptor from a managed update; an environment marker cannot bypass the lock.
- `linux_service.py` / `wrapper_exec.py` — Linux Supervisor adapter for the same
  Wrapper installer/update transaction. Explicitly bind the root, Unix control
  config, program/file, user, HOME and external environment/device files. Require
  root-owned non-replaceable code/config paths. Never infer arbitrary launchers:
  first adoption requires `--adopt-supervisor` and operator-reconciled environment
  selectors. Only replace/reload that program; preserve other sections and the
  independent Claude service. Preflight supports older Supervisor RPC responses
  (including 4.2.1/4.2.4) without the directory/uid fields added in 4.2.5. Always
  require the native `reloadConfig` comparison against active groups to report
  no changes for the bound program; a reread does not apply those changes.
  A failed fresh installation removes its newly
  loaded Supervisor group after stopping it and removing the new definition;
  previously configured programs remain registered during rollback.
  Snapshot selectors, Relay preflight, readiness and
  retention use that same binding. See the complete
  [Supervisor installation/adoption procedure](../docs/installation_en.md#linux-supervisor-installation-and-adoption).
  Partial process visibility defers retention; it is not permission to delete
  container releases. Container recreation/persistence remains runtime-owned.
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
  `current`, installs a per-user LaunchAgent, root-managed systemd unit or
  explicitly selected Supervisor program, and
  restores the previous release/service definition on failure. The installer
  requires and explicitly selects the service user's daily
  `~/.local/bin/claude`; it never silently falls back to the SDK-bundled CLI.
  After activation, `check_codex_readiness.py` reads a fresh, release- and
  process-bound result from Wrapper startup. Codex is optional: missing or
  incompatible CLI/account connections produce a separate warning instead of
  rolling back an otherwise healthy Claude/Wrapper installation.
  An existing Linux immutable system-service installation can explicitly migrate
  with `--adopt-root /absolute/legacy/root --user USER`. `adopt_wrapper.py` checks
  its actual user, command, ownership, environment layout and absence of drop-ins
  before activation. The standard `/opt/cc-remote-wrapper` destination must not
  already be installed. The old root is preserved; external credentials and
  service policy survive both migration and subsequent upgrades. A failure restores
  the old service and state rather than registering an incomplete destination.
  Unsupported layouts require explicit reconciliation, not a first-install
  overwrite. Root privileges are required; a narrowly authorized custom activation
  helper is not permission to bypass the system installer or its ownership checks.
- `prepare_wrapper_stage.py` — unprivileged preflight for an existing manual
  immutable-Wrapper topology. It reuses an active venv only when the dependency
  lock and Python pin are identical; otherwise it builds a platform-local venv
  with the pinned uv/Python, hashed binary wheels, and copy link mode. It
  validates imports plus the Python/Web protocol pair and writes a bound stage
  manifest for the separate privileged activation step. It never switches
  `current` or restarts a service.
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
  `cc-remote.example.com` with your domain. The application CSP allows HTTPS
  images from the exact GitHub hosts listed in the template, including the
  dedicated attachment redirect bucket, but not arbitrary external images,
  scripts, or fetch connections. The HTML preview runner remains isolated.
  Audio previews use bounded local Blob URLs permitted by `media-src blob:`.
  Image/media-policy changes require the managed Caddy configuration to be updated
  through the VPS activation transaction; replacing the Web bundle alone is
  insufficient. Do not replace the host allowlist with `https:` or wildcards.
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
- `Dockerfile` / `docker-compose.yml` / `env.relay.docker.example` — the same
  relay release as a container (build `web/dist` in a Node stage, install the
  hash-locked wheels, run as the `ccremote` user). See the container section
  below; this is an alternative to the systemd + Caddy path, not a fork of it.
- `nginx-reverse-proxy.conf.example` — a WebSocket reverse-proxy front for
  hosts that already run nginx instead of the managed Caddy. Loopback-only
  requirement is documented in the file header.
- `work_registry_snapshot.py` — snapshots provider-local Work SQLite databases
  through SQLite's backup API plus the complete private Claude/Codex profile
  migration transaction, restores matching pre-release data before an older
  wrapper is restarted, and verifies both engines' Work ownership backfills.

Protocol v74 is a coordinated upgrade: publish freshly built Relay/Web and
Wrapper artifacts from the same tagged commit. The strict protocol gate is
intentional and mixed protocol versions will not communicate. `setup-vps.sh`
rejects a missing or mismatched web build manifest. Stop the wrapper first;
activate the v74 relay/web release; then start the v74 wrapper.

The wrapper installer treats local Work data and versioned private control state
as part of the release
transaction. It stops the existing service, writes a private snapshot below
the install root's `rollback-data/`, starts the new release, and refuses the
activation unless the Claude and Codex Work schemas and all legacy profile
ownership rows are ready. On failure it stops the new process, restores both
SQLite images and the matching private profile state, then restores and starts
the previous code. If data restoration fails, it leaves the
wrapper stopped instead of running old code against a new schema. A manual or
legacy-layout deployment must use the same order: stop the wrapper, run
`work_registry_snapshot.py snapshot` from the new staging tree, activate and
verify v74, and retain that snapshot with the previous release. To roll back,
stop v74, run `work_registry_snapshot.py restore`, then switch and start the old
release. Never copy only `registry.sqlite3` while the wrapper is live because
committed state may still be in its WAL file. Restoring a pre-release snapshot
also restores pre-release Work metadata: sessions, projects, or schedule state
created after activation will no longer be registered (their private files are
not deleted). Use this for immediate failed activation; after normal use,
prefer a roll-forward fix unless that metadata rollback is explicitly accepted.

Snapshot format v3 includes an explicit allowlist of Claude/Codex controls,
turn leases, pins, aliases, fork/BTW records, plans, presentation receipts,
Viewer associations, and both pending/completed profile journals. An absent
file is recorded too and removed on rollback if activation created it. Codex
checkpoint journals include their directory layout and local object data:
profile migration renames those directories, so manifest-only backup is not
sufficient. This private archive rejects symlinks and special files and is
bounded to 65,536 entries / 8 GiB of payload; exceeding a limit aborts before
activation, not with a partial usable snapshot. Restoring checkpoints retains
the displaced tree in `.checkpoint-displaced-*` under the private state
directory for recovery. Account configuration, credentials, native transcripts,
and project files are not part of this snapshot. Retain snapshots locally;
never publish them as release artifacts. Legacy v1/v2 snapshots remain
restorable only within their original, narrower scope; they cannot provide
complete rollback for a new profile migration.

## Container deploy (Docker) and the nginx alternative

For the optional static remote Viewer feature, also read
[`docs/remote-viewer.md`](../docs/remote-viewer.md). Default Bridge mode reuses
the existing origin, including explicitly allowed HTTP/IP access; no extra DNS/TLS
is needed. Include the runner asset and both Viewer WebSocket routes; preserve
the relay's HTTP sandbox/CSP on `/__cc_viewer/bridge/*` using the updated proxy
template. Home-directory page discovery is enabled by default; opt out with
`CC_REMOTE_VIEWER_HOME_PREVIEW=0` in the Wrapper environment. It verifies explicit
HTML references or owned Python static listeners, not arbitrary URL proxies;
automatic pages stay in their session lists. Existing manual publications are
preserved. Check one actual page on each resource device, not just the catalog.
Optional Isolated mode
still requires wildcard TLS and a narrow frame-src addition. Never serve raw
Viewer scripts on the main application origin. Verify real mobile access before
reporting this optional feature as deployed.

The official relay install is a systemd venv staged by `setup-vps.sh` behind a
managed Caddy. Two alternative topologies are supported for hosts that already
manage their own services or TLS:

**Docker container.** `Dockerfile` builds the same relay release as a
multi-stage image: the Node stage compiles `web/dist` from source, the Python
stage installs the same hash-locked wheels `setup-vps.sh` pins and runs
`python -m cc_remote.relay` as a non-root `ccremote` user. From the `deploy/`
directory:

```bash
cp env.relay.docker.example env.relay   # then fill in the secrets
docker compose up -d --build
curl https://your-domain/healthz        # -> {"ok":true,...}
```

The compose file publishes the relay only to the host loopback
(`127.0.0.1:8765`) and mounts a named volume for the SQLite device/Web Push
state. Public TLS + WebSocket termination stays with your existing front.

**nginx instead of Caddy.** `nginx-reverse-proxy.conf.example` terminates TLS
and proxies the `/ws` WebSocket to `127.0.0.1:8765`. Keep it loopback-only:
the relay trusts forwarded transport metadata only from loopback peers.

**Mainland-China mirrors.** The Docker build defaults to PyPI.org. Behind the
GFW, build with Aliyun as the primary index and TUNA as the fallback (both
carry the sdist-only `http-ece` wheel):

```bash
docker build -f deploy/Dockerfile \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  --build-arg PIP_EXTRA_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t cc-remote-relay .
```

## Native terminal coordination

- **Claude Code:** run `claude` directly for the untouched official process;
  Remote treats direct CLI, Desktop, and Agent View ownership as read-only.
  Explicit takeover may gracefully terminate the exact same-user Claude process
  with SIGTERM and then resume through the SDK, but it never kills the terminal
  shell, escalates to SIGKILL, or silently adopts a process.
- **Codex Code:** `CC_REMOTE_CODEX_DAEMON=auto` requires Codex's official shared
  app-server daemon. Set it to `off` only to force the legacy private stdio path.
  Optional multi-account installs provide either inline
  `CC_REMOTE_CODEX_PROFILES_JSON` or a private
  `CC_REMOTE_CODEX_PROFILES_FILE`; the macOS LaunchAgent defaults the latter to
  `~/.cc-remote/codex-profiles.json`. Each unique `CODEX_HOME` owns a daemon.
  When a sibling home has no duplicate standalone payload, first bootstrap
  safely reuses the verified primary managed CLI through a profile-local
  `current` link; account data and daemon sockets remain isolated.
  Leaving both empty preserves the exact single-account path and UI.
- **Work:** both engines stay on private per-process control planes regardless
  of the Code settings. Codex Work sessions and schedules may select any
  configured profile; the local registry freezes that ownership across retries
  and default-profile changes.

### Codex Code shared control plane acceptance

The required topology is **CLI → the same official app-server ← Wrapper**,
not merely two processes reading the same rollout. Check every enabled Code
account separately; never merge accounts into one `CODEX_HOME` to get sharing.
This does not apply to Work's deliberately private app-server.

1. Resolve the actual daily CLI (including shell aliases/launchers), the
   Wrapper's selected executable (`CODEX_BIN` if set), service user, and each
   account's effective `CODEX_HOME`. Compare real paths, not command names.
   A Codex CLI `--profile` is a configuration profile, not cc-remote's account
   home selection. Do not read or copy auth files. Both selected CLIs must
   support `app-server daemon` and `app-server proxy`; an npm installation
   alone neither proves nor disproves that capability.
2. Keep `CC_REMOTE_CODEX_DAEMON=auto` for sharing; an explicitly configured
   `off` survives upgrades. Wrapper startup reuses each account's official
   daemon or invokes the native idempotent `start` if it isn't reachable. It
   does not bootstrap, restart, replace a lagging daemon, or toggle Codex's
   separate cloud remote-control setting: those commands can interrupt native
   clients. Local TUI/proxy sharing uses the Unix listener without cloud remote
   control. Do not add a second daemon or another startup service.
   If sharing is unavailable, Code reports a connection error instead of silently
   starting a private stdio server. Explicit `off` retains the legacy private path.
   Startup compares the CLI found on the service's PATH with Wrapper's selected
   binary, checks account socket and CLI/server versions, and initializes their
   official WebSocket proxy connections without creating a thread or model turn.
   `Codex profile shared transport ready` and the private rebuildable
   `codex-readiness.json` receipt describe this transport check. The Release
   installer checks its source release, fresh timestamp and live process identity
   before displaying the result. A failed or missing result never passes
   sharing acceptance, even if the Wrapper itself was installed successfully.
   This does not verify an operator's aliases, launch arguments or an already
   open TUI. Complete step 4 separately; do not relabel transport readiness as a
   verified ordinary terminal connection.
3. As the same OS user, compare the following **read-only** probes using the
   resolved account home and both executable paths (replace placeholders):

   ```bash
   CODEX_HOME="<account-home>" "<daily-codex-bin>" app-server daemon version
   CODEX_HOME="<account-home>" "<wrapper-codex-bin>" app-server daemon version
   ```

   Require a running daemon, compatible CLI/app-server versions, and the same
   resolved `socketPath`/managed server identity. For an already connected Code
   session, also check the Wrapper's actual `app-server proxy --sock …` target
   and successful connection, not just that a socket file exists. With npm,
   Node and its native Codex child are one launch chain, not two independent
   clients. Do not expose complete process environments or user prompts in logs.
4. Verify the operator's normal `codex resume <session-id>` workflow actually
   connects to that same endpoint. Use an operator-approved idle test session
   or an already connected terminal; do not resume a busy production session
   for testing. Current Unix-socket connection evidence or structured app-server
   connection records tied to that CLI's lifetime can establish the route;
   old log rows, matching home paths and `active writer` errors alone cannot.
   Confirm a shared session does not become read-only solely because its CLI is
   open. A live two-direction prompt test spends model tokens and requires
   explicit authorization; otherwise report transport verification separately
   from an untested live-message round trip.

The official CLI supports explicit endpoint selection with
`codex resume --remote unix:// <session-id>` for the selected home's default
socket, or `--remote unix://<absolute-socket-path>` for a specific endpoint.
See [the official connection documentation](https://learn.chatgpt.com/docs/app-server#connect-the-cli-terminal-ui).
This is a diagnostic/explicit connection option, **not a mandatory suffix for
all resumes**. If explicit connection works but plain resume does not, sharing
via automatic discovery has not passed acceptance: compare the actual CLI
build, home, endpoint and startup/connection errors. Do not assume all builds
auto-attach simply because a daemon is running, or mask the difference by
silently changing the user's shell alias.

The inspected official CLI 0.154.0 automatically probes its account's default
socket for ordinary launches. Additional launch configuration (for example
`-c`, a config profile, strict config or a custom exec-server) can select an
embedded server instead; a failed automatic connection can also fall back.
See the [versioned native startup implementation](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/tui/src/lib.rs).
Installers preserve these user choices rather than rewriting aliases or adding
`--remote` to every invocation. Version mismatches are reported so the operator
can finish active work before updating/restarting Codex.

An existing private CLI writer is not migrated into the daemon by starting it
later. Let the operator finish and exit that CLI normally, then reconnect to
the verified shared endpoint. Never kill an active CLI, delete locks/rollouts,
disable ownership checks, or force takeover to make this check pass. Report any
unverified account or stdio fallback as a remaining coordination issue, even
when Relay/Web health is green; do not claim bidirectional deployment complete.

### Optional Codex App attachment

After core deployment and Codex CLI sharing checks, inspect each in-scope
Wrapper desktop for an installed official Codex App. This is a **post-deploy
offer**, not an installer side effect or a condition of Relay/Web health.

- macOS and Linux desktops have separate attachment paths. The
  `cc_remote.codex_desktop` helper is macOS-only; Linux uses the
  [account-scoped Linux launcher](../docs/codex-desktop-linux.md).
  Do not install an App on a headless server, crawl unrelated machines, or treat
  a PWA named cc-remote as Codex App. On macOS inspect bundle metadata
  (`com.openai.codex`); on Linux inspect the official package and desktop entry's
  actual executable. The Linux App may be named ChatGPT and includes Codex mode.
- If no supported App is installed, skip the offer. If a previously approved
  shared entry is still verified for the selected account, preserve it without
  prompting again. A different account or changed setup needs a new choice.
- Otherwise ask, in the user's language, for example: “检测到本机装有 Codex App。
  要让它与这个账号的 CLI、cc-remote 共用同一个会话服务吗？这会新增独立的
  Codex Shared 启动入口，原 App 不改动；不接入也不影响 cc-remote。”
  Explain that the Desktop launch override is experimental and version-dependent.
  On multi-account hosts, confirm which account/home to use; do not silently
  select the first profile or change the default account.
- A decline or no answer means no App/launcher/configuration changes. Record
  `declined` or `pending consent` in the handoff, not a failed core deployment.
  Respect that choice on follow-up deploys unless the user changes it; do not
  invent a new tracking database solely to remember this offer.
- An explicit request to attach the selected account already supplies consent;
  do not ask the same question again while carrying it out.
- After consent, follow the complete runbook for
  [macOS](../docs/codex-desktop-launcher.md) or
  [Linux](../docs/codex-desktop-linux.md), including
  preflight, installation, live transport checks, user-controlled quit/reopen and
  removal. Use that checkout's helper or documented launcher template; do not
  download unrelated scripts or patch the official App. Recheck the installed
  App build's launch transport rather than treating an internal environment
  variable as an official cross-version guarantee.
- App-control MCP tools are a **separate opt-in**. Describe that a prompt from
  CLI/cc-remote could then operate the desktop App, subject to native approvals.
  Only after that choice, follow [the MCP guide](../docs/codex-app-tools.md).
  Inspect the App's bundled native plugin before adding an adapter. The custom
  macOS adapter does not implement Linux discovery.

Never force a running private App into sharing, kill a CLI/daemon, merge account
homes, modify the original App, relax signatures, or use global environment
overrides to pass this optional check. App attachment is not part of the
three-tier wire protocol activation and must not restart otherwise healthy
Wrapper/Relay services. Report core health, CLI sharing, App sharing and optional
tools separately; a visible launcher or `queued` UI action is not proof that a
panel opened or that full three-client messaging was tested.

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
