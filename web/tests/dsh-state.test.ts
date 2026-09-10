import assert from "node:assert/strict";
import { createServer } from "vite";
import { PROTOCOL_VERSION } from "../src/protocol.ts";
import type { Turn } from "../src/reducer.ts";

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { initialState, createRuntime, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const { canForkTurn } = await harness.ssrLoadModule("/src/session-worktree.ts");
  const { mergeAuthoritativeTurnDetail } = await harness.ssrLoadModule("/src/history-merge.ts");
  const { turnNotificationPresentation } = await harness.ssrLoadModule("/src/turn-notification.ts");
  assert.deepEqual(turnNotificationPresentation({ sid: "dsh@session",
    result: { subtype: "success", is_error: false },
    notification_context: { engine: "dsh", space: "code" } }, "session"), {
    title: "DSH 会话", body: "DSH 会话已经完成", sessionId: "dsh@session", engine: "dsh", space: "code",
  });
  const sid = "dsh@session";
  let state = { ...initialState, focusedSid: sid, runtimes: {
    [sid]: { ...createRuntime(), state: "running", syncReady: true },
    "dsh@other": createRuntime(),
  } };
  let seq = 0;
  const emit = (body: Record<string, unknown>) => {
    state = reduce(state, { type: "event", event: {
      v: PROTOCOL_VERSION, sid, ts: 1, seq: ++seq, ...body,
    } });
  };
  emit({ type: "user_msg", msg_id: "first", client_msg_id: "first", prompt: "first" });
  emit({ type: "turn_binding", msg_id: "first", turn_id: "dsh-seq-2" });
  const text = (turn: string, message: string, value: string, replace = false) => {
    emit({ type: "assistant_msg_start", turn_id: turn, message_id: message });
    emit({ type: "delta", turn_id: turn, message_id: message, text: value, replace });
  };
  text("dsh-seq-2", "first-answer", "discarded attempt");
  text("dsh-seq-2", "first-answer", "", true);
  text("dsh-seq-2", "first-answer", "old answer");
  emit({ type: "user_msg", msg_id: "steer", client_msg_id: "steer", prompt: "steer" });
  emit({ type: "turn_binding", msg_id: "steer", turn_id: "dsh-seq-8" });
  text("dsh-seq-2", "first-answer", "old answer complete", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-2", message_id: "first-answer" });
  emit({ type: "turn_end", turn_id: "dsh-seq-2", result: { subtype: "steered", duration_ms: 0, is_error: false } });
  text("dsh-seq-8", "steer-answer", "new answer", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-seq-8", message_id: "steer-answer" });
  emit({ type: "turn_end", turn_id: "dsh-seq-8", result: { subtype: "success", duration_ms: 5, is_error: false } });
  emit({ type: "turn_binding", msg_id: "dsh-turn-2", turn_id: "dsh-turn-2", autonomous: true });
  text("dsh-turn-2", "auto-answer", "goal continuation", true);
  emit({ type: "assistant_msg_end", turn_id: "dsh-turn-2", message_id: "auto-answer" });
  emit({ type: "turn_end", turn_id: "dsh-turn-2", result: { subtype: "success", duration_ms: 5, is_error: false } });
  const rows = state.runtimes[sid].turns as Turn[];
  assert.deepEqual(rows.map(row => row.id), ["first", "steer", "dsh-turn-2"]);
  assert.deepEqual(rows.map(row => row.blocks.filter(b => b.kind === "text").map(b => b.text)),
    [["old answer complete"], ["new answer"], ["goal continuation"]]);
  assert.ok(rows.every(row => row.done));
  assert.deepEqual(rows.map(row => canForkTurn("dsh", row)), [false, true, true]);
  assert.equal(rows[0].forkPointId, "dsh-seq-2", "keep the native identity for late event routing");
  const canonicalOld = { ...rows[0], forkPointId: undefined, forkAvailable: undefined };
  assert.equal(canForkTurn("dsh", mergeAuthoritativeTurnDetail(canonicalOld, rows[0])), false,
    "refreshing a completed steer segment must not restore its fork button");
  emit({ type: "turn_binding", msg_id: "dsh-turn-cancelled", turn_id: "dsh-turn-cancelled", autonomous: true });
  emit({ type: "turn_end", turn_id: "dsh-turn-cancelled", result: { subtype: "interrupted", duration_ms: 1, is_error: false } });
  assert.equal(state.runtimes[sid].turns.at(-1).interrupted, true,
    "a native cancellation is not rendered as successful completion");
  emit({ type: "dsh_state", sid: "dsh@other", connected: false, error: "连接中断" });
  assert.equal(state.runtimes[sid].dsh, undefined);
  assert.equal(state.runtimes["dsh@other"].dsh.error, "连接中断");
  assert.ok(state.runtimes[sid].turns.every((row: Turn) => row.done));
  emit({ type: "models", engine: "dsh", models: [{ id: "native-model", display_name: "Native" }], dsh_presets: [] });
  emit({ type: "models", engine: "dsh", models: [], dsh_presets: [], error: "未连接" });
  assert.deepEqual(state.catalog.dsh, []);
  console.log("DSH identity, retry, autonomous round and scoped state regressions passed");
} finally {
  await harness.close();
}
