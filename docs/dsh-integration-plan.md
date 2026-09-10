# DSH 0.1.5 integration plan

Add DeepSeek Harness as an optional Code engine using the current upstream
contracts. Preserve cc-remote's current session, history, device, and preview
behavior while replacing the obsolete DSH adapter.

## Baseline

- Development branch: `feat/dsh-v015`, initially based on `d69b698`.
- Upstream target: `@deepseek-ai/dsh@0.1.5-rc.2`, pinned exactly rather than
  following a moving npm dist-tag.
- Reference implementation: `agent/dsh-backend` at `8ae99a4`, which targets
  `0.1.0-rc.6`. Use it for UI behavior, the feature inventory, and regression
  scenarios. Its transport, event fixtures, persistence assumptions, and shared
  runtime changes are not the implementation baseline.
- After the current session-continuity changes land, align this branch with the
  reviewed `master` before publishing the DSH integration PR.

## Initial scope

Keep the integration in Code. Cover engine availability, Agent Preset selection,
session discovery and creation, history and reconnect, text and image prompts,
queue and steer, cancellation, questions and approvals, model and reasoning
selection, native permission presets, context usage, effective Skills and
commands, and supported session forks. Reuse the current composer, sidebar,
process timeline, file previews, and completion behavior.

DSH continues to own model authentication, plugins, tools, and native session
storage. Plugin installation/configuration and Work support remain separate
features. Closing a view or detaching a Wrapper subscription must not cancel an
Agent or terminate the DSH process.

## Implementation sequence

1. **Connection and contract validation.** Implement the supported local
   authentication bootstrap, scoped cookies, named Remote arguments, bounded
   responses, and `/api/remote.mux` logical streams. Surface unsupported versions
   and authentication failures clearly. Keep DSH connection secrets on the
   Wrapper; do not send them through chat, relay messages, or URLs exposed to
   cc-remote clients. Confirm credential acquisition before choosing a pairing
   flow; do not disable upstream authentication.
2. **Session and history projection.** Integrate `page`, `follow`, and `control`,
   including opening snapshots, durable cursors, V3 surface placement, and
   temporary assistant attempts followed by durable settlement. Verify cold
   history separately: upstream `follow` can promote a Session after its opening
   snapshot, so it must not be assumed to be a read-only history operation.
   Preserve native message/turn identity across pagination, steering, forks,
   reconnects, and multiple clients.
3. **Controls and interactions.** Implement prompt admission, queue changes,
   cancellation, model/permission controls, Skills/commands, attachments, and
   forks against the new public interfaces. Bind `$events` questions and
   `$events/result` responses to their exact client generation and event identity;
   account for cancellation and reconnect before accepting a reply.
4. **UI integration and verification.** Add the engine to current UI components
   and capability routing. Translate old tests into behavioral cases with current
   upstream fixtures instead of preserving obsolete RPC payloads. Allocate the
   next available cc-remote protocol version when the wire changes are finalized,
   and keep Python, TypeScript, build metadata, and deployment documentation in
   sync.

## Implemented integration

The Code engine and UI are implemented on protocol v61. The current supported
feature inventory, setup and verification commands are maintained in
[`integrations/dsh/README.md`](../integrations/dsh/README.md).

- Local authority-scoped pairing and bounded Remote RPC/mux subscriptions.
- V3 ownership projection, read-only cold paging, lazy detail/images, canonical
  retry settlement and late pre-steer reply placement.
- Shared Wrapper session routing, native controls, questions/approvals, goals,
  attachments, cancellation and Wrapper-owned queues.
- Native model/preset/command catalogs with explicit unsupported or disconnected
  states. Code navigation, file previews and completion receipts use the current
  application; desktop/mobile and light/dark styles are covered.
- An isolated native acceptance runner for the pinned release, including public
  legacy-to-V3 codecs and original-source preservation. Model adapters are
  disabled except a deterministic local fixture; no production credentials are
  used and no operator sessions are upgraded.

## Acceptance

- Use isolated fixtures and data directories for compatibility and migration
  checks. Never exercise an upgrade against the operator's live DSH sessions.
- Verify supported legacy-to-V3 migration through upstream's migration machinery,
  preservation of original records, and the limits of downgrade reads. Include
  unsupported plugin events and failed migrations.
- Cover read-only cold browsing, interrupted/retried attempts, duplicate replay,
  late events, completed goals, answered questions, Wrapper queue ownership,
  multiple clients, and device/session isolation.
- Cover desktop Chromium and mobile WebKit, including mixed image prompts and
  current file/audio previews.
- Run the complete repository gate before PR publication. A merge of the old
  branch or a successful connection alone does not satisfy compatibility.

## Upstream references

- [0.1.5 release notes](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.5-rc.1)
- [0.1.5-rc.2 changes](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.5-rc.2)
- [Remote Gateway](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.2/packages/api/gateway/README.md)
- [Session Controller](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.2/packages/api/session-controller/src/index.ts)
- [Connection authentication](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.2/packages/client/connection/src/browser-auth.ts)
- [V2 to V3 migration](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.1.5-rc.2/packages/session/session-format-v2-to-v3/README.md)
