# Codex 0.167 control-plane adaptation

Target: official stable `codex-cli 0.167.0`. Existing controls and layouts are
reused; composer predictions are deliberately not enabled. Tab retains its
existing queue action. The daily CLI and Wrapper must still share the same
account's official daemon; changing a binary on disk is not daemon acceptance.
Use the repository deployment workflow for the live upgrade.

## Adapted contracts

- Command approvals use the native ordered `availableDecisions`, including exact
  command-rule and network-rule objects. Network prompts show their destination
  and protocol; additional filesystem/network permissions remain visible.
  Unknown decisions fail closed. More than five choices use the existing question
  flow in bounded pages. Legacy approval methods retain their original response
  spelling and four choices. Shared `serverRequest/resolved` cancellation still
  owns the lifetime of the question.
- Native `modelProviderMismatch` rejections show recovery guidance for resume,
  new input and steering. Bootstrap must not create a replacement conversation
  for this rejection. History remains readable; changing provider or explicitly
  forking remains a user choice. Arbitrary upstream error details are not shown.
- Code pin/unpin uses `thread/section/move` and the built-in Pinned identity,
  independent of its editable name. Catalogs include a bounded indexed page of
  older native pins. Work and old servers retain local pins. Existing local pins
  remain visible until explicitly toggled; a successful native toggle retires
  that local preference. Unpinning a legacy local pin must not remove an unrelated
  native custom section. Code unpin always consults native metadata, including
  when the catalog cache is absent or still reports an older local-only pin.
- `thread/list.readStates` drives the existing completion badge. Acknowledgement
  rechecks the exact revision represented by the row the browser saw, then sends
  it as `expectedRevision`. Conflicts and unknown outcomes are never retried with
  a newer revision or replaced with a local successful acknowledgement. Live
  terminal receipts also update native read state: a one-turn summary page must
  prove that the seen result is still the newest completed turn. Explicit unread
  marks and newer results are preserved. Work stays local; legacy servers may
  fall back only when the read-state contract is absent or explicitly unsupported.
  Native
  metadata notifications are account-scoped catalog hints; they
  cannot become chat events, turn terminals or focus changes. Delayed reads
  cannot overwrite newer completion receipts. The presentation-store disk schema
  and wire schema are unchanged.
- Async question text supports safe Markdown emphasis, code and links in the
  existing question window. The compact entry never nests links inside a button;
  raw HTML and images are not loaded. Submitted option values remain verbatim.
- The guarded oversized-resume fallback recognizes the Desktop distribution's
  `Contents/Resources/codex-cli/bin/codex`, while ordinary Code sessions continue
  to use the shared standalone daemon.

## Native features intentionally not substituted

- `attachToSource` requires a durable paginated fork and is incompatible with
  `ephemeral` and `deferGoalContinuation`. Remote BTW stays temporary, and ordinary
  forks stay visible independent conversations. Thread routing always uses
  `thread.id`, not the new shared-root `thread.sessionId`.
- Stable 0.167 exposes CLI `--worktree`, but its generated app-server request
  schema has no managed-worktree creation method. Existing Remote worktrees
  retain their tested Git lifecycle; no undocumented RPC or interactive TUI is
  launched to imitate the Desktop worktree manager.
- Work isolation and user authentication/provider settings are preserved.
  Dynamic model/effort/service-tier discovery continues through existing native
  APIs; this adaptation does not hard-code a model catalog.

## Verification

Native daemon ownership also accepts 0.167's `daemon.pid` record. Its PID,
owner, process generation and executable still undergo the existing checks,
including Linux file descriptor limits. The legacy `app-server.pid` is used
only when the new record is absent; an invalid new record cannot select a
retired daemon.

The stable binary's experimental JSON schema was exported offline and compared
with 0.159.2. In an isolated account directory, a zero-model-turn metadata probe
verified section listing, pin/unpin, unread/read updates and their notifications.
The actual Work handle also connected to the isolated 0.167 server with the
`cc_remote_work` permission profile and no active/model turn.
The unit regressions cover exact approval objects, unsupported grants, account
scope, stale read revisions, unknown mutation outcomes, old-pin discovery,
completion/history identity, and Markdown safety. Existing daemon, history,
fork, Work and frontend reliability checks remain required.

Live acceptance used temporary Code and Work conversations on macOS and Code
conversations on Linux. It verified streamed replies, shell execution with a
one-shot approval, native pin/unpin and read acknowledgements, history summaries
and expanded details, and ordinary CLI resume on the shared daemon. Existing
opt-in task MCP servers were refreshed and their read-only `task_status` calls
verified; this did not exercise a complete task start/completion callback cycle.

Persistent command/network grants, provider-mismatch recovery, old-pin migration,
read-revision races, question Markdown safety and the oversized-resume fallback
remain regression-covered rather than individually live-tested. One Linux
custom-provider account timed out connecting to its upstream API: control-plane
and CLI-sharing checks passed, but its model round trip and authentication state
were not verified. Keep that limitation separate from deployment health.

Sources: [official app-server contracts](https://learn.chatgpt.com/docs/app-server),
[official worktree behavior](https://learn.chatgpt.com/docs/environments/git-worktrees),
and the JSON schema emitted by `codex app-server generate-json-schema
--experimental` in stable 0.167.0.
