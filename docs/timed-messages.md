# Timed messages

Codex's ordinary queue messages do not include a schedule or a timer-origin
flag. cc-remote therefore uses explicit local task receipts, never prompt
wording, to render the **定时任务** tag and sidebar countdown.

The scheduled-message helper uses the account's existing official app-server.
It queues into the specified native thread; it does not create a new session
or a Goal. The helper process is independent of the Wrapper process.

From the cc-remote checkout or installed release directory, using its Python
environment:

```bash
.venv/bin/python -m cc_remote.timed_tasks start \
  --codex-home /absolute/path/to/the/accounts/codex-home \
  --thread NATIVE_THREAD_UUID \
  --title '每分钟测试' --message '测试' --after 60 --every 60 --count 3
```

Use the actual account home and native thread ID of the destination. A sidebar
routing ID such as `primary@UUID` is not a native thread ID. For an agent-created
reminder, obtain that identity from the current native session; do not guess the
most recently modified session. The destination must already exist, and its
shared daemon must be running. This helper does not launch or take over a daemon.

The command returns a task ID and the first scheduled send time. A one-shot
reminder uses `--after 900 --count 1`. The finite count is required by the task
contract; do not turn an ordinary reminder into a persistent Goal.

```bash
.venv/bin/python -m cc_remote.timed_tasks status TASK_UUID
.venv/bin/python -m cc_remote.timed_tasks cancel TASK_UUID
```

For a non-default Wrapper state location, put
`--state-dir /absolute/path/to/wrapper-state` before the subcommand.
The helper and Wrapper must read the same state directory. Prompts and account
paths stay in its private SQLite store; public task metadata contains only a
title, schedule, counters and message receipt identity.

The sidebar remains idle while waiting. A task's outline stops after the final
message is accepted by the native queue, on cancellation/failure, or when its
worker heartbeat expires. Native queue acceptance is not model completion;
ordinary session state continues to describe the model's work. Cancelling
prevents subsequent sends and does not withdraw a message already submitted.
An uncertain submission is never automatically resent. After machine sleep,
missed intervals are not sent in a burst.

Existing ad-hoc scripts that invoke `codex queue` directly have no reliable
next-send metadata. Their messages remain ordinary messages until explicit
receipts are provided; cc-remote does not infer schedules from text or label all
cross-session messages as timers.
