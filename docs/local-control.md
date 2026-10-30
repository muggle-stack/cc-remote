# Releasing a Claude session to a local process

A browser that opens a Claude Code session makes the Wrapper keep an idle
`claude --resume` subprocess for it until capacity eviction. Another local tool
that runs the same session itself, for example a task runner resuming it with
the Claude Agent SDK, sees that subprocess as the session's owner and must not
write next to it. The Wrapper therefore lets a local process of the same user
ask it to let go of an idle session.

## Socket

The Wrapper always serves `<CC_REMOTE_STATE_DIR>/control/control.sock`: a
private 0700 directory, a mode 0600 socket and a same-user lifetime lock, like
the [session messages](session-messages.md) socket. If the path is unsafe or
another Wrapper holds the lock, the Wrapper logs a warning and runs without it.

One JSON line in, one JSON line out:

```json
{"op": "release_claude_session", "config_dir": "/home/me/.claude", "session_id": "<native session UUID>"}
```

- `config_dir` is the absolute Claude config root of a configured account; it
  selects the account the same way the Wrapper's profiles do.
- `{"status": "released"}`: the session was idle and left the resident pool,
  exactly like a capacity eviction, even while a browser is viewing it. Its
  subprocess has exited when the reply is sent.
- `{"status": "busy"}`: a turn, queued input, user question, background task,
  scheduled command or control change holds it, or it is a side chat or Work
  session. A release already in progress also returns `busy`; that earlier
  request must finish before another process can claim the session.
- `{"status": "not_resident"}`: the Wrapper holds no subprocess for it.
- `{"status": "unknown", "error": …}`: disconnect failed, timed out or was
  cancelled, so native process exit could not be confirmed. Do not take over.
  The current Wrapper retains this uncertainty: repeat release requests stay
  `unknown`, and browser queries, session switches and other cold-resume paths
  cannot start another writer. Inspect the original process/service and recover
  ownership before attempting takeover; losing a client connection is not proof
  that its native worker exited.
- `{"status": "rejected", "error": …}`: malformed request or unknown account.

A request never carries a prompt and cannot start model work.

## After a release

The browser keeps its runtime. Its next prompt to that session resumes it
through the ordinary spawn path, as a cold message delivery does, and is sent
if the session is writable. While the other process still runs the session,
ownership detection marks it read-only and the prompt is rejected with
「该会话正由本机其他程序使用，本次消息未发送」. Switching to the session waits until
the released subprocess has exited before resuming it.
