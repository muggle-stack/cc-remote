import assert from "node:assert/strict";
import { createServer } from "vite";
import { QueryAcceptanceLatch } from "../src/outbox.ts";
import { acceptanceKindFor, canSubmitSteer, pendingAcceptanceIds } from "../src/pending-steers.ts";
import { PROTOCOL_VERSION, type ServerEvent } from "../src/protocol.ts";
import type { AppState, Turn } from "../src/reducer.ts";

const latch = new QueryAcceptanceLatch();
for (const id of ["a", "b", "c"]) assert.equal(latch.begin("s", id, undefined, null, true), true);
const firstReceipt = latch.receiptFor("s", "a")!;
const secondReceipt = latch.receiptFor("s", "b")!;
latch.rekeySession("s", "real");
assert.equal(latch.receiptFor("real", "a"), firstReceipt);
assert.equal(latch.accept({ type: "error", sid: "real", msg_id: "b", code: "not_steerable" }), true);
assert.equal((await secondReceipt).accepted, false);
assert.deepEqual(latch.pendingMessageIds("real"), ["a", "c"]);
assert.equal(latch.accept({ type: "turn_steered", sid: "other", msg_id: "a" }), false);
assert.equal(latch.accept({ type: "turn_steered", sid: "real", msg_id: "a" }), true);
assert.deepEqual(await firstReceipt, { accepted: true });
assert.deepEqual(latch.pendingMessageIds("real"), ["c"]);
latch.accept({ type: "error", sid: "real", msg_id: "c", code: "steer_outcome_unknown" });
assert.equal(latch.canBeginSteer("real", true), false);
assert.deepEqual(latch.pendingMessageIds("real"), ["c"]);
latch.accept({ type: "turn_steered", sid: "real", msg_id: "c" });
assert.equal(latch.canBeginSteer("real", true), true);
for (let i = 0; i < 32; i++) assert.equal(latch.begin("cap", `m${i}`, undefined, null, true), true);
assert.equal(latch.begin("cap", "overflow", undefined, null, true), false);
assert.equal(latch.begin("query", "q"), true);
assert.equal(latch.begin("query", "guide", undefined, null, true), false);
const bounded = new QueryAcceptanceLatch();
assert.equal(bounded.begin("one", "large", undefined, null, true, 64 * 1024 * 1024), true);
assert.equal(bounded.begin("two", "overflow", undefined, null, true, 1), false);
bounded.accept({ type: "turn_steered", sid: "one", msg_id: "large" });
assert.equal(bounded.begin("two", "available", undefined, null, true, 1), true);

const harness = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null } });
try {
  const { createRuntime, initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts") as
    typeof import("../src/reducer.ts");
  for (const sid of ["claude-main", "btw-claude"]) {
    const root: Turn = { id: "root", clientMsgId: "root", prompt: "start", blocks: [],
      done: false, ts: 1000, liveTaskId: "native-root" };
    let state: AppState = { ...initialState, focusedSid: sid,
      btwByParentSid: sid.startsWith("btw-") ? { parent: { activeSid: sid,
        chats: [{ sid, engine: "claude", createdAt: 1, state: "running" }] } } : {},
      runtimes: {
      [sid]: { ...createRuntime(), state: "running", turns: [root],
        controlGeneration: "g", historyGeneration: "g", historyRevision: "r", historyBuildSeq: 1 },
    } };
    const send = (id: string) => {
      assert.equal(canSubmitSteer(state.runtimes[sid], "claude"), true);
      state = reduce(state, { type: "steer_sent", concurrent: true,
        sid, msg_id: id, prompt: "same prompt", ts: 2000 });
    };
    const event = (body: Record<string, unknown>) => {
      state = reduce(state, { type: "event", event: { v: PROTOCOL_VERSION, sid, ts: 3,
        ...body } as ServerEvent });
    };
    send("a"); send("b"); send("c");
    const pending = state;
    assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["a", "b", "c"]);
    assert.equal(canSubmitSteer(state.runtimes[sid], "codex"), false);
    event({ type: "delta", message_id: "before", channel: "commentary", text: "still original" });
    assert.equal(state.runtimes[sid].turns[0].blocks.length, 1);
    assert.ok(state.runtimes[sid].turns.slice(1).every(t => !t.blocks.length));
    event({ type: "turn_steered", msg_id: "a", turn_id: "native-a", prompt: "same prompt", seq: 10 });
    assert.deepEqual(state.runtimes[sid].turns.map(t => t.id), ["root", "a", "b", "c"]);
    assert.deepEqual(state.runtimes[sid].turns.map(t => t.done), [true, false, false, false]);
    assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["b", "c"]);
    assert.deepEqual(pendingAcceptanceIds(pending.runtimes[sid]), ["a", "b", "c"], "immutable prior state");
    event({ type: "delta", message_id: "after-a", text: "accepted a", seq: 11 });
    assert.equal(state.runtimes[sid].turns[1].blocks.length, 1);
    assert.ok(state.runtimes[sid].turns.slice(2).every(t => !t.blocks.length));
    event({ type: "error", msg_id: "b", code: "not_steerable", message: "cancelled" });
    assert.deepEqual(state.runtimes[sid].turns.map(t => t.id), ["root", "a", "c"]);
    assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["c"]);
    event({ type: "turn_steered", msg_id: "c", turn_id: "native-c", prompt: "same prompt", seq: 12 });
    assert.equal(state.runtimes[sid].acceptancePending, null);
    assert.deepEqual(state.runtimes[sid].turns.map(t => t.done), [true, true, false]);
    event({ type: "turn_steered", msg_id: "c", turn_id: "native-c", prompt: "same prompt", seq: 13 });
    assert.equal(state.runtimes[sid].turns.length, 3, "duplicate receipt does not duplicate a row");

    // A non-head failure cannot release or poison its siblings.
    state = pending;
    event({ type: "error", msg_id: "b", code: "steer_outcome_unknown", message: "uncertain" });
    assert.equal(acceptanceKindFor(state.runtimes[sid], "b"), "steer_unknown");
    assert.equal(canSubmitSteer(state.runtimes[sid], "claude"), false);
    event({ type: "turn_steered", msg_id: "b", turn_id: "native-b", prompt: "same prompt", seq: 10 });
    assert.deepEqual(state.runtimes[sid].turns.map(t => t.id), ["root", "b", "a", "c"]);
    assert.equal(canSubmitSteer(state.runtimes[sid], "claude"), true);
    assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["a", "c"]);

    // A recovered exact native echo may arrive without TurnSteered. It still
    // separates accepted output from every unconsumed optimistic input.
    for (const proof of [
      { type: "user_msg", msg_id: "native-b", client_msg_id: "b", prompt: "same prompt" },
      { type: "turn_binding", msg_id: "b", turn_id: "native-b" },
    ]) {
      state = pending;
      event({ ...proof, seq: 10 });
      assert.deepEqual(state.runtimes[sid].turns.map(t => t.id), ["root", "b", "a", "c"]);
      assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["a", "c"]);
      event({ type: "delta", message_id: "after-echo", text: "accepted b", seq: 11 });
      assert.equal(state.runtimes[sid].turns[1].blocks.length, 1);
      assert.ok(state.runtimes[sid].turns.slice(2).every(t => !t.blocks.length && !t.done));
    }

    // Lost replay prefixes and service replacement retain ALL exact pending rows.
    for (const rebuild of [false, true]) {
      state = pending;
      event({ type: "replay_start", generation: rebuild ? "g2" : "g", from_seq: 0,
        to_seq: 20, truncated: true, rebuild });
      event({ type: "replay_end", to_seq: 20, truncated: true });
      assert.deepEqual(state.runtimes[sid].turns.filter(t => t.id !== "root").map(t => t.id), ["a", "b", "c"]);
      assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]), ["a", "b", "c"]);
    }
    if (sid.startsWith("btw-")) continue;

    // Canonical history can confirm several inputs at once, or only a later
    // one. Same text and timestamp must never acknowledge an unrelated sibling.
    for (const accepted of [["a"], ["b"], ["a", "b", "c"]]) {
      state = pending;
      event({ type: "history", session_id: sid, revision: "r", generation: "g", build_seq: 20,
        live_seq: 20, in_progress: true, detail: "summary", events: [], has_more: false,
        newest_id: `native-${accepted.at(-1)}`, turns: [
          { ...root, done: true },
          ...accepted.map((id, i) => ({ id: `native-${id}`, clientMsgId: id,
            prompt: "same prompt", ts: 3000, blocks: [], done: i < accepted.length - 1 })),
        ] });
      assert.deepEqual(pendingAcceptanceIds(state.runtimes[sid]),
        ["a", "b", "c"].filter(id => !accepted.includes(id)));
      assert.deepEqual(state.runtimes[sid].turns.map(t => t.id),
        ["root", ...accepted, ...["a", "b", "c"].filter(id => !accepted.includes(id))]);
      assert.ok(state.runtimes[sid].turns.filter(t => pendingAcceptanceIds(state.runtimes[sid]).includes(t.id))
        .every(t => !t.done && !t.blocks.length));
    }
    state = pending;
    event({ type: "session_rekey", old_key: sid, session_id: "renamed" });
    assert.deepEqual(pendingAcceptanceIds(state.runtimes.renamed), ["a", "b", "c"]);
  }
} finally { await harness.close(); }
console.log("consecutive Claude steering checks passed");
