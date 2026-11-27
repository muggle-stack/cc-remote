import assert from "node:assert/strict";
import { createServer } from "vite";
import type { ServerEvent, SessionInfo } from "../src/protocol.ts";
import { PROTOCOL_VERSION } from "../src/protocol.ts";
import { resolveRelatedSession } from "../src/session-messages.ts";
import { outgoingSessionMessages, sessionMessageForTool } from "../src/session-message-receipts.ts";
import type { ToolBlock } from "../src/domain/conversation.ts";

const sessions: SessionInfo[] = [
  { session_id: "nyx@sender", engine: "codex", space: "code", summary: "开发" },
  { session_id: "iris@sender", engine: "codex", space: "code", summary: "Other account" },
  { session_id: "nyx@claude", engine: "claude", space: "code" },
  { session_id: "nyx@work", engine: "codex", space: "work" },
  { session_id: "agy@gemini-session", engine: "antigravity", space: "code", summary: "Gemini 接力" },
];
assert.equal(resolveRelatedSession("sender", "nyx@receiver", sessions)?.summary, "开发");
for (const target of ["iris@sender", "../sender", "https://example.com", "missing", "claude", "work", "receiver"]) {
  assert.equal(resolveRelatedSession(target, "nyx@receiver", sessions), undefined);
}
assert.equal(resolveRelatedSession("sender", "other@receiver", sessions), undefined);
assert.equal(resolveRelatedSession("sender", "receiver", sessions), undefined);
assert.equal(resolveRelatedSession("ccr:codex:iris@sender", "nyx@receiver", sessions)?.summary, "Other account");
assert.equal(resolveRelatedSession("ccr:claude:nyx@claude", "nyx@receiver", sessions)?.engine, "claude");
assert.equal(resolveRelatedSession("ccr:antigravity:gemini-session", "nyx@receiver", sessions)?.session_id, "agy@gemini-session");
assert.equal(resolveRelatedSession("ccr:antigravity:gemini-session", "agy@gemini-session", sessions), undefined);
for (const ref of ["ccr:codex:nyx@claude", "ccr:codex:nyx@work", "ccr:claude:../nyx@claude"]) {
  assert.equal(resolveRelatedSession(ref, "nyx@receiver", sessions), undefined);
}
assert.equal(resolveRelatedSession("ccr:claude:nyx@claude", null, sessions), undefined);
const receiptTurn = { id: "t", prompt: "send", blocks: [], done: true,
  sessionMessages: [{ itemId: "call", threadId: "sender", status: "sent" as const }] };
for (const [status, expected] of [["accepted", "sent"], ["rejected", "failed"], ["unknown", "sending"], ["queued", "queued"]]) {
  for (const wrapped of [false, true]) {
    for (const tool of ["mcp__cc_remote_messages__send_message", "mcp__cc-remote-ask__send_message"]) {
    const body = JSON.stringify({ status });
    const receipts = outgoingSessionMessages({ ...receiptTurn, sessionMessages: [], blocks: [{
      kind: "tool", message_id: "m", tool_use_id: "call", tool,
      input: { session_ref: "ccr:claude:nyx@claude", request_id: "r", message: "hi" }, done: true,
      result: { content: wrapped ? JSON.stringify([{ type: "text", text: body }]) : body, is_error: status === "rejected" },
    }] });
    assert.deepEqual(receipts, [{ itemId: "call", threadId: "ccr:claude:nyx@claude", status: expected }]);
    }
  }
}
assert.deepEqual(outgoingSessionMessages(receiptTurn), receiptTurn.sessionMessages);
assert.deepEqual(outgoingSessionMessages({ ...receiptTurn, blocks: [{
  kind: "tool", message_id: "m", tool_use_id: "call", tool: "send_message_to_thread",
  server: "codex_app", input: { threadId: "sender" }, done: true,
  result: { content: "rejected", is_error: true },
}] }), [{ itemId: "call", threadId: "sender", status: "failed" }]);

const sendTool: ToolBlock = { kind: "tool", message_id: "m", tool_use_id: "send-one",
  tool: "mcp__cc_remote_messages__send_message", done: false,
  input: { session_ref: "ccr:claude:nyx@claude", message: "Line one\nLine two", request_id: "r" } };
assert.equal(sessionMessageForTool(sendTool)?.message, "Line one\nLine two");
assert.equal(sessionMessageForTool({ ...sendTool,
  input: { ...sendTool.input, session_ref: "ccr:antigravity:gemini-session" },
})?.threadId, "ccr:antigravity:gemini-session");
assert.equal(sessionMessageForTool({ ...sendTool, tool: "mcp__other__send_message" }), null);
assert.equal(sessionMessageForTool({ ...sendTool, input: { ...sendTool.input, session_ref: "https://example.com" } }), null);
assert.equal(sessionMessageForTool(sendTool, [
  { itemId: "send-two", threadId: "ccr:claude:nyx@claude", status: "sent" },
  { itemId: "send-one", threadId: "ccr:claude:another@target", status: "sent" },
])?.status, "sending", "another call or target cannot supply this tool's receipt");
assert.equal(sessionMessageForTool({ ...sendTool, done: true,
  result: { content: '{"status":"queued"}', is_error: false } }, [
  { itemId: "send-one", threadId: "ccr:claude:nyx@claude", status: "sent" },
])?.status, "sent", "a later delivery receipt updates the original queued tool in place");
assert.equal(sessionMessageForTool({ ...sendTool, done: true,
  result: { content: "transport failed", is_error: true } })?.status, "failed");
assert.equal(sessionMessageForTool({ ...sendTool, tool: "send_message_to_thread", server: "codex_app",
  input: { threadId: "sender", input: "Native message body" } })?.message, "Native message body");

const harness = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null } });
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
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

} finally { await harness.close(); }
console.log("session message provenance, identity and routing: passed");
