# DSH integration on the current cc-remote architecture

## Baseline and ownership

DSH integration ships in cc-remote **v5.0.0**. Development started from
`v4.0.29` (`fb60c7ef9348`) without merging the historical DSH branch.
The implementation target is exactly `@deepseek-ai/dsh@0.2.2-rc.1`, upstream
commit `66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db`.

DSH owns model credentials, Agent execution, plugins, native session storage,
and native message admission. cc-remote owns device-scoped routing, its current
history projection and UI. Detaching a client or restarting a Wrapper must not
cancel an Agent. Unknown outcomes of mutations must not be retried.

## Required integration and acceptance

| Area | Required behavior |
| --- | --- |
| Connection | Authenticated local official API, bounded HTTP/mux streams, generation-scoped event answers, explicit disconnect and compatibility errors |
| Sessions | Current engine selector and new-chat composer, native presets and model catalog, list/search/rename/pin/archive/fork, native identity preservation |
| History | Read-only cold pages, V4 messages and surfaces, lazy process detail and images, exact live replay and replacement boundaries |
| Input | Text, images and files; native commands and Skills; steering, queue edits and explicit cancellation |
| Questions | Current multi-question UI, single/multiple selection and free text, collapse/restore without answering, native cancellation and reconnect |
| Permissions | Native permissions/approval choices, exact session and event ownership, no implied approval |
| Execution | Native model/effort/defaults, context/compaction, goals and plans; no fabricated account quota or token-budget controls |
| Background work | Native subagent/job status in the shared background-task UI, details and supported continuation/control |
| Deliverables | Current Artifacts and preview interfaces, native declared output files, exact historical changed-file records |
| Extensions | Native effective commands/Skills/plugin inventory and supported native management; capability absence is explicit |
| UI | Current desktop/mobile and theme system, engine marks, keyboard navigation, side panels, completion state |
| Compatibility | Existing Claude/Codex behavior, device authorization, queue ownership, no model turn for read-only operations |

Completion requires behavioral tests against the pinned native runtime in an
isolated home with a deterministic model fixture, plus the repository's complete
gate. Production DSH homes and model credentials are never used for migration
tests. A feature is not complete merely because its button renders or an RPC
accepts a request. Unsupported upstream capabilities must be documented rather
than emulated by writing native files.

## Verified upstream contracts

- The Gateway retains named-argument unary requests and `/api/remote.mux`.
  `/connect` provides a dedicated API Peer with a socket-owned lifetime; event
  answers require the current `$events` `clientId` and exact `eventId`.
- `session/list` takes `_request`. `session/projections` returns an exact
  `asOfSeq` for current-format cold sessions, or `migration-required` hints.
  `session/page` can read through that exact cursor without creating an Agent.
- `session/follow` is passive by default in 0.2.2; cc-remote does not set
  `activate`. Cold history remains bounded projection/page I/O, and reading
  a prepared session or subscribing to it must not create an Agent or model turn.
- Control projection frames contain one atomic `changes: [{key, val}]` batch
  at a native sequence, including null values. Attachment reads take the full
  native image reference, retained privately from that exact session history;
  browser-supplied attachment references are never accepted.
- Session logs use the current V4 message contract. Projection-cache hints are
  not proof of live lifecycle or message ownership.
- Models are provider/model pairs with native reasoning choices. Never limit
  the catalog to a model hard-coded by an old adapter.

## Official references

- [Pinned release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.2-rc.1)
- [Connection](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/packages/client/connection)
- [Gateway](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/packages/api/gateway)
- [Session Controller](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/packages/api/session-controller)
- [Native Session](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/packages/core/session)
- [SDK protocol](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.2-rc.1/packages/sdk/protocol)

## Local setup

Use Node 24 and an independently installed `@deepseek-ai/dsh@0.2.2-rc.1`.
Do not point migration experiments at an existing DSH home. DSH still owns its
provider login, API endpoints, credentials, presets, and plugin configuration.

Add this integration extension to the native DSH patch configuration, using the
absolute path in the active cc-remote source tree or Wrapper release:

```yaml
- insert:
    - id: cc-remote-metadata
      name: /absolute/path/to/cc-remote/integrations/dsh/cc-remote.mjs
```

Start DSH with that patch and its normal native configuration:

```sh
dsh --profile web --patch /absolute/path/to/patch.yml --no-open
python -m cc_remote.wrapper.dsh_pair --file ~/.config/cc-remote/dsh-connection.json
```

The pairing command prompts without echo for DSH's local login URL. It verifies
the pinned native contract without creating an Agent, then atomically saves a
mode-0600 control pairing file. Set `CC_REMOTE_DSH_CONNECTION_FILE` in the
Wrapper's external environment to that file's absolute path. Never put its
contents or the login URL in shell arguments, Git, browser state, or chat.
Use a literal loopback IP with an explicit port; pairing does not expose DSH to
the public network. Re-pair if DSH rotates its native control credential.

The metadata extension is required for this integration's command/Skill catalog,
full-text match location, and declared deliverables. It adds authenticated,
bounded read endpoints using metadata/catalog contract 3. Native schedules
use the official `schedule/create`, `schedule/edit`, and `schedule/run` APIs;
the old private schedule-creation shim is removed.
Session execution, native history, models,
questions, approvals, goals, jobs, subagents, and plugin toggles use official
DSH Remote APIs. The extension never performs model requests or mutates native
session files.

## Product behavior and boundaries

- DSH uses the current cc-remote Code page, composer, history, multi-question
  cards, collapsible question UI, Artifacts, file previews and background tasks.
  Its light default is the former Codex white/blue palette; the current Claude
  and Codex defaults are retained. Appearance overrides are separate per engine
  and light/dark mode.
- New sessions select native agent presets, models, reasoning and permissions.
  `/` commands use native dispatch; selecting a Skill inserts DSH's native
  `/skill-name` syntax. Controls and metadata follow the selected preset.
  Existing DSH sessions show their actual native preset beside the Code page
  title, using the catalog's display name. Missing state never falls back to
  the default preset or the new-chat selection.
- Idle history, search, previews and catalogs never create a model turn. Native
  cold history requires V4; old sessions that require migration are reported
  explicitly and must be migrated by the official client first.
- DSH shares the resident session pool with Claude and Codex. Capacity reclaims
  eligible idle contexts across engines while preserving focus, side chats,
  queued input, pending questions and active work. New DSH sessions check
  capacity before native creation; invalid cold metadata does not evict a
  healthy resident.
- Model request admission, steering and termination are native. The existing
  Wrapper queue owns deferred messages. Closing the page or reconnecting never
  answers a question or stops an Agent. Ambiguous writes are not retried; an
  unanswered interaction must be confirmed again by native event delivery.
  Session creation, persistent forks, native slash commands and goal actions keep durable,
  device-local intent receipts before native I/O. After a Wrapper restart, a
  known created session is restored without resubmitting its initial prompt.
  An unresolved receipt requires inspecting native state instead of repeating
  the mutation. Receipts contain request hashes and bounded results, not prompts
  or attachments, and must be retained with Wrapper private state. Native
  controls, goal actions and child stops are excluded from the browser reconnect
  outbox. Goal receipts bind the action, session, exact revision and requested
  values; completed retries return the saved outcome without activating an
  Agent or issuing another goal mutation. Settings and plugin changes also
  require a live connection and are never automatically resubmitted.
  Fork receipts bind the stable request ID to its parent and completed turn.
  A known child is recorded before history/catalog reads; retries resume only
  those reads or replay the completed response. A lost native fork result never
  authorizes another fork. These records do not enable the gated side-chat UI.
- Native jobs expose output and explicit stop. Continuable child agents expose
  queue/steer/stop only while their owning parent is available. One-shot and
  unavailable descendants remain read-only. ZIP exports and process output are
  private bounded reads, not part of replayed chat history.
- Running children use the existing composer task dock. When a child returns
  after the parent's answer, the existing continuation row gains a clickable
  `代理返回` link. Returns during an active response do not split or fold the
  response, and ordinary background jobs do not gain agent-return links.
  Claude, Codex and DSH keep their own execution lifecycles. The parent
  continuation label requires actual subsequent parent output, not merely a
  child notification. Summary history retains the return metadata, and the
  local projection cache is rebuilt at schema 50.
- DSH child details use the shared process side panel, including child prompts,
  folded reasoning, tool results and final answers. Nested children can be
  opened and navigated back through the same panel. Reads validate the exact
  native ancestry and never follow, activate or resume a child. PTC calls use
  native root/parent call identities, retaining the original prompt owner if a
  steering message arrives while the tool is running.
- DSH session tools include workflows, execution trajectory, automation,
  presets, runtime settings, plugin packages and terminals. Workflow pages show
  recorded phases and child identities, and retain newer terminal outcomes when
  earlier pages load. They do not invent future phases or start missing agents.
  Automation reads the global native catalog and filters private side-chat
  involvement. Tasks target an existing session, a new session reused after
  its first execution, or a fresh session each time. New targets accept a project
  directory; reserved session IDs are never treated as existing sessions.
  Create/edit preserves native once/interval/daily/weekly/cron/RRULE timing.
  Pause/resume/delete/edit use the observed `updatedAt` revision; conflicts
  retain drafts. Run-now respects native admission and delivery floors. History
  shows the actual delivered-to session, never claiming task completion.
  Management mutations have durable intent receipts and never replay an unknown
  write after disconnect or Wrapper recovery. Conversational creation remains available. Preset creation stages a native
  conversational request for the user to review and send.
- Performance statistics use native turn/step records without activating an
  agent. The existing tools panel shows wall/model/tool durations, weighted
  cache hit rate, tokens, per-step rows and inner PTC calls. Overlapping tool
  intervals are counted once; missing measurements remain unknown. Earlier
  turns are paged and oversized detail is explicitly marked as truncated.
- Automation's overflow menu opens a centered confirmation for bulk cleanup.
  It reads the complete catalog, checks the observed eligible set again, then
  deletes only those confirmed identities with native `schedule/edit` revision
  checks. Newly ended tasks and private side-chat tasks cannot enter the batch.
  Ongoing tasks and conversation history remain intact. A conflict or uncertain
  write stops the remaining batch, reports the confirmed deletion count, and
  persists that result without automatically replaying partial cleanup.
- Runtime settings expose only simple execution preferences from an allowlist
  of native namespaces. Writes use the observed native revision. Provider
  configuration and credential fields stay in DSH's native manager.
- Plugin inventory and enable/disable reflect native application receipts,
  including restart-required outcomes. Package install/remove uses the native
  package manager; a restart-required result never restarts DSH automatically.
  The plugin-source tab reads only native configured indexes, without inventing
  a default market. Updatable registry packages expose native compatible version
  choices, with prerelease/deprecation labels. Updates verify native metadata
  again, report failed/cancelled installation and restoration separately, and
  retain restart-required/overridden outcomes. Native diagnostics and registry
  credentials are not forwarded. Installation/update RPCs have a bounded ten
  minute timeout and no automatic retry. Official bundles stay installation-owned.
  Plugin sources can be added/removed with an observed-list revision, then
  searched and installed through native market APIs. The optional native market
  bundle must be enabled; no index URL is supplied by cc-remote. Installation
  resolves the selected source, version and compatibility again on the device.
  Rich Markdown details are loaded on expansion; raw HTML, local-resource links
  and automatic image fetching are disabled. Source and registry secrets stay
  local. Optional official-plugin recommendation cards use the native
  action-card projection and exact originating turn. Enable and conversation
  dismissal use native action receipts; reading or viewing a card cannot enable
  a plugin. Generated personalized-page hosting is not part of this adapter.
  External OAR subagents remain native-owned: their status and returned results
  are shown, without inventing a DSH child history or follow-up controls.
  Structured `ABORTED`/`ABORTED_BEFORE_DISPATCH` results render as cancelled.
  Native scheduler reminder inputs remain visible with a task receipt in both
  live and cold history; schedule-change notices stay hidden. Batched reminders
  share their native input/answer boundary. Consumed `tool-jobs` notices retain
  a compact source receipt, so autonomous replies show “DeepSeek 继续处理” after
  reload without creating a human prompt or a false child-agent link.
  Native `apply_patch` changes use the existing authoritative file-change pipeline;
  cc-remote does not enable optional tools or alter native permissions.
  No DSH Work mode, quota estimate, worktree
  migration, rollback or session deletion is synthesized. Unsupported actions
  are unavailable instead of silently falling back to Claude/Codex.
- The terminal is a lazily loaded xterm view of DSH's own PTY. Only explicit
  creation starts a shell; Ctrl+C interrupts the foreground command, while
  Close terminal ends the shell. Closing a panel,
  changing sessions or detaching the Wrapper only detaches the viewer. Each
  attachment belongs to one browser, native session and API Peer generation;
  keyboard input is ordered and pipelined within the native acknowledged input
  window, never stored in the recovery outbox or resent after an unknown result.
  Output is privately pushed over the existing control link, batched for xterm
  and bounded by browser render acknowledgements; there is no output polling.
- DSH side chats are temporarily unavailable until the official API supports
  session deletion. The session menu explains this limitation; side-chat
  shortcuts and `/btw` cannot open a panel or create a fork. Wrapper also rejects
  stale clients' open requests before contacting DSH. The exact-prefix fork
  implementation and existing private associations remain intact for future
  integration; no existing session is deleted, archived or transferred by this
  change. Claude/Codex side chats remain available.
- This adaptation uses existing protocol v90 envelopes. Stage DSH 0.2.2-rc.1
  with this integration extension and Wrapper together. Pairing remains private,
  and an incompatible native version is refused. Back up the native home before
  activation; native persistence migration belongs to DSH. Installing this code
  alone does not upgrade or restart an existing DSH process.
- DSH 0.2.2 changes native defaults for read-before-write/external-modification
  checks and private-network WebFetch access. Existing explicit operator policy
  remains authoritative; cc-remote does not silently re-enable or disable those
  native policies. Full worktree/PR review UI and DSH side chats remain out of scope.

## Verification

Unit tests: `pytest tests/test_dsh_*.py`. Frontend regression tests are included
in `npm --prefix web run test:reliability`. Desktop/mobile acceptance:
`cd web && npx playwright test -c playwright.dsh.config.ts`.

The explicit offline native probe uses an isolated installation and temporary
HOME/DSH_HOME, disables network model providers, and supplies a deterministic
model adapter. It starts and stops only its own temporary native process:

```sh
python -m tests.dsh_native --installation /path/to/isolated/npm-install
```

The probe covers official authentication and API Peer lifetimes, cold catalogs,
streaming, native question/approval/multi-question reconnect, steering, queue
drain, interruption, file/image admission, Artifacts, jobs, delegated history,
plugin toggles, exact-revision goals, Plan commands and archive/unarchive. It
also reads management catalogs and completed child history and exercises native
PTY creation, resize, input, output, detach retention and explicit close. It also
exercises all three schedule destinations, repeated delivery and native revision
conflicts. A loopback npm fixture verifies real plugin installation, version
selection, update, and restoration after a deliberately broken package. No
provider credentials or arbitrary provider settings are used by the probe.
It must be run explicitly; ordinary pytest never calls a live model.
`--product-only` runs the statistics, bulk cleanup, market/detail/update and
recommendation-card acceptance subset against the same isolated runtime.

The DSH mark in `web/src/assets/dsh-mark.svg` derives from the pinned upstream
`apps/web/public/favicon.svg`; it keeps the upstream geometry with a CSS mask
for light/dark legibility. Upstream's MIT license is retained at
[`integrations/dsh/LICENSE`](../integrations/dsh/LICENSE).
