import assert from "node:assert/strict";
import { createServer } from "vite";
import type { ServerEvent, SessionInfo } from "../src/protocol.ts";
import { PROTOCOL_VERSION } from "../src/protocol.ts";
import { resolveRelatedSession, outgoingSessionMessages } from "../src/session-messages.ts";

const sessions: SessionInfo[] = [
  { session_id: "nyx@sender", engine: "codex", space: "code", summary: "开发" },
  { session_id: "iris@sender", engine: "codex", space: "code", summary: "Other account" },
  { session_id: "nyx@claude", engine: "claude", space: "code" },
  { session_id: "nyx@work", engine: "codex", space: "work" },
];
assert.equal(resolveRelatedSession("sender", "nyx@receiver", sessions)?.summary, "开发");
for (const target of ["iris@sender", "../sender", "https://example.com", "missing", "claude", "work", "receiver"]) {
  assert.equal(resolveRelatedSession(target, "nyx@receiver", sessions), undefined);
}
assert.equal(resolveRelatedSession("sender", "other@receiver", sessions), undefined);
assert.equal(resolveRelatedSession("sender", "receiver", sessions), undefined);
const receiptTurn = { id: "t", prompt: "send", blocks: [], done: true,
  sessionMessages: [{ itemId: "call", threadId: "sender", status: "sent" as const }] };
assert.deepEqual(outgoingSessionMessages(receiptTurn), receiptTurn.sessionMessages);
assert.deepEqual(outgoingSessionMessages({ ...receiptTurn, blocks: [{
  kind: "tool", message_id: "m", tool_use_id: "call", tool: "send_message_to_thread",
  server: "codex_app", input: { threadId: "sender" }, done: true,
  result: { content: "rejected", is_error: true },
}] }), [{ itemId: "call", threadId: "sender", status: "failed" }]);

const harness = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null } });
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { SessionMessageReader } = await harness.ssrLoadModule("/src/session-message-reader.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: PROTOCOL_VERSION, ts: 10, ...body,
  } as ServerEvent);
  const sid = "nyx@receiver";
  const user = event({ type: "user_msg", sid, msg_id: "native-input", prompt: "检查代码", source_thread_id: "sender" });
  const turn = { id: "native-input", prompt: "检查代码", sourceThreadId: "sender", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false, sessionMessages: receiptTurn.sessionMessages };
  const history = event({ type: "history", sid, session_id: sid, revision: "r", generation: "g",
    detail: "summary", events: [], turns: [turn], has_more: false, oldest_id: turn.id });
  for (const events of [[user, history, user], [history, user], [user, user, history]]) {
    let state = { ...initialState, focusedSid: sid, sessions, runtimes: { [sid]: createRuntime() } };
    for (const e of events) state = reduce(state, { type: "event", event: e });
    assert.equal(state.runtimes[sid].turns.length, 1, "live/history races use native identity, never append a duplicate");
    assert.equal(state.runtimes[sid].turns[0].sourceThreadId, "sender");
  }
  let state = { ...initialState, focusedSid: sid, sessions, runtimes: { [sid]: createRuntime() } };
  for (const e of [user, event({ ...user, msg_id: "different-native-id" })]) {
    state = reduce(state, { type: "event", event: e });
  }
  assert.equal(state.runtimes[sid].turns.length, 2, "identical words in separate native messages remain separate");
  state = reduce(state, { type: "event", event: event({ type: "turn_steered", sid,
    msg_id: "steer", turn_id: "native-task", source_thread_id: "sender", prompt: "跟进" }) });
  assert.equal(state.runtimes[sid].turns.at(-1).sourceThreadId, "sender");

  const reader = new SessionMessageReader(sid);
  assert.deepEqual(reader.requestPage(0), { before: null });
  assert.equal(reader.accept(event({ ...history, session_id: "iris@receiver" })), false);
  assert.equal(reader.accept(event({ ...history, has_more: true })), true);
  assert.equal(reader.turns[0].sourceThreadId, "sender");
  assert.equal(reader.state.focusedSid, sid);
  assert.deepEqual(reader.requestPage(1), { before: "native-input" });
  assert.equal(reader.accept(history), false, "late initial page cannot replace an older page request");
  assert.equal(reader.accept(event({ ...history, before: "native-input", turns: [{ ...turn, id: "old" }], oldest_id: "old" })), true);
  assert.deepEqual(reader.turns.map((t: { id: string }) => t.id), ["old"]);
  assert.deepEqual(reader.requestPage(0), { before: null });
  reader.accept(history);
  assert.equal(reader.requestDetail("wrong-turn"), false);
  assert.equal(reader.requestDetail("native-input"), true);
  reader.fail("timeout");
  assert.equal(reader.turns[0].detailLoading, false);
  assert.equal(reader.turns[0].detailError, "timeout");
  assert.equal(reader.requestDetail("native-input"), true);
  assert.deepEqual(reader.requestPage(0), { before: null });
  assert.equal(reader.accept(event({ type: "turn_detail", session_id: sid,
    turn_id: "native-input", revision: "r", before: null, events: [] })), false,
  "a late detail from the previous page cannot settle the pending page read");
  assert.equal(reader.loading, true);
  assert.equal(reader.accept(history), true);
  assert.equal(initialState.focusedSid, null, "read-only preview never mutates the live state singleton");
} finally { await harness.close(); }
console.log("session message provenance, identity, routing and read-only history: passed");
