# Generic asynchronous task MCP

This optional local MCP lets Codex start a command, continue other work, and
receive its eventual result in the original thread. Commands can run scripts,
tests, external agents or other CLIs. No provider-specific adapter or model
credential handling is involved.

The four tools are `task_start`, `task_status`, `task_result` and `task_cancel`.
`task_status` without an ID lists the caller's most recent 50 tasks. A start
requires `argv`, an existing absolute `cwd`, a short `title` and a stable
`request_id`. Repeating the same request ID with the same arguments returns the
existing task; changing the arguments under that ID is rejected. The optional
`timeout_seconds` defaults to 3600 and is bounded to 1–86400, including time
waiting to start. Arguments are an argv vector, not implicitly shell-expanded.
Use an explicit shell command when shell syntax is intentional. A zero exit
code means the process exited successfully; Codex must still assess its work.
For an external agent or remote job, the command must wait for that job's real
completion and print its result. A launcher that only returns a PID/job ID would
report the launch as complete; use that tool's wait mode or a polling script.
This contract is the same for every external agent and service.

## Enable for one account

Use Codex **0.159.2 or newer** with an already-running shared official daemon.
The account's CLI, App (if attached) and cc-remote must use the same `CODEX_HOME`.
Add this entry to that account's `config.toml`, replacing both absolute paths:

```toml
[mcp_servers.cc_remote_tasks]
command = "/absolute/path/to/cc-remote"
args = ["tasks", "mcp", "--codex-home", "/absolute/path/to/codex-home"]
```

The installed cc-remote launcher must include this feature. For development,
run the repository venv's Python with `-m cc_remote.async_tasks mcp
--codex-home /absolute/path/to/codex-home`, with the repository as `PYTHONPATH`.
Configuration is opt-in; installing/upgrading cc-remote does not enable this MCP
or restart any engine automatically.

### Refresh after an upgrade

For an already-enabled standard launcher, run the installed release's command:

```bash
cc-remote tasks refresh --codex-home /absolute/codex-home \
  --thread-id EXISTING_THREAD_UUID
```

It binds only `mcp_servers.cc_remote_tasks.cwd` to the executing immutable release,
using the official version-checked config write and MCP reload APIs. Codex may
reuse an unchanged MCP configuration: switching cc-remote's `current` link and
requesting a reload alone does not prove an existing thread stopped using old
code. The release-specific working directory makes an upgrade visible to the
native MCP manager. Account credentials, launch command, arguments and other
servers remain untouched. A disabled/missing server, custom launcher or conflicting
configuration override is not silently replaced. Development launchers need their
own explicit native configuration refresh.

With `--thread-id`, refresh checks that thread's tool catalog **and calls its
read-only `task_status` tool**. It does not resume a thread, start a task, or send
a model message. A stale caller-metadata error fails verification even when all
four tools appear in the catalog. Without a thread ID, the result reports only
`refresh_requested: true`, with `thread_verified: false`; a global catalog is
not a substitute for a real thread call. Running the command again on the same
release verifies without rewriting the configuration. A failed verification can
leave the new cwd saved; inspect/retry the refresh, never restart the account
daemon or resubmit an uncertain task just to pass the check.

The tool request must carry native caller identity: the official app-server's
`_meta.threadId` or the `thread_id` in `_meta["x-codex-turn-metadata"]`.
Model-driven calls send the latter as a structured object; JSON-string transport
is also accepted. If both are present they must agree. Missing, malformed or conflicting
identities fail closed. Neither a tool argument nor an
ambient `CODEX_THREAD_ID` can select the callback recipient. The server's fixed
account home selects the socket and private database, so tasks cannot be read
or cancelled through a different account/session. State is local to that device.

## Execution and completion

A detached local worker holds the native `command/exec` connections, using
the **account's configured command permissions**. It does not copy the thread's
temporary permission overrides or grant fresh approvals. The MCP must not be
presented as inheriting a thread's more restrictive temporary sandbox. Choose
appropriate account defaults before enabling it. There is no private app-server,
unrestricted subprocess fallback, automatic daemon restart or forced takeover.
Linux's `cc-remote tasks` entry runs as the invoking user and does not use sudo.

MCP disconnection and closing a browser do not stop the worker. Cancel requests
terminate the native command and suppress unsent notifications. Closing a worker
connection causes the official daemon to terminate its command; after worker or
machine failure the command is marked interrupted, never silently re-executed.
Starting this MCP again recovers retained tasks. There is no boot-time service
installation in this feature.

On completion the worker calls official `turn/start` with empty `input` and
`toolOutput` in namespace `cc_remote_tasks`. Idle Codex threads continue;
already-active turns receive queued tool output. The native result remains tool
data in history, not a forged human message. cc-remote renders it as a task
receipt. CLI/App presentation remains owned by the official client.

In cc-remote, the composer shows a clickable **后台任务** count while tasks run
or wait for their result to be delivered. The panel shows each title, elapsed
time and current status. It remains available after the main reply ends and
does not keep the main conversation busy. Wrapper reads the account-local task
store without sending model requests; reconnecting or refreshing restores the
current level. Tasks from another account or thread do not appear here.

When a native result arrives, its follow-up is labeled **Codex 收到后台消息，继续处理**.
This source label also survives compact history restore; expanding the process
shows the bounded result. Several receipts consumed together share one label.
Tasks leave the panel after the callback turn completes successfully or a pending
notification is cancelled. Accepted callbacks stay visible while processing;
failed, rejected or unconfirmed callbacks show a retained-result warning. Their
command output remains available through `task_result`. The panel is not a
complete archive of successfully delivered tasks.

Disconnected delivery is retried every 30 seconds **only before submission**.
A safely identified, active on-disk thread may be resumed on the same daemon
without configuration overrides; archived/deleted or unverifiable threads are
not revived. If transmission might have succeeded but its acknowledgment was
lost, notification state becomes `unknown` and is not automatically resent.
Inspect `task_result` and the conversation in that case. Native rejection is
recorded as `rejected`, not success.

An initial receipt records `accepted` plus `notification_turn_id`, not success.
The worker checks that exact thread and turn through read-only, paginated
`thread/turns/list` calls with `itemsView="notLoaded"`; it does not fetch items,
resume a thread or send another message to check progress. Checks run every 30
seconds, including after worker recovery, and do not occupy a command slot for
the model's whole turn. Only that turn's `completed` status records `delivered`.
A `failed` or `interrupted` turn records notification `failed`, independently of
the command's own success. Completion means the callback turn ended normally,
not that the model semantically understood the result. If no outcome can be
confirmed within 24 hours, the receipt becomes `unknown` and remains retained.
Neither accepted nor failed/unconfirmed callbacks are automatically resent.
Older `delivered` records without a turn ID retain their original meaning
(acceptance only); upgrading does not invent an outcome for them.

Some strict Responses providers reject the call-ID-less tool output generated
by Codex 0.159.2. Such callbacks now report a failed turn instead of apparent
delivery success. This feature does not normalize the upstream model request or
make those providers compatible: inspect `task_result` for the retained output.
Raw provider error messages are not copied into the public background panel.

State is in `<CODEX_HOME>/cc-remote-async-tasks/` (private directory and SQLite
file). Each task retains at most 64 KiB of combined output. At most eight commands
are active per account. The store retains at least the newest 100 ordinary
terminal receipts, prunes older acknowledged/cancelled/rejected records on new
submissions, and caps all records at 200. Uncertain and undelivered records are
retained; hitting the cap rejects new tasks rather than deleting those records.
Idempotency applies while the receipt is retained. Tasks have one shared worker
lock per account rather than accumulating a lock file for every job.

A native timeout (exit 124 at its deadline) is reported as `timed_out`; an early
explicit exit 124 remains `failed`. Cancellation still suppresses its callback.

The worker pins its executing release through its cwd. Deployment cleanup must
continue respecting live cwd/open-file dependencies as required by the normal
deployment procedure. Private task state is not part of release cleanup.

## Validation

`tests/test_async_tasks.py` exercises execution, failures, cancellation, bounded
output, caller isolation, ambiguous delivery, recovery and MCP calls using a
local fake app-server and trivial subprocesses. Callback regressions cover
acceptance followed by provider failure, exact-turn pagination, recovery without
resubmission, unavailable history, bounded tracking and concurrent old-store
migration. These tests spend no model tokens.
They do not establish real App/CLI rendering or native end-to-end inference;
those require a separate, explicitly scoped live acceptance run. The caller
regression covers both `_meta.threadId` from the official 0.159.2 app-server's
`mcpServer/tool/call` and the structured turn metadata observed on a real
model-driven call, including the MCP SDK's parsed meta object and stdio
serialization. A tool catalog check alone does not validate
caller binding, command execution or automatic completion delivery.

`tests/test_codex_task_activity.py` covers account/session isolation, read-only
snapshots, reconnect seeding and receipt recovery in native history summaries.
`tests/test_task_refresh.py` covers upgrades with a stale thread MCP despite a
successful catalog lookup, version-conflict rejection, account/launcher boundaries,
and the distinction between a requested refresh and a verified tool call.
Web reliability tests cover continuation ordering, main-turn independence and
history/detail merging. `web/tests/background-tasks.spec.ts` exercises the task
panel and receipt expansion in desktop Chromium and mobile WebKit with local
fixtures, including the shared Claude UI. These do not submit live model turns.

Official interfaces: [app-server turn start](https://learn.chatgpt.com/docs/app-server#start-a-turn)
and its locally generated `command/exec` schema. No Relay/Web protocol change.
