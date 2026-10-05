# Codex cross-session messages

cc-remote displays the native Codex App cross-thread envelope. Sending continues
through the optional official `codex_app.send_message_to_thread` tool; this
feature does not install/enable the adapter, grant tool permissions, invent a
second message transport, or submit prompts itself. See
[codex-app-tools.md](codex-app-tools.md) for the shared account/daemon prerequisite.

Incoming messages show **来自会话 · name**. The Wrapper recognizes both the older
`userMessage` envelope and the newer `functionCallOutput` from the `codex_app`
namespace (`create_thread`, `send_message_to_thread`, `handoff_thread`). It keeps
the native item id and source thread id across live delivery, steering, official
history, rollout compatibility reads and browser caches. Unknown/malformed
markup remains ordinary text. Provenance is native display metadata, not an
independent authentication claim.

Native send calls show a compact outgoing receipt with the actual tool outcome.
A failed call does not become “sent”. The small receipt survives summary
materialization when the source includes that tool; for an older summary that
omits tool items, expand the process to read them. Tool bodies stay deferred.

A source/recipient link resolves against the current device's Codex Code catalog
and the current account routing prefix. Missing, deleted, cross-account and
Work targets remain labelled but disabled. The link opens a read-only history
view in the same window. It only uses `GetHistory` / `GetTurnDetail`; it does not
change Wrapper focus, resume an engine, send a message or navigate Codex App.
The original conversation stays mounted with output-follow paused. Closing the
view (Back or Escape) restores focus without scrolling the original chat.
The preview reads bounded pages, rejects mismatched cursors/revisions, and offers
retry after timeout or history invalidation.

The native envelope supplies a source thread id, not the sending call/turn id.
The link therefore opens that session, without guessing a corresponding record
from matching text. Codex App keeps its own native rendering and source links.
End-to-end sending still requires a running eligible App and its tool adapter.

This change uses protocol **v74**. Deploy Web, Relay and Wrapper together.
Derived Codex history pages and browser projections rebuild on upgrade; native
transcripts, credentials and original tool outputs are unchanged.

Validation uses native-shape fixtures without model calls: incoming format and
escaping, exact ids, live/history replay races, same-text separate messages,
steering, sender failure receipts, account/device routing, history pagination,
and desktop/mobile browser return-position checks. Live App/MCP delivery is a
separate acceptance check; fixture success alone does not prove it.
