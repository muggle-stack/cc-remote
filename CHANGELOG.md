# Changelog

[中文](CHANGELOG_zh.md)

## Unreleased

- Repair process ownership and empty details when steering during Codex compaction, including refresh and session reload.
- Align the Codex context gauge with native compaction estimates (protocol v63).
  Match bounded local log reads to the account, thread and latest rollout sample;
  invalidate estimates after compaction and show recent request usage without a
  percentage when no matching estimate is available. Refresh active visible sessions.
- Add DeepSeek Harness 0.1.5-rc.2 as an optional Code engine (protocol v61):
  native Agent Presets, models/effort, permission presets, goals, commands,
  questions/approvals, attachments, steering, queued prompts, cancellation and
  forks. Keep authentication local, cold history non-activating, and durable
  message ownership stable across retries, steering and reconnects. Include
  desktop/mobile themes and an isolated zero-provider native acceptance runner.
- Set a per-session maximum usable Codex context (protocol v60), validated against
  the native model catalog. A 300k setting means a 300,000-token window, with
  compaction near 95% subject to native limits. Preserve existing saved numbers,
  report applied capacity separately from compaction, and apply window reductions
  only when the native thread can safely reload without disconnecting other clients.
- Add a session file browser through the folder button and `/open [path]`, with paged directories and the existing file preview sidebar.

- Play linked audio directly in desktop and mobile file previews (protocol v58),
  with native playback/seeking, speed selection, original-file download, and
  playback cleanup on close or file changes. Preserve the existing 8 MiB limit,
  exact-file authorization and requester-only transport; allow media Blob URLs
  in the application CSP without expanding script or network permissions.
- Speed up Codex history reads without changing wire/UI behavior: prefetch one
  older summary page after an explicit idle-session read, reuse bounded pages
  only while the exact rollout fingerprint is unchanged, and batch native
  detail-cursor skips. Keep live heads fresh, discard changed/invalidated
  snapshots, isolate accounts, and preserve normal errors on cache misses.
- Keep already-painted Codex history and reading position when a send/steer
  learns message-ID aliases. Separate additive projection continuity from
  rollback/restart invalidation, revoke stale page requests without closing the
  reading view, and route background summary refreshes through the same page
  provider as explicit history reads so older-page cursors remain usable.
- Restore per-turn changed-file summaries for idle and historical sessions across
  official Codex pagination, native rollout fallback and cached full-history
  reads. Recover both legacy patch records and current `FileChange` items without
  reading the worktree or expanding tool details. Distinguish file types with
  quiet badges, prominent filenames and shortened directory labels.
- Page each turn's changed files in batches of 64 with scoped load-more/retry
  actions and exact totals. Capture the native file index before tool-card
  clipping, store per-file patches separately and read only the requested diff.
  Session/version changes discard stale pages; later edits cannot alter a
  completed turn's revision. Resource limits remain explicit (4096 indexed files
  and bounded per-file/per-turn evidence), never fake load-more links.
- Preserve Codex patch truncation through live and historical diff archives.
  Never present clipped patches as complete; retain
  valid native diffs when only tool output was clipped. Rebuild affected derived
  caches and revalidate older per-tool captures without deleting native history,
  images or immutable archive rows.
- Add immutable, session-scoped per-turn file-diff revisions (protocol v57)
  in a private Wrapper archive independent of the history cache. Fold each
  turn's file list, compose repeated edits when native evidence is complete,
  and label missing/truncated history without substituting the current worktree.
  Preserve reading position on send, keep code-copy controls in the visible
  block, and remove the empty working-footer gap. Correct Claude task-terminal
  ownership and expose native model fallback notices in live and cached history.
  Clarify empty-system-content 400 errors without changing transcripts or retrying.
- Fix home-directory page discovery (protocol v55): explicitly referenced HTML
  and verified, user-owned Python static-server links become private session
  previews without per-project registration. Preserve manual publications,
  cloud links, no-follow file checks and the separate binary channel. Never
  crawl home or populate the global catalog with automatic pages. Support
  exact/longest-prefix import maps used by local Three.js addons.
- Add session-scoped page associations (protocol v54), persisted on the parent
  Wrapper. Confirm explicitly referenced/written HTML against existing source
  publications, surface compact "View page" actions outside process details,
  and make the global catalog an explicit secondary association picker. Keep
  cloud links native, preserve source-device identity, and never widen paths.
- Add registered static remote Viewer previews (protocol v53): default opaque
  Bridge frames reuse HTTPS or explicitly allowed HTTP/IP origins without extra
  DNS/TLS; optional Isolated mode keeps per-preview origins. Explicit device-local
  resource publications and separate pull-driven
  binary channel, desktop side panels and mobile full-screen viewing. Directory
  traversal, symlinks and unregistered resources fail closed. No browser engine,
  model call or arbitrary LAN HTTP proxy is introduced. See `docs/remote-viewer.md`.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v52. Codex native
  async questions retain their bounded structured metadata in live events and
  history, and render as nonblocking inline forms. Replies use the existing
  session-scoped query/steer outbox; they never masquerade as approval requests
  or mark a running turn complete. Rebuild older Codex history projections.
- Scope the BTW panel's open preference by device, surface, engine and parent,
  preserving background chats without opening empty panels on unrelated
  sessions. The legacy unscoped visibility flag is ignored; retained chats are
  untouched. Restore safe Markdown details/summary disclosures, and preserve
  native Mac drag-selection auto-scrolling through deferred layout updates.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v51. `/btw` now
  behaves as a resident side-chat workspace: the shortcut collapses instead of
  destroying it, each parent may own several explicitly closable chats, and an
  relay-authenticated owner catalog plus bounded ring replay restores them
  after reload or reconnect in any tab or device signed in as that owner.
  Page-scoped connection ids keep duplicated tabs independent. Codex side chats
  also use the Codex presentation path, hiding successful hook plumbing while
  retaining actionable failures. New side chats start at `xhigh` reasoning
  (clamped to the selected model's capabilities), and the compact Goal/Plan
  monitor yields while a mobile input method is open, then returns unchanged.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v49 and split the
  experimental Codex managed-browser/Computer Use stack into its own feature
  branch. The Claude runtime line no longer ships Playwright, browser control
  frames, dynamic browser tools, or the `/browser` UI; artifact image, PDF,
  GIF, SVG, Markdown, and HTML previews remain available.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v48. Codex
  ChatGPT accounts now expose earned rate-limit reset credits through the
  existing sanitized status surface and can redeem one with the official
  app-server API. Redemption is idle-only, explicitly confirmed, requester-
  scoped, and uses the reliable command id as the native idempotency key;
  paid credit balances and spend controls remain private.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v47. Managed
  Claude sessions delegate the default autocompact window to Claude Code so it
  follows the selected model, account, gateway, and native settings; v3 control
  records created with cc-remote's short-lived forced 500K default migrate back
  to that native behavior. Explicit reductions still compact and verify a
  native boundary before reconnecting, while crash-safe desired/applied state
  prevents restarts and forks from applying a smaller window early. Delayed SDK
  title metadata no longer triggers a false external reload, and genuine
  transcript reloads preserve the selected long-context model. cc-remote no
  longer intercepts or rewrites Claude's native
  image/PDF `Read` tools; model context limits, media handling, and automatic
  compaction stay owned by Claude Code.
- Expose the Codex Fast service tier in Work for both new and resident
  sessions while keeping Claude Work on its engine-neutral command surface.
- Upgrade the verified Claude Agent SDK pin to `0.2.151` (bundled Claude Code
  `2.1.258`) and replace the curated Fable 5 and Mythos 5 model cards with the
  official `claude-fable-5-1` and `claude-mythos-5-1` releases. Managed Code
  sessions select these through Claude Code's native `[1m]` context marker
  (which is stripped before provider routing); legacy unsuffixed curated aliases
  retain their family/version and are normalized to that marker, while other
  recorded model identities remain unchanged. The wrapper now rejects daily
  Claude Code releases older than `2.1.258` instead of silently launching
  without the native runtime controls this integration depends on.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v44 and add
  Claude account profiles. Each user-defined profile owns one explicit
  `CLAUDE_CONFIG_DIR`; Code, Work, schedules, models, Skills, extensions,
  history, external ownership, and forks remain bound to that account. Empty
  configuration preserves the original single-account IDs and UI. Profile
  topology changes migrate local ownership by resolved config-directory path,
  fail closed on ambiguity, and are covered by the same rollback-aware Work
  deployment transaction as Codex profiles. Claude's authoritative background
  task level now restores a native-style detached Bash/Agent monitor on every
  client Hello without reopening an idle session; task-completion follow-ups
  retain their source-time boundary, and real compact boundaries use the
  existing context-compaction process presentation.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v42. Pending
  Claude and Codex questions are now authoritative session state restored on
  every client Hello, independent of the replay ring. Fresh clients retain a
  compacted live suffix when a long active turn has evicted its opening marker,
  install one bounded canonical-detail head while older process pages remain
  explicitly pageable, and cannot publish a queued question after an interrupt
  boundary. Late Claude resume bookkeeping no longer moves a completed answer's
  terminal clock; affected server and browser projections rebuild once.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v41. Automatic
  Claude context reads no longer issue a blocking native control request;
  explicit `/context` reads prefer the exact native breakdown and otherwise
  retain a visibly labelled cached or recent-turn token total when that
  optional control plane times out.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v40. Codex
  heavyweight turn-detail pagination now binds every cursor to an immutable,
  source-scoped snapshot, so an actively growing multi-hundred-MiB rollout
  cannot invalidate the next page or silently substitute a different window.
  If that bounded snapshot is later evicted, Web keeps the expanded content
  mounted and performs one fresh-head reset instead of retrying the stale
  cursor, duplicating rows, or jumping the reader's position.
- Treat an unexpected shared Codex daemon replacement as an incomplete control
  boundary: reconnect once without replaying the prompt or inventing a terminal,
  and retain an explicit reasoning effort only for the same thread, model, and
  working directory when the replacement resume reports a nullable value.
- Upgrade Claude Agent SDK to `0.2.142`; the wrapper remains pinned to this
  exact verified patch and continues to launch the user's configured Claude
  Code executable.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v39. Claude
  subagent detail, detached background-process ownership, paged turn detail,
  sanitized Claude quota events, and per-session automatic-compaction controls
  now cross an explicit compatibility boundary. Code, Work, BTW, new sessions,
  and forks can inherit Claude's setting, use automatic mode, or select a
  bounded `100K–1M` token threshold; busy changes wait for a proven terminal
  boundary. Ambiguous Codex steer owners can no longer revive an idle spark.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v36. Conversation
  summaries now distinguish exact visible process, exact direct answers, and
  opaque native detail; truncated content keeps its own disclosure, and Codex
  process timing starts at the first visible process event instead of the user
  message timestamp.
- Add an official container deploy for the Relay: a multi-stage
  `deploy/Dockerfile` builds `web/dist` from source and installs the
  hash-locked wheels as a non-root `ccremote` user, with `docker-compose.yml`,
  a Docker-oriented `env.relay.docker.example`, and a compose layout that only
  publishes the relay to the host loopback so an existing nginx can keep TLS
  and WebSocket termination. `deploy/nginx-reverse-proxy.conf.example`
  documents that alternative front, and the Docker build exposes
  `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` build args plus documented
  mainland-China mirror guidance.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v35. Exact Codex
  app-server and source-validated rollout terminals now travel independently of
  the narrative History projection, so a multi-hundred-MiB rollout cannot keep
  a completed turn spinning while its content index catches up. Terminal facts
  remain profile-, revision-, and source-bound; they never guess the newest open
  row or create a second completion receipt.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v34. Main-session
  completion acknowledgements and exact Goal-generation dismissals now live in
  bounded wrapper-owned state, so reading or hiding them in one browser updates
  every connected browser and survives reconnects without hiding replacements.
- Store files and images attached to Work messages under that conversation's
  private `workspace/uploads` directory, where the Work sandbox can actually
  read them, while keeping uploaded source material out of generated Artifacts
  and retaining history compatibility with the earlier sibling layout.
- Let mobile Markdown source editors fill the available file panel, keep active
  session cards visibly selected, restore code-copy contrast in dark mode, and
  keep desktop dark code blocks visually separate from the page background.
- Restore the exact active-turn owner before reconnect tail replay, persist its
  source-bound browser/native identity into completed Codex History, and expire
  projections written before that identity was durable instead of repainting a
  duplicate response after refresh. Legacy CLI rollout user rows now reuse the
  adjacent native app-server item id as well, so a history refresh cannot paint
  one terminal prompt and its live mirror as two separate turns.
  Remote steers flushed under a concurrent CLI turn now retain their exact
  official `clientId` without admitting foreign CLI content, so delayed native
  History rows collapse into the original optimistic message instead of
  repainting it. Live rollout and official History item ids may coexist as
  exact aliases of that one browser message instead of conflicting with each
  other. Initial `turn/start` inputs now carry the same exact identity;
  restart recovery can reattach an officially active split control/rollout turn
  even after an oversized task's lifecycle marker has fallen outside the
  bounded tail, preventing false interruption and duplicate prompt projections.
  When that bounded tail has also evicted the active `TurnBinding`, its original
  sequence now proves and pre-binds the retained current-turn suffix before Web
  reduces it; projections cached with the old reversed order are expired once.
  Once an unraced official Codex History page reports the thread idle, its
  exact persisted success or failure also replaces a provisional live terminal
  without discarding live detail or weakening Claude SDK terminal authority.
- Upgrade the coordinated Wrapper/Relay/Web wire gate to protocol v33 and add
  multi-account Codex Work. New Work sessions and schedules can select any
  configured profile, persist that ownership independently of the current
  default, and keep the same account across retries and wrapper restarts.
  Legacy Work rows are idempotently assigned to the upgrade-time default even
  when an earlier account-topology migration already completed. Removed
  profiles leave existing Work bound and fail closed instead of being
  reassigned; transient catalog read failures remain warnings, retain each
  failed profile's last-known
  session projection, and never trigger a silent fallback. Wrapper activation
  now snapshots both Work SQLite registries, verifies the ownership migration,
  and restores matching data before restarting old code after a failed release.
- Upgrade the coordinated Wrapper/Relay/Web gate to protocol v31. Wrapper-side
  catalog mutations now broadcast an unbuffered invalidation instead of an
  uncorrelated session list; each visible browser coalesces the hint into its
  own generation- and surface-bound list read. Streaming math recognizes
  delimiters split across deltas, paused Goal resumes retain one bounded
  objective anchor, and compact continuation no longer renders a live spinner
  beside a real interrupted terminal.
- Upgrade the coordinated Wrapper/Relay/Web wire gate to protocol v32 and add
  concurrent Codex account profiles. Each configured `CODEX_HOME` owns its own
  official daemon, catalog, controls, and history namespace; Code combines the
  sessions with account labels and filters, while Work remains on the default
  profile only. Single-account installs retain native ids and their old UI;
  multi-account cards use stable colored `default`/celestial ribbons. Local
  profile-key migrations are crash-resumable and include aliases, fork recovery,
  turn leases, controls, pins, Work ownership, and rollback checkpoints.
  Headless profiles now bootstrap their own official remote-control daemon
  instead of silently degrading to a private stdio process. OAuth-only sibling
  homes safely reuse the verified managed standalone CLI entry while retaining
  independent auth, rollout, socket, and daemon state; existing custom layouts
  are never replaced. An unavailable account control plane fails clearly, while
  single-account fallback semantics remain unchanged. An authenticated account
  whose quota read temporarily
  returns no windows is also shown as a refreshable read failure rather than a
  missing account.
- Keep Claude prompts stable across refreshes and Claude/Codex surface switches.
  Claude Code's internal `promptId` is no longer mistaken for the browser
  message id; the wrapper persists only exact native-user aliases observed
  after a generation-bound Agent SDK transcript boundary (with SDK replay as
  fallback) or a broker-owned append boundary. Learning this Claude metadata
  no longer invalidates Codex account caches. Derived Claude pages from the old
  identity model are invalidated without discarding Codex history pages. While
  an Agent SDK turn is live, transcript EOF now remains an open projection and
  the first ownership scan no longer mirrors a duplicate partial page; the real
  `ResultMessage` is the sole completion boundary. A
  narrowly-proved delayed `request_retry` branch no longer hides the already
  completed sibling tail, and entering or retrying a resident-session switch
  publishes the current lifecycle state with a fresh sequence instead of
  replaying a stale `running` frame.
- Add protocol v28 Codex account activity. The existing one-shot status read
  carries a validated, bounded 53-week daily token series, and Web exposes a
  Codex-only Desktop-style activity calendar without storing it in live replay.
  The five heat levels are relative to the visible daily peak, and localized
  compact counts keep large token totals readable.
- Add protocol v27 session-directory migration for idle Codex Code threads.
  The wrapper resumes the same native thread ID in the selected existing
  directory, preserves its deferred-query queue, and rolls back to the original
  cwd if the new resume fails. The selected cwd survives wrapper restarts;
  migration never forks or steals browser focus.
- Add requester-local confirmation for cwd-external previews under the
  coordinated protocol v30 gate. Grants are bound to the engine, space,
  session, canonical path, owner UID, device, and inode; a changed file asks
  again. User-approved files remain read-only, while successful structured
  writes can retain exact-file edit access. Relative assets in external
  Markdown resolve beside that document without becoming browser filesystem
  URLs.
- Add isolated artifact and file-activity rendering under the coordinated v30
  Wrapper/Relay/Web wire gate. HTML
  artifacts now retain document CSS and offer an explicit isolated interactive
  preview; standalone, Markdown, and conversation SVG images share one bounded
  sanitizer. Successful built-in reads can preview exact cwd-external image
  snapshots without granting directory access, and file activities use
  read/create/update/delete/move semantics instead of one generic edit icon.
- Move busy-session follow-up queues from browser memory to the always-on
  wrapper. Protocol v25 lets queued and interrupt-replacement messages continue
  as soon as the active turn ends even when every Web/PWA client is asleep or
  disconnected, and restores the authoritative queue when a client reconnects.
  Queue chips retain only bounded previews; opening one fetches its full prompt
  privately and edits it atomically in the wrapper without dropping attachments.
- Upgrade the coordinated Wrapper/Relay/Web wire gate to protocol v26 and give
  Codex `$` completion a lightweight Skills-only inventory path. Slow Apps or
  MCP discovery can no longer hide an already returned Skill catalog, while
  the full Extensions sheet retains the complete native inventory.
- Add official Codex named permission profiles as a control separate from the
  approval policy. The compact permissions sheet can select Read Only,
  Workspace, Full Access, or cwd-aware custom profiles without adding another
  composer-bar control. Protocol v24 carries the new controls across Wrapper,
  Relay, and Web.
- Add per-session Codex Web Search selection (`cached` / `live`). The override
  survives controlled reconnects and wrapper restarts without modifying the
  user's global `config.toml`.

- Upgrade Claude Agent SDK to `0.2.128` and make wrappers explicitly run the
  user's daily `~/.local/bin/claude` instead of silently selecting the SDK
  bundle, keeping Remote and terminal credentials and CLI updates aligned.
- Preserve the user's Claude subscription OAuth setting inside isolated Work
  policies, and render the built-in `AskUserQuestion` flow as its original
  single- or multi-select questions instead of a generic tool approval.
- Align busy Codex input with the official client: sending defaults to native
  `turn/steer`, queue remains available, and Stop stays an explicit separate
  action. Claude keeps its interrupt-and-send behavior.
- Page heavyweight detail from the configured safe source window inside a
  single very long turn, so the browser's 256-block presentation cap no longer
  replaces otherwise available process rows with a synthetic omission marker.
- Reject foreign shared-daemon lifecycle frames during resume binding and
  reconcile a proven inactive spontaneous turn without leaving a phantom
  running state.
- Upgrade the coordinated Wrapper/Relay/Web wire gate to protocol v22, with
  replay-safe user-question close events and multi-select answers.
- Continue an in-flight Codex task on the replacement daemon after an account
  switch, without unlocking queued messages. Active goals resume through
  Codex's native goal loop; ordinary turns use hidden contextual continuation.
  A Goal turn already running when the daemon restarts follows the same contract
  and falls back to a hidden continuation if app-server restores the Goal state
  without launching its next turn.
- Upgrade the coordinated wire contract to protocol v23 and correlate Codex
  status responses with their originating `request_id` so a delayed old-account
  snapshot cannot overwrite newer limits after a switch.
- Show the current Codex account's five-hour and weekly remaining quota beside
  context usage, with generation-safe refresh after an account switch.

## v3.0.0 — 2026-07-24

cc-remote v3 adds an isolated Cowork-style Work surface to the established
Claude Code + Codex remote control plane, while rebuilding history, native
client coexistence, multi-machine routing, mobile reliability, and release
operations.

### Code and Work

- Add separate Code / Work spaces for both Claude and Codex, with independent
  session lists, focus, directories, prompts, permissions, and recovery state.
- Add provider-scoped Work projects with file, link, and note knowledge sources,
  reusable instruction templates, and materialized per-work context.
- Add persistent one-shot, daily, and weekly schedules with run records, leases,
  heartbeats, retries, and overlap prevention.
- Keep every Work item inside a registry-owned private directory. External
  material enters only through attachments or project knowledge sources.
- List files produced by a Work item as artifacts and preview source, Markdown,
  sanitized HTML, images, PDFs, and sandbox-converted Office documents locally.

### Sessions, controls, and extensions

- Add reliable delete, rename, archive, per-message fork, ephemeral side-chat,
  queue, interrupt, and background-session control without focus stealing.
- Add native Codex compact and Review, plus isolated Git worktree forks. The
  unfinished Codex Rollback and Claude Rewind surfaces remain unavailable.
- Keep model, reasoning effort, service tier, collaboration/Plan mode,
  permissions, context, goals, status, usage, and rate limits scoped to the
  active session.
- Add live Skills, Plugins, Apps, MCP, and Hooks catalogs. Code can manage
  Skills, plugins, and Claude Hooks where supported; Codex Hooks and all Work
  extension categories remain read-only.
- Forward Claude tool approval and Codex command, file-change, user-input,
  general-permission, and MCP elicitation requests to the controlling browser.

### Local-first history

- Paint the browser's last validated IndexedDB projection before network
  validation.
- Materialize source-fingerprinted turn summaries in a rebuildable wrapper
  SQLite index.
- Load newest turns first, page older history, and fetch heavy tool/reasoning
  detail only when that turn is expanded.
- Preserve the viewport while prepending pages and converge appended sources in
  the background.
- Resolve historical image assets on demand instead of embedding them in every
  history page.

### Long Codex sessions and native lifecycle

- Read Codex rollouts backward by turn without re-uploading history to the model
  or replacing app-server-native resume and compaction state.
- Add a narrowly gated official HTTP transport fallback for oversized Codex
  Desktop + OpenAI resumes whose WebSocket closes before completion.
- Keep Codex shared-daemon CLI activity distinct from private Codex App
  ownership.
- Bind prompts, steering, commentary, tools, compaction, aborts, and completion
  to their authoritative turn so history cannot drift to the bottom.
- Mirror interrupted and externally running work without stale read-only locks
  or permanent thinking indicators.

### Devices and ownership

- Add Device Center, expiring single-use pairing codes, hashed machine
  credentials, rename/revoke controls, and online state.
- Add optional multi-user account policies that restrict each account to an
  explicit set of wrapper machines.
- Enforce account-to-machine authorization on discovery, commands, events, and
  push subscriptions.
- Scope working directories and delayed focus/rekey frames by device, surface,
  engine, socket generation, and session ownership.
- Add shared Darwin/Linux process identity scanning for native Claude ownership
  while keeping takeover limited to an exact same-user process.
- Add privacy-preserving Web Push for background completion/failure state,
  scoped by user and machine. Existing users migrate to generic notices; an
  explicit session mode adds a bounded display name and an exact validated
  device/surface/session route, never prompt, answer, path, or tool content.

### Mobile and artifact experience

- Add stable upward history pagination, local-first session switching, and
  bounded live-tail replay.
- Add on-demand conversation images and a touch-friendly lightbox with
  tap-to-close and pinch zoom.
- Support multiple image attachments, stable pending previews, and per-session
  composer drafts across session and engine switches.
- Keep Markdown relative links/images, source files, sanitized HTML, PDFs, and
  sandbox-converted Office previews inside the wrapper security boundary.
- Refresh PWA and notification assets and fix narrow-screen sheets, process
  timelines, and persistent error presentation.
- Group authenticated notification, theme, and logout actions behind an
  accessible three-dot popover on desktop and safe-area-aware sheet on mobile.
- Keep running indicators above queue/interrupt controls, preserve Claude turn
  durations, and compact repeated tool activity without hiding final replies.

### Release and operations

- Align Python, Codex `clientInfo`, Web package metadata, and the public build
  manifest on product version `3.0.0`.
- Upgrade the strict wire gate to protocol v20.
- Publish reproducible, checksummed Relay/Wrapper archives for Linux x86_64,
  Linux arm64, macOS Intel, and macOS Apple Silicon, with GitHub artifact
  attestations.
- Add a verified role bootstrap, managed Python 3.13 environments, a macOS
  LaunchAgent installer, and a Linux Wrapper systemd installer. Device
  credentials remain outside immutable releases and service definitions.
- Validate product and protocol versions together before staging or activating
  a release.
- Use immutable VPS releases, release-local virtual environments, atomic
  activation, readiness checks, and rollback.

### Upgrade notes

- v3.0.0 uses wire protocol v20. Wrapper, relay, and Web must be upgraded
  together; mixed protocol versions are rejected.
- Hard-refresh already-open browser tabs after deployment so they load the v3
  hashed assets and rebuild their local projection against protocol v20.
- Keep runtime secrets and machine state outside release directories. Do not
  replace `.env`, `~/.cc-remote`, Claude transcripts, or Codex rollouts.
- Claude integration remains pinned to `claude-agent-sdk==0.2.119`.
- History browsing remains a local read: it does not resume Claude/Codex or
  create a model turn.
