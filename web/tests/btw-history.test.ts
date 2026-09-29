import assert from "node:assert/strict";
import { createServer } from "vite";
import type { AppState, Turn } from "../src/reducer.ts";
import type { ServerEvent } from "../src/protocol.ts";

const harness = await createServer({
  root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const { initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  const sid = "btw-history";
  let state: AppState = initialState;
  const event = (body: Record<string, unknown>) => {
    state = reduce(state, { type: "event", event: {
      v: 72, ts: 1, sid, ...body,
    } as ServerEvent });
  };
  event({ type: "btw_opened", request_id: "r", parent_sid: "main",
    btw_sid: sid, engine: "codex", created_at: 1, revision: 1 });
  const orphan: Turn = {
    id: "message-first", prompt: "", done: true,
    blocks: [{ kind: "text", message_id: "message-first", channel: "commentary",
      text: "，确认它在做什么", done: true }],
  };
  state = { ...state, runtimes: { ...state.runtimes, [sid]: {
    ...state.runtimes[sid], turns: [orphan],
  } } };
  const restore = () => {
    event({ type: "replay_start", generation: "g", from_seq: 0, to_seq: 100,
      truncated: false, rebuild: true });
    event({ type: "user_msg", msg_id: "human", prompt: "只读看看这个会话" });
    event({ type: "turn_binding", msg_id: "human", turn_id: "native" });
    event({ type: "assistant_msg_start", message_id: "message-first", channel: "commentary" });
    event({ type: "delta", message_id: "message-first", channel: "commentary",
      text: "我先只读核查，确认它在做什么" });
    event({ type: "assistant_msg_end", message_id: "message-first", channel: "commentary" });
    event({ type: "turn_end", turn_id: "native", result: {
      subtype: "success", duration_ms: 1200, is_error: false,
    } });
    event({ type: "replay_end", to_seq: 100, truncated: false });
  };
  restore();
  restore();
  const turns = state.runtimes[sid].turns;
  assert.equal(turns.length, 1, "reconnect must remove the orphan, not duplicate its reply");
  assert.equal(turns[0].prompt, "只读看看这个会话");
  assert.equal(turns[0].blocks[0].kind === "text" && turns[0].blocks[0].text,
    "我先只读核查，确认它在做什么");
  assert.equal(turns[0].done, true);
  assert.equal(state.runtimes[sid].loading, false);

  // The wrapper omits items whose user was evicted. An empty bounded snapshot
  // must clear the stale local fragment and keep an explicit truncation flag.
  event({ type: "replay_start", generation: "g", from_seq: 95, to_seq: 100,
    truncated: true, rebuild: true });
  event({ type: "replay_end", to_seq: 100, truncated: true });
  assert.deepEqual(state.runtimes[sid].turns, []);
  assert.equal(state.runtimes[sid].truncated, true);

  state = { ...state, runtimes: { ...state.runtimes, [sid]: {
    ...state.runtimes[sid], acceptancePending: "pending",
    turns: [{ id: "pending", clientMsgId: "pending", prompt: "next input",
      blocks: [], done: false }],
  } } };
  restore();
  assert.deepEqual(state.runtimes[sid].turns.map(t => t.id), ["human", "pending"],
    "a snapshot racing acceptance must preserve the pending input after older replies");

  event({ type: "replay_start", generation: "g", from_seq: 0, to_seq: 110,
    truncated: false, rebuild: true });
  event({ type: "replay_start", generation: "g", from_seq: 0, to_seq: 110,
    truncated: false, rebuild: true });
  event({ type: "user_msg", msg_id: "pending", prompt: "next input" });
  event({ type: "turn_binding", msg_id: "pending", turn_id: "next-native" });
  event({ type: "delta", message_id: "stream", text: "Beginning ", channel: "commentary" });
  event({ type: "replay_end", to_seq: 110, truncated: false });
  event({ type: "state", state: "running", seq: 111 });
  event({ type: "delta", message_id: "stream", text: "continues live", channel: "commentary",
    seq: 112 });
  const active = state.runtimes[sid];
  assert.equal(active.turns.length, 1);
  assert.equal(active.turns[0].id, "pending");
  assert.equal(active.turns[0].done, false, "restoring history must preserve the active turn");
  assert.equal(active.turns[0].blocks[0].kind === "text" && active.turns[0].blocks[0].text,
    "Beginning continues live");
  assert.equal(active.state, "running");
  assert.equal(active.liveOwner?.turnId, "pending", "the activity spark follows the restored owner");

  event({ type: "replay_start", generation: "g", from_seq: 0, to_seq: 112,
    truncated: false, rebuild: true });
  state = { ...state, runtimes: { ...state.runtimes, [sid]: {
    ...state.runtimes[sid], acceptancePending: "new-local",
    turns: [{ id: "new-local", prompt: "input during replay", blocks: [], done: false }],
  } } };
  restore();
  assert.equal(state.runtimes[sid].turns.at(-1)?.id, "new-local",
    "a repeated snapshot must preserve a new input submitted during the earlier replay");
} finally {
  await harness.close();
}
console.log("BTW history recovery checks passed");
