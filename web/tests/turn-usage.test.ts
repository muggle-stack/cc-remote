import assert from "node:assert/strict";
import { createServer } from "vite";
import { compactTokens, rememberTurnUsage, usageForTurn } from "../src/turn-usage.ts";
import { PROTOCOL_VERSION, type TurnUsage } from "../src/protocol.ts";

const reading = (turn_id: string, seq: number, input_tokens = 184_001): TurnUsage => ({
  v: PROTOCOL_VERSION, type: "turn_usage", ts: seq, sid: "session", turn_id, seq,
  usage: { input_tokens, output_tokens: 2400, cache_read_tokens: 180_000 },
});
const first = rememberTurnUsage(undefined, reading("a", 5));
assert.deepEqual(rememberTurnUsage(first, reading("a", 5)), first);
assert.equal(rememberTurnUsage(first, reading("a", 4, 1)), first);
assert.equal(usageForTurn({ id: "human", forkPointId: "a", prompt: "", blocks: [], done: false }, first)?.input_tokens, 184_001);
assert.equal(usageForTurn({ id: "other", prompt: "", blocks: [], done: false }, first), undefined);
let bounded = first;
for (let i = 0; i < 100; i++) bounded = rememberTurnUsage(bounded, reading(`t-${i}`, i));
assert.equal(Object.keys(bounded).length, 32);
assert.equal(compactTokens(184_001), "184k");
assert.equal(compactTokens(2400), "2.4k");
assert.equal(compactTokens(1_250_000), "1.3m");
assert.equal(compactTokens(null), "—");
assert.equal(compactTokens(0), "0");

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts");
  let state = { ...initialState, focusedSid: "session", runtimes: {
    session: createRuntime(), other: createRuntime(),
  } };
  state = reduce(state, { type: "event", event: reading("a", 5) });
  assert.equal(state.runtimes.session.turnUsage.a.usage.input_tokens, 184_001);
  assert.equal(state.runtimes.session.turns.length, 0, "usage cannot create phantom turns");
  assert.equal(state.runtimes.session.state, "idle", "usage cannot resurrect a finished turn");
  assert.equal(state.runtimes.other.turnUsage, undefined);
  state = reduce(state, { type: "event", event: { v: PROTOCOL_VERSION, ts: 10,
    sid: "session", type: "snapshot", state: "running", tail_text: "", generation: "one",
    turn_usage: [reading("a", 6, 200_000)] } });
  assert.equal(state.runtimes.session.turnUsage.a.usage.input_tokens, 200_000);
  state = reduce(state, { type: "event", event: reading("a", 5) });
  assert.equal(state.runtimes.session.turnUsage.a.usage.input_tokens, 200_000);
  state = reduce(state, { type: "event", event: { v: PROTOCOL_VERSION, ts: 11,
    sid: "session", type: "snapshot", state: "running", tail_text: "", generation: "two",
    turn_usage: [reading("b", 1, 30)] } });
  assert.equal(state.runtimes.session.turnUsage.a, undefined);
  assert.equal(state.runtimes.session.turnUsage.b.usage.input_tokens, 30);
  state = reduce(state, { type: "event", event: { v: PROTOCOL_VERSION, ts: 12,
    sid: "session", type: "replay_end", to_seq: 20, truncated: false,
    turn_usage: [reading("b", 19, 60)] } });
  assert.equal(state.runtimes.session.turnUsage.b.usage.input_tokens, 60);
} finally { await harness.close(); }

console.log("turn usage: native identity, replay, isolation and fresh generations passed");
