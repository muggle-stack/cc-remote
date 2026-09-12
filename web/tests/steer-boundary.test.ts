import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  mergeAuthoritativeTurnDetail, mergeInitialHistory, restoreObservedLiveTurnDetails,
} from "../src/history-merge.ts";
import type { Turn } from "../src/reducer.ts";
import type { TextBlock } from "../src/domain/conversation.ts";

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

// Two steers can materialize consecutively after compaction. The first is a
// real user row with no process of its own. Older clients cached the previous
// segment's items and clock under it, then reported an endless detail failure.
const compactProcess: Turn["blocks"][number] = {
  kind: "process", processKind: "compaction", item_id: "compact-old",
  phase: "end", status: "succeeded", title: "Compacted", done: true,
};
const compactHistory: Turn[] = [{
  id: "original", prompt: "deploy", done: true,
  blocks: [compactProcess], processDetailState: "present",
  detailReasons: ["process"], detailEventCount: 1,
  processStartedTs: 10_000, processDoneTs: 30_000,
}, {
  id: "native-latest", clientMsgId: "latest", prompt: "最新的", done: true,
  blocks: [], processDetailState: "none", detailReasons: [], detailEventCount: 0,
}, {
  id: "native-flash", clientMsgId: "flash", prompt: "ds v4.1 flash适配了吗？",
  forkPointId: task, done: false, blocks: [], processDetailState: "unknown",
}];
const reconcileOptions = {
  reconcileReplayOrphans: true, preserveLiveTailOpen: true, activeOwnerId: "flash",
};
for (const retainedFork of [undefined, task]) {
for (const retainedBlocks of [[], [compactProcess]]) {
  const stale: Turn[] = [compactHistory[0], {
    ...compactHistory[1], id: "latest", historyTurnId: "native-latest",
    forkPointId: retainedFork,
    blocks: retainedBlocks,
    processDetailState: "present", detailReasons: ["process"], detailEventCount: 6,
    processStartedTs: 20_000, processDoneTs: 30_000, detailLoaded: true,
    detailError: "详细过程未完整返回，请重试",
  }, { ...compactHistory[2], id: "flash" }];
  let healed = mergeInitialHistory(compactHistory, stale, reconcileOptions, true);
  healed = restoreObservedLiveTurnDetails(healed, stale);
  const latest = healed.find(turn => turn.id === "latest")!;
  assert.equal(latest.processDetailState, "none");
  assert.equal(latest.processStartedTs, undefined);
  assert.equal(latest.processDoneTs, undefined);
  assert.equal(latest.detailEventCount, 0);
  assert.deepEqual(latest.detailReasons, []);
  assert.equal(latest.detailError, undefined);
  assert.deepEqual(latest.blocks, []);
  assert.equal(healed[0].blocks.length, 1,
    "repair keeps the original compaction instead of deleting its process");
  assert.equal(healed[2].done, false, "the latest steer keeps running");
  const refreshed = mergeInitialHistory(compactHistory, healed, reconcileOptions, true);
  assert.equal(refreshed[1].processDetailState, "none",
    "another refresh must not restore the phantom disclosure");
  const loaded = mergeAuthoritativeTurnDetail(latest, stale[1], compactHistory);
  assert.equal(loaded.processDetailState, "none",
    "expanded detail cannot restore an optimistic task alias or foreign process");
  assert.equal(loaded.processStartedTs, undefined);
  assert.equal(loaded.detailEventCount, 0);
  assert.deepEqual(loaded.blocks, []);
}
}

const opaqueSummary = { ...compactHistory[1], processDetailState: "unknown" as const };
const realDeferred: Turn = {
  ...compactHistory[1], processDetailState: "present", detailEventCount: 12,
  detailReasons: ["process"], detailHasMore: true,
};
assert.equal(mergeInitialHistory([opaqueSummary], [realDeferred], reconcileOptions, true)[0].processDetailState,
  "present", "an opaque or bounded page cannot erase real deferred process");
assert.equal(mergeInitialHistory([compactHistory[1]], [realDeferred], reconcileOptions, true)[0].processDetailState,
  "present", "unread detail pages keep the process affordance on a closed steer");
const retainedReal = { ...realDeferred, detailHasMore: false, blocks: [compactProcess] };
assert.equal(mergeInitialHistory([compactHistory[1]], [retainedReal], reconcileOptions, true)[0].processDetailState,
  "present", "an omitted process item without another proven owner remains visible");

const harness = await createServer({ root: process.cwd(), appType: "custom",
  logLevel: "silent", server: { middlewareMode: true, watch: null } });
try {
  const { initialState, createRuntime, reduce } =
    await harness.ssrLoadModule("/src/reducer.ts");
  const { ChatView } = await harness.ssrLoadModule("/src/components/ChatView.tsx");
  for (const local of [false, true]) {
  for (const channel of ["final", "commentary"] as const) {
  for (const explicitTask of [undefined, task]) {
  for (const storage of ["blocks", "liveSpillBlocks"] as const) {
    const partial = { kind: "text" as const, message_id: "streaming-old",
      channel, text: "周期约 **1", done: false };
    const old: Turn = { id: "streaming-user", forkPointId: task,
      prompt: "检查当前状态", done: false, blocks: [] };
    old[storage] = [partial];
    let state = { ...initialState, focusedSid: "s", runtimes: { s: {
      ...createRuntime(), state: "running", syncReady: true,
      legacyLiveFallbackBlocked: true, turns: [old],
    } } };
    const emit = (body: Record<string, unknown>) => {
      state = reduce(state, { type: "event", event: {
        v: 64, sid: "s", ts: 1, ...body,
      } });
    };
    const text = () => {
      const turn: Turn = state.runtimes.s.turns[0];
      return [...turn.blocks, ...(turn.liveSpillBlocks ?? []),
        ...(turn.detailProjection?.blocks ?? [])].find((block): block is TextBlock =>
        block.kind === "text" && block.message_id === "streaming-old")!;
    };
    if (local) state = reduce(state, { type: "steer_sent", sid: "s", ts: 1000,
      msg_id: "streaming-steer", prompt: "补充当前现象" });
    emit({ type: "turn_steered", msg_id: "streaming-steer", turn_id: task,
      prompt: "补充当前现象", seq: 10 });
    assert.equal(text().done, false,
      "accepting a steer cannot complete a still-streaming native message");
    emit({ type: "delta", message_id: "streaming-old", turn_id: explicitTask,
      channel, text: " ms**，仍在继续。", seq: 11 });
    assert.equal(text().text, "周期约 **1 ms**，仍在继续。",
      "the predecessor keeps receiving text without a History refresh");
    assert.equal(state.runtimes.s.turns[0].done, true);
    assert.equal(state.runtimes.s.liveOwner?.turnId, "streaming-steer");
    emit({ type: "assistant_msg_end", message_id: "streaming-old",
      turn_id: explicitTask, channel, seq: 12 });
    emit({ type: "delta", message_id: "streaming-old", turn_id: explicitTask,
      channel, text: " ms**，仍在继续。", seq: 13 });
    assert.equal(text().text, "周期约 **1 ms**，仍在继续。",
      "a genuinely completed native message still rejects replay deltas");
    assert.equal(text().done, true);
    assert.equal(state.runtimes.s.turns[1].blocks.length, 0,
      "late text stays in its original row, including spilled blocks");
  }
  }
  }
  }
  const repairedMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "compact-steers", engine: "codex",
    turns: mergeInitialHistory(compactHistory, [{
      ...compactHistory[1], processDetailState: "present", detailLoaded: true,
      detailEventCount: 6, detailReasons: ["process"], processStartedTs: 20_000,
      processDoneTs: 30_000,
    }], reconcileOptions, true),
    onEdit: () => {}, onGetDiff: () => {}, onLoadDetail: () => {},
  }));
  assert.doesNotMatch(repairedMarkup, /详细过程未完整返回|加载失败/);
  assert.match(repairedMarkup, /最新的/);
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
