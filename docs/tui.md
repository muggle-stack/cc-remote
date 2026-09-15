# Terminal workspace

[中文](tui_zh.md) | English

The terminal workspace is a client of the same relay and wrapper as Web.
It does not launch another Codex/Claude process or take over a native writer.
Closing the client does not stop wrapper-owned tasks or deferred queries.

Only the transcript viewport position controls auto-follow: exactly at the
bottom before new output, it follows; anywhere above it, it stays put. Focus
in the draft or transcript, Vim mode, and selections do not change this rule.
Following preserves the reading cursor and selection. Sending a message does
not jump a scrolled-up viewport. Scroll to the bottom or press `G` to resume;
terminal reflow preserves the bottom or the source position being read.
Each turn has a **Turn details** section, expanded by default. Progress prose
stays visible; consecutive tools/thinking under each progress message form a
collapsed summary, such as `3 个工具调用 · 修改 2 个文件 · Bash ×2 · Edit ×1`.
Enter toggles either level. Enter/Esc inside a tool body closes its tool group;
inside progress prose it closes the outer section. The final answer remains
outside the fold, and Enter on it toggles its own turn's section. New output
and completion preserve manually chosen folds. History details load on demand.

The compact bottom status keeps mode, execution state and elapsed time visible;
long activity/notice text is truncated to the terminal width. Shortcut lists
live in `Space h` rather than permanently occupying the bottom of the screen.

## Run

To try reading, selection and independent drafts without connecting to a
server, run `.venv/bin/python -m cc_remote.tui --demo`. The demo never logs in,
sends commands or starts a model. Sending is deliberately disabled.

Use the same source/protocol version as your relay and wrapper. A mismatched
server is reported explicitly; the client does not downgrade the wire schema.
Installing/running this client does not upgrade or restart either service.

```sh
uv venv .venv
uv pip install --python .venv/bin/python \
  -r requirements.txt -r requirements-tui.txt
.venv/bin/python -m cc_remote.tui --engine codex
```

For a stable command independent of the current directory, run
`scripts/cc-remote-tui` from this checkout, or install a user-local link:

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/scripts/cc-remote-tui" "$HOME/.local/bin/cc-remote-tui"
cc-remote-tui
```

Keep `~/.local/bin` on PATH. The launcher resolves symlinks and always uses its
own checkout's virtualenv and module. In contrast, `.venv/bin/python -m ...`
inside another checkout imports that checkout, which may still contain the
legacy line-mode TUI and different login handling. This launcher changes no
credentials or running services.

With a Linux user-level `cc-remote-relay.service`, local startup discovers the
actual port and reuses the same user's running relay configuration without a
password prompt. It does not persist another password, disable Web authentication,
or exempt loopback from authentication. Automatic credentials are never used for
an explicitly selected unrelated address or port. The relay must already allow
loopback access (for example, `ALLOW_PRIVATE_ORIGINS=1`); the TUI does not change
that security configuration.

For a remote relay, pass `--url wss://your-domain/ws`; remote login still prompts
without echo when needed. Existing `LOGIN_PASSWORD`, `LOGIN_USERNAME`,
`PUBLIC_ORIGIN`, `RELAY_URL`, and `ENGINE` configuration remains supported.
Non-systemd installations, SSH tunnels and multi-user logins need explicit
credentials; unavailable local configuration fails clearly without prompting.
Do not put credentials in command arguments or URLs, or send them over plain LAN
HTTP.

Pass an optional session ID to attach directly. `--machine <id>` selects an
authorized relay device. `--line-mode` retains the original line-oriented
terminal client without requiring Textual.

## Interaction

Opened sessions appear as tabs above the current-session title. In Normal mode:

- `H` / `L`: previous / next opened session, wrapping at either end.
- `Space ,`: search opened sessions across all engines and Code/Work. Type
  immediately; Ctrl+j/k or arrows select; Enter opens the result.
- `Space b d`: close the current **local tab**, choosing its neighbor. Closing
  the last tab leaves an empty workspace. This never deletes a server session,
  interrupts a task, or cancels a queued message. Reopen from `Space e`.

Tabs show a cyan circle while running, a green circle for an unread completed
turn, or a red circle for an unread interrupted/failed turn. Opening the tab in
a focused TUI clears its result marker, even when reading older messages.
Successful completion also respects the server's cross-client read receipts;
failure markers are local to this TUI process (the protocol has no failure
receipt). Merely becoming idle does not invent a completed turn.

The tab working set and selected tab are saved automatically when changed and
on exit, under `$XDG_STATE_HOME/cc-remote/tui-tabs` (default:
`~/.local/state/cc-remote/tui-tabs`). State is private and scoped to relay URL,
device and login; it contains no prompts or credentials. Concurrent terminals
merge tab additions/removals instead of overwriting each other's working set.
Startup restores these tabs and checks the catalog before attaching the selected
one; it does not resume every saved session or start a model turn. Closing all
tabs deliberately restores an empty workspace. An explicit startup session ID
overrides saved focus. Temporary and ephemeral side-chat IDs are not persisted.
The offline demo does not read or write this state.

Tabs retain loaded messages/details, drafts and reading positions in this TUI
process. Ordinary switches do not fetch history again; reconnects, replay gaps
and server history invalidation revalidate the cache. Sessions evicted from the
Wrapper pool also revalidate when reopened, since they lack a continuous live
stream. Tabs are not saved across
TUI exits. Background reports now use `Space b b` (previously `Space b`), leaving
the `Space b` prefix available for buffer commands. Update that binding in any
existing custom config to avoid a prefix conflict.

The conversation uses the full terminal width, with a one-line current-session
title at the top. Long titles are ellipsized. There is no persistent session
list until `Space e` opens the left-hand session tree. It groups sessions by
current directory using the catalog, not a disk scan; migrated sessions appear
only under their new directory. Labels show folder basenames; grouping and
search retain full paths. Use j/k or arrows to move, h/l to fold/unfold,
and Enter to open a session. The tree stays open while focus returns to chat.
Tree navigation also supports `gg` / `G` (first / last visible row),
`50j` / `50k` (counted movement), and `50G` / `50gg` (visible row 50).
`H` collapses all folders; `L` expands all folders. These tree-local keys do
not switch session tabs. `r` renames the highlighted session, not the attached
session: press `i` to edit, Esc for Normal, Enter to submit. Folder rows cannot
be renamed. The `[tree]` key layer is configurable; search keeps these letters
as ordinary text. See `Space h` for the active bindings.
Press `/` in the tree to search titles, directories or IDs (all search terms
must match). Ctrl+j/k or arrows select results; Enter opens. Esc clears search
and returns to the tree; another Esc closes it. Then Ctrl+j focuses the draft.
Ctrl+p has no default binding and no longer opens a picker.
Each session retains a separate draft, input cursor and reading position in
this process. Background output never switches the focused session.
Startup selects the newest session after the current surface's catalog arrives,
matching Web; an explicit session ID takes precedence. Use `--engine codex
--space work` to choose the initial surface. The header shows engine and space.
The default engine is Codex; --engine or ENGINE can explicitly override it.
In Normal mode, `Space c` toggles Claude/Codex and `Space w` toggles Code/Work.
The tree lists only that engine and space. Each of the four
surfaces remembers its last focused session during this TUI process; a missing
bookmark falls back to the newest remaining session. An empty surface never
continues displaying or submitting to the old session.
For an unarchived Codex Code session, confirmed deletion first requests native
conversation-tree archival, then waits for the matching archived catalog result
and command ACK before deleting. Rejection or an unconfirmed archive stops the
workflow; ACK alone never proves success. Running/queued ownership checks remain
server-owned. A local tab is removed only after confirmed deletion.

Archived sessions are separated under a collapsed virtual `Archived` folder,
then grouped by their original directory: `Archived → abc → session`.
Normal and archived rows never share a folder, even with the same cwd.
Search includes archives and expands matching ancestors; `H` / `L` also work
through this nested tree. Rename/delete still target the session leaf, never
the virtual folder. Use the archive action with `archived: false` to restore
one to its normal directory group.

On a highlighted session, `d` opens a Yes/No deletion confirmation. No is
selected by default: Enter, `n` or Esc cancels; `y` deletes, or select Yes
with `j` and press Enter. `D` requests deletion without that local dialog.
This is permanent server-side session deletion, unlike `Space b d`, which
only closes a local tab. Folder rows never delete a group. The server still
enforces its running/queued-session rules; a row disappears only after a
refreshed server catalog confirms removal. Renames use the same confirmed
catalog refresh, so a submitted command is not mistaken for a saved title.
Configure `[tree].delete`, `[tree].delete_direct` and `[confirmation]` to
change these keys. Search input keeps `r`, `d` and `D` as literal text.

### Configure shortcuts

All application shortcuts use one registry for dispatch, help and inspection.
Help groups effective bindings by scope and shows their stable configuration
IDs. Empty optional global aliases are omitted: `keys.sessions = []` means
there is no global tree alias, while `normal.tree = ["space e"]` still opens
the tree in Normal mode. It does not mean that the tree has no shortcut.
In Help, `/` opens the shortcut index ready to search by action, key or scope.
Choosing a search result only inspects its ID; it never executes that action.

Inspect without authentication or connecting to a server:

```sh
.venv/bin/python -m cc_remote.tui --list-keys
.venv/bin/python -m cc_remote.tui --list-keys 'session tree' --json
.venv/bin/python -m cc_remote.tui --config /path/to/tui.toml --list-keys
```

The JSON index includes ID, layer, scope, action, keys and enabled state,
including disabled aliases. Python callers can use `KeyConfig.index(query)`
or `KeyConfig.lookup(key, layer=...)` for reverse lookup.

`[draft].send` configures Normal-mode submission; `[keys].send` is an optional
global alias. `[reader]` owns latest-message jumps, follow, detail toggling,
older/newer detail-page loading and the command editor. `[tree].choose`,
`[tree_search].choose`, `[picker].choose` and `[form].confirm` are independent.
`[file_hints].select_1` through `select_9` configure numbered file selection.
Each dialog layer also exposes `next_field` and `previous_field`.
An empty array disables that application's shortcut, without a hidden fallback
to the old key. Standard Vim editing, Insert newlines and Esc-to-Normal remain
editing grammar, not configurable application actions. Help and inline hints
show the effective keys after overrides.

Yanks briefly highlight the copied text in every Vim text surface, including
chat, forms, question details and Markdown previews. Text objects such as
`yi(` land at the range's start; line motions such as `yy` keep the column.
The flash does not create a Visual selection or move focus. A character count
confirms the local copy; terminal clipboard forwarding still depends on OSC 52
support. Configure `[vim] yank_highlight_ms = 200` in `tui.toml` (0 disables
the flash, maximum 5000). Document changes and session switches clear it.

```toml
[draft]
send = ["ctrl+y"]

[reader]
details = ["x"]
latest_assistant = ["g A"]
```

Panel shortcuts use letter-area `Space + letter` sequences instead of function
keys. They work in main-pane Normal/Visual mode, not Insert or search inputs.
`Space h` displays the effective bindings. Defaults are listed in
[tui-keys.example.toml](tui-keys.example.toml).

Load overrides from `~/.config/cc-remote/tui.toml` (respecting `XDG_CONFIG_HOME`),
`CC_REMOTE_TUI_CONFIG`, or `--config PATH`, in increasing precedence. Relaunch
only the TUI after editing; no service restart is necessary.

```toml
[keys]
sessions = ["ctrl+y"]
focus_draft = ["ctrl+n"] # Also next picker result
focus_read = ["ctrl+b"]  # Also previous picker result

[normal]
tree = ["space e"]
engine = ["space c"]
space = ["space x"]
help = ["space h"]
```

Omitted actions keep their defaults; `[]` disables an action's binding.
Overrides replace the old keys. Unknown actions, invalid keys, duplicates and
prefix conflicts fail clearly rather than silently overriding each other.
Help, footer bindings and picker hints reflect the loaded configuration.
`[keys]` configures global workspace actions; `[normal]` configures Normal-mode
panels, scope switching and quoting. Standard Vim editing grammar (`hjkl`,
`ci(`, `daw`), Insert newlines and Esc-to-Normal retain their usual behavior.
Application confirmation and navigation keys use their respective layers above.

| Mode | Keys | Action |
| --- | --- | --- |
| Normal | `j/k`, `h/l` | Move the reading cursor |
| Normal | `Ctrl+d/u` | Move half a screen |
| Normal | `gg`, `G` | Loaded history start / newest output and follow |
| Normal | `[m`, `]m` | Previous / next message |
| Normal | `gu`, `ga` | Latest user message / latest assistant answer |
| Normal | `o` | Load four older turns |
| Normal | Enter | Expand a tool or fetch details of the current turn |
| Normal | `v`, `V` | Character / line selection |
| Visual | `y` | Copy, stay at the reading position |
| Visual | `Space q` | Quote the selection into the draft; do not send |
| Insert | Enter | Insert newline |
| Draft Insert | Esc | Stay in the draft, switch to Draft Normal |
| Reader Normal | Ctrl+o / Ctrl+i (Tab) | Previous / next jump-list position |
| Any workspace pane | `Ctrl+j`, `Ctrl+k` | Focus draft / reading pane |
| Draft Normal | `i/a`, `I/A` | Insert here / after cursor, or at line start / end |
| Draft Normal | `h/j/k/l`, `w/b`, `0/$`, `gg/G` | Move within the draft |
| Draft Normal | `o/O` | Open a line below / above and insert |
| Draft Normal | `x`, `dd/dw/d$`, `cc/cw/c$` | Delete or change text |
| Draft Normal | `yy`, `p`, `u`, `Ctrl+r` | Copy line, paste, undo, redo |
| Draft Normal | `v/V`, then `d/c/y` | Select text / line, delete/change/copy |
| Draft Normal | `ci(`, `diw`, `daw`, `yi"`, etc. | Change/delete/copy a text object |
| Draft Normal | Enter | Submit draft / command / answer |
| Draft Insert | Enter | Insert a newline, never send |
| Any | `Ctrl+e` | Submit draft to the wrapper-owned deferred queue |
| Main chat / draft | `Ctrl+x` | Stop the current turn; preserve draft and queue |
| Any | `Ctrl+t` | Answer the visible pending question |
| Draft | `Ctrl+Space` | Complete `/` actions or cached `$skill` names |
| Any | `Ctrl+q` | Exit the terminal client, not the task |

The stop shortcut is `[keys].stop` in `~/.config/cc-remote/tui.toml`:
`stop = ["ctrl+x"]` selects the default; `stop = []` disables it. It works
in main-pane Normal/Insert mode, not in the session tree or modal dialogs.
The server confirms the terminal state; queued messages may then start.
All application shortcuts share the configurable help/index registry; Vim
editing motions, text objects, and mode transitions keep their Vim semantics.

Focus and Vim mode are independent: Ctrl+j focuses the draft in Normal mode;
press i to type. Esc stays in the draft so editing motions work there. Ctrl+k
returns to the saved reading position. Reading-pane i does not change focus.
`Space q` still quotes selected history without moving the reading cursor.
Ctrl+j focuses the draft; Ctrl+k returns to reading. Repeating the key for the
already-focused pane does not change its cursor or mode. In the session picker,
these keys instead select the next/previous result, just like the arrow keys.
Enter still inserts a newline in Draft Insert; Ctrl+j is reserved for navigation.

Chat, expanded details, read-only reports, drafts, forms and modal searches
share one Vim operator/text-object implementation. Readers allow selection and
copying only, retaining their reading cursor. Draft `p` uses the latest shared
register, including text just copied in chat.

Text objects combine `d/c/y` (delete/change/copy) with `i/a` (inside/around).
Supported objects are `w/W` (word/whitespace-delimited WORD), `()`, `[]`, `{}`,
`<>`, double/single quotes and backticks; `b/B` alias parentheses/braces, and
`p` selects a blank-line-separated paragraph.
For example, `ci(` replaces text inside the nearest enclosing parentheses;
`daw` deletes the current word with surrounding whitespace. `viw` selects a
word. Nested and multiline bracket pairs work; quoted strings stay on the
current line and honor escaped quotes. Bracket matching is textual, not a
language-aware syntax parser. Missing/unmatched objects leave the draft intact.

Use `yi(`, `ya(`, `yiw`, `yaw` directly in chat, or `vi(`/`va(` followed by
`Space q` to quote. Counts compose: `2yaw`, `y2aw`, `2y3w`, `3yy`; `2yi(`
selects the next enclosing bracket pair. Motions include `w/W`, `b/B`, `e/E`,
`ge/gE`, `0/^/$`, `gg/G`, `%`, and line-local `f/F/t/T` character search with
`;`/`,` repeat/reverse. `yf:` includes the colon; `yt:` excludes it.
`2gg` goes to line two. Commands act only on loaded text, without model calls.

A change such as `c2w` plus its Insert input is one undo transaction (`u`,
`Ctrl+r`). Esc cancels pending operators; half commands do not cross panes,
sessions or panels. Search pickers are the exception: type immediately,
Ctrl+j/k or arrows select results, and one Esc closes the picker. Forms and
the draft retain Vim Insert-to-Normal editing.

This is not embedded Neovim: macros, named registers, dot-repeat changes, Ex
commands and plugins are not implemented. Tree nodes remain list navigation;
masked secret fields never copy their contents to the shared register.

Ctrl+t opens a scoped dialog for a pending model question (a choice or a
request for clarification). Type option numbers or permitted free text, then
Enter to submit. Secret input is masked and never copied into the normal draft
or transcript. It is not an ordinary new prompt. With no pending question it
only shows a notice. Non-blocking questions also have a dedicated answer panel;
their replies use ordinary user input through steer while the task is running.
See "Answering questions" below. The offline demo never sends an answer.

From reading Normal, `:` opens a command editor without overwriting the draft. Enter a
command such as `new /absolute/project/path`, `stop`, `sessions`, `model <id>`,
`effort high`, or `engine claude`, then Normal Enter. Esc first enters editor Normal;
another Esc cancels and restores the draft, as does Ctrl+k back to reading.
Known local `/` commands open their panel or action form; unrecognized slash
prefixes remain literal prompt text. `/goal resume` and `/goal pause` change
the existing Goal status; `/goal clear` requires confirmation. `/goal text`
sets an objective. Skills are prefetched for the engine, directory and profile;
typing `$` reads that cache instead of starting a directory scan. Space r refreshes
the full capability catalog if skills were changed outside this client.

Copy uses Textual's OSC52 clipboard support; the terminal and tmux must permit
it. The application clipboard is still available if the outer terminal blocks
OSC52. Quote-to-draft is entirely internal and does not depend on clipboard
configuration. Ctrl+s is unbound by default. Tab and Ctrl+i are the same key
in many terminals, so both navigate forward in the reader's per-session jump
list. Ctrl+o goes back; gg, G and message jumps record stable positions.
New jumps after going back discard the forward branch.

## Panels and shared controls

| Key | Panel | Contents / controls |
| --- | --- | --- |
| Space h | Help | Reading, editing and panel shortcuts |
| Space g | Goal / Plan | Goal status, budget, elapsed time, plan steps; edit/hide |
| Space u | Usage / Context | Context, account quotas, relative daily token activity |
| Space l | Queue | Server queue, full prompt, edit and confirmed cancellation |
| Space a | Actions | Search every public wrapper action; validated parameter forms |
| Space s | Settings | Effective model/permissions/search/fast mode; available models |
| Space r | Reports | Capabilities, skills/MCP/hooks/plugins, artifacts and read results |
| Space b b | Background | Authoritative background processes and their state |
| Space t | Status | Native status, runtime, account and partial-read errors |
| Space n | Notices | Retry, compatibility and other server notices |

The fixed progress strip shows the Goal when present, with its Plan in Space g.
Without a Goal it shows the current Plan. Finished progress retires at the next
ordinary user-message boundary; an in-flight clarification does not discard an
unfinished plan. Switching sessions and loading older history cannot replace
a newer live plan with a stale one.

Transcript headers distinguish progress, final answers, thinking summaries,
tools and process activity. Tools/thinking collapse by default and expand with
Enter. Enter or Esc folds an expanded block from any body line, returning to
its header. `o` loads older turn-detail pages; `O` returns toward newer pages.
Both directions show their configured shortcut when available. Native timestamps,
durations, exit codes, errors and completion state
are preserved. The footer shows the current activity and elapsed time, not an
estimated completion time. It cannot expose reasoning the engine did not send.

In a panel, Tab / Shift+Tab moves between fields; Enter selects an option.
Esc returns to Normal before closing that panel. Searchable palettes also
accept arrows and Ctrl+j/k. Panels stay pinned to the session they were opened
for. Returning leaves the reading cursor and unsent draft intact.

Space a uses the existing Python protocol schema, not a separate RPC implementation.
The v66 actions include `set_codex_context` in Settings / Usage, plus
`browse_files` and `get_turn_file_changes` in Reports. Directory and turn-file
pages remain read-only; context settings apply on Enter.
Their responses appear in the same session's panels, including pending context
settings and application errors. Archived turn-file reads need the native turn
ID and revision from `turn_file_changes`; they never start a model turn.
Search an action, use Tab/Shift+Tab to select a named field, and press i to edit.
Esc returns to Normal; Enter applies ordinary settings once. Destructive actions
still lock their parameters for a second confirmation. Structured fields show
a readable summary; explicitly press Ctrl+r (`[form].advanced`) to edit their
raw JSON when needed. **Field help** shows the same schema's allowed
types and enum values. Session identity is pinned; select a different session
before editing that session. Submitted does not mean completed: the wrapper's
reply in Reports or the session state is authoritative.

Examples include rename/archive/pin/fork/worktree/migrate/rollback, interrupt,
explicit takeover, model/effort/search/permission changes, Goal controls,
plugin/skill/hook management and Work projects/sources/schedules. Engine and
ownership restrictions are still enforced by the same wrapper as Web. Space s
shows model/profile catalogs; Space r keeps file previews, diffs and other reports.
Message-level fork/rewind forms seed the native fork/checkpoint identity from
the turn under the reading cursor, not an invented UI row ID. Review the target
before confirming. BTW side conversations are listed alongside main sessions;
they recover through their private replay ring, not native history/resume.
When a side conversation is closed elsewhere its retained text stays readable
but its input is disabled.

Normal Enter submits immediately (steers an already-running Codex turn).
Without inventing a running native turn, ordinary sends appear as local
`awaiting confirmation` receipts, then merge with the server echo/history by
message ID. Rejected messages keep their text with a failed status. Ctrl+e
transfers a deferred prompt to the wrapper immediately. A queue item starts at
the real terminal boundary even after the TUI exits. Space l fetches full prompts
privately instead of editing their shortened queue previews; edits preserve
attachments and cannot change an item that already started.

Use `:file /absolute/path` or `:image /absolute/path` to attach a local file to
the current draft. `:attachments` lists them; `:detach 1` or `:detach all`
removes unsent attachments. Limits and content validation reuse the wrapper's
shared validator. Existing image messages show attachment markers. File
reading requires an explicit path and rejects directories and special files.

In the main draft, `Ctrl+v` attaches a PNG/JPEG/WebP image from the Linux desktop
clipboard without sending it. Wayland uses `wl-paste` (wl-clipboard); X11 uses
`xclip`. The TUI inherits the desktop environment; it does not guess another
display or poll the clipboard. A plain SSH session cannot read the clipboard on
your SSH client computer: save/transfer the image and use `:image /path` instead.
Terminal text paste (usually Ctrl+Shift+v) works in both Normal and Insert
mode in editable fields, retaining the mode. Normal-mode paste is one undo
step; pasted newlines never submit the draft. Transcript readers stay locked.
Masked secret inputs still require Insert mode.
`Ctrl+v` in modal forms retains Textual's internal text-clipboard behavior.
The configurable `[keys].paste_image` shortcut only stages an attachment. The
original draft owns it even if you switch sessions before the read completes.

Context and quota percentages display whole numbers. The footer uses neutral
gray text with a muted blue accent for the model/current mode and shortcuts;
unrestricted permissions use a muted amber warning. Separators, idle state,
elapsed time and hint descriptions stay subdued rather than rainbow-colored.

Focused, followed completions send the same exact completion receipt as Web.
Goal dismissal is also server-owned and synchronized with other clients. A
background session is never marked read just because this terminal is open.

## Terminal boundaries

Keyboard-native controls: `Space Enter` creates a session with directory,
model, effort and allowed permission choices in one `NewSession` command.
`Space m` selects a model, `Space p` permissions, and `Space s` all settings.
Use j/k or arrows, Enter to open a field, i to edit, Esc for Normal and Enter to
save text. Existing settings apply when a value is selected with Enter, without
a JSON confirmation page. The displayed value follows the server response.
In a new-session form, select the final “Create session” row and press Enter.
Work keeps its wrapper-owned cwd/safe policy.
Model catalogs are engine/account scoped; permission catalogs are also cwd
scoped and never offer disallowed profiles. Switching cwd clears old choices.

New sessions default to `~`. Enter/i on cwd opens a directory picker powered
by the system `fzf` binary (required on the TUI machine). Type immediately;
Ctrl+j/k or arrows select, Ctrl+Left/Right browse parent/child roots, Enter
chooses a directory, and Ctrl+r refreshes. Listings come from the wrapper,
not the TUI machine. Recursive results appear progressively and refresh every
five seconds while the picker is visible, retaining the search and selection.
Each scan allows four concurrent reads, at most 256 listings and 8192 paths;
a limit notice asks you to narrow the root. Hidden directories are omitted
by the wrapper, and recursion never follows symlinks outside the root.
Selections are revalidated remotely. Esc closes the picker immediately.

Panels contain hints, not buttons: r refresh, a related actions, i edit Goal, x hide
Goal. Pickers start in Insert; Ctrl+j/k navigates results while typing.
Only the main Space a palette lists all actions. i on Goal details enters
Insert directly; returning from a nested screen still restores Normal.
Esc closes search directly. Forms reuse draft Vim text objects and Esc-to-Normal.
The `[panel]`, `[picker]` and `[form]` key layers are configurable separately
from `[normal]` and global `[keys]`. Global send targets only the top form.

`[u`/`]u` navigate user messages; `[a`/`]a` navigate AI answers (one preferred
final answer per turn); gu/ga go to the latest. Previous-message navigation
fetches another page at the boundary; o also loads older pages manually.
User text has a separate background. Progress prose and final answers share
normal text colors, including inside expanded details; thinking and tools
remain subdued. Styles do not change copied text.
Context shows percent used; 5h/Week show percent remaining. Missing quota
windows are hidden rather than rendered as question marks.

- This is a Vim-style subset, not Vim: no macros, named registers,
  sentence/tag objects or full Vim operator/motion semantics yet.
- History starts with four summary turns. Process details are fetched on
  demand. The terminal projection keeps at most 160 blocks, with 64 KiB per
  block; oversized blocks are explicitly marked. It is not a transcript export.
- PDF/SVG/Mermaid and interactive Viewer rendering need a graphical Web
  surface. `:web` gives a manual handoff address and the selected session ID;
  it never opens a browser automatically or places a credential in the URL.
  Markdown has a read-only preview; diffs remain source text.

### Chat Markdown

Assistant replies and progress prose render Markdown directly in the chat:
bold, italic, strikethrough, headings, lists, quotes, code and tables. User
prompts, thinking and tool logs retain their literal text. Expanded turn
details preserve this distinction.

Links show `label (complete URL)`, not a hidden URL behind a label. Tables
also list their link targets below the grid, so Kitty URL hints can find
complete URLs rather than column-wrapped fragments. Narrow tables switch to
label/value rows rather than hiding columns. URLs are not fetched or opened
automatically.

Vim selection, yank and quote use the visible text, including displayed URLs,
but skip image-only spacing. Stored messages retain their original Markdown.
Source anchors preserve navigation across rendering and reflow. Render caches
are bounded; excessive link expansion falls back to literal Markdown.

### Managing the server queue

Open `Space l`: `j/k` select, `Enter` reads the full prompt, and `i`
opens it for editing. Press `Esc` for Normal, then `Enter` to save.
Attachments are retained; rejected edits keep your text available to retry.
`d` asks for cancellation (`y` confirms, `n`/`Esc` returns).
`K` moves the selected message earlier; `J` moves it later.
All these keys are configurable under `[queue]` and listed in Help.

The wrapper owns the order and broadcasts changes to Web and other TUIs.
Reordering compares the displayed queue against the server queue atomically;
if another client changes it or a message is starting, refresh and retry.
This requires coordinated protocol v67 Relay/Web/Wrapper/TUI deployment.

### Markdown and image previews

When choosing from multiple files, `Esc` in the preview returns to the
same file-list page and selection. A second `Esc` returns to chat.

`Space v` discovers local Markdown/image paths in loaded assistant messages
(links, inline code and plain paths). One file opens directly; multiple files
show numbered hints. Press `1–9` to open immediately, `h/l` to page, or `j/k`
and Enter. Esc cancels. Discovery is capped at 64 paths and never scans disk.
Reads use the existing wrapper preview API, including remote cwd resolution.

Markdown is rendered as terminal text with headings, tables and code blocks.
The reader shares Vim navigation/text objects/yanking (`yiw`, `yi(`, `v`, `y`),
with Esc to close and `r` to reload. There are no edit/save actions or automatic
network link/image requests. External files require an explicit `a` gesture
in the preview to grant the wrapper's exact-file read authorization.

Graphics support is probed once before Textual owns stdin. A compatible
terminal renders images below the referring assistant paragraph, inside the
scrolling transcript. Images are clipped at the reader boundary, never cover
the draft, and disappear with their session or collapsed message. Original
paths remain selectable; copying/yanking skips display-only image spacing.
Only visible images are fetched, with a bounded cache and two concurrent reads.
Previews fit the available width and at most 12 terminal rows, preserving aspect
ratio. `Space v` opens the large image preview (choose a numbered file if
there are several). It retains the original resolution: `zi` zooms in, `zo`
zooms out, `h/j/k/l` or arrows pan, `zf` fits the whole image, and `Esc` closes.
Zoom is relative to fit size (100%–3200%). Kitty receives the original image
once; panning/zooming only updates its crop and placement, without resampling,
PNG encoding, or pixel uploads per keypress. Other renderers resample the
visible region locally. Scrolling out and back reuses up to eight parked
inline images; eviction/session changes release their terminal resources.
Removing one image never clears other images.
Under tmux, the TUI probes the end-to-end Kitty graphics transport rather than
trusting tmux's SIXEL advertisement (which can produce `SIXEL IMAGE` / `+`
placeholders). If that transport is unavailable, a colored half-cell preview
renders the image at reduced resolution without changing tmux settings.
Other unsupported terminals keep paths as text without automatic file reads.
Supported raster formats are PNG/JPEG/WebP/GIF/AVIF (first frame only), bounded
to 5 MiB/16 million pixels and downscaled to 1024×768. See the
[terminal compatibility notes](https://github.com/lnqs/textual-image#supported-terminals)
for SSH/tmux protocol forwarding; `$TERM` alone is not sufficient.

Configure `[normal].preview`, `[file_hints]`, `[preview]` (Markdown), and
`[image]` independently. Image zoom chords are written as `"z i"` in TOML.
Install the updated `requirements-tui.txt` for the optional graphics widget.
- Browser/device administration (enrollment, push notifications, PWA install)
  remains a Web/CLI administration concern, not a terminal session action.
- Drafts survive session switches and socket reconnects, not process exit.
  The reliable outbox is bounded process memory, just like the original TUI.
  Accepted deferred queries belong to the wrapper, not this terminal.

## Answering questions

The strip above the draft announces pending questions. `Ctrl+t` opens a compact
bottom panel. Blocking questions return an answer to their original request.
Non-blocking questions use steer while Codex is running, never interrupt; an
answer starts a new turn if the session has already become idle.

Questions wrap in a scrollable reading area above the reply. Ctrl+k focuses that
area; j/k, gg/G and Vim paging navigate it without submitting an answer. Ctrl+j
returns to the options/reply. The reading position is shown, and switching
questions retains each question's cursor and draft. The panel adapts to terminal
resizes, keeping the answer area available.

For async questions, select with `j/k`, arrows, or `Ctrl+j`, then confirm with
Normal `Enter`. Use `i` for a free-text answer, `Esc` for Normal, then `Enter`.
All questions must be confirmed before sending. `Ctrl+Left/Right` revisits
questions;
navigation alone does not commit a draft. The ordinary chat draft and its
attachments remain independent. Normal `Esc` closes the panel without stopping
the task; `Ctrl+t` reopens it. Configure the local keys in `[question]` and the
global entry in `[keys].answer`.

Async questions stay outside activity folds and are not final answers.
Neither questions nor turn completion reset the chosen fold state. Question UI
changes preserve the same bottom-only follow policy as streaming content.

## Tests

Install `requirements-dev.txt`, which includes the optional TUI dependency.
`python -m pytest tests/test_tui*.py` runs model-free state, transport and
headless keyboard/resize regressions, including public-event parity and an
inventory test for all protocol commands. No live model is needed.
The repository's complete local gate still applies before a PR.
