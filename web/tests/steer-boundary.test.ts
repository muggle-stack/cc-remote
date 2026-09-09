import assert from "node:assert/strict";
import { createServer } from "vite";
import { mergeInitialHistory } from "../src/history-merge.ts";
import type { Turn } from "../src/reducer.ts";

// A final item and a user steer can be recorded 78 ms apart inside ONE native
// task. The native task is a routing identity, not a visible conversation row.
const task = "native-task";
const history: Turn[] = ["old", "guided"].map((name, index) => ({
  id: `user-${name}`, forkPointId: task, prompt: `prompt-${name}`,
  done: true, ts: 1000 + index * 78,
  blocks: [{ kind: "text", message_id: `answer-${name}`, channel: "final",
    text: `answer ${name}`, done: true }],
}));
const oldLive: Turn = { ...history[0], id: task };
const merged = mergeInitialHistory(history, [oldLive]);
assert.equal(merged.length, 2);
assert.deepEqual(merged.map((turn) => turn.blocks.map((block) =>
  block.kind === "text" ? block.message_id : null)),
[["answer-old"], ["answer-guided"]],
"a native task alias cannot move the old final under the newer steer");
assert.equal(merged[0].prompt, "prompt-old");

// An item-free projection has insufficient evidence to select either segment.
const ambiguous = mergeInitialHistory(history, [{ ...oldLive, blocks: [] }]);
assert.equal(ambiguous.length, 3,
  "ambiguous task-only aliases wait for exact user/item identity");

// Repair a previously persisted wrong-owner copy only with exact canonical
// item ownership. Equal prose under different item ids must remain intact.
const polluted = history.map((turn) => ({ ...turn, blocks: [...turn.blocks] }));
polluted[1].blocks.unshift({ ...history[0].blocks[0] });
const repaired = mergeInitialHistory(history, polluted,
  { reconcileReplayOrphans: true }, true);
assert.deepEqual(repaired.map((turn) => turn.blocks.map((block) =>
  block.kind === "text" ? block.message_id : null)),
[["answer-old"], ["answer-guided"]]);

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { initialState, createRuntime, reduce } =
    await harness.ssrLoadModule("/src/reducer.ts");
  for (const explicitTask of [undefined, task]) {
  for (const priorDone of [false, true]) {
    let state = { ...initialState, focusedSid: "s", runtimes: { s: {
      ...createRuntime(), state: "running", syncReady: true,
      legacyLiveFallbackBlocked: true,
      turns: [{ ...history[0], done: priorDone }],
    } } };
    const emit = (body: Record<string, unknown>) => {
      state = reduce(state, { type: "event", event: {
        v: 57, sid: "s", ts: 1.078, ...body,
      } });
    };
    state = reduce(state, { type: "steer_sent", sid: "s", ts: 1078,
      msg_id: "user-guided", prompt: "prompt-guided" });
    emit({ type: "turn_steered", msg_id: "user-guided", turn_id: task,
      prompt: "prompt-guided", seq: 10 });
    for (const body of [
      { type: "assistant_msg_start" },
      { type: "delta", text: "answer old" },
      { type: "assistant_msg_end" },
    ]) emit({ ...body, message_id: "answer-old", channel: "final",
      turn_id: explicitTask });
    assert.equal(state.runtimes.s.liveOwner?.turnId, "user-guided",
      "replaying a settled predecessor cannot steal the current live owner");
    for (const body of [
      { type: "assistant_msg_start" },
      { type: "delta", text: "answer guided" },
      { type: "assistant_msg_end" },
    ]) emit({ ...body, message_id: "answer-guided", channel: "final",
      turn_id: explicitTask });
    const rows = state.runtimes.s.turns as Turn[];
    assert.deepEqual(rows.map((turn) => turn.blocks.map((block) =>
      block.kind === "text" ? block.message_id : null)),
    [["answer-old"], ["answer-guided"]],
    "late native replay updates its exact item owner across a steer fence");
    assert.equal(rows[0].blocks[0].kind === "text"
      && rows[0].blocks[0].text, "answer old");
    assert.deepEqual(mergeInitialHistory(history, rows, {
      reconcileReplayOrphans: true,
    }, true).map((turn) => turn.blocks.length), [1, 1]);
  }
  }
} finally {
  await harness.close();
}
const equalTextRows = history.map((turn) => ({ ...turn,
  blocks: turn.blocks.map((block) => ({ ...block, text: "same words" })),
}));
assert.deepEqual(mergeInitialHistory(equalTextRows, equalTextRows, {
  reconcileReplayOrphans: true,
}, true).map((turn) => turn.blocks.length), [1, 1],
"distinct native items with equal prose remain separate replies");
console.log("steer boundary regression tests passed");
