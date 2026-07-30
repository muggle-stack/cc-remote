# CLAUDE.md — cc-remote

Guidance for Claude Code (and human contributors) working in this repo. User-facing setup/run docs live in [README.md](README.md) / [README_en.md](README_en.md).

## What this is
Self-hosted remote control for Claude Code and Codex: a phone/browser drives a
local `claude` or `codex` session through a WebSocket relay. Two independent links:
- **model link** — the local CLI → whatever its own settings/authentication point
  at. cc-remote never touches model credentials or the model API.
- **control link** (this repo) — client ⇄ relay ⇄ wrapper ⇄ Claude Agent SDK /
  Codex app-server ⇄ local CLI. Native CLI ownership is detected and mirrored
  separately.

## Critical constraints / traps
- **Drain footgun**: after `ClaudeSDKClient.interrupt()`, the SDK does NOT kill
  the session — the current turn's stream still emits a terminal
  `ResultMessage(subtype="error_during_execution")`. You MUST keep consuming
  `receive_response()` until that ResultMessage before the next `query()`, else
  stale deltas from the interrupted turn bleed into the new turn. The wrapper
  handles this structurally: one `async for` per turn runs through the interrupt
  to the terminal ResultMessage; state only returns to `idle` (and the next
  query is only accepted) after that break. Reject-while-busy prevents a second
  query racing the drain.
- **cwd must match resume**: a session's jsonl lives at
  `~/.claude/projects/<cwd-with-/-as->/<uuid>.jsonl`. `ClaudeAgentOptions.cwd`
  MUST equal the original session's cwd or `resume` can't find it.
- **SDK pinned to `claude-agent-sdk==0.2.128`**: message-type shapes and the
  interrupt/drain contract can shift between minor versions. Re-run the
  interrupt+drain verification after any upgrade (`SdkHandle.preflight()` guards
  the major/minor at startup).
- **Claude Code is the user's daily CLI, not the SDK bundle**: the wrapper
  defaults `CLAUDE_BIN` to `~/.local/bin/claude` and passes that path explicitly
  to the SDK. An empty value keeps this default; only another absolute path may
  override it. Keep that CLI updated and signed in before starting the wrapper.
- **`include_partial_messages`** is a `ClaudeAgentOptions` field (set at
  construction, not on `query()`). Streaming events arrive as `StreamEvent`
  (`.event` = raw Anthropic API stream-event dict) — NOT
  `SDKPartialAssistantMessage` (doesn't exist in 0.2.128). Extract
  `content_block_delta` → `delta.text` from `StreamEvent.event`.
- **tool_use is batched, not streamed**: emit one `tool_use` event from the
  assembled `AssistantMessage` (full `input`), never as JSON-fragment deltas.
  Text deltas still stream live via `StreamEvent`.
- **Claude only — don't set `setting_sources=[]`**: we WANT `~/.claude/settings.json` loaded so
  `claude` inherits the model link (`ANTHROPIC_BASE_URL`), model id, and
  `bypassPermissions`. Note: settings.json's `env` block overrides the process
  env, so redirecting the model backend from cc-remote is not possible — it's
  the user's `settings.json` that decides.
- **Auth is URL-secret-free**: the wrapper uses `Authorization: Bearer <token>`
  at WS upgrade. Web clients POST `LOGIN_PASSWORD` to `/api/login` and receive a
  short-lived HttpOnly/SameSite cookie; `/ws` enforces exact `PUBLIC_ORIGIN`.
  When `ALLOW_PRIVATE_ORIGINS=1`, the only additional origins are literal
  private/loopback IPs on `RELAY_PORT`, and their scheme/host/port must match
  the effective request target. Cookie `Secure` follows that trusted request
  transport, never the caller's Origin. Uvicorn trusts forwarded transport
  metadata only from loopback Caddy. Never put tokens in URLs or protocol
  message bodies; logging redacts token/password fields.
- **History scroll anchoring lives in `@tanstack/virtual-core`, not in
  `react-virtual`**: `web/package.json` pins `@tanstack/react-virtual`, but
  `anchorTo` / `followOnAppend` / `scrollEndThreshold` /
  `useAnimationFrameWithResizeObserver` and the estimate→actual scroll
  compensation are all implemented in the transitive `virtual-core` (currently
  exactly pinned by react-virtual itself). Its `defaultShouldAdjust` does NOT
  compensate re-measurements during backward scroll. Any bump of either package
  changes pull-to-load-older feel, so re-run `npm --prefix web run
  test:history-browser` after one. Note also that ChatView's residual-correction
  `useLayoutEffect` is deliberately dependency-free — late virtualizer/image
  measurements settle without a React render, and constraining it to its read
  set reintroduces a full-viewport jump on touch release.
- **Protocol version gate**: current wire protocol v27 is declared by
  `PROTOCOL_VERSION` in both `protocol.py` and `web/src/protocol.ts`.
  `deserialize` hard-rejects a version mismatch, and
  `_Base` is `extra="forbid"`, so ANY protocol change must be deployed to all
  three tiers together (wrapper + relay + web) and the relay restarted — the
  relay imports `protocol.py` and drops frames it can't parse.
- **Device scope is an authorization boundary**: one relay can serve multiple
  wrappers. Every browser command, event, push subscription, and pairing token
  is scoped by `machine_id`; a credential for one enrolled device must never be
  accepted for another. Keep `cc_remote/device.py`, `relay/devices.py`, relay
  routing, and the Web device selector aligned when this contract changes.
- **Multi-session routing key**: the wrapper runs a POOL of resident sessions
  (`WrapperMachine.sessions: dict[key, SessionContext]`, cap
  `MAX_CONCURRENT_SESSIONS`). `ctx.key` is the routing identity = the real cc sid
  once known, else `tmp-<uuid>`. Every emit stamps `sid = ctx.session_id or
  ctx.key`, so a brand-new session's pre-capture frames route deterministically
  (never leak into the focused runtime). Keep `ctx.key` in sync with the pool
  dict key on every re-key.
- **Focus vs re-key (don't conflate)**: switching the viewed session is
  `SessionFocus` (focus only, no disconnect — the previous session keeps
  streaming). A new session capturing its real id mid-turn is `SessionRekey`
  (rename tmp-key→sid), which moves focus ONLY if the client was already viewing
  the temp key. Emitting SessionFocus on id-capture = focus-steal by background
  sessions.
- **Session cwd migration is in-place** (protocol v27): only an idle Codex Code
  session may move. A cold session is first resumed without changing focus;
  then resume the same native thread id under `SessionContext.query_lock`,
  preserve its wrapper-owned deferred queue, and emit `SessionMigrated` without
  changing focus. The target must already be an absolute directory; reconnect
  the old cwd before reporting a failed move. Persist the accepted cwd in the
  private Codex control store and overlay it on cold resumes/session listings:
  `thread/resume.cwd` is live state and does not rewrite the native catalog
  metadata until a later turn materializes that context.
- **Deferred queries are wrapper-owned** (protocol v25): queue/replace submits
  transfer the complete bounded `Query` to the resident `SessionContext`
  immediately. The wrapper waits for the active managed/spontaneous task's real
  terminal boundary and launches the next item without any browser callback.
  Web/PWA code only renders `QueryQueueState`; never reintroduce an idle-driven
  browser drain. Queued contexts (including a worker's pop-to-preflight window)
  are not eligible for pool eviction or deletion. Full prompt inspection is a
  private one-shot read, and edits atomically replace the prompt under the queue
  lock; never put complete queued payloads in the replay ring.
- **External ownership is engine-specific**: a native Claude CLI owns its
  transcript and is mirrored read-only until it exits or the user explicitly
  takes over. Codex Code sessions use the official app-server; shared-daemon
  CLI activity and private Codex App activity are different ownership sources
  and must not be collapsed into one "external process" heuristic. Ordinary
  shared sessions stay on the daemon; only the guarded oversized-resume path may
  select a newer official private app-server for compatibility.
- **History = local projection + materialized summary pages; reconnect = live-tail replay**
  (protocol v24): IndexedDB paints the browser's last projection before network
  validation. `GetHistory(detail="summary")` returns a small canonical turn page
  (newest four, then `before`/`limit` pagination), while the wrapper's rebuildable
  SQLite index avoids retranslating unchanged transcript/rollout bytes. Heavy
  tools, reasoning, process output and oversized final text stay local until
  `GetTurnDetail` expands that exact turn. The relay remains stateless. A fresh
  hello sends lightweight resident `Snapshot`s; reconnect cursors replay only
  the bounded missing live tail. Source fingerprints invalidate appended pages,
  and rollback explicitly invalidates both server and browser projections. These
  reads never spawn/resume an engine or create a model turn.
- **Token-aware residency**: resuming an evicted Claude SDK session may rebuild
  a cold prompt cache, so it only happens on first spawn / re-focus after
  eviction; raising the cap trades RAM for fewer cold re-sends. Codex context is
  owned by the official app-server: cc-remote must page history and use native
  resume/compaction state, never re-upload a whole rollout. Browsing history is
  transcript/rollout I/O and must not create a model turn.

## Module map

### Shared (`cc_remote/`)
- `protocol.py` — pydantic wire schema; all modules depend on it.
  `serialize`/`deserialize` with `v` check; `is_downstream` for seq/buffer.
  Control frames: `SessionFocus` / `SessionRekey` / `GetHistory` / `History` /
  `GetTurnDetail` / `TurnDetail` / `SessionInfo.state`.
- `config.py` — env-driven config (`RelayConfig`, `WrapperConfig`). Real env
  wins; a local `.env` is loaded for development. No hardcoded hosts.
- `log.py` — JSON logging with token redaction; use `logger("...")`.
- `device.py` — `python -m cc_remote.device pair <relay-url> <PAIR-CODE>`:
  enrolls this machine and persists its per-machine wrapper credential.
- `claude_paths.py` — canonical Claude config/transcript paths.
  `CLAUDE_CONFIG_DIR` is a storage-root selector (a different session catalog),
  NOT a provider selector; keep every direct filesystem reader aligned with the
  SDK so a settings-only provider switch never changes the catalog.
- `workspaces.py` — durable SQLite Work registry: only the cc-remote product
  identity and private working directory per Work chat. The engines remain the
  transcript authority, so Work and Code list/delete independently.
- `attachments.py` — decoded attachment limits shared by wrapper validation and
  tests; the WebSocket frame size is capped separately.
- `tui.py` — terminal client that ATTACHES to a wrapper-owned session over the
  relay as just another client (bidirectional sync with the browser). Not the
  native `claude` TUI.
- `claude_remote.py` — `python -m cc_remote.claude_remote`; explicit entry point
  that never shadows the official `claude` executable.

### Wrapper (`cc_remote/wrapper/`) — runs where `claude`/`codex` live
Shared core:
- `machine.py` — the brain, and by far the largest module: session pool
  `dict[key, SessionContext]` + `focused_sid`, per-turn consumers, the drain,
  and command handlers (`_handle_query` / `_handle_steer` /
  `_handle_interrupt`, session lifecycle, `_handle_get_history` /
  `_handle_get_turn_detail`, previews, rollback, goals, status).
- `command_router.py` — the behavior-free command-type → existing handler map;
  scheduling lanes, ownership checks, reliable-command deduplication and ACKs
  stay in `machine.py`.
- `session_ctx.py` — one `SessionContext` per resident session: ring buffer, seq
  counter, state machine, turn task, translator, pending ask futures, emit lock.
- `ringbuffer.py` — monotonic-seq buffer; `replay_from` serves reconnect cursors
  (missing cursor → snapshot, cursor older than the head → truncated replay).
- `transport.py` — outbound-only WS client to the relay (no inbound ports),
  reconnect with backoff, `on_connected` → re-hello. Live sends are best-effort;
  the ring buffer + client replay is the source of truth.
- `history_store.py` — rebuildable SQLite materialized turn pages bound to a
  source fingerprint; discardable without affecting engine state or recovery.
- `session.py` — persists the cc session id keyed by cwd so a restart can
  `--resume` (resume requires the cwd to match).
- `session_pins.py` — durable sidebar pins (a cc-remote preference, never
  written into provider-owned transcripts or databases).
- `sanitize.py` — bound model-originated UI payloads before they enter rings/WS.
- `child_env.py` — keep relay control-plane credentials out of model/tool
  subprocesses.
- `process_scan.py` — cross-platform process identity helpers for both engines'
  ownership scans.
- `ask.py` — in-process MCP server exposing `ask_user` and `set_mode` to the
  agent.
- `engine_capabilities.py` — bounded, display-safe discovery of Skills, Plugins,
  Apps, MCP, and Hooks; never imports settings, credentials, or plugin code.
- `rollback_commands.py` — durable at-most-once journal for destructive rollback
  commands (a browser retry after a wrapper crash must not re-submit).
- `source_fetch.py` — bounded capture of public HTTP(S) sources for Work.
- `work_context.py` / `work_prompt.py` — Work-only context growth and the shared
  prompt policy for isolated general-purpose Work sessions.

Claude:
- `sdk.py` — `ClaudeSDKClient` lifecycle (connect/query/interrupt/receive/
  resume); isolates the version-sensitive `include_partial_messages` call site.
- `stream.py` — `StreamTranslator` (SDK → wire events) plus the history readers
  `translate_history`, `translate_subagent_history`, `transcript_timestamps`,
  `transcript_path`, `extract_session_id`.
- `claude_runtime.py` — version policy + discovery of the effective daily CLI
  (`CLAUDE_BIN`), preflighted before a session starts.
- `claude_external.py` — detect sessions owned by another local Claude process
  (prefer an explicit sid from the command line over transcript growth).
- `claude_controls.py` — private Remote controls and one-time native handoff.
- `claude_forks.py` — durable fork correlation + crash recovery so a retry never
  creates a second child transcript.
- `claude_goal.py` — `/goal` state recovered from the authoritative transcript
  (Claude has no goal RPC; goals are a session-scoped Stop hook).
- `claude_rewind.py` — structured rewind results; conversation rewind lives
  behind a private control protocol, so probe the capability before mutating.
- `claude_broker_handle.py` / `claude_broker_history.py` — adapter and bounded
  transcript lifecycle parsing for an official TUI owned by the local broker.
  Disconnecting a Web session must never kill the user's terminal TUI.

Codex:
- `codex_handle.py` — the Codex analog of `sdk.py`: drives either a private
  `app-server --stdio` process or a short-lived `app-server proxy` connection to
  the shared daemon, behind one async JSON-RPC surface.
- `codex_stream.py` — app-server notifications → rich events. Reasoning
  *summary* is forwarded; raw/encrypted reasoning and terminal stdin are not.
- `codex_daemon.py` — shared daemon discovery/lifecycle (process-global). A
  handle owns only its own proxy/stdio child.
- `codex_external.py` — ownership via another process holding the rollout inode
  open for writing, with turn markers as fallback.
- `codex_sessions.py` — sidebar metadata from the app-server state DB; rollouts
  remain the source for history, cwd fallback, and per-turn settings.
- `codex_models.py` — the app-server IS the model catalog (`model/list` with
  `supportedReasoningEfforts`); never hardcode models or efforts.
- `codex_rpc.py` — bounded one-shot control-plane requests.
- `codex_forks.py` / `codex_worktrees.py` — durable `thread/fork` correlation and
  the wrapper-owned Git worktree the app-server deliberately does not create.
- `codex_checkpoints.py` — client-owned Git checkpoints for undoing turns;
  `thread/rollback` prunes conversation only and leaves the filesystem to us.
- `codex_provider_repair.py` — repair process-local HTTP provider aliases left in
  durable thread state by the oversized-resume compatibility path.

### Local Claude PTY broker (`cc_remote/claude_broker/`)
Same-user Unix socket server holding persistent official `claude` PTYs, driven
by the explicit `claude-remote` CLI — separate from the Agent SDK path.
- `server.py` (socket server) / `session.py` (bounded PTY lifecycle) /
  `client.py` (programmatic client) / `cli.py` (explicit `claude-remote`
  command line, never shadows official `claude`) / `control_store.py` (bounded
  per-session runtime preferences) / `paths.py` (canonical local endpoint).
- `protocol.py` — its own small framing protocol with `BROKER_PROTOCOL_VERSION`
  (currently 2), independent of the wire `PROTOCOL_VERSION`.

### Relay (`cc_remote/relay/`) — stateless, VPS-hosted
- `server.py` — FastAPI `/api/login`, `/ws`, `/healthz`, optional same-origin
  static hosting of the web client.
- `auth.py` — wrapper Bearer credentials + HMAC cookie browser sessions.
- `devices.py` — persistent device enrollment; stores only SHA-256 digests of
  wrapper credentials and one-time pairing codes.
- `pairing.py` — one wrapper slot per `machine_id`, client registration by
  `client_id`, `to=`/broadcast fan-out.
- `forward.py` — bounded per-client queues (item AND byte limits). Exceeding
  either drops the whole connection; deltas are never silently shed.
- `push.py` — durable machine-scoped Web Push subscriptions. Generic notices are
  the default privacy boundary; only an explicit opt-in gets a bounded display
  name and navigation route.
- `log_safety.py` — uvicorn/WebSocket log redaction for auth-bearing data.

### Web (`web/src/`)
- `protocol.ts` — mirror of `protocol.py`; keep `PROTOCOL_VERSION` in sync.
- `ws.ts` — relay client; cookie auth (no credential in JS or the URL); demuxes
  every inbound frame by `msg.sid` and tracks `lastSeq` per session.
- `reducer.ts` — `AppState.runtimes` keyed by sid (or wrapper temp key), each
  with its own turns/state/model/perm/queue. `focusedSid` is a pure view change.
- History: `history-browse.ts` (paging), `history-merge.ts` (page/detail merge),
  `history-page-cache.ts` (best-effort deep-history storage, deliberately
  independent of `cache.ts`), `history-requests.ts`, `history-recovery.ts`,
  `history-detail-projection.ts`, `history-image-assets.ts`.
- `cache.ts` — IndexedDB per-session turns + `lastSeq` so a reload paints locally
  and asks only for the delta.
- `outbox.ts` — reliable command retry across reconnects.
- `runtime-bounds.ts` / `runtime-drain.ts` — browser-memory bounds for long-lived
  multi-session tabs; unfinished work is retained, completed turns are evictable.
- `data.ts` — slash commands, models, permission modes, split by Code/Work
  surface and by client-side vs. forwarded-to-engine.
- `push.ts` / `notification-*.ts` — Web Push binding and notification routing.
- `components/` — `ChatView`, `Composer`, `SessionsSidebar`, `ArtifactPanel`,
  `DeviceSheet` (enrolled machines), `ProcessTimeline`, `HeaderMenu`, sheets.
- The remaining top-level `web/src/*.ts` files are single-purpose presentation
  and input helpers (scrolling, diffs, previews, composer drafts, IME, session
  ordering, tool/notice formatting); they carry no cross-tier contract.

### Ops
- `deploy/` — release build/manifest, protocol-bundle validation, atomic symlink
  activation, installers, Caddy, systemd/launchd units.
- `tests/` — zero-token unit tests (`test_*.py`); `e2e_*.py` here and
  `scripts/live/` are explicit, may spend model tokens.
- `.github/workflows/ci.yml` — required push/PR regression gate.
  `.github/workflows/release.yml` reuses it before building, attesting, and
  publishing per-role/per-arch release bundles.

## Commit and PR gate

- Keep every commit coherent and reviewable. Before staging, inspect
  `git status --short --branch` and preserve unrelated user changes. Stage only
  the intended scope, then review `git diff --cached --stat`,
  `git diff --cached`, and `git diff --cached --check`.
- Use an English Conventional Commit subject (`type(scope): summary`). Do not
  add tool prefixes such as `[Codex]` / `[Claude]`, generated-by trailers, or
  `Co-Authored-By` unless the user explicitly requests one.
- For a multiline message, use `git commit -F <message-file>` with exactly one
  blank line after the subject and consecutive direct `- ` bullets. After
  committing, verify the stored message and scope with
  `git log -1 --format=raw --stat`, then recheck
  `git status --short --branch`.
- Before opening or updating **every** PR, run the complete local gate below;
  a docs-only or apparently narrow change does not skip it unless the user
  explicitly accepts that exception. Every command must exit zero. Expected
  platform-defined test skips are allowed, but failures or missing tools must
  be reported rather than silently bypassed.

```bash
.venv/bin/python -m pytest
uvx --from ruff==0.15.13 ruff check cc_remote tests deploy
npm --prefix web run build
npm --prefix web run test:reliability
npm --prefix web run test:history-browser
npm --prefix web run lint
bash -n \
  deploy/install.sh \
  deploy/install-relay.sh \
  deploy/install-wrapper.sh \
  deploy/setup-vps.sh
shellcheck -x \
  deploy/install.sh \
  deploy/install-relay.sh \
  deploy/install-wrapper.sh \
  deploy/setup-vps.sh \
  deploy/setup_transaction.sh
git diff --check
```

- `.github/workflows/ci.yml` repeats this gate for pushes and PRs. A local pass
  is required before PR publication and does not replace green remote CI before
  merge. These checks are zero-token; do not substitute a live model probe.

## Run / test
```bash
python -m pip install -r requirements-dev.txt
python -m cc_remote.relay        # terminal 1 (set WEB_STATIC_DIR=web/dist to serve the UI)
python -m cc_remote.wrapper      # terminal 2 (on each machine running Claude/Codex)
pytest                           # zero-token unit tests
npm --prefix web run test:reliability
npm --prefix web run lint
npm --prefix web run build
```
`pytest.ini` restricts collection to `tests/test_*.py`; these are zero-token
unit/regression tests (stub transport, no model). Real relay/wrapper/model probes
live under `scripts/live/` and may spend model tokens — run them explicitly,
keep prompts trivial ("hi"), and prefer the unit tests.
