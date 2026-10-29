# Changelog

[中文](CHANGELOG_zh.md)

## v5.3.3

Align Claude native controls, ownership and terminal activity; fix side-chat reconnects, rewind history and duplicate prompts after switching engines. Wire protocol v85 and Claude Agent SDK 0.2.174 are unchanged.

- Close successful rewind dialogs and preserve the active history branch across subsequent CLI use.
- Let keyboard navigation leave streaming output and resume following at the bottom.
- Change Linux desktop engine shortcuts to Alt+1/2/3.

See the bilingual [release notes](docs/releases/v5.3.3.md).

## v5.3.2

Restore Claude file diffs after refreshing or reopening a session. Wire protocol v85 and Claude Agent SDK 0.2.174 are unchanged.

- Read native Edit/Write patches from the transcript envelope and preserve exact file paths.
- Rebuild cached Claude history so existing sessions recover their saved diffs, while preserving native transcripts, binary assets and other engines' caches.
- Keep missing, failed or discontinuous changes unavailable instead of inventing a cumulative diff.

See the bilingual [release notes](docs/releases/v5.3.2.md).

## v5.3.1

Keep Codex question/reply order and supplemental-answer bubbles stable across refresh, session switches and paged history. Upgrade Claude Agent SDK to 0.2.174; wire protocol v85 is unchanged.

- Preserve corrected native timestamps and interleave partial history around shared message identities.
- Render complete supplemental answers before their earlier question page loads, while keeping answered-state matching tied to the native question.
- Align the account-isolated transport with the new SDK session-state handshake and retain explicit settings.

See the bilingual [release notes](docs/releases/v5.3.1.md), including the independent Claude service upgrade boundary.

## v5.3.0

Add Claude Code rewind from historical user messages or `/rewind`, with explicit
conversation, file, and combined restore choices. Wire protocol v85 requires a
coordinated Relay, Wrapper and Web upgrade.

- Bound long prompts and keep rounded confirmation buttons visible on phones.
- Check native capability and file checkpoints, reject stale/busy confirmations,
  preserve drafts, and report partial results without repeating a mutation.
- Follow durable native rewind anchors across refresh and other clients,
  including returning to an empty conversation before the first message.

See the bilingual [release notes](docs/releases/v5.3.0.md).

## v5.2.2

Fix Claude child-agent return links being attached to an earlier reply when several results arrive close together. Wire protocol v84 is unchanged. See the bilingual [release notes](docs/releases/v5.2.2.md).

- Attach each return link when Claude consumes the result, keeping task completion status independent.
- Correct stale link positions and progress titles from older live/cache data, including refresh, session switches and detail expansion.

## v5.2.1

Fix the brief “Retry loading details” message when sending a new Claude message. Wire protocol v84 is unchanged. See the bilingual [release notes](docs/releases/v5.2.1.md).

- Wait for live output on new turns instead of reading history before the native transcript exists, including the transition from reply completion to idle.
- Preserve automatic detail recovery for historical turns and Claude continuations after refresh or device switches.

## v5.2.0

Add persistent Claude plans and conversation image navigation, and repair permission and continuation recovery. Wire protocol v84 replaces v83; Relay, Wrapper and Web must upgrade together. See the bilingual [release notes](docs/releases/v5.2.0.md).

- Keep approved plan documents and native task progress across refresh, device changes and restarts. Manual completion dismisses the monitor across clients while retaining the plan in history; document-only plans keep manual completion.
- Synchronize native permission changes and provide a styled, keyboard-accessible permission picker during plan approval. Preserve pending tool requests until answered or explicitly closed.
- Restore running Claude continuations and child-agent return links after refresh and device switches without reopening completed human responses or sending another model prompt.
- Browse uploaded and generated images with mobile swipes, keyboard and navigation buttons; preserve zoom panning and expose original-image retry controls over cached thumbnails.
- Preserve plan identity, checklist and dismissal state through replay, native mirroring and rollback; include the plan database in activation snapshots and refresh canonical plans in the TUI.

## v5.1.1

Explain update failures, recover undelivered Relay update submissions, and report rollback state accurately. Wire protocol v83 is unchanged. See the bilingual [release notes](docs/releases/v5.1.1.md).

- Identify DNS, TLS, timeout, SSH authentication and sudo failures without changing the user's network configuration or exposing credentials.
- Reuse the original remote update job with durable launch receipts; preserve uncertain and legacy transactions for explicit reconciliation.
- Distinguish preflight failures that leave the existing service unchanged from completed or incomplete rollback.

## v5.1.0

Add opt-in account quota windows and keep cross-session delivery receipts with their send tools. Wire protocol advances from v82 to v83. See the bilingual [release notes](docs/releases/v5.1.0.md).

- Add optional monthly account quotas alongside 5-hour and weekly windows, with
  per-account active usage queries disabled by default. Native Claude quota
  events continue without a helper or model request. Wire protocol v83 requires
  Relay, Wrapper and Web to upgrade together.
- Wrap long inline hashes and keep permission labels readable on narrow screens.
- Show message delivery status, recipient, account and copyable content inside the originating MCP tool call; preserve receiving-session navigation and collapse raw tool data.

## v5.0.1

Repair legacy Desktop compatibility, refresh the bilingual feature guides, and preserve Claude background continuations in history. Wire protocol v82 is unchanged. See the bilingual [release notes](docs/releases/v5.0.1.md).

- Stage a signed repair for the known macOS Desktop shell so it loads the configured Relay UI instead of stale bundled assets; preserve cookies, settings and native integrations.
- Keep Claude continuation identity, timeline order and terminal outcomes consistent across live events, refreshed history and browser caches; retain original provider errors without marking successful continuations incomplete.
- Rebuild derived Claude history pages once when the history cache advances from schema 50 to 51; preserve native transcripts, indexes and binary assets.
- Document Claude/Codex/DSH capabilities and current boundaries in both READMEs, with 13 sanitized Desktop screenshots and a local demonstration fixture.

## v5.0.0

Add optional DSH integration alongside Claude/Codex, stabilize session history and previews, and harden reconnect recovery. Wire protocol v82 requires a coordinated upgrade from v79. See the bilingual [release notes](docs/releases/v5.0.0.md).

- Reconcile streamed Claude text and thinking with canonical transcript message IDs to avoid duplicates after history refresh. Wire protocol v82 requires Relay, Wrapper and Web to upgrade together.
- Retain file previews, centered layout, preview mode and reading position when switching engines or sessions.
- Keep inline site icons visible in Markdown previews by excluding them from document image spacing.

- Show the current DSH session mode beside the Code page title. Replace the unavailable official plugin-market search with an explanation while retaining local plugin management.
- Stream DSH terminal output with bounded render acknowledgements, pipeline ordered input, and keep the final row visible when centering the panel. Use the shared terminal picker instead of a system dropdown.
- Restore touch scrolling through DSH terminal history; incoming output preserves the reading position and swipes never send shell input.
- Create native DSH schedules directly from a form. Keep the DSH side-chat implementation dormant until the official API supports session deletion: disable menu, shortcut and `/btw` entry points, block stale clients from creating forks, and preserve existing records. Claude/Codex side chats remain available.

- Add DSH 0.2.1-rc.2 on the current cc-remote UI, with a separate white/blue default palette. Wire protocol v82 requires Relay, Wrapper and Web to upgrade together. See [DSH integration](docs/dsh-integration.md).
- Reuse the background-task dock and process side panel for DSH, with nested child navigation, folded reasoning and native PTC tool hierarchy. History reads never activate an agent.
- Preserve clickable native agent-return receipts for Claude, Codex and DSH. Show parent continuation only after actual parent activity resumes.
- Add DSH workflow, trajectory, automation, preset, runtime settings, plugin package and native terminal panels. Closing a terminal view retains its process, and reconnect never resends keyboard input.
- Align DSH session cards, mode descriptions, permissions and appearance controls; hide setup-only commands and empty older-history pages from new sessions.
- Render bare Markdown `<br>` breaks in answers and tables without adding a leading blank line; keep code examples and other HTML literal.

- Hide the return-to-bottom button after an intentional downward gesture reaches the latest content, including clamped wheel and touch gestures.
- Ignore stale Codex child catalog entries whose source is missing, while retaining previously observed activity during temporary source failures.
- Register agent-detail requests before sending them and honor replacement text updates to avoid lost fast responses and duplicated content.

- Protect DSH creation, forks, uploads and controls against uncertain outcomes and reconnect replay; preserve queued work and shared resident capacity.
- Reduce regression overhead and default local/CI pytest to two workers, preserving serial diagnostics and CI timing reports.

## v4.0.29

Refresh the new-chat composer and make Claude/Codex controls usable before the first message. Wire protocol advances to v79. See the bilingual [release notes](docs/releases/v4.0.29.md).

- Use a compact, growing Code composer with current engine icons and consistent account, directory, attachment, model, effort, speed and permission controls.
- Route slash commands like active conversations; keep local controls local and preserve drafts for commands requiring an existing session.
- Configure Claude permissions before creation and apply the selected mode to the first message, preserving existing defaults and Work policy.
- Preserve explicit hidden/provider model IDs; share effort choices with validation so unavailable levels are not offered and supported selections survive catalog refresh.
- Open extension catalogs using the draft account and directory; recover scoped skill discovery after failures and reconnects without creating a model turn.
- Keep Work's private-directory boundary and reuse unchanged signed Android preview.2 and unsigned HarmonyOS preview.5 shells with original provenance.
- Web, Relay and Wrapper must upgrade together to wire protocol v79.

## v4.0.28

Restore delivered files in Artifacts and show native Codex agents in background tasks. Wire protocol v78 is unchanged. See the bilingual [release notes](docs/releases/v4.0.28.md).

- Discover explicit local paths and directory-introduced filenames in assistant lists and tables, including Markdown tables with optional outer pipes.
- Restrict table discovery to the filename/path column; preserve file access boundaries and avoid directory crawling.
- Track native Codex child agents through their own turn boundaries, including legacy completion markers, and show their activity beside MCP background tasks.
- Preserve known task state during partial refreshes or source failures, isolate inventory limits per parent, and retain reconnect synchronization.
- Reuse the unchanged signed Android preview.2 and unsigned HarmonyOS preview.5 shells from v4.0.27 with their original build provenance; existing users need no native reinstall.

## v4.0.27

Restore link previews for larger site images and keep monochrome site icons readable across themes. Wire protocol v78 is unchanged. See the bilingual [release notes](docs/releases/v4.0.27.md).

- Accept bounded larger source images and multi-size favicons, including Hugging Face assets, then resize them to the existing static PNG output limits.
- Render transparent neutral single-color icons, such as GitHub's, in the theme's foreground color in message links and preview headers.
- Preserve colored and opaque artwork, image decoding limits, public-address validation and metadata-only fallback.
- Include unchanged signed Android preview.2 and unsigned HarmonyOS preview.5 shells; existing mobile users need no native reinstall.

## v4.0.26

Customize colors and fonts independently for each engine and appearance mode, with clearer preset content colors. Wire protocol v78 is unchanged. See the bilingual [release notes](docs/releases/v4.0.26.md).

- Add an Appearance panel with live accent, background, text and font editing; preserve legacy themes and sync edits across browser tabs.
- Save light/dark styles separately for each engine, follow the system mode, and reset only the visible style.
- Use themed palette/font menus with aligned samples and a touch/keyboard color picker with HEX input; derive custom status from actual color edits.
- Coordinate the Codex light default with its purple identity, and distinguish bold text, links, inline code and selections across all six presets.
- Keep settings controls readable for low-contrast custom colors and retain code's monospace font.
- Include unchanged signed Android preview.2 and unsigned HarmonyOS preview.5 shells; existing mobile users need no native reinstall.

## v4.0.25

Render Graphviz SVG exports correctly and open HarmonyOS external links in the system browser. Wire protocol v78 is unchanged. See the bilingual [release notes](docs/releases/v4.0.25.md).

- Accept complete SVG prolog comments around a DOCTYPE and self-closing SVG roots without relaxing file-access or size limits.
- Serialize sanitized SVG as XML so non-breaking spaces remain valid in file previews, Markdown and Mermaid images.
- Route user-initiated external HTTP(S) links and popup destinations from the HarmonyOS client to the system browser, preserving the mounted chat and drafts.
- Keep same-origin authenticated navigation in the app, settle popup handlers, and prevent duplicate browser launches with failure feedback.
- Include updated unsigned HarmonyOS preview.5 and the unchanged signed Android preview.2 shell; iOS, Android and shared Web link routing are unchanged.

## v4.0.24

Restore Claude MCP question tools and let users read the conversation before answering. Wire protocol v78 is unchanged. See the bilingual [release notes](docs/releases/v4.0.24.md).

- Advertise single and batched `ask_user` inputs with a compatible object schema, validating exclusive forms in the handler.
- Recover interrupted ask/message tool refreshes without replacing the native process; respect disabled routes and retain recovery intent until tool discovery succeeds.
- Collapse pending questions into a resume bar above the composer divider in main and side chats. Keep Claude waiting and preserve the question page, answers and scroll position.
- Clear a single-choice selection when the user clicks or edits a custom answer, while preserving choices during Tab traversal and programmatic focus. Multi-select supplemental answers remain supported.
- Include the unchanged signed Android preview.2 and unsigned HarmonyOS preview.4 shells; existing users need no phone reinstall for this release.

## v4.0.23

Add account-scoped Claude/Codex session messaging, durable delivery to busy recipients, and paginated user questions. Wire protocol advances to v78. See the bilingual [release notes](docs/releases/v4.0.23.md).

- Add an opt-in, device-local messaging MCP for Claude and Codex, with account-scoped caller identity, delivery receipts and retry deduplication.
- Open the exact source or destination conversation from message links, including across engines and accounts, while preserving session drafts.
- Present Claude native `AskUserQuestion` and cc-remote MCP `ask_user` batches in the same centered, paginated form, preserving edits between pages and submitting all answers together.
- Indicate selected options with themed card colors and use rounded Previous/Next controls without selection icons.
- Refresh old persistent Claude workers' message tools at an idle boundary without restarting the native process; retain messages for busy targets in a durable, deduplicated inbox.
- Wire protocol v78 requires Web, Relay and Wrapper to upgrade together; see [interactive questions](docs/user-questions.md) and [session messaging](docs/session-messages.md).

## v4.0.22

Preserve shared Codex daemons across Linux Wrapper restarts and preview raster
images whose file extension does not match their content. Wire protocol v76 is
unchanged. See the bilingual [release notes](docs/releases/v4.0.22.md).

- Launch native Codex lifecycle commands through an isolated systemd user service, preserving account environment and resource limits.
- Hold a process-bound launch barrier across upgrade inspection and stop, rejecting busy, changed or unverifiable service state without forcing a stop.
- Require a one-time maintenance stop before upgrading a running legacy Linux systemd Wrapper that cannot acknowledge the barrier.
- Detect supported raster formats from file contents in previews, Artifacts and Markdown images while retaining file-access and size boundaries.
- Include signed Android preview.2 and unsigned HarmonyOS preview.4 packages; native shell code is unchanged.

## v4.0.21

Add per-session Artifacts and preserve unfinished chat state across Claude/Codex
navigation. Wire protocol v76 requires Web, Relay and Wrapper to upgrade together.
See the bilingual [release notes](docs/releases/v4.0.21.md).

- Collect uploaded attachments and delivered files in a compact, date-grouped catalog with separate previews and bounded thumbnails.
- Validate cold-session account/Code/Work ownership before file access; cap durable upload storage and handle multibyte filenames safely.
- Reserve Cmd/Ctrl+Shift+E for centered side chat and add Cmd/Ctrl+1/2 engine switching while preserving questions, drafts and file previews.
- Center mobile question dialogs, use attributed engine icons and a replayable Claude completion animation, and refine notices.
- Remove native-only Harmony chat controls; publish the updated unsigned preview.4 HAP alongside the unchanged Android preview.2 shell.

## v4.0.20

Add preview Android and native HarmonyOS clients, optional Huawei Push Kit
notifications, and a Codex checkpoint correction. Wire protocol v74 is unchanged.
See the bilingual [release notes](docs/releases/v4.0.20.md).

- Keep chat and drafts mounted while browsing links inside either mobile client.
- Add opt-in HarmonyOS completion push without changing Web/PWA notifications.
- Recover from lazy notification adapter failures and revoke ambiguous native subscriptions before retrying.
- Allow unchanged pre-existing untracked files after a clean forward Git commit without a false checkpoint warning; committed turns still use conversation-only rollback.
- Publish a signed Android APK and an unsigned HarmonyOS HAP requiring the user's own device signing profile; retain preview acceptance limitations.

## v4.0.19

Speed up large-session history loading and fix Claude conversations that remain
marked as running after a steered reply finishes. Wire protocol v74 is unchanged.
See the bilingual [release notes](docs/releases/v4.0.19.md).

- Reuse bounded history pages and transcript metadata instead of repeatedly scanning and translating unchanged records.
- Reuse read-only Codex readers within one history request and prefetch one older summary page when idle.
- Validate cached pages against their source while keeping native turn identities, images and live activity current.
- Retire the matching implicit Claude background activity when the real steered human reply ends, including after journal replay.
- Existing independent Claude services need the documented drained owner upgrade for the steering fix; Wrapper-only updates preserve the old owner.

## v4.0.18

Simplify side-chat access and headers, keep empty mobile chats at their normal
height, and fix shared launcher compatibility on macOS. Wire protocol v74 is
unchanged. See the bilingual [release notes](docs/releases/v4.0.18.md).

- Open side chat from the session menu using the existing chat and composer focus behavior.
- Use a chat icon, short title and Codex/Claude badge in the side-chat header.
- Give empty and populated mobile chats the same sheet height while respecting the keyboard viewport.
- Compile new macOS shared launchers with an explicit macOS 13.0 target and matching bundle metadata, avoiding an unsupported toolchain default.

## v4.0.17

Add webpage previews and automatic site icons, refine file-path tooltips, and focus
explicitly opened side chats. Wire protocol v74 is unchanged.
See the bilingual [release notes](docs/releases/v4.0.17.md).

- Preview public links in chat, side chats and Markdown files without shifting the surrounding content.
- Discover site icons automatically; keep hover cards open while metadata loads and replace old popovers immediately when moving between links.
- Wrap local paths in compact rounded tooltips while preserving the exact copied path.
- Focus explicitly opened Claude/Codex side-chat composers without stealing focus when returning to a session.
- Restore the running indicator on the exact current Claude side-chat turn after reopening or refreshing, including when an older background response claim remains present.
- Bound source image dimensions and normalize the first frame to a static thumbnail; cap decoder concurrency and include Pillow in Relay packages.

## v4.0.16

Restore Claude input after completed background responses and include the centered
side-chat slash shortcut from the unpublished v4.0.15 tag. Wire protocol v74 is
unchanged. See the bilingual [release notes](docs/releases/v4.0.16.md).

- Preserve the original response owner when later task notifications join the same response, including journal replay after reconnect.
- Accept the next message after the real completion without confusing unrelated continuations or queued human input.
- Focus centered Claude/Codex side-chat input with `/`; keep main-chat focus when side chat is docked or hidden.
- Existing independent Claude services need the documented drained owner upgrade; updating only Wrapper does not activate the service fix.

## v4.0.15

Focus the correct composer with `/` when a side chat is expanded in the center.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.15.md).

- Focus the centered Claude/Codex side-chat composer; keep the main composer as the target when side chat is docked or hidden.
- Preserve normal slash typing in editable fields and focus within nested dialogs and menus.
- Keep read-only centered chats and other centered panels from redirecting focus behind themselves.

## v4.0.14

Expand side panels into a centered view and restore side-chat and Claude completion state reliably.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.14.md).

- Expand or dock side chats, files, diffs and previews while preserving drafts and reading state.
- Keep existing side-chat content visible during history synchronization; use paperclip attachment controls with cards above the input.
- Contain keyboard focus across menus, dialogs and preview frames; preserve UTF-16 HTML and correct HTTP framing.
- Keep Claude compaction summaries separate from human inputs so completed turns finish correctly.
- Clarify built-in Claude/Codex multi-account support for native OAuth and compatible API accounts.

## v4.0.13

Refine Goal dialogs and keep running indicators on the current Codex turn.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.13.md).

- Use a target icon, smaller objective text and compact metrics in the Goal dialog,
  preserving Codex budget controls and Claude check details.
- Keep mobile dialogs positioned correctly outside the transformed chat pane.
- Bind every automatic Codex turn before its first output so the running spark
  and process label move to the successor even without an idle gap.
- Preserve Stop state and reconnect ownership while allowing official user
  messages to refine the turn binding without duplicate rows.

## v4.0.12

Correct Codex Goal continuation and background callback state reporting.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.12.md).

- Keep automatic turns running across stream handoffs and recover an active Goal
  after reconnect by verifying its exact native turn on the shared daemon.
- Let the current native turn handle account switches, preserving a completed
  predecessor's result and its successor's separate lifecycle.
- Confirm callback failures across separated reads and reconcile later completion
  without resending callbacks or rerunning commands.
- Repair legacy failed/unconfirmed receipts with known callback IDs within one
  bounded tracking window; retain unresolved results for inspection.

## v4.0.11

Add optional Codex asynchronous tasks and restore Claude side-chat history reliably.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.11.md).

- Run commands or external agents through the shared official Codex daemon and
  return results to the originating account/thread without a provider-specific adapter.
- Show background activity above the composer and label resumed processing; track
  the exact callback turn before marking delivery successful and retain failed results.
- Enforce task deadlines at submission, classify native timeouts correctly and
  avoid replaying commands or callbacks whose outcome is uncertain.
- Refresh an enabled task MCP after upgrades without restarting its daemon.
- Display restored Claude BTW history at once, preserve completion markers and
  keep background continuations in order.
- Fix macOS Bash 3.2 installation and Supervisor preflight compatibility, and
  document recommended Release installations with one-command updates.

## v4.0.10

Add managed Linux migration, Supervisor installation/adoption and verified upgrade
backup retention. Wire protocol v74 is unchanged. See the bilingual
[release notes](docs/releases/v4.0.10.md).

- Register the CLI and actual service binding while preserving native login,
  pairing, external configuration and the independent Claude service.
- Support older Supervisor interfaces and remove newly registered groups when a
  fresh installation rolls back; preserve prepared Relay releases on backup failure.
- Retain the active release, one previous recovery set and referenced dependencies;
  clean only recorded generations after acceptance and revalidate before deletion.
- Discover dormant services, environment files and executable aliases. Unresolved
  indirect commands or incomplete visibility defer deletion and retain older files.
- Document one-command updates and explicit adoption for older custom installations.

## v4.0.9

Improve mobile sidebar gestures and preserve session-list momentum after opening.
Wire protocol v74 is unchanged. See the bilingual [release notes](docs/releases/v4.0.9.md).

- Reduce conversation and Markdown rendering during sidebar navigation and streaming.
- Preserve flick speed and repeated gesture reversals; make fade, blur and rounded
  corners follow drag progress in both themes.
- Keep mobile scroll layers stable and avoid interrupting a list fling started
  during the opening animation; cancel abandoned long presses.
- Preserve viewport positioning for image previews and paste editors.
- iPhone PWA testing confirms the immediate-scroll interruption is resolved;
  a small first-drag jitter remains.

## v4.0.8

Add native Codex cross-session message views and responsive mobile drawers;
fix session state, model restoration and deployment cleanup. Wire protocol v74
requires a coordinated upgrade. See the bilingual
[release notes](docs/releases/v4.0.8.md).

- Display native Codex cross-session messages and open their source conversations
  in a scoped read-only history view, preserving delivery outcomes and the
  original conversation's reading position.
- Reveal the mobile sidebar beneath a rounded foreground conversation, with
  theme-aware shading, optional opening feedback and responsive repeated swipes.
- Stabilize the main conversation layout when opening desktop BTW side chats.
- Preserve Claude continuation order during live history refreshes and prevent
  slow session-list reads from restoring a completed session's running badge.
- Restore the native Codex model and settings when resuming long sessions.
- Keep one complete previous deployment rollback generation, plus all active
  dependencies and unresolved recovery files. Trace retained artifacts' complete
  dependency chains and refuse cleanup when process visibility is incomplete.
- Start Codex daemon lifecycle commands from a stable home directory and add a
  read-only configuration check to deployment acceptance.

## v4.0.7

Add model-specific Codex speed selection and repair Claude recovery and private
side-chat cleanup. Wire protocol v73 requires a coordinated upgrade. See the
bilingual [release notes](docs/releases/v4.0.7.md).

- Read Codex speed options from the current account's native model catalog and
  offer them in a model-bound picker for Code, Work, and side conversations.
  Preserve exact native tiers across reconnects and clear unsupported tiers
  when changing models. Ultrafast appears only when the account/model offers it.
- Recover live Claude prewarm sessions before their first transcript exists and
  reuse recovered default-account residents instead of spawning extra workers.
- Retain private side-chat identity receipts after cleanup so late native writes
  cannot expose retired forks; hide proven metadata-only catalog entries.
- Jump to the latest message immediately when the bottom button is clicked.
- Use explicit HTTP proxy settings for the Relay connection while preserving
  environment bypass rules and direct loopback access.

## v4.0.6

Preserve session history and completion state, recover sessions safely, and add
optional active Claude quota reads. Wire protocol remains v72. See the bilingual
[release notes](docs/releases/v4.0.6.md).

- Preserve long-turn output, paged process details, side-chat user boundaries and
  Codex steering identities through history hydration and live replay.
- Match Claude terminal receipts to consumed human input and recover old sessions
  only after a proven service restart, without replaying accepted queries.
- Retain rejected prompts and attachments for manual retry; restore local file
  preview links and ignore cost-only Claude transcript growth.
- Refresh Claude quota on focus/reconnect, meter open/manual refresh and visible
  page polling through optional account-scoped, operator-owned usage helpers.
  Preserve valid cached readings on failure and distinguish missing configuration.

## v4.0.5

Improve Claude responsiveness, Stop recovery, and shared-control feedback.
Wire protocol remains v72. See the bilingual [release notes](docs/releases/v4.0.5.md).

- Keep slow session startup and context reads from blocking unrelated commands;
  preserve session ordering, bounded intake, and reliable command receipts.
- Isolate relay sends by device and move stalled browser cleanup off the forwarding path.
- Keep one Claude Stop recovery owner and its original deadline across background
  handoff; distinguish a stop timeout from a confirmed interruption.
- Fix Codex Shared launch and App discovery with protected native socket aliases,
  retaining account, ownership, and permission checks.
- Restore animated subagent activity and independent process expansion; initialize
  BTW model selection from the parent and unblock `/` when its settings are closed.

## v4.0.4

Discover Claude models from the installed CLI and improve inline code emphasis.
Wire protocol remains v72. See the bilingual [release notes](docs/releases/v4.0.4.md).

- Populate Web, side-chat and TUI model pickers with the native model catalog and
  supported effort levels, without sending prompts or resuming sessions.
- Refresh the catalog when opening model settings, scope its cache by account,
  directory and CLI build, and preserve explicitly selected historical models.
- Keep Work discovery within its runtime policy and inherit native Claude model
  and effort defaults instead of imposing a fixed Opus/max selection.
- Refresh Codex models on page reload, reconnect and picker open; share concurrent
  reads per account and retain that account's last good list on discovery failure.
- Support Codex 0.156's protected socket aliases while preserving account ownership
  checks and detecting replacement of the underlying app-server listener.
- Upgrade the verified Python Claude Agent SDK from 0.2.151 to 0.2.157. Wrapper
  upgrades across this SDK boundary require the documented drain/installer path.
- Render inline code in softer harness colors without a background badge; actual
  links retain their link styling and fenced code blocks retain their background.
- Expand Claude thinking text by default. Pressing `/` outside an editor focuses
  the composer without inserting a character; pressing it again types normally.

## v4.0.3

Fix duplicated and misplaced Claude messages when browsing overlapping history pages.
Wire protocol remains v72. See the bilingual [release notes](docs/releases/v4.0.3.md).

- Reconcile optimistic browser message IDs with their unique native user-message
  aliases across loaded and cached history pages.
- Keep established row order during partial cache updates, including when
  timestamps are missing or equal.
- Preserve expanded process details and native detail lookup IDs without changing
  live completion state. Keep distinct native messages even when their text matches.
- Repair duplicate copies when reading cached pages. Cover pagination and reloads
  with reliability tests and Chromium/iPhone WebKit regressions.

## v4.0.2

Fix stale Claude activity and coordinate device upgrades with their Relay.
Wire protocol remains v72. See the bilingual [release notes](docs/releases/v4.0.2.md).

- Retire anonymous activity received before the native human-input echo when
  that response ends. Preserve unrelated background tasks and support existing
  persistent Claude services without restarting them or resubmitting prompts.
- Check/update the paired VPS before activating a device update; skip an already
  current compatible Relay. Preserve the exact remote transaction across lost
  SSH connections. The downloaded installer also covers the first v4.0.1 upgrade.
- Show authenticated device/server version mismatches in the Web device list
  and active conversation without changing the wire protocol.
- Allow explicit registration of existing immutable Mac installations, retaining
  their root, LaunchAgent identity, environment and log paths.
- Initialize Work stores before optional Codex readiness checks so a slow probe
  cannot cause a false first-install migration failure.

## v4.0.1

Add managed release updates and verify Codex shared connections after activation.
The wire protocol remains v72. See the bilingual [release notes](docs/releases/v4.0.1.md)
for the one-time upgrade from v4.0.0.

- Add `cc-remote update` for installer-managed local Relay/Wrapper releases,
  with stable-version discovery, check-only and pinned-version modes, verified
  bundles, preserved service identity, exclusive activation and protocol-change
  guards. Reuse immutable installation and rollback; leave independent Claude
  services and custom deployments under their existing lifecycle.
- Preserve operator-provided macOS Wrapper environment settings during upgrades.
- Prepare account-scoped Codex daemons without restarting active native clients,
  check CLI and Wrapper transport readiness, and print the result after activation.
  Report unavailable shared connections instead of silently falling back to a
  private server; preserve explicit opt-outs and existing terminal tasks.

## v4.0.0

cc-remote v4 brings persistent Claude sessions, native steering, durable history
and per-turn diffs, richer side chats and previews, and a terminal workspace
preview to the Claude/Codex control plane. See the bilingual
[release notes](docs/releases/v4.0.0.md) for highlights and upgrade requirements.

### Final stability fixes

- Drain injected Claude background responses without blocking the human Result
  or the next prompt. Track actual native continuations independently of child
  task status, including when the CLI omits an internal user echo.
- Keep task results absorbed during an active response on that response's
  original timeline. Restore consumed queued human prompts from native JSONL
  and remove empty continuation process disclosures.
- Recover the original working directory from bounded native transcript reads
  when a large leading image hides it from the SDK catalog.
- Deduplicate manual compaction using the exact native boundary receipt.
- Yield live-output scroll corrections to touch/scrollbar gestures and keyboard
  viewport changes; suppress duplicate iOS keyboard paste deliveries while
  preserving deliberate repeated pastes.

### Changes since v3.0.0

- Track native Claude continuations and their live processing state under protocol v72.
- Combine terminal queue controls and session recovery under protocol v71.
  Upgrade Relay, Wrapper, Web and TUI together. TUI recovery replaces replayed
  text without duplicating existing output.

- Add an optional independent Claude session service: Wrapper reconnects recover
  accepted turns and pending questions without resubmitting prompts. Include a
  separate service installer and preserve account isolation.
- Support native Claude steering during active work, retaining message, attachment
  and token ownership. Reserve private BTW identities before launch, refresh live
  context without overwriting newer readings, and retry busy session-list reads.
- Replace replayed text prefixes and bound history summaries. Protocol v70
  requires Wrapper, Relay and Web to be upgraded together.

- Preview `.mmd` and `.mermaid` Work artifacts as diagrams, with a source view.

- Keep Claude's internal recovery prompts hidden after compaction and rebuild
  stale projections. Show autonomous replies after agent completion in separate
  live process sections, with their own thinking, tools and working indicator.
- Stream subagent text without waiting for whole messages. Recover agent-detail
  read errors and timeouts, refresh active source-backed agents, and show each
  agent's own running state.
- Use a subdued text shimmer for active process and tool summaries. Present
  readable command/search inputs and structured tool output, with raw data
  available on demand.

- Show native input/output token counts beside the working spark, with exact
  counts and cache usage on click or tap. Use subdued counters that show
  the latest readings directly. Keep per-turn ownership and
  replace-only recovery snapshots across reconnects (protocol v68).


- Request readable Claude thinking summaries in SDK sessions and show returned
  text in the existing thinking timeline. Preserve native thinking mode,
  token budget and effort across new sessions, resume and private forks.

- Accept provider-native Claude model ids (e.g. `glm-5.2` behind a custom
  `ANTHROPIC_BASE_URL` gateway) as explicit model selections: they are handed
  to Claude Code, persisted in the private session store, and restored across
  reconnect and cold resume. A model id merely *observed* in transcript or
  `/context` metadata still cannot replace an explicit selection, so a
  gateway's raw upstream name never masquerades as the model the user chose.
  Claude-branded observations keep working, including Vertex/enterprise forms
  such as `claude-sonnet-4-5@20250929`. Official `claude-*` selections and
  native takeover are unchanged.
- Add atomic server-owned queue reordering (protocol v67), with configurable
  TUI editing, cancellation and ordering controls. Reject stale queue snapshots
  and changes while a message is starting. Deploy Relay, Web, Wrapper and TUI
  together; protocol v66 clients cannot connect to protocol v67 services.
- Replace Claude's persistent background-task panel with a compact composer
  indicator. Open details above the trigger on desktop or in a centered mobile
  dialog; native task completion removes finished items and closes the indicator
  when no tasks remain, independently of the parent reply's completion.
- Track Claude compaction through native status and boundary events, preserve
  valid context readings during refresh failures, and show the applied automatic
  compaction threshold separately from model capacity. Hide internal command
  messages and keep completed-turn timestamps stable when rebuilding history.
  Require Claude Code 2.1.263 or newer for the verified control APIs.
- Keep account-specific Claude controls attached to the correct session and
  omit transient Codex thread notifications that have no readable history from
  the session list.
- Show explicit timed-message tags, a rounded moving session outline and the
  next scheduled send time. Tasks use account-scoped native queue receipts
  independently of Goals (protocol v67). Session menus and task popovers
  avoid the sidebar footer and the mobile visual viewport.

- Allow a reverse proxy that reaches the relay from a non-loopback address (a
  Tailscale IP, a LAN address, the Docker bridge gateway) to be trusted through
  `FORWARDED_ALLOW_IPS`, which appends IPs or CIDR networks to the built-in
  loopback defaults. Previously only a loopback proxy was trusted, so uvicorn
  ignored `X-Forwarded-Proto` and every WebSocket was rejected with 403 while
  page loads and `/api/*` kept working. Wildcard and malformed entries are
  rejected at startup instead of silently never matching.
- Backport shared improvements from the DSH branch without adding a third
  engine (protocol v66): rounded Claude/Codex Goal dialogs with native save
  confirmation and mobile keyboard recovery; directory links open `/open`, and
  XLSX previews show saved cells, sheet tabs and original-file downloads.
- Keep streamed replies updating after steering, preserve manually expanded
  process details, and support Enter/Shift+Enter/Escape in assistant questions.
  Retain valid context readings during transient refresh failures, use a compact
  custom engine menu, and allow failed Skills reads to retry.
- Reorganize the bilingual setup documentation around source deployment and
  document shared Codex App, CLI and Wrapper daemons on macOS and Linux.

- Align the Codex context gauge with native compaction estimates (protocol v62).
  Match bounded local log reads to the account, thread and latest rollout sample;
  invalidate estimates after compaction and show recent request usage without a
  percentage when no matching estimate is available. Refresh active visible sessions.
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
