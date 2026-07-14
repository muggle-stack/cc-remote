# CLAUDE.md — cc-remote

Guidance for Claude Code (and human contributors) working in this repo. User-facing setup/run docs live in [README.md](README.md) / [README_en.md](README_en.md).

## What this is
Self-hosted remote control for Claude Code and Codex: a phone/browser drives a
local `claude` or `codex` session through a WebSocket relay. Two independent links:
- **model link** — the local CLI → whatever its own settings/authentication point
  at. cc-remote never touches model credentials or the model API.
- **control link** (this repo) — client ⇄ relay ⇄ wrapper ⇄ Claude SDK / Codex
  app-server ⇄ local CLI.

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
- **SDK pinned to `claude-agent-sdk==0.2.110`**: message-type shapes and the
  interrupt/drain contract can shift between minor versions. Re-run the
  interrupt+drain verification after any upgrade (`SdkHandle.preflight()` guards
  the major/minor at startup).
- **`include_partial_messages`** is a `ClaudeAgentOptions` field (set at
  construction, not on `query()`). Streaming events arrive as `StreamEvent`
  (`.event` = raw Anthropic API stream-event dict) — NOT
  `SDKPartialAssistantMessage` (doesn't exist in 0.2.110). Extract
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
  short-lived HttpOnly/SameSite cookie; `/ws` also enforces exact
  `PUBLIC_ORIGIN`. Never put tokens in URLs or protocol message bodies; logging
  redacts token/password fields.
- **Protocol version gate**: `PROTOCOL_VERSION` in both `protocol.py` and
  `web/src/protocol.ts`. `deserialize` hard-rejects a version mismatch, and
  `_Base` is `extra="forbid"`, so ANY protocol change must be deployed to all
  three tiers together (wrapper + relay + web) and the relay restarted — the
  relay imports `protocol.py` and drops frames it can't parse.
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
- **History = on-demand bulk read; reconnect recovery = bounded ring replay**
  (protocol v9; aligns
  with cc-on-web / web chats): the client fetches a session's history via
  `GetHistory` → the wrapper reads the transcript (`get_session_messages` +
  `translate_history`, in a thread) and returns it as ONE `History` frame
  (paginated by turn via `before`/`limit`), routed to the requester — no spawn,
  no ring buffer. A fresh hello sends one lightweight `Snapshot` per resident
  session (state dot); a reconnecting client supplies per-session cursors and
  gets only the missing bounded live tail. It never replays every resident ring
  wholesale (that multi-thousand-frame flood made refreshes slow and serialized).
  History and the live stream are the
  SAME event types, deduped client-side by msg_id/message_id, so a completed turn
  from the transcript never doubles a live one; an in-flight (`!done`) turn is
  preserved across a refetch. History timestamps come from the transcript
  entries (`transcript_timestamps`), not `time.time()`, so past turns show their
  real ask/answer time.
- **Token-aware residency**: `resume` = cold prompt cache = full context re-send,
  so it only happens on first spawn / re-focus-after-eviction; raising the cap
  trades RAM for fewer cold re-sends. Reading history is free (plain transcript
  I/O), so browsing/refreshing never costs model tokens.

## Module map
- `cc_remote/protocol.py` — pydantic wire schema; all modules depend on it.
  `serialize`/`deserialize` with `v` check; `is_downstream` for seq/buffer.
  Control frames: `SessionFocus` / `SessionRekey` / `GetHistory` / `History` /
  `SessionInfo.state`.
- `cc_remote/config.py` — env-driven config (`RelayConfig`, `WrapperConfig`).
- `cc_remote/log.py` — JSON logging with token redaction; use `logger("...")`.
- `cc_remote/wrapper/` — sdk.py (client lifecycle), machine.py (session pool +
  per-ctx state machine + drain + `_handle_get_history`), session_ctx.py (one
  `SessionContext` per resident session), stream.py (SDK→protocol translate +
  `translate_history`/`transcript_timestamps`), ringbuffer.py (seq + live-tail),
  transport.py (WS client to relay), session.py (session id persistence).
- `cc_remote/relay/` — server.py (FastAPI `/ws` + `/api/login` + static), auth.py
  (wrapper bearer + HMAC cookie session), pairing.py (single wrapper slot +
  `to=`/broadcast fan-out), forward.py (bounded per-client queues; slow clients
  are disconnected without silently shedding deltas).
- `web/src/` — reducer.ts (per-session runtimes + `history` reducer), ws.ts (WS
  client + `sendGetHistory`), protocol.ts (mirror of protocol.py — keep in sync),
  components/.

## Run / test
```bash
python -m pip install -r requirements-dev.txt
python -m cc_remote.relay        # terminal 1 (set WEB_STATIC_DIR=web/dist to serve the UI)
python -m cc_remote.wrapper      # terminal 2 (on the machine where claude runs)
pytest                           # zero-token unit tests
npm --prefix web run test:reliability
npm --prefix web run lint
npm --prefix web run build
```
`pytest.ini` restricts collection to `tests/test_*.py`; these are zero-token
unit/regression tests (stub transport, no model). Real relay/wrapper/model probes
live under `scripts/live/` and may spend model tokens — run them explicitly,
keep prompts trivial ("hi"), and prefer the unit tests.
